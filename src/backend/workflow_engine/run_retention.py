#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""Continuous run retention and staging (rtsp-rtmp-stream-cameras
Requirement 12; design component 15).

A continuous workflow can run several times a second, so its runs cannot
be kept like triggered ones. :class:`RunRetention` bounds them:

- **Staging.** Continuous runs write their artifacts under the RAM-backed
  :data:`STAGING_ROOT` (``capture_root_for``) when it is writable with at
  least :data:`MIN_STAGING_FREE_BYTES` free, and under the persistent
  capture root otherwise. The ``{root}/{workflow_id}/{execution_id}``
  layout is kept, since the marshal model derives the workflow id from it.
- **Classification.** A completed run is a Notable_Run when it failed, an
  output binding sent something (a node-status detail that is not a
  ``not sent: ...`` skip), or an Event_Gate recorded an ``activated`` or
  ``cleared`` transition. A staged notable run moves to the persistent
  capture root, and its ``output_dir``/``log_path`` follow it; the artifact
  routes read those columns, so staged and promoted runs are served alike.
- **Eviction**, in this order: each registration keeps its newest
  ``keep_recent_runs`` continuous runs plus its newest
  ``keep_notable_runs`` older Notable_Runs; the device keeps the bytes of
  persisted continuous runs within ``continuous.retentionBytes`` (oldest
  Notable_Runs first); and staging within ``continuous.stagingBytes``
  (oldest non-notable runs first). An evicted run loses its
  ``workflow_executions`` row and its directory.
- **Only continuous runs are touched**: rows whose Trigger_Context source
  is ``continuous``. Every other run, including a manual run of a paused
  continuous workflow, is never deleted (Requirement 12.8).
- **Housekeeping**, every :data:`HOUSEKEEPING_INTERVAL_S`: the byte caps,
  the registered housekeeping tasks (the Continuous_Runner counter
  snapshots), a staging re-check, and the size bound on
  ``${COMPONENT_WORK_PATH}/gst-debug.log`` (Requirement 12.7).
"""
import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from workflow_engine.stream_feed import CONTINUOUS_SOURCE

logger = logging.getLogger(__name__)

MIB = 1024 * 1024

#: The RAM-backed staging root of continuous runs.
STAGING_ROOT = "/dev/shm/dda-continuous"
#: Staging is used only with at least this much free space.
MIN_STAGING_FREE_BYTES = 64 * MIB
HOUSEKEEPING_INTERVAL_S = 60.0

#: The GStreamer debug log ``gst_pipeline.run_pipeline`` points every run
#: at (``GST_DEBUG=4``), bounded here: past :data:`DEBUG_LOG_MAX_BYTES` of
#: real disk use, its newest :data:`DEBUG_LOG_KEEP_BYTES` go to ``.1`` and
#: the file is truncated in place (GStreamer keeps it open).
DEBUG_LOG_NAME = "gst-debug.log"
DEBUG_LOG_MAX_BYTES = 64 * MIB
DEBUG_LOG_KEEP_BYTES = 8 * MIB

#: The executor binding kinds that send something off the device.
OUTPUT_BINDING_KINDS = ("digital_output", "mqtt_publish", "opcua_write", "modbus_write")
#: The node-status detail prefix of a skipped output (``not sent: ...``).
NOT_SENT_PREFIX = "not sent"
#: The Event_Gate transitions that make a run notable.
NOTABLE_TRANSITIONS = ("activated", "cleared")

#: ``trigger_context_json`` of a continuous run (``json.dumps`` default
#: separators), for a cheap SQL prefilter; the JSON is parsed to confirm.
_CONTINUOUS_LIKE = '%"source": "continuous"%'

_ACTIVE_STATUSES = ("pending", "running")
_FAILED = "failed"


# --- pure helpers ---------------------------------------------------------------


def is_continuous_context(trigger_context: Any) -> bool:
    """Whether a Trigger_Context is a Continuous_Runner run's."""
    return isinstance(trigger_context, Mapping) and trigger_context.get("source") == CONTINUOUS_SOURCE


