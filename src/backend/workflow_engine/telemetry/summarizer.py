#
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

"""The Interval summarizer of the Telemetry_Agent (workflow-telemetry-dashboards
design §4.1, §5.2 ``summarizer.py``, §5.4).

:class:`PairAccumulator` folds the observations of one registration (one
Pair) over the Interval in progress: run counts (``dropped`` = gate discards
+ ``skippedBusy``), a bounded latency sample of at most 2,000
``finished_ms - run_started_ms`` values taken only from records whose
``run_started_ms`` is known, per-class totals and maxima with the 64-label
cap and ``__other__`` folding, ``detections.total`` from the uncapped
``detection_count`` (``truncated`` when labels folded or a run carried more
detections than its capped list), person-class per-run maxima, outcomes,
the ``model`` block (states, waits and ``waitedMs`` from the per-run gate,
a continuous wait counted once on the None -> non-None transition; the
``modelUnavailable`` tick is ignored because it counts the same transition)
and the continuous counters.

:class:`IntervalSummarizer` keeps one accumulator per registration id of the
``RegistrationView`` snapshot (created when the id appears, dropped after a
final window when it leaves; a transient accumulator folds a late
observation for an id that is gone and emits once), and runs the ticker:
at every wall-clock multiple of ``summaryIntervalS`` plus at most 1 s it
emits one ``dda.summary.v1`` per registration that had a run in the last 5
minutes, and at every multiple of ``idleHeartbeatS`` a Heartbeat for every
other registration (``idle: true``, ``intervalS = idleHeartbeatS``). ``seq``
counts per ``workflowId`` within the ``instance`` (the Portal's Pair key),
starting at 1, shared by the workflow's Summaries and Heartbeats and by all
its registrations and versions, and kept when an accumulator is dropped,
recreated or transient; consecutive messages of a Pair differ by exactly 1.
The ``seq`` property and the status' ``seq`` count every emission of the
instance. A first
window shorter than 1 s is folded into the next boundary; a longer first
window is declared with the configured ``intervalS`` (the schema's + 1 s
allowance accepts it). While ``enabled`` is false every observation is
discarded; on enable every accumulator resets and the first Interval starts
at the enable time; on disable the Interval in progress is discarded and
``seq`` continues.

Everything runs under one lock on the summarizer's own thread (or the
test's, through :meth:`IntervalSummarizer.tick`); the observers only enqueue.
"""

import collections
import json
import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "SCHEMA",
    "PRODUCER_NAME",
    "MODEL_STATES",
    "DEFAULT_PERSON_CLASSES",
    "SummarizerSettings",
    "PairAccumulator",
    "IntervalSummarizer",
    "Emission",
    "normalize_state",
    "worst_state",
]

SCHEMA = "dda.summary.v1"
PRODUCER_NAME = "engine"
#: The producer version the Summaries carry by default (the agent passes its own).
DEFAULT_PRODUCER_VERSION = "1.0.0"

#: The latency sample is bounded per Interval (§4.1, §5.2).
MAX_LATENCY_SAMPLES = 2000
#: ``detections.perClass`` carries at most this many keys, ``__other__`` included.
MAX_LABELS = 64
OTHER_LABEL = "__other__"
MAX_LABEL_CHARS = 64
#: ``model.models`` carries at most this many entries of at most 64 characters.
MAX_MODELS = 16
MAX_MODEL_CHARS = 64
#: A Pair "had a run in the last 5 minutes" (Summary) else it Heartbeats.
ACTIVE_WINDOW_MS = 5 * 60 * 1000
#: A window shorter than this is never emitted (folded into the next boundary).
MIN_WINDOW_MS = 1000
#: The schema's allowance over the declared interval (``intervalS x 1000 + 1000``).
WINDOW_ALLOWANCE_MS = 1000
#: A Summary or Heartbeat is at most this large.
MAX_SUMMARY_BYTES = 16384
#: Observations waiting for the summarizer thread.
INBOX_MAXLEN = 20000
#: A removed registration's info is remembered this long for late observations.
RECENT_INFO_TTL_MS = ACTIVE_WINDOW_MS

#: The eight states in worst-state order (§4.1): ``NONE < READY < UNLOADING <
#: LOADING < INCOMPLETE < NOT_DEPLOYED < UNKNOWN < UNAVAILABLE``.
MODEL_STATES = ("NONE", "READY", "UNLOADING", "LOADING", "INCOMPLETE",
                "NOT_DEPLOYED", "UNKNOWN", "UNAVAILABLE")