def output_node_ids(document: Any) -> Tuple[str, ...]:
    """The node ids of the document's output bindings."""
    bindings = document.get("executorBindings") if isinstance(document, Mapping) else None
    return tuple(str(binding.get("nodeId")) for binding in (bindings or [])
                 if isinstance(binding, Mapping) and binding.get("binding") in OUTPUT_BINDING_KINDS
                 and binding.get("nodeId"))


def outputs_sent(node_status: Any, output_ids: Iterable[str]) -> int:
    """How many of ``output_ids`` sent something: a successful output
    node whose detail is not a ``not sent`` skip."""
    if not isinstance(node_status, Mapping):
        return 0
    sent = 0
    for node_id in output_ids:
        entry = node_status.get(node_id)
        if not isinstance(entry, Mapping) or entry.get("status") != "success":
            continue
        detail = entry.get("detail")
        if isinstance(detail, str) and detail.strip() and not detail.lower().startswith(NOT_SENT_PREFIX):
            sent += 1
    return sent


def has_notable_transition(tag_values: Any) -> bool:
    """Whether an Event_Gate recorded an activated or cleared transition."""
    events = tag_values.get("event") if isinstance(tag_values, Mapping) else None
    if not isinstance(events, Mapping):
        return False
    return any(isinstance(gate, Mapping) and gate.get("transition") in NOTABLE_TRANSITIONS
               for gate in events.values())


def classify_run(status: Any, node_status: Any, output_ids: Iterable[str], tag_values: Any) -> Tuple[bool, int]:
    """``(notable, outputs sent)`` of a finished run (see the module
    docstring)."""
    sent = outputs_sent(node_status, output_ids)
    notable = status == _FAILED or sent > 0 or has_notable_transition(tag_values)
    return notable, sent


def resolve_staging_root(candidate: str = STAGING_ROOT, min_free_bytes: int = MIN_STAGING_FREE_BYTES,
                         disk_usage: Callable = shutil.disk_usage) -> Optional[str]:
    """``candidate`` when it can be created, is writable, and has at least
    ``min_free_bytes`` free; None otherwise (use persistent storage)."""
    try:
        os.makedirs(candidate, mode=0o700, exist_ok=True)
        if not os.access(candidate, os.W_OK | os.X_OK):
            return None
        if disk_usage(candidate).free < min_free_bytes:
            return None
    except OSError:
        return None
    return candidate


def directory_bytes(path: Optional[str]) -> int:
    """The bytes of the regular files under ``path`` (0 when absent)."""
    if not path or not os.path.isdir(path):
        return 0
    total = 0
    for directory, _subdirectories, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                continue
    return total


def rotate_debug_log(path: Optional[str], max_bytes: int = DEBUG_LOG_MAX_BYTES,
                     keep_bytes: int = DEBUG_LOG_KEEP_BYTES) -> bool:
    """Bound ``path``'s disk use; True when it was rotated.

    GStreamer holds the file open for the life of the process, so it is
    truncated in place after its newest ``keep_bytes`` are copied to
    ``<path>.1``. Real disk use (allocated blocks) is measured, because a
    writer that did not open the file for appending keeps writing at its
    old offset after a truncation, leaving a sparse file whose apparent
    size overstates it."""
    if not path:
        return False
    try:
        status = os.stat(path)
    except OSError:
        return False
    blocks = getattr(status, "st_blocks", None)
    used = min(status.st_size, blocks * 512) if blocks is not None else status.st_size
    if used <= max_bytes:
        return False
    try:
        with open(path, "rb") as source:
            source.seek(max(0, status.st_size - keep_bytes))
            tail = source.read(keep_bytes)
        with open(path + ".1", "wb") as backup:
            backup.write(tail.lstrip(b"\x00"))
        with open(path, "r+b") as target:
            target.truncate(0)
    except OSError:
        logger.exception("Could not rotate %s", path)
        return False
    logger.info("Rotated %s at %d MiB of disk use", path, used // MIB)
    return True


def _default_limits():
    from stream_ingest.settings import get_device_settings
    return get_device_settings().limits()


def _default_persistent_root() -> str:
    from workflow_engine import pipeline_executor
    return pipeline_executor._WORKFLOW_CAPTURE_ROOT


def _default_debug_log_path() -> Optional[str]:
    work_path = os.environ.get("COMPONENT_WORK_PATH")
    return os.path.join(work_path, DEBUG_LOG_NAME) if work_path else None


def _load_json(text: Any) -> Any:
    if not isinstance(text, str) or not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _read_run_metadata(output_dir: Optional[str], capture_id: Optional[str]) -> Dict[str, Any]:
    if not output_dir or not capture_id:
        return {}
    try:
        with open(os.path.join(output_dir, "{0}.json".format(capture_id)), "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _run_directory(row) -> Optional[str]:
    if row.output_dir:
        return row.output_dir
    if row.log_path:
        return os.path.dirname(row.log_path)
    return None


def _within(path: str, root: Optional[str]) -> bool:
    if not path or not root:
        return False
    real_root = os.path.realpath(root)
    return os.path.realpath(path).startswith(real_root.rstrip(os.sep) + os.sep)


# --- the index --------------------------------------------------------------------


@dataclass
class RetainedRun:
    """A continuous run the retention keeps track of."""

    execution_id: str
    registration_id: str
    tick_at_ms: int
    notable: bool
    run_dir: Optional[str]
    size: int
    staged: bool


@dataclass(frozen=True)
class RunOutcome:
    """What :meth:`RunRetention.on_run_complete` learned about a run."""

    status: Optional[str]
    notable: bool
    outputs_sent: int
    #: The frame sequence number the run analyzed (``stream.<node>.seq``).
    processed_seq: Optional[int] = None


def _sort_key(run: RetainedRun):
    return run.tick_at_ms, run.execution_id


class RunRetention:
    """See the module docstring."""

    def __init__(self, session_factory: Optional[Callable] = None,
                 persistent_root: Optional[str] = None,
                 staging_candidate: str = STAGING_ROOT,
                 limits: Callable[[], Any] = _default_limits,
                 disk_usage: Callable = shutil.disk_usage,
                 debug_log_path: Optional[str] = None,
                 interval_s: float = HOUSEKEEPING_INTERVAL_S,
                 min_staging_free_bytes: int = MIN_STAGING_FREE_BYTES):
        self._session_factory = session_factory
        self._persistent_root = persistent_root
        self._staging_candidate = staging_candidate
        self._limits = limits
        self._disk_usage = disk_usage
        self._debug_log_path = debug_log_path if debug_log_path is not None else _default_debug_log_path()
        self._interval_s = interval_s
        self._min_staging_free = min_staging_free_bytes
        self._lock = threading.RLock()
        self._staging_root: Optional[str] = None
        self._staging_resolved = False
        self._loaded = False
        #: registration id -> its continuous runs, oldest first.
        self._runs: Dict[str, List[RetainedRun]] = {}
        #: registration id -> its keep_recent_runs (for the device cap).
        self._windows: Dict[str, int] = {}
        self._output_ids: Dict[str, Tuple[str, ...]] = {}
        self._tasks: List[Callable[[], None]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- roots ---------------------------------------------------------------

    @property
    def persistent_root(self) -> str:
        return self._persistent_root or _default_persistent_root()

    @property
    def staging_root(self) -> Optional[str]:
        """The active staging root, or None when continuous runs write to
        persistent storage."""
        with self._lock:
            if not self._staging_resolved:
                self._refresh_staging_locked()
            return self._staging_root

    def _refresh_staging_locked(self) -> None:
        root = resolve_staging_root(self._staging_candidate, self._min_staging_free, self._disk_usage)
        if root is not None and os.path.realpath(root) == os.path.realpath(self.persistent_root):
            root = None
        if self._staging_resolved and root != self._staging_root:
            logger.warning("Continuous run staging %s", "resumed at " + root if root else
                           "is unavailable; continuous runs write to " + self.persistent_root)
        elif not self._staging_resolved and root is None:
            logger.info("Continuous run staging is unavailable; continuous runs write to %s",
                        self.persistent_root)
        self._staging_root, self._staging_resolved = root, True

    def capture_root_for(self, registration, trigger_context) -> Optional[str]:
        """The artifact root of a run: the staging root for a continuous
        run while staging is available, else None (the capture root)."""
        if not is_continuous_context(trigger_context):
            return None
        return self.staging_root

    def _staged(self, path: Optional[str]) -> bool:
        return bool(path) and _within(path, self._staging_candidate)

    # -- sessions ------------------------------------------------------------

    def _session(self):
        if self._session_factory is None:
            from dao.sqlite_db.sqlite_db_operations import SessionLocal
            self._session_factory = SessionLocal
        return self._session_factory()

    # -- the index -------------------------------------------------------------

    def _registration_output_ids(self, session, registration_id: str) -> Tuple[str, ...]:
        if registration_id not in self._output_ids:
            from workflow_engine.discovery import COMPILED_PIPELINE_FILE
            from workflow_engine.models import WorkflowRegistration

            registration = session.get(WorkflowRegistration, registration_id)
            document = None
            if registration is not None and registration.artifact_path:
                try:
                    with open(os.path.join(registration.artifact_path, COMPILED_PIPELINE_FILE), "r",
                              encoding="utf-8") as handle:
                        document = json.load(handle)
                except (OSError, ValueError):
                    document = None
            self._output_ids[registration_id] = output_node_ids(document)
        return self._output_ids[registration_id]

    def _record(self, session, row, output_ids: Optional[Sequence[str]] = None,
                context: Optional[Mapping[str, Any]] = None) -> Tuple[RetainedRun, int, Dict[str, Any]]:
        context = context if context is not None else (_load_json(row.trigger_context_json) or {})
        tag_values = _read_run_metadata(row.output_dir, row.capture_id)
        if output_ids is None:
            output_ids = self._registration_output_ids(session, row.registration_id)
        notable, sent = classify_run(row.status, _load_json(row.node_status_json), output_ids, tag_values)
        tick = context.get("tickAtMs")
        if not isinstance(tick, int) or isinstance(tick, bool):
            tick = int(row.started_at or 0) * 1000
        run_dir = _run_directory(row)
        record = RetainedRun(execution_id=row.id, registration_id=row.registration_id, tick_at_ms=tick,
                             notable=notable, run_dir=run_dir, size=directory_bytes(run_dir),
                             staged=self._staged(run_dir))
        return record, sent, tag_values

    def _load_locked(self) -> None:
        """Index the continuous runs already on the device (once)."""
        if self._loaded:
            return
        from workflow_engine.models import WorkflowExecution

        session = self._session()
        try:
            rows = (session.query(WorkflowExecution)
                    .filter(WorkflowExecution.trigger_context_json.like(_CONTINUOUS_LIKE)).all())
            indexed = set()
            for row in rows:
                context = _load_json(row.trigger_context_json)
                if not is_continuous_context(context) or row.status in _ACTIVE_STATUSES:
                    continue
                record, _sent, _tags = self._record(session, row, context=context)
                self._runs.setdefault(row.registration_id, []).append(record)
                indexed.add(row.id)
            for runs in self._runs.values():
                runs.sort(key=_sort_key)
            self._remove_orphans(session, indexed)
        finally:
            session.close()
        self._loaded = True

    def _remove_orphans(self, session, indexed) -> None:
        """Remove staged run directories no execution row owns (a run the
        device lost, e.g. across a crash)."""
        from workflow_engine.models import WorkflowExecution

        root = self._staging_candidate
        if not root or not os.path.isdir(root):
            return
        for workflow_id in os.listdir(root):
            workflow_dir = os.path.join(root, workflow_id)
            if not os.path.isdir(workflow_dir):
                continue
            for execution_id in os.listdir(workflow_dir):
                if execution_id in indexed:
                    continue
                if session.get(WorkflowExecution, execution_id) is None:
                    self._remove_dir(os.path.join(workflow_dir, execution_id), execution_id)

    # -- completion ------------------------------------------------------------

    def on_run_complete(self, execution_id: str, keep_recent_runs: int, keep_notable_runs: int,
                        output_ids: Optional[Sequence[str]] = None,
                        stream_node_id: Optional[str] = None) -> RunOutcome:
        """Classify a finished continuous run, promote it when notable, and
        evict what the limits no longer allow."""
        from workflow_engine.models import WorkflowExecution

        with self._lock:
            self._load_locked()
            session = self._session()
            try:
                row = session.get(WorkflowExecution, execution_id)
                if row is None:
                    return RunOutcome(None, False, 0)
                context = _load_json(row.trigger_context_json) or {}
                # Read before any commit: an evicted row cannot be refreshed.
                status = row.status
                record, sent, tag_values = self._record(session, row, output_ids=output_ids, context=context)
                if output_ids is not None:
                    self._output_ids[row.registration_id] = tuple(output_ids)
                processed = None
                stream = tag_values.get("stream")
                if stream_node_id and isinstance(stream, Mapping):
                    seq = (stream.get(stream_node_id) or {}).get("seq") if isinstance(
                        stream.get(stream_node_id), Mapping) else None
                    processed = seq if isinstance(seq, int) and not isinstance(seq, bool) else None
                if not is_continuous_context(context):
                    # Not a continuous run: never retained or deleted here.
                    return RunOutcome(status, record.notable, sent, processed)
                if record.notable and record.staged:
                    self._promote_locked(session, row, record)
                runs = self._runs.setdefault(row.registration_id, [])
                runs[:] = [run for run in runs if run.execution_id != record.execution_id]
                runs.append(record)
                runs.sort(key=_sort_key)
                self._windows[row.registration_id] = max(0, int(keep_recent_runs))
                evicted = self._window_evictions_locked(row.registration_id, keep_recent_runs,
                                                        keep_notable_runs)
                evicted += self._cap_evictions_locked(exclude=evicted)
                self._delete_locked(session, evicted)
                return RunOutcome(status, record.notable, sent, processed)
            finally:
                session.close()

    def _promote_locked(self, session, row, record: RetainedRun) -> None:
        """Move a staged notable run to the persistent capture root."""
        source = record.run_dir
        workflow_id = os.path.basename(os.path.dirname(os.path.realpath(source)))
        target = os.path.join(self.persistent_root, workflow_id, row.id)
        if os.path.exists(target):
            logger.warning("Not promoting run %s: %s already exists", row.id, target)
            return
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(source, target)
        except (OSError, shutil.Error):
            logger.exception("Could not persist notable run %s; it stays staged", row.id)
            if os.path.isdir(source):
                shutil.rmtree(target, ignore_errors=True)
            return
        if row.output_dir:
            row.output_dir = target
        if row.log_path and os.path.realpath(row.log_path).startswith(os.path.realpath(source) + os.sep):
            row.log_path = os.path.join(target, os.path.relpath(os.path.realpath(row.log_path),
                                                                os.path.realpath(source)))
        try:
            session.commit()
        except Exception:  # noqa: BLE001 - the directory moved; keep going
            session.rollback()
            logger.exception("Could not record the new location of run %s", row.id)
        record.run_dir, record.staged = target, False

    # -- eviction --------------------------------------------------------------

    def _window_evictions_locked(self, registration_id: str, keep_recent: int,
                                 keep_notable: int) -> List[RetainedRun]:
        runs = self._runs.get(registration_id) or []
        keep_recent, keep_notable = max(0, int(keep_recent)), max(0, int(keep_notable))
        split = max(0, len(runs) - keep_recent)
        recent, older = runs[split:], runs[:split]
        older_notable = [run for run in older if run.notable]
        kept = {run.execution_id for run in recent}
        if keep_notable:
            kept.update(run.execution_id for run in older_notable[max(0, len(older_notable) - keep_notable):])
        return [run for run in runs if run.execution_id not in kept]

    def _recent_ids_locked(self) -> set:
        """The execution ids inside each registration's recent window (a
        run the window step evicts is always older than the window)."""
        recent = set()
        for registration_id, runs in self._runs.items():
            window = self._windows.get(registration_id, 20)
            if window > 0:
                recent.update(run.execution_id for run in runs[max(0, len(runs) - window):])
        return recent

    def _cap_evictions_locked(self, exclude: Iterable[RetainedRun] = ()) -> List[RetainedRun]:
        """The runs the device byte cap and the staging cap remove."""
        limits = self._limits()
        excluded = {run.execution_id for run in exclude}
        live = [run for runs in self._runs.values() for run in runs if run.execution_id not in excluded]
        evicted: List[RetainedRun] = []
        # The device cap on persisted runs: the oldest Notable_Runs outside
        # the recent windows first, then the oldest persisted runs of any
        # kind. The staging cap: the oldest non-notable runs first.
        for staged, cap in ((False, int(limits.retention_bytes)), (True, int(limits.staging_bytes))):
            pool = [run for run in live if run.staged == staged]
            total = sum(run.size for run in pool)
            if total <= cap:
                continue
            pool.sort(key=_sort_key)
            if staged:
                first = [run for run in pool if not run.notable]
            else:
                recent = self._recent_ids_locked()
                first = [run for run in pool if run.notable and run.execution_id not in recent]
            removed = set()
            for run in first + pool:
                if total <= cap:
                    break
                if run.execution_id in removed:
                    continue
                removed.add(run.execution_id)
                total -= run.size
                evicted.append(run)
        return evicted

    def _delete_locked(self, session, runs: Sequence[RetainedRun]) -> None:
        if not runs:
            return
        from workflow_engine.models import WorkflowExecution

        ids = [run.execution_id for run in runs]
        try:
            (session.query(WorkflowExecution).filter(WorkflowExecution.id.in_(ids))
             .delete(synchronize_session=False))
            session.commit()
        except Exception:  # noqa: BLE001 - retried by the next eviction
            session.rollback()
            logger.exception("Could not delete %d evicted continuous runs", len(ids))
            return
        gone = set(ids)
        for registration_id in list(self._runs):
            self._runs[registration_id] = [run for run in self._runs[registration_id]
                                           if run.execution_id not in gone]
        for run in runs:
            self._remove_dir(run.run_dir, run.execution_id)

    def _remove_dir(self, path: Optional[str], execution_id: str) -> None:
        """Remove one run directory, and only a run directory: it must be
        named for its execution and sit under a retention root."""
        if not path or not os.path.isdir(path):
            return
        if os.path.basename(os.path.realpath(path)) != execution_id or not (
                _within(path, self._staging_candidate) or _within(path, self.persistent_root)):
            logger.warning("Not removing %s: it is not a continuous run directory", path)
            return
        shutil.rmtree(path, ignore_errors=True)

    # -- housekeeping ------------------------------------------------------------

    def add_housekeeping_task(self, task: Callable[[], None]) -> None:
        with self._lock:
            self._tasks.append(task)

    def enforce_caps(self) -> None:
        with self._lock:
            self._load_locked()
            evicted = self._cap_evictions_locked()
            if evicted:
                session = self._session()
                try:
                    self._delete_locked(session, evicted)
                finally:
                    session.close()

    def housekeeping_once(self) -> None:
        """One housekeeping pass, every step contained."""
        try:
            with self._lock:
                self._refresh_staging_locked()
            self.enforce_caps()
        except Exception:  # noqa: BLE001 - housekeeping must keep running
            logger.exception("Continuous run retention housekeeping failed")
        with self._lock:
            tasks = list(self._tasks)
        for task in tasks:
            try:
                task()
            except Exception:  # noqa: BLE001
                logger.exception("A continuous run housekeeping task failed")
        try:
            rotate_debug_log(self._debug_log_path)
        except Exception:  # noqa: BLE001
            logger.exception("Could not bound the GStreamer debug log")

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="continuous-run-retention", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            self.housekeeping_once()

    # -- queries -------------------------------------------------------------------

    def retained(self, registration_id: str) -> List[str]:
        """The execution ids retained for a registration, oldest first."""
        with self._lock:
            self._load_locked()
            return [run.execution_id for run in self._runs.get(registration_id) or []]

    def notable_ids(self, registration_id: str) -> List[str]:
        """The registration's retained Notable_Runs, newest first (the run
        list's notable filter, Requirement 16.2)."""
        with self._lock:
            self._load_locked()
            return [run.execution_id for run in reversed(self._runs.get(registration_id) or []) if run.notable]

    def usage(self) -> Dict[str, int]:
        """Bytes of persisted and staged continuous runs."""
        with self._lock:
            runs = [run for runs in self._runs.values() for run in runs]
            return {"persistedBytes": sum(run.size for run in runs if not run.staged),
                    "stagedBytes": sum(run.size for run in runs if run.staged)}