_STATE_RANK = {state: rank for rank, state in enumerate(MODEL_STATES)}
STATE_NONE = "NONE"
STATE_UNKNOWN = "UNKNOWN"

DEFAULT_PERSON_CLASSES = ("person", "human")

#: Continuous counter keys the summarizer folds; every other key is ignored
#: (``modelUnavailable`` deliberately: ``notify_model_wait`` counts that wait).
CONTINUOUS_KEY_DROPPED = "skippedBusy"
CONTINUOUS_KEY_NO_NEW_FRAME = "skippedNoNewFrame"

Emission = collections.namedtuple("Emission", ["registration_id", "workflow_id", "payload"])


def normalize_state(value: Any) -> str:
    """The state string of the eight-state vocabulary; anything else is
    ``UNKNOWN``."""
    if not isinstance(value, str):
        return STATE_UNKNOWN
    state = value.strip().upper()
    return state if state in _STATE_RANK else STATE_UNKNOWN


def worst_state(states: Sequence[str]) -> str:
    """The worst of ``states`` under the ordering (``NONE`` for none)."""
    worst = STATE_NONE
    for state in states:
        normalized = normalize_state(state)
        if _STATE_RANK[normalized] > _STATE_RANK[worst]:
            worst = normalized
    return worst


@dataclass(frozen=True)
class SummarizerSettings:
    """What the ``dda-workflow-telemetry`` shadow tunes (§5.4)."""

    enabled: bool = False
    summary_interval_s: int = 10
    idle_heartbeat_s: int = 60
    person_classes: Tuple[str, ...] = DEFAULT_PERSON_CLASSES


def _percentile(sorted_values: List[int], quantile: float) -> int:
    """Nearest-rank percentile of a non-empty sorted list."""
    count = len(sorted_values)
    index = max(0, int(math.ceil(quantile * count)) - 1)
    return int(sorted_values[min(index, count - 1)])


def _label_of(item: Any) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    label = item.get("label")
    if label is None:
        return None
    text = str(label)
    return text[:MAX_LABEL_CHARS] if text else None


class PairAccumulator:
    """The Interval accumulator of one registration (see the module docstring).

    ``model_states`` (the last known state per model) and the continuous
    wait flag persist across windows; everything else is reset by
    :meth:`reset_window`. ``transient`` marks the accumulator of a late
    observation for an id no longer in the snapshot.
    """

    def __init__(
        self,
        registration_id: str,
        workflow_id: Optional[str],
        version: Optional[str],
        has_triton: bool,
        window_start_ms: int,
        *,
        transient: bool = False,
    ) -> None:
        self.registration_id = str(registration_id)
        self.workflow_id = str(workflow_id) if workflow_id else None
        self.version = str(version) if version is not None else None
        self.has_triton = bool(has_triton)
        self.transient = bool(transient)
        self.last_run_ms: Optional[int] = None
        self.model_states: "OrderedDict[str, str]" = OrderedDict()
        self._wait_active = False
        self.reset_window(window_start_ms)

    # -- the window --------------------------------------------------------------

    def reset_window(self, start_ms: int) -> None:
        self.window_start_ms = int(start_ms)
        self.completed = 0
        self.failed = 0
        self.dropped = 0
        self.latency_samples: List[int] = []
        self.latency_max: Optional[int] = None
        self.runs_with_detections = 0
        self.detections_total = 0
        self.detections_present = False
        self.detections_truncated = False
        self.per_class: "OrderedDict[str, List[int]]" = OrderedDict()
        self.people_seen_max = 0
        self.outcomes_present = False
        self.anomalous = 0
        self.normal = 0
        self.window_states: List[str] = []
        self.waits = 0
        self.waited_ms = 0
        self.skipped_no_new_frame = 0
        self.continuous_seen = False
        self.observations = 0

    def has_recent_run(self, at_ms: int) -> bool:
        return self.last_run_ms is not None and at_ms - self.last_run_ms < ACTIVE_WINDOW_MS

    # -- observations --------------------------------------------------------------

    def observe_run(self, record: Any, stamp_ms: int, person_classes: Sequence[str]) -> None:
        self.observations += 1
        self.last_run_ms = int(stamp_ms)
        if getattr(record, "status", None) == "completed":
            self.completed += 1
        else:
            self.failed += 1
        run_started = getattr(record, "run_started_ms", None)
        finished = getattr(record, "finished_ms", None)
        if run_started is not None and finished is not None:
            sample = max(0, int(finished) - int(run_started))
            if len(self.latency_samples) < MAX_LATENCY_SAMPLES:
                self.latency_samples.append(sample)
            self.latency_max = sample if self.latency_max is None else max(self.latency_max, sample)
        detections = getattr(record, "detections", None)
        if isinstance(detections, list):
            self.detections_present = True
            count = int(getattr(record, "detection_count", 0) or 0)
            self.detections_total += count
            if count > 0:
                self.runs_with_detections += 1
            if count > len(detections):
                self.detections_truncated = True
            self._fold_labels(detections, person_classes)
        tag_values = getattr(record, "tag_values", None)
        if isinstance(tag_values, dict) and "is_anomalous" in tag_values:
            self.outcomes_present = True
            if _truthy(tag_values.get("is_anomalous")):
                self.anomalous += 1
            else:
                self.normal += 1

    def _fold_labels(self, detections: List[Any], person_classes: Sequence[str]) -> None:
        counts: Dict[str, int] = {}
        people = 0
        wanted = {str(c).strip().lower() for c in person_classes if isinstance(c, str)}
        for item in detections:
            label = _label_of(item)
            if label is None:
                continue
            counts[label] = counts.get(label, 0) + 1
            if label.strip().lower() in wanted:
                people += 1
        if people > self.people_seen_max:
            self.people_seen_max = people
        other = 0
        for label, count in counts.items():
            entry = self.per_class.get(label)
            if entry is None:
                # The cap counts ``__other__``: real labels stop at 63 once
                # folding is needed, so the map never exceeds 64 keys.
                if len(self.per_class) < MAX_LABELS - 1:
                    entry = self.per_class[label] = [0, 0]
                else:
                    other += count
                    continue
            entry[0] += count
            if count > entry[1]:
                entry[1] = count
        if other:
            self.detections_truncated = True
            entry = self.per_class.get(OTHER_LABEL)
            if entry is None:
                entry = self.per_class[OTHER_LABEL] = [0, 0]
            entry[0] += other
            if other > entry[1]:
                entry[1] = other

    def observe_drop(self, amount: int = 1) -> None:
        self.observations += 1
        self.dropped += max(0, int(amount))

    def observe_continuous(self, key: str, amount: int) -> None:
        if key == CONTINUOUS_KEY_DROPPED:
            self.observations += 1
            self.continuous_seen = True
            self.dropped += max(0, int(amount))
        elif key == CONTINUOUS_KEY_NO_NEW_FRAME:
            self.observations += 1
            self.continuous_seen = True
            self.skipped_no_new_frame += max(0, int(amount))
        # every other key (``modelUnavailable`` included) is ignored

    def observe_readiness(self, model: Any, state: Any, waited_ms: Any, ready: Any) -> None:
        self.observations += 1
        normalized = normalize_state(state)
        self._set_model_state(model, normalized)
        self.window_states.append(normalized)
        waited = max(0, int(waited_ms or 0))
        self.waited_ms += waited
        if not ready or waited > 0:
            self.waits += 1

    def observe_wait(self, document: Any) -> None:
        self.observations += 1
        if isinstance(document, dict):
            normalized = normalize_state(document.get("state"))
            self._set_model_state(document.get("model"), normalized)
            self.window_states.append(normalized)
            if not self._wait_active:
                self.waits += 1
            self._wait_active = True
        else:
            # None clears nothing: the state stays until the next observation.
            self._wait_active = False

    def _set_model_state(self, model: Any, state: str) -> None:
        name = str(model)[:MAX_MODEL_CHARS] if model is not None else ""
        if not name:
            return
        if name in self.model_states:
            self.model_states[name] = state
            return
        if len(self.model_states) >= MAX_MODELS:
            self.model_states.popitem(last=False)
        self.model_states[name] = state

    # -- the blocks ------------------------------------------------------------------

    def model_block(self) -> Dict[str, Any]:
        """The ``model`` block of a non-idle Summary (§4.1)."""
        if self.window_states:
            state = worst_state(self.window_states)
        elif self.model_states:
            # No observation in this Interval: the last known states, with
            # the window's (zero) waits.
            state = worst_state(list(self.model_states.values()))
        elif self.has_triton:
            state = STATE_UNKNOWN
        else:
            return {"state": STATE_NONE, "models": {}, "waits": 0, "waitedMs": 0}
        return {
            "state": state,
            "models": dict(self.model_states),
            "waits": int(self.waits),
            "waitedMs": int(self.waited_ms),
        }

    def summary_blocks(self, status: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """``runs``, ``latency``, ``detections``, ``outcomes``, ``people``,
        ``model`` and ``continuous`` for the window in progress."""
        blocks: Dict[str, Any] = {
            "runs": {"completed": self.completed, "failed": self.failed, "dropped": self.dropped},
        }
        if self.latency_samples:
            ordered = sorted(self.latency_samples)
            blocks["latency"] = {
                "p50Ms": _percentile(ordered, 0.5),
                "p95Ms": _percentile(ordered, 0.95),
                "maxMs": int(self.latency_max if self.latency_max is not None else ordered[-1]),
                "n": len(ordered),
            }
        if self.detections_present:
            blocks["detections"] = {
                "runsWithDetections": self.runs_with_detections,
                "total": self.detections_total,
                "perClass": {label: {"total": entry[0], "max": entry[1]}
                             for label, entry in self.per_class.items()},
                "truncated": bool(self.detections_truncated),
            }
            blocks["people"] = {"seen": self.people_seen_max}
        if self.outcomes_present:
            blocks["outcomes"] = {"anomalous": self.anomalous, "normal": self.normal}
        blocks["model"] = self.model_block()
        if status is not None or self.continuous_seen:
            status = status if isinstance(status, dict) else {}
            try:
                fps = float(status.get("effectiveFps") or 0.0)
            except (TypeError, ValueError):
                fps = 0.0
            blocks["continuous"] = {
                "skippedNoNewFrame": self.skipped_no_new_frame,
                "paused": bool(status.get("paused", False)),
                "effectiveFps": max(0.0, fps),
            }
        return blocks


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


class IntervalSummarizer:
    """See the module docstring.

    ``settings`` is a :class:`SummarizerSettings`; ``clock`` returns epoch
    seconds (the wall clock, so the ticker aligns to wall-clock multiples);
    ``registry_view`` exposes ``snapshot() -> {registration_id:
    RegistrationInfo}``. Keyword-only collaborators: ``device_id`` (the thing
    name), ``instance`` (the agent's UUID), ``producer_version``, ``sink``
    (called with each :class:`Emission`, outside the lock), ``status_provider``
    (``registration_id -> {paused, effectiveFps} | None``, sampled at each
    Interval end), ``counters_provider`` (cumulative ``{queueDrops,
    publishFailures, eventsDropped}``; the Summaries carry the deltas since
    the previous emission of this instance) and ``size_cap_bytes``.
    """

    def __init__(
        self,
        settings: SummarizerSettings,
        clock: Callable[[], float],
        registry_view: Any,
        *,
        device_id: str = "",
        instance: str = "",
        producer_version: str = DEFAULT_PRODUCER_VERSION,
        sink: Optional[Callable[[Emission], None]] = None,
        status_provider: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        counters_provider: Optional[Callable[[], Dict[str, int]]] = None,
        size_cap_bytes: int = MAX_SUMMARY_BYTES,
        poll_s: float = 0.5,
    ) -> None:
        self._settings = settings if isinstance(settings, SummarizerSettings) else SummarizerSettings()
        self._clock = clock
        self._view = registry_view
        self.device_id = str(device_id)
        self.instance = str(instance)
        self.producer_version = str(producer_version)
        self._sink = sink
        self._status_provider = status_provider
        self._counters_provider = counters_provider
        self._size_cap = int(size_cap_bytes)
        self._poll_s = float(poll_s)
        self._lock = threading.RLock()
        self._inbox: Deque[Tuple[str, int, tuple]] = collections.deque(maxlen=INBOX_MAXLEN)
        self._accumulators: "OrderedDict[str, PairAccumulator]" = OrderedDict()
        self._recent: Dict[str, Tuple[Any, int]] = {}
        self._next_summary_ms: Optional[int] = None
        self._next_heartbeat_ms: Optional[int] = None
        self._last_counters: Dict[str, int] = {}
        #: Every emission of this instance (the status ``seq``).
        self._seq = 0
        #: The last ``seq`` per workflowId (the Portal's Pair key); kept
        #: when an accumulator is dropped, recreated or transient.
        self._seq_by_workflow: Dict[str, int] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.emitted_summaries = 0
        self.emitted_heartbeats = 0
        self.discarded_observations = 0
        self.inbox_drops = 0
        self.oversized = 0
        self.last_emission_ms: Optional[int] = None
        if self._settings.enabled:
            now_ms = self._now_ms()
            self._arm(now_ms)
            self._adopt_snapshot(now_ms, [])

    # -- time ------------------------------------------------------------------------

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    @staticmethod
    def _next_boundary(now_ms: int, step_ms: int) -> int:
        return (int(now_ms) // int(step_ms) + 1) * int(step_ms)

    def _arm(self, now_ms: int) -> None:
        self._next_summary_ms = self._next_boundary(now_ms, self._settings.summary_interval_s * 1000)
        self._next_heartbeat_ms = self._next_boundary(now_ms, self._settings.idle_heartbeat_s * 1000)

    # -- settings ------------------------------------------------------------------------

    @property
    def settings(self) -> SummarizerSettings:
        return self._settings

    @property
    def seq(self) -> int:
        """The number of Summaries and Heartbeats this instance emitted (the
        sum of the per-workflow ``seq`` counters)."""
        return self._seq

    def configure(self, settings: SummarizerSettings) -> None:
        """Apply new settings (§5.4). Enable resets every accumulator and
        starts the first Interval now; disable discards the Interval in
        progress (``seq`` continues); an interval change re-arms the ticker."""
        if not isinstance(settings, SummarizerSettings):
            raise TypeError("settings must be a SummarizerSettings")
        with self._lock:
            previous = self._settings
            self._settings = settings
            now_ms = self._now_ms()
            if settings.enabled and not previous.enabled:
                self._accumulators.clear()
                self._recent.clear()
                self._inbox.clear()
                self._arm(now_ms)
                self._adopt_snapshot(now_ms, [])
                logger.info("Telemetry summarizer enabled (interval %d s, heartbeat %d s)",
                            settings.summary_interval_s, settings.idle_heartbeat_s)
            elif previous.enabled and not settings.enabled:
                discarded = sum(acc.observations for acc in self._accumulators.values())
                self._accumulators.clear()
                self._recent.clear()
                self._inbox.clear()
                self._next_summary_ms = self._next_heartbeat_ms = None
                logger.info("Telemetry summarizer disabled; the Interval in progress is discarded "
                            "(%d observations)", discarded)
            elif settings.enabled and (
                    settings.summary_interval_s != previous.summary_interval_s
                    or settings.idle_heartbeat_s != previous.idle_heartbeat_s):
                self._arm(now_ms)
        self._wake.set()

    # -- observers (enqueue and return) -------------------------------------------------

    def _enqueue(self, kind: str, args: tuple) -> None:
        with self._lock:
            if not self._settings.enabled:
                self.discarded_observations += 1
                return
            if len(self._inbox) >= self._inbox.maxlen:
                self.inbox_drops += 1
            self._inbox.append((kind, self._now_ms(), args))

    def observe_run(self, record: Any) -> None:
        self._enqueue("run", (record,))

    def observe_drop(self, registration_id: Optional[str], node_id: Optional[str], reason: str) -> None:
        self._enqueue("drop", (registration_id, node_id, reason))

    def observe_continuous(self, registration_id: str, key: str, amount: int) -> None:
        self._enqueue("continuous", (registration_id, key, amount))

    def observe_model(self, kind: str, *args: Any) -> None:
        """The ``register_model_observer`` shape: ``("readiness",
        registration_id, execution_id, model, state, waited_ms, ready)`` or
        ``("wait", registration_id, document_or_None)``."""
        if kind == "readiness" and len(args) == 6:
            self._enqueue("readiness", tuple(args))
        elif kind == "wait" and len(args) == 2:
            self._enqueue("wait", tuple(args))

    def wake(self) -> None:
        """Wake the ticker (the registration view's listener calls it)."""
        self._wake.set()

    # -- the ticker -----------------------------------------------------------------------

    def start(self) -> "IntervalSummarizer":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._loop, name="telemetry-summarizer", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._poll_s)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the next tick runs
                logger.exception("Telemetry summarizer: tick failed")

    def tick(self, now_ms: Optional[int] = None) -> List[Emission]:
        """Fold the queued observations, emit at every boundary passed and
        reconcile the registration snapshot. Returns (and hands to the sink)
        the emissions."""
        emissions: List[Emission] = []
        with self._lock:
            now = self._now_ms() if now_ms is None else int(now_ms)
            items = list(self._inbox)
            self._inbox.clear()
            if not self._settings.enabled:
                self.discarded_observations += len(items)
                return []
            if self._next_summary_ms is None or self._next_heartbeat_ms is None:
                self._arm(now)
            interval_ms = self._settings.summary_interval_s * 1000
            heartbeat_ms = self._settings.idle_heartbeat_s * 1000
            boundaries: List[Tuple[int, str]] = []
            while self._next_summary_ms <= now:
                boundaries.append((self._next_summary_ms, "summary"))
                self._next_summary_ms += interval_ms
            while self._next_heartbeat_ms <= now:
                boundaries.append((self._next_heartbeat_ms, "heartbeat"))
                self._next_heartbeat_ms += heartbeat_ms
            boundaries.sort()
            index = 0
            for boundary_ms, kind in boundaries:
                while index < len(items) and items[index][1] <= boundary_ms:
                    self._fold(items[index])
                    index += 1
                if kind == "summary":
                    self._summary_boundary(boundary_ms, now, emissions)
                else:
                    self._heartbeat_boundary(boundary_ms, now, emissions)
            while index < len(items):
                self._fold(items[index])
                index += 1
            self._adopt_snapshot(now, emissions)
            self._expire_recent(now)
        if self._sink is not None:
            for emission in emissions:
                try:
                    self._sink(emission)
                except Exception:  # noqa: BLE001 - the sink never fails the ticker
                    logger.warning("Telemetry summarizer: sink failed", exc_info=True)
        return emissions

    # -- folding -----------------------------------------------------------------------------

    def _fold(self, item: Tuple[str, int, tuple]) -> None:
        kind, stamp_ms, args = item
        try:
            if kind == "run":
                record = args[0]
                registration_id = getattr(record, "registration_id", None)
                acc = self._accumulator_for(registration_id, stamp_ms, record)
                if acc is not None:
                    acc.observe_run(record, stamp_ms, self._settings.person_classes)
            elif kind == "drop":
                acc = self._accumulator_for(args[0], stamp_ms)
                if acc is not None:
                    acc.observe_drop(1)
            elif kind == "continuous":
                registration_id, key, amount = args
                acc = self._accumulator_for(registration_id, stamp_ms)
                if acc is not None:
                    acc.observe_continuous(str(key), int(amount or 0))
            elif kind == "readiness":
                registration_id, _execution_id, model, state, waited_ms, ready = args
                acc = self._accumulator_for(registration_id, stamp_ms)
                if acc is not None:
                    acc.observe_readiness(model, state, waited_ms, ready)
            elif kind == "wait":
                registration_id, document = args
                acc = self._accumulator_for(registration_id, stamp_ms)
                if acc is not None:
                    acc.observe_wait(document)
        except Exception:  # noqa: BLE001 - one bad observation never stops the fold
            logger.debug("Telemetry summarizer: could not fold a %s observation", kind, exc_info=True)

    def _accumulator_for(self, registration_id: Any, stamp_ms: int,
                         record: Any = None) -> Optional[PairAccumulator]:
        if not isinstance(registration_id, str) or not registration_id:
            return None
        acc = self._accumulators.get(registration_id)
        if acc is not None:
            if acc.transient and record is not None and acc.workflow_id is None:
                acc.workflow_id = str(getattr(record, "workflow_id", "") or "") or None
                acc.version = str(getattr(record, "workflow_version", "") or "") or None
            return acc
        # A late observation for an id that is not in the snapshot: a
        # transient accumulator that emits once (with a run) and is dropped.
        recent = self._recent.get(registration_id)
        info = recent[0] if recent is not None else None
        workflow_id = getattr(info, "workflow_id", None) if info is not None else None
        version = getattr(info, "version", None) if info is not None else None
        has_triton = bool(getattr(info, "has_triton", False)) if info is not None else False
        if record is not None:
            workflow_id = workflow_id or getattr(record, "workflow_id", None)
            version = version or getattr(record, "workflow_version", None)
        acc = PairAccumulator(registration_id, workflow_id, version, has_triton, stamp_ms, transient=True)
        if info is not None:
            acc.model_states.update(getattr(info, "model_states", {}) or {})
        self._accumulators[registration_id] = acc
        return acc

    # -- boundaries ---------------------------------------------------------------------------------

    def _summary_boundary(self, boundary_ms: int, now_ms: int, emissions: List[Emission]) -> None:
        for registration_id, acc in list(self._accumulators.items()):
            if acc.transient:
                if acc.workflow_id and acc.last_run_ms is not None:
                    if self._emit_summary(acc, boundary_ms, now_ms, emissions):
                        del self._accumulators[registration_id]
                    # a window still shorter than 1 s waits for the next boundary
                else:
                    del self._accumulators[registration_id]
                continue
            if acc.has_recent_run(boundary_ms):
                self._emit_summary(acc, boundary_ms, now_ms, emissions)

    def _heartbeat_boundary(self, boundary_ms: int, now_ms: int, emissions: List[Emission]) -> None:
        for acc in list(self._accumulators.values()):
            if acc.transient:
                continue
            if not acc.has_recent_run(boundary_ms):
                self._emit_heartbeat(acc, boundary_ms, now_ms, emissions)

    def _adopt_snapshot(self, now_ms: int, emissions: List[Emission]) -> None:
        """Create accumulators for new registration ids; emit the final
        window of ids that left and drop them (remembering their info)."""
        try:
            snapshot = self._view.snapshot() if self._view is not None else {}
        except Exception:  # noqa: BLE001 - the snapshot keeps its last value
            logger.debug("Telemetry summarizer: registration snapshot unavailable", exc_info=True)
            return
        if not isinstance(snapshot, dict):
            return
        for registration_id, info in snapshot.items():
            acc = self._accumulators.get(registration_id)
            workflow_id = getattr(info, "workflow_id", None)
            version = getattr(info, "version", None)
            has_triton = bool(getattr(info, "has_triton", False))
            if acc is None:
                self._accumulators[registration_id] = PairAccumulator(
                    registration_id, workflow_id, version, has_triton, now_ms)
            else:
                acc.transient = False
                if workflow_id:
                    acc.workflow_id = str(workflow_id)
                if version is not None:
                    acc.version = str(version)
                acc.has_triton = has_triton
        for registration_id, acc in list(self._accumulators.items()):
            if acc.transient or registration_id in snapshot:
                continue
            self._final_window(acc, now_ms, emissions)
            self._recent[registration_id] = (_RememberedInfo(acc), now_ms)
            del self._accumulators[registration_id]

    def _final_window(self, acc: PairAccumulator, now_ms: int, emissions: List[Emission]) -> None:
        if acc.has_recent_run(now_ms):
            emitted = self._emit_summary(acc, now_ms, now_ms, emissions)
        else:
            emitted = self._emit_heartbeat(acc, now_ms, now_ms, emissions)
        if not emitted:
            logger.debug("Telemetry summarizer: registration %s left with a window shorter than 1 s; "
                         "nothing emitted", acc.registration_id)

    def _expire_recent(self, now_ms: int) -> None:
        for registration_id in [rid for rid, (_, seen) in self._recent.items()
                                if now_ms - seen > RECENT_INFO_TTL_MS]:
            del self._recent[registration_id]

    # -- emission -----------------------------------------------------------------------------------

    def _emit_summary(self, acc: PairAccumulator, end_ms: int, now_ms: int,
                      emissions: List[Emission]) -> bool:
        if not acc.workflow_id:
            return False
        interval_ms = self._settings.summary_interval_s * 1000
        start_ms = acc.window_start_ms
        if end_ms - start_ms < MIN_WINDOW_MS:
            return False
        if end_ms - start_ms > interval_ms + WINDOW_ALLOWANCE_MS:
            start_ms = end_ms - interval_ms
        status = None
        if self._status_provider is not None and not acc.transient:
            try:
                status = self._status_provider(acc.registration_id)
            except Exception:  # noqa: BLE001 - a provider failure is no continuous block
                status = None
        payload = self._envelope(acc, start_ms, end_ms, max(now_ms, end_ms),
                                 self._settings.summary_interval_s, idle=False)
        payload.update(acc.summary_blocks(status))
        payload["telemetry"] = self._telemetry_counters()
        self._finish(acc, payload, end_ms, emissions)
        self.emitted_summaries += 1
        return True

    def _emit_heartbeat(self, acc: PairAccumulator, end_ms: int, now_ms: int,
                        emissions: List[Emission]) -> bool:
        if not acc.workflow_id:
            return False
        heartbeat_ms = self._settings.idle_heartbeat_s * 1000
        start_ms = max(acc.window_start_ms, end_ms - heartbeat_ms)
        if end_ms - start_ms < MIN_WINDOW_MS:
            return False
        payload = self._envelope(acc, start_ms, end_ms, max(now_ms, end_ms),
                                 self._settings.idle_heartbeat_s, idle=True)
        payload["telemetry"] = self._telemetry_counters()
        self._finish(acc, payload, end_ms, emissions)
        self.emitted_heartbeats += 1
        return True

    def _envelope(self, acc: PairAccumulator, start_ms: int, end_ms: int, emitted_ms: int,
                  interval_s: int, idle: bool) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "intervalStart": int(start_ms),
            "intervalEnd": int(end_ms),
            "emittedAt": int(emitted_ms),
            "deviceId": self.device_id,
            "workflowId": acc.workflow_id,
            "workflowVersion": str(acc.version if acc.version is not None else ""),
            "producer": {"name": PRODUCER_NAME, "version": self.producer_version,
                         "instance": self.instance},
            "seq": 0,  # assigned in _finish
            "intervalS": int(interval_s),
            "idle": bool(idle),
        }

    def _finish(self, acc: PairAccumulator, payload: Dict[str, Any], end_ms: int,
                emissions: List[Emission]) -> None:
        self._seq += 1
        workflow_seq = self._seq_by_workflow.get(acc.workflow_id, 0) + 1
        self._seq_by_workflow[acc.workflow_id] = workflow_seq
        payload["seq"] = workflow_seq
        self._fit_to_cap(payload)
        acc.reset_window(end_ms)
        self.last_emission_ms = int(end_ms)
        emissions.append(Emission(acc.registration_id, acc.workflow_id, payload))

    def _telemetry_counters(self) -> Dict[str, int]:
        current: Dict[str, int] = {}
        if self._counters_provider is not None:
            try:
                raw = self._counters_provider() or {}
                for key in ("queueDrops", "publishFailures", "eventsDropped"):
                    current[key] = max(0, int(raw.get(key, 0) or 0))
            except Exception:  # noqa: BLE001 - counters are best-effort
                current = {}
        block = {}
        for key in ("queueDrops", "publishFailures", "eventsDropped"):
            value = current.get(key, 0)
            block[key] = max(0, value - self._last_counters.get(key, 0))
        if current:
            self._last_counters = current
        return block

    def _fit_to_cap(self, payload: Dict[str, Any]) -> None:
        """Reduce a payload above the size cap: fold ``perClass`` into
        ``__other__`` (``truncated``), then drop ``model.models``."""
        if _size(payload) <= self._size_cap:
            return
        self.oversized += 1
        detections = payload.get("detections")
        if isinstance(detections, dict) and detections.get("perClass"):
            per_class = detections["perClass"]
            total = sum(int(v.get("total", 0)) for v in per_class.values())
            maximum = max(int(v.get("max", 0)) for v in per_class.values())
            detections["perClass"] = {OTHER_LABEL: {"total": total, "max": maximum}}
            detections["truncated"] = True
            if _size(payload) <= self._size_cap:
                return
        model = payload.get("model")
        if isinstance(model, dict) and model.get("models"):
            model["models"] = {}
            if _size(payload) <= self._size_cap:
                return
        payload.pop("detections", None)
        payload.pop("people", None)
        logger.warning("Telemetry summarizer: a Summary for workflow %s exceeded %d bytes; its "
                       "detections were dropped", payload.get("workflowId"), self._size_cap)

    # -- diagnostics ------------------------------------------------------------------------------------

    def tracked_ids(self) -> List[str]:
        with self._lock:
            return [rid for rid, acc in self._accumulators.items() if not acc.transient]

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._settings.enabled,
                "summaryIntervalS": self._settings.summary_interval_s,
                "idleHeartbeatS": self._settings.idle_heartbeat_s,
                "personClasses": list(self._settings.person_classes),
                "trackedRegistrations": [rid for rid, acc in self._accumulators.items()
                                         if not acc.transient],
                "seq": self._seq,
                "emittedSummaries": self.emitted_summaries,
                "emittedHeartbeats": self.emitted_heartbeats,
                "discardedObservations": self.discarded_observations,
                "inboxDrops": self.inbox_drops,
                "oversized": self.oversized,
                "lastEmissionMs": self.last_emission_ms,
                "threadAlive": bool(self._thread is not None and self._thread.is_alive()),
            }


class _RememberedInfo:
    """What a dropped registration leaves behind for a late observation."""

    def __init__(self, acc: PairAccumulator) -> None:
        self.workflow_id = acc.workflow_id
        self.version = acc.version
        self.has_triton = acc.has_triton
        self.model_states = dict(acc.model_states)


def _size(payload: Dict[str, Any]) -> int:
    return len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))
