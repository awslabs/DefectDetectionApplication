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

"""Device_Score_Job runner (Requirements 6.4, 6.9, 6.11, 6.15, 9.6).

An ``llm_inference`` node's model is device-local (the Text_Generation_API
on ``localhost``), so the Portal cannot replay a Candidate against it. It
instead delivers a **Device_Score_Job** through the device's
``dda-workflow-tuning`` named shadow — the mechanism Camera_Bindings
already use (``camera_binding_store``) — and this module executes it:

    desired.jobs[jobId] = {manifestKey, cancel}
        -> read s3://{bucket}/{manifestKey}
        -> per (sample, repeat), concurrency 1, on a dedicated worker:
             GET the sample's image bytes
             render_prompt(prompt_template, the manifest's metadataSnippet)
             build_llm_invocation(...)          (the shared builder)
             the executor's own transport       (URL, timeout, loading wait)
             parse_verdict -> categorize_outcome
        -> append outcome batches of <= 20 objects to
           {root}sessions/{sessionId}/runs/{runId}/outcomes-{n}.json
    reported.jobs[jobId] = {status, done, total, error?, updatedAt}

Design constraints implemented literally:

- **Faithful replay** (Requirements 6.2, 6.4, Property 6). The request is
  built by the shared ``workflow_core.anomaly_invocation`` builder and
  sent through the executor's ``_default_llm_invoker`` — the same URL,
  timeout, body construction and 409/``state=loading`` wait policy a
  production run uses — with the executor's invoker arity rules, so a
  Candidate's score predicts the deployed node.
- **Never in a run's way** (Requirements 6.15, 11.1, 11.2). Job work runs
  on its own single worker thread (concurrency 1), never touches the
  workflow executor, the run database (except one read-only lookup of the
  node's registered parameters), run artifacts or Run_Metadata, and every
  step is contained: a defect can only fail the job it occurred in.
- **Inert unless tuning is configured** (Requirements 2.6, 11.3). Without
  an :class:`ExportConfig` (the same ``workflowTuning`` component
  configuration Sample_Export reads) :func:`start_job_runner` installs
  nothing: no shadow subscription, no S3 client, no thread.
- **Carries only identifiers, prompts and keys** (Requirement 9.6). The
  device reads sample bytes from the Sample_Store and writes outcome
  batches only under the job's own session/run prefix; nothing else in
  the bucket is written.

Stdlib only at import time; ``boto3`` is imported lazily by the default
client factory, which is injectable so tests never need it.
"""

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
)

from workflow_engine.llm_inference import (
    UnresolvedPlaceholderError,
    render_prompt,
)
from workflow_engine.output_bindings import (
    _accepts_keyword,
    _default_llm_invoker,
    _downscale_frame_or_original,
)
from workflow_engine.tuning.backfill import tunable_nodes
from workflow_engine.tuning.sample_export import (
    THING_NAME_ENV,
    ExportConfig,
    sample_exporter,
)
from workflow_engine.vendor.workflow_core.anomaly_invocation import (
    LABEL_NOK,
    LABEL_OK,
    NODE_TYPE_LLM_INFERENCE,
    build_llm_invocation,
    categorize_outcome,
    parse_verdict,
)

logger = logging.getLogger(__name__)

#: The named shadow carrying Device_Score_Jobs (design: "Device_Score_Job
#: (shadow + manifest)"), delivered exactly like Camera_Bindings.
TUNING_SHADOW_NAME = "dda-workflow-tuning"

#: Shadow key holding the job map in both ``desired`` and ``reported``.
JOBS_KEY = "jobs"

#: Sample_Outcomes per S3 object (Requirement 6.9).
OUTCOME_BATCH_SIZE = 20

#: Repeats per sample are bounded to 1..3 (Requirement 6.7). The Portal
#: enforces the bound; the device clamps defensively so a malformed
#: manifest can never issue an unbounded number of invocations.
MIN_REPEATS = 1
MAX_REPEATS = 3

#: Concurrency of job replay (Requirement 6.9): strictly one invocation
#: at a time, on a worker separate from the executor (Requirement 6.15).
JOB_CONCURRENCY = 1

#: Attempts (with doubling backoff) per outcome-batch upload before the
#: job is reported failed — a batch the Portal never sees would make
#: ``done == total`` unreachable.
MAX_BATCH_ATTEMPTS = 3
BATCH_BACKOFF_BASE_SECONDS = 1.0

#: Reported job statuses (design: ``reported.jobs[jobId].status``).
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

#: Outcome-batch document schema version.
OUTCOME_SCHEMA_VERSION = 1

#: Sample_Store prefix segment the tuning root is derived from, and the
#: job/session segments beneath that root. The component configuration
#: delivers ``workflow-tuning/samples/``; manifests live under
#: ``workflow-tuning/jobs/`` and outcome batches under
#: ``workflow-tuning/sessions/`` (design: the S3 layout).
SAMPLES_SEGMENT = "samples/"
JOBS_SEGMENT = "jobs/"
SESSIONS_SEGMENT = "sessions/"

#: Manifest object name under ``{root}jobs/{jobId}/``.
MANIFEST_OBJECT = "manifest.json"

#: Outcome batch object name pattern under the run's prefix. ``n`` counts
#: from 1 and is NOT zero-padded (the design's ``outcomes-{n}.json``), so
#: the Portal must ingest batches by set difference rather than by
#: lexicographic order.
OUTCOME_OBJECT_TEMPLATE = "outcomes-{0}.json"

#: Content type of the outcome batch objects.
JSON_CONTENT_TYPE = "application/json"


# ---------------------------------------------------------------------------
# S3 layout
# ---------------------------------------------------------------------------

def tuning_root(prefix: Any) -> str:
    """The ``workflow-tuning/`` root derived from the Sample_Store prefix.

    The component configuration delivers the SAMPLES prefix
    (``workflow-tuning/samples/``); jobs and sessions are siblings of
    ``samples/`` under the same root, so the trailing segment is removed.
    A prefix that is not the samples prefix is used as the root as-is (a
    missing trailing ``/`` is added), so a Use_Case that configures a
    different layout keeps everything under its own prefix.
    """
    text = str(prefix or "")
    if text.endswith(SAMPLES_SEGMENT):
        return text[:-len(SAMPLES_SEGMENT)]
    if text and not text.endswith("/"):
        return text + "/"
    return text


def manifest_key(prefix: Any, job_id: str) -> str:
    """``{root}jobs/{jobId}/manifest.json`` — the fallback location when
    the shadow entry carries no ``manifestKey``."""
    return "{0}{1}{2}/{3}".format(
        tuning_root(prefix), JOBS_SEGMENT, job_id, MANIFEST_OBJECT)


def outcomes_prefix(prefix: Any, session_id: str, run_id: str) -> str:
    """``{root}sessions/{sessionId}/runs/{runId}/`` — the only place this
    job writes (Requirement 9.6)."""
    return "{0}{1}{2}/runs/{3}/".format(
        tuning_root(prefix), SESSIONS_SEGMENT, session_id, run_id)


def outcomes_key(prefix: Any, session_id: str, run_id: str, index: int) -> str:
    """The ``outcomes-{n}.json`` key of one batch (``index`` from 1)."""
    return outcomes_prefix(prefix, session_id, run_id) + (
        OUTCOME_OBJECT_TEMPLATE.format(int(index)))


# ---------------------------------------------------------------------------
# The job manifest (Requirement 9.6: identifiers, prompts and keys only)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ManifestSample:
    """One Tuning_Sample to replay: its Sample_Store keys, its Label and
    the Run_Metadata snippet its Prompt_Template renders against."""

    sample_id: str
    input_key: str
    label: str
    reference_key: Optional[str] = None
    metadata_snippet: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JobManifest:
    """A Device_Score_Job's manifest, as the Portal writes it.

    ``node_parameters`` are the Node_Parameters of the Tunable_Node in the
    latest Workflow_Definition version and ``prompt_set`` the Candidate's
    Prompt_Set; both are merged with the device's own registered
    parameters by :meth:`JobRunner.resolve_parameters`.
    """

    job_id: str
    session_id: str
    run_id: str
    workflow_id: str
    node_id: str
    samples: Tuple[ManifestSample, ...]
    repeats: int = MIN_REPEATS
    node_parameters: Mapping[str, Any] = field(default_factory=dict)
    prompt_set: Mapping[str, Any] = field(default_factory=dict)
    outcomes_prefix_override: Optional[str] = None
    #: Samples the manifest listed that cannot be replayed (no id, no
    #: input key, or a Label outside {OK, NOK} — only labelled,
    #: non-excluded samples are scored, Requirement 4.3). They are
    #: excluded from ``total`` so ``done == total`` stays reachable.
    unusable: int = 0

    @property
    def total(self) -> int:
        """The planned invocations: replayable samples × repeats."""
        return len(self.samples) * self.repeats

    @classmethod
    def from_document(
        cls, document: Any, job_id: str
    ) -> Optional["JobManifest"]:
        """Parse a manifest document, or ``None`` when it is unusable.

        ``None`` (the job is reported failed naming the reason) for a
        non-mapping document or one missing the session/run identifiers
        the outcome objects are keyed by.
        """
        if not isinstance(document, Mapping):
            return None
        session_id = _text(document.get("sessionId"))
        run_id = _text(document.get("runId"))
        if not session_id or not run_id:
            return None
        samples: List[ManifestSample] = []
        unusable = 0
        raw_samples = document.get("samples")
        for entry in raw_samples if isinstance(raw_samples, list) else []:
            sample = _parse_sample(entry)
            if sample is None:
                unusable += 1
                continue
            samples.append(sample)
        repeats = _clamp_repeats(document.get("repeats"))
        parameters = document.get("nodeParameters")
        prompt_set = document.get("promptSet")
        override = document.get("outcomesPrefix")
        return cls(
            job_id=_text(document.get("jobId")) or job_id,
            session_id=session_id,
            run_id=run_id,
            workflow_id=_text(document.get("workflowId")),
            node_id=_text(document.get("nodeId")),
            samples=tuple(samples),
            repeats=repeats,
            node_parameters=(
                dict(parameters) if isinstance(parameters, Mapping) else {}
            ),
            prompt_set=(
                dict(prompt_set) if isinstance(prompt_set, Mapping) else {}
            ),
            outcomes_prefix_override=(
                override if isinstance(override, str) and override else None
            ),
            unusable=unusable,
        )


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _clamp_repeats(raw: Any) -> int:
    """Repeats within 1..3, defaulting to 1 (Requirement 6.7)."""
    try:
        repeats = int(raw)
    except (TypeError, ValueError):
        return MIN_REPEATS
    return max(MIN_REPEATS, min(MAX_REPEATS, repeats))


def _parse_sample(entry: Any) -> Optional[ManifestSample]:
    """One manifest sample, or ``None`` when it cannot be replayed."""
    if not isinstance(entry, Mapping):
        return None
    sample_id = _text(entry.get("sampleId"))
    input_key = _text(entry.get("inputKey"))
    label = _text(entry.get("label")).upper()
    if not sample_id or not input_key or label not in (LABEL_OK, LABEL_NOK):
        return None
    reference_key = _text(entry.get("referenceKey")) or None
    snippet = entry.get("metadataSnippet")
    return ManifestSample(
        sample_id=sample_id,
        input_key=input_key,
        label=label,
        reference_key=reference_key,
        metadata_snippet=dict(snippet) if isinstance(snippet, Mapping) else {},
    )


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

def _default_s3_factory():
    """The device's S3 client, constructed on first use.

    ``boto3`` is imported here (never at module import) so this module
    stays importable everywhere and an unconfigured device builds no
    client at all (Requirement 2.6).
    """
    import boto3

    return boto3.client("s3")


class JobRunner:
    """Executes Device_Score_Jobs delivered on the tuning named shadow.

    ``config`` of ``None`` builds an INERT runner: :meth:`sync` reads no
    shadow, no client is constructed and no worker is started — the shape
    a device without tuning sample export runs with (Requirement 2.6).

    Thread-safe: :meth:`sync` runs on the MQTT delta thread while the
    single job worker replays samples.
    """

    def __init__(
        self,
        config: Optional[ExportConfig],
        shadow_accessor: Any = None,
        s3_factory: Optional[Callable[[], Any]] = None,
        thing_name: Optional[str] = None,
        shadow_name: str = TUNING_SHADOW_NAME,
        invoker: Optional[Callable] = None,
        session_factory: Optional[Callable] = None,
        batch_size: int = OUTCOME_BATCH_SIZE,
        max_batch_attempts: int = MAX_BATCH_ATTEMPTS,
        backoff_base_seconds: float = BATCH_BACKOFF_BASE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._shadow = shadow_accessor
        self._s3_factory = s3_factory
        self._thing_name = (
            thing_name
            if thing_name is not None
            else os.environ.get(THING_NAME_ENV, "")
        )
        self.shadow_name = shadow_name
        self._invoker = invoker or _default_llm_invoker
        self._session_factory = session_factory
        self._batch_size = max(1, int(batch_size))
        self._max_batch_attempts = max(1, int(max_batch_attempts))
        self._backoff_base_seconds = float(backoff_base_seconds)
        self._sleep = sleep
        self._clock = clock
        self._monotonic = monotonic
        self._client: Any = None
        self._worker: Optional[threading.Thread] = None
        self._stopping = False
        self._running_job: Optional[str] = None
        self._condition = threading.Condition()
        #: Job ids accepted but not yet executed, in arrival order. Only
        #: allocated for a configured runner.
        self._pending: Optional[Deque[Tuple[str, Dict[str, Any]]]] = (
            deque() if config is not None else None
        )
        #: Job ids this process already accepted (a job stays in
        #: ``desired`` until the Portal finalizes it, so it must not be
        #: re-executed on every delta).
        self._handled: Dict[str, str] = {}
        #: Job ids whose ``desired`` entry requests cancellation.
        self._cancelled: set = set()

    # -- state -----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._pending is not None

    @property
    def config(self) -> Optional[ExportConfig]:
        return self._config

    @property
    def thing_name(self) -> str:
        return self._thing_name

    @property
    def pending_jobs(self) -> int:
        with self._condition:
            return len(self._pending) if self._pending is not None else 0

    @property
    def running_job(self) -> Optional[str]:
        with self._condition:
            return self._running_job

    # -- shadow ----------------------------------------------------------
    def on_delta(self, message: Optional[Mapping[str, Any]] = None) -> None:
        """Shadow delta notification: re-read ``desired`` and act on it.

        Never raises — a delta handler failure must not close the
        subscription stream (the ``camera_binding_store`` contract).
        """
        try:
            self.sync()
        except Exception:  # noqa: BLE001 - handler isolation (11.2)
            logger.exception(
                "Could not process a workflow-tuning shadow delta; the next "
                "delta or restart retries")

    def sync(self) -> None:
        """Reconcile against ``desired.jobs``: accept new jobs, record
        cancellations and prune stale ``reported`` entries.

        Cheap and non-blocking — the accepted jobs execute on the worker
        thread, so the delta handler never waits for a replay.
        """
        if self._pending is None:
            return
        state = self._read_shadow()
        if state is None:
            return
        desired = _jobs_of(state.get("desired"))
        reported = _jobs_of(state.get("reported"))
        accepted: List[Tuple[str, Dict[str, Any]]] = []
        with self._condition:
            for job_id, entry in desired.items():
                if _truthy(entry.get("cancel")):
                    self._cancelled.add(job_id)
                else:
                    self._cancelled.discard(job_id)
                if job_id in self._handled:
                    continue
                self._handled[job_id] = STATUS_QUEUED
                accepted.append((job_id, dict(entry)))
            for job_id, queued in accepted:
                self._pending.append((job_id, queued))
            if accepted:
                self._condition.notify()
        # The Portal removes a finalized job from ``desired``; its
        # ``reported`` entry is then stale and is deleted (a shadow value
        # of null removes the key).
        stale = [job_id for job_id in reported if job_id not in desired]
        prunable: List[str] = []
        with self._condition:
            for job_id in stale:
                if job_id == self._running_job:
                    # Never forget a job that is executing: its final
                    # report still has to land.
                    continue
                self._handled.pop(job_id, None)
                self._cancelled.discard(job_id)
                prunable.append(job_id)
        if prunable:
            self._prune_reported(prunable)
        for job_id, _ in accepted:
            self._report(job_id, STATUS_QUEUED, 0, None)
        if accepted:
            self.start()

    def _read_shadow(self) -> Optional[Dict[str, Any]]:
        """The tuning shadow's state, or ``None`` when it is unreadable
        or does not exist (nothing was ever delivered)."""
        if self._shadow is None:
            return None
        try:
            state = self._shadow.get_thing_shadow_state_request(
                self._thing_name, self.shadow_name)
        except Exception:  # noqa: BLE001 - transport errors => retry later
            logger.exception("Could not read the workflow-tuning shadow")
            return None
        if state is False or state is None:
            return None
        return dict(state) if isinstance(state, Mapping) else None

    def _update_shadow(self, jobs: Mapping[str, Any]) -> None:
        """Merge ``reported.jobs`` into the shadow; contained."""
        if self._shadow is None:
            return
        try:
            self._shadow.update_thing_shadow_state_request(
                self._thing_name, self.shadow_name,
                {"reported": {JOBS_KEY: dict(jobs)}})
        except Exception:  # noqa: BLE001 - reporting is best-effort
            logger.warning(
                "Could not report workflow-tuning job progress; the Portal "
                "finalizes the run on its silence timeout", exc_info=True)

    def _prune_reported(self, job_ids: List[str]) -> None:
        """Delete ``reported.jobs`` entries with no ``desired`` entry."""
        logger.debug(
            "Pruning %d stale workflow-tuning reported job entrie(s)",
            len(job_ids))
        self._update_shadow({job_id: None for job_id in job_ids})

    def _report(
        self,
        job_id: str,
        status: str,
        done: int,
        total: Optional[int],
        error: Optional[str] = None,
    ) -> None:
        """Publish ``reported.jobs[jobId] = {status, done, total,
        error?, updatedAt}`` (Requirement 6.9)."""
        entry: Dict[str, Any] = {
            "status": status,
            "done": int(done),
            "total": int(total) if total is not None else None,
            "updatedAt": int(self._clock()),
        }
        if error is not None:
            entry["error"] = str(error)[:512]
        with self._condition:
            if job_id in self._handled:
                self._handled[job_id] = status
        self._update_shadow({job_id: entry})

    # -- worker lifecycle ------------------------------------------------
    def start(self) -> None:
        """Start the job worker (idempotent, no-op when disabled).

        One worker, one job at a time, one invocation at a time
        (Requirements 6.9, 6.15): the workflow executor's threads are
        never touched.
        """
        if self._pending is None:
            return
        with self._condition:
            if self._stopping:
                return
            if self._worker is not None and self._worker.is_alive():
                return
            worker = threading.Thread(
                target=self._run, name="tuning-job-runner", daemon=True)
            self._worker = worker
        worker.start()

    def stop(self, timeout: Optional[float] = 5.0) -> None:
        """Ask the worker to stop after the job in flight; never raises."""
        worker = self._worker
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        if worker is not None and worker.is_alive():
            try:
                worker.join(timeout)
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                logger.debug(
                    "Tuning job worker join failed", exc_info=True)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until no job is pending or running (test/shutdown
        helper; ``False`` on timeout). Never used on a run path."""
        if self._pending is None:
            return True
        deadline = self._monotonic() + max(0.0, float(timeout))
        with self._condition:
            while self._pending or self._running_job is not None:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait(1.0)
                if not self._pending:
                    if self._stopping:
                        return
                    continue
                job_id, entry = self._pending.popleft()
                self._running_job = job_id
            try:
                self.run_job(job_id, entry)
            except Exception:  # noqa: BLE001 - a defect must not kill the
                # worker: the next job still runs.
                logger.exception(
                    "Workflow-tuning job %s failed unexpectedly; the runner "
                    "continues", job_id)
            finally:
                with self._condition:
                    self._running_job = None
                    self._condition.notify_all()

    # -- job execution ---------------------------------------------------
    def is_cancelled(self, job_id: str) -> bool:
        """True when ``desired.jobs[jobId].cancel`` requested cancellation
        (Requirement 6.11)."""
        with self._condition:
            return job_id in self._cancelled

    def run_job(
        self, job_id: str, entry: Optional[Mapping[str, Any]] = None
    ) -> None:
        """Execute one Device_Score_Job end to end.

        Never raises: a failure is reported as ``failed`` with its reason
        so the Portal finalizes the Score_Run instead of waiting out its
        silence timeout.
        """
        entry = entry if isinstance(entry, Mapping) else {}
        config = self._config
        if config is None:
            return
        key = _text(entry.get("manifestKey")) or manifest_key(
            config.prefix, job_id)
        try:
            document = self._read_json(key)
        except Exception as error:  # noqa: BLE001 - reported, not raised
            logger.error(
                "Workflow-tuning job %s failed: could not read the manifest "
                "%s (%s)", job_id, key, error)
            self._report(
                job_id, STATUS_FAILED, 0, None,
                "could not read the job manifest {0}: {1}".format(key, error))
            return
        manifest = JobManifest.from_document(document, job_id)
        if manifest is None:
            logger.error(
                "Workflow-tuning job %s failed: the manifest %s is malformed",
                job_id, key)
            self._report(
                job_id, STATUS_FAILED, 0, None,
                "the job manifest {0} is malformed (sessionId/runId "
                "missing)".format(key))
            return
        if manifest.unusable:
            logger.warning(
                "Workflow-tuning job %s: %d manifest sample(s) are not "
                "replayable (missing id/key or an unscoreable label) and are "
                "excluded", job_id, manifest.unusable)
        try:
            parameters = self.resolve_parameters(manifest)
        except Exception as error:  # noqa: BLE001 - reported, not raised
            logger.error(
                "Workflow-tuning job %s failed: %s", job_id, error)
            self._report(job_id, STATUS_FAILED, 0, manifest.total, str(error))
            return
        logger.info(
            "Workflow-tuning job %s starting: %d sample(s) × %d repeat(s) "
            "for node '%s' of workflow %s", job_id, len(manifest.samples),
            manifest.repeats, manifest.node_id, manifest.workflow_id)
        self._report(job_id, STATUS_RUNNING, 0, manifest.total)
        self._execute(job_id, manifest, parameters)

    def _execute(
        self,
        job_id: str,
        manifest: JobManifest,
        parameters: Dict[str, Any],
    ) -> None:
        """Replay every ``(sample, repeat)`` once, batching outcomes."""
        total = manifest.total
        done = 0
        batch_index = 0
        batch: List[Dict[str, Any]] = []

        def flush() -> bool:
            """Persist the pending batch; ``True`` when it landed."""
            nonlocal batch, batch_index, done
            if not batch:
                return True
            batch_index += 1
            try:
                self._write_batch(manifest, batch_index, batch)
            except Exception as error:  # noqa: BLE001 - reported per 6.12
                logger.error(
                    "Workflow-tuning job %s failed: could not persist "
                    "outcome batch %d (%s)", job_id, batch_index, error)
                self._report(
                    job_id, STATUS_FAILED, done, total,
                    "could not persist outcome batch {0}: {1}".format(
                        batch_index, error))
                return False
            done += len(batch)
            batch = []
            self._report(job_id, STATUS_RUNNING, done, total)
            return True

        for sample in manifest.samples:
            for repeat in range(1, manifest.repeats + 1):
                if self.is_cancelled(job_id) or self._stopping:
                    # Cancellation stops the device after the batch in
                    # flight: the outcomes already produced are kept
                    # (Requirement 6.11).
                    if not flush():
                        return
                    logger.info(
                        "Workflow-tuning job %s cancelled after %d/%d "
                        "invocation(s)", job_id, done, total)
                    self._report(job_id, STATUS_CANCELLED, done, total)
                    return
                batch.append(
                    self._replay(sample, repeat, parameters, manifest.node_id))
                if len(batch) >= self._batch_size and not flush():
                    return
        if not flush():
            return
        logger.info(
            "Workflow-tuning job %s completed: %d/%d invocation(s) in %d "
            "batch(es)", job_id, done, total, batch_index)
        self._report(job_id, STATUS_COMPLETED, done, total)

    def _replay(
        self,
        sample: ManifestSample,
        repeat: int,
        parameters: Dict[str, Any],
        node_id: str = "",
    ) -> Dict[str, Any]:
        """Replay one Candidate on one sample once: the Sample_Outcome.

        The whole path is the executor's: the shared builder constructs
        the request, the executor's transport sends it (URL, timeout and
        the 409/``loading`` wait policy included) and the shared parser
        and categorizer produce the outcome (Requirements 6.4, 6.5).
        """
        outcome: Dict[str, Any] = {
            "sampleId": sample.sample_id,
            "repeat": int(repeat),
            "label": sample.label,
        }
        try:
            input_bytes = self._read_bytes(sample.input_key)
        except Exception as error:  # noqa: BLE001 - per-sample outcome
            return _invocation_error(
                outcome, sample.label,
                "could not read the sample image {0}: {1}".format(
                    sample.input_key, error))
        reference_bytes: Optional[bytes] = None
        if sample.reference_key:
            try:
                reference_bytes = self._read_bytes(sample.reference_key)
            except Exception as error:  # noqa: BLE001 - per-sample outcome
                return _invocation_error(
                    outcome, sample.label,
                    "could not read the sample reference image {0}: "
                    "{1}".format(sample.reference_key, error))
        try:
            rendered = render_prompt(
                str(parameters.get("prompt_template") or ""),
                dict(sample.metadata_snippet or {}),
            )
        except UnresolvedPlaceholderError as error:
            # Replay never invents metadata (design: Error Handling).
            return _invocation_error(
                outcome, sample.label,
                "unresolved placeholder {0}".format(error.name))
        invocation = build_llm_invocation(
            parameters,
            rendered,
            input_bytes,
            reference_bytes,
            downscaler=(
                lambda data, max_dim, port: _downscale_frame_or_original(
                    data, max_dim, node_id, port)
            ),
        )
        # The invoker's third positional argument carries the generation
        # parameters, with the resolved Output_Token_Budget — exactly as
        # the executor hands them to the same transport.
        invoker_parameters = dict(parameters)
        invoker_parameters.update(invocation.generation)
        metrics: List[Dict[str, Any]] = []

        def capture_metrics(payload: Any) -> None:
            if isinstance(payload, dict):
                metrics.append(payload)

        if invocation.image_b64 is not None and (
            invocation.reference_b64 is not None
        ):
            invoker_args: Tuple[Any, ...] = (
                invocation.model_name, invocation.prompt, invoker_parameters,
                invocation.image_b64, invocation.reference_b64,
            )
        elif invocation.image_b64 is not None:
            invoker_args = (
                invocation.model_name, invocation.prompt, invoker_parameters,
                invocation.image_b64,
            )
        else:
            invoker_args = (
                invocation.model_name, invocation.prompt, invoker_parameters)
        invoker_kwargs: Dict[str, Any] = {}
        if invocation.system_prompt is not None:
            invoker_kwargs["system_prompt"] = invocation.system_prompt
        if _accepts_keyword(self._invoker, "metrics_sink"):
            invoker_kwargs["metrics_sink"] = capture_metrics
        started = self._monotonic()
        try:
            text = self._invoker(*invoker_args, **invoker_kwargs)
        except Exception as error:  # noqa: BLE001 - per-sample outcome (6.5)
            outcome["latencyMs"] = _elapsed_ms(started, self._monotonic())
            return _invocation_error(outcome, sample.label, str(error))
        outcome["latencyMs"] = _elapsed_ms(started, self._monotonic())
        outcome["rawAnswer"] = text
        outcome["outputTokens"] = _output_tokens(metrics)
        try:
            verdict = parse_verdict(text)
        except ValueError as error:
            outcome["category"] = categorize_outcome(sample.label, None, None)
            outcome["isAnomalous"] = None
            outcome["confidence"] = None
            outcome["error"] = None
            # The parser's rejection reason, shown per parse failure
            # (Requirement 7.4) beside the verbatim raw answer.
            outcome["parseError"] = str(error)
            return outcome
        outcome["category"] = categorize_outcome(sample.label, verdict, None)
        outcome["isAnomalous"] = bool(verdict.get("is_anomalous"))
        outcome["confidence"] = verdict.get("confidence")
        outcome["error"] = None
        outcome["parseError"] = None
        return outcome

    # -- parameters ------------------------------------------------------
    def resolve_parameters(self, manifest: JobManifest) -> Dict[str, Any]:
        """The parameters the replay builds its invocation from.

        Layered, most specific last:

        1. the manifest's ``nodeParameters`` (the Node_Parameters of the
           latest Workflow_Definition version) — the fallback for a
           workflow this device does not have registered;
        2. the node's parameters as REGISTERED ON THIS DEVICE (design:
           "resolves the current registration's Node_Parameters"), so the
           replay targets the model the device actually serves;
        3. the Candidate's ``promptSet`` (``prompt_template``/``prompt``,
           ``system_prompt``, ``max_tokens``) — what is being scored;
        4. ``anomaly_mode`` forced true: a Device_Score_Job only ever
           scores a Tunable_Node, and the Verdict_Instruction must be
           appended even when the manifest omits the flag.

        Raises ``ValueError`` naming the reason when no ``modelName`` can
        be resolved — the job then fails as a whole instead of producing
        one ``invocation_error`` per sample.
        """
        parameters: Dict[str, Any] = dict(manifest.node_parameters)
        registered = self.registered_parameters(
            manifest.workflow_id, manifest.node_id)
        if registered:
            parameters.update(registered)
        prompt_set = dict(manifest.prompt_set)
        template = prompt_set.get("prompt_template")
        if template is None:
            template = prompt_set.get("prompt")
        if template is not None:
            parameters["prompt_template"] = template
        for key in ("system_prompt", "max_tokens"):
            if key in prompt_set:
                parameters[key] = prompt_set[key]
        parameters["anomaly_mode"] = True
        if not _text(parameters.get("modelName")):
            raise ValueError(
                "no modelName for node '{0}' of workflow {1}: the node is "
                "not registered on this device and the manifest carries "
                "none".format(manifest.node_id, manifest.workflow_id))
        return parameters

    def registered_parameters(
        self, workflow_id: str, node_id: str
    ) -> Dict[str, Any]:
        """The node's parameters from this device's newest registration of
        the workflow, or ``{}``.

        Read-only and contained: without a registration (or without the
        on-device DAO layer) the manifest's Node_Parameters are used
        alone.
        """
        if not workflow_id or not node_id:
            return {}
        try:
            factory = self._session_factory or _default_session_factory()
            from workflow_engine.models import WorkflowRegistration

            with factory() as session:
                registrations = (
                    session.query(WorkflowRegistration)
                    .filter(WorkflowRegistration.workflow_id == workflow_id)
                    .order_by(
                        WorkflowRegistration.registered_at.desc(),
                        WorkflowRegistration.id.desc(),
                    )
                    .all()
                )
                for registration in registrations:
                    for node in tunable_nodes(_graph_document(registration)):
                        if node.node_id == node_id and (
                            node.node_type == NODE_TYPE_LLM_INFERENCE
                        ):
                            return dict(node.parameters)
        except Exception:  # noqa: BLE001 - the manifest's parameters stand
            logger.debug(
                "Could not resolve the registered parameters of node '%s' "
                "(workflow %s); using the manifest's Node_Parameters",
                node_id, workflow_id, exc_info=True)
        return {}

    # -- S3 --------------------------------------------------------------
    def _s3(self) -> Any:
        if self._client is None:
            factory = self._s3_factory or _default_s3_factory
            self._client = factory()
        return self._client

    def _read_bytes(self, key: str) -> bytes:
        """GET one object's body from the Use_Case bucket."""
        response = self._s3().get_object(
            Bucket=self._config.bucket, Key=key)
        body = response["Body"]
        return body.read()

    def _read_json(self, key: str) -> Any:
        return json.loads(self._read_bytes(key).decode("utf-8"))

    def _write_batch(
        self,
        manifest: JobManifest,
        index: int,
        outcomes: List[Dict[str, Any]],
    ) -> None:
        """Persist one outcome batch under the run's prefix, at most
        :data:`MAX_BATCH_ATTEMPTS` times with doubling backoff."""
        prefix = manifest.outcomes_prefix_override or outcomes_prefix(
            self._config.prefix, manifest.session_id, manifest.run_id)
        key = prefix + OUTCOME_OBJECT_TEMPLATE.format(int(index))
        document = {
            "schemaVersion": OUTCOME_SCHEMA_VERSION,
            "jobId": manifest.job_id,
            "sessionId": manifest.session_id,
            "runId": manifest.run_id,
            "batch": int(index),
            "thingName": self._thing_name,
            "writtenAt": int(self._clock()),
            "outcomes": list(outcomes),
        }
        body = json.dumps(document, sort_keys=True, default=str).encode(
            "utf-8")
        last_error: Optional[Exception] = None
        for attempt in range(1, self._max_batch_attempts + 1):
            try:
                self._s3().put_object(
                    Bucket=self._config.bucket,
                    Key=key,
                    Body=body,
                    ContentType=JSON_CONTENT_TYPE,
                )
                logger.debug(
                    "Workflow-tuning job %s wrote outcome batch %s (%d "
                    "outcome(s))", manifest.job_id, key, len(outcomes))
                return
            except Exception as error:  # noqa: BLE001 - retried, then raised
                last_error = error
                if attempt >= self._max_batch_attempts:
                    break
                delay = self._backoff_base_seconds * (2 ** (attempt - 1))
                logger.debug(
                    "Workflow-tuning outcome batch %s attempt %d/%d failed "
                    "(%s); retrying in %.1fs", key, attempt,
                    self._max_batch_attempts, error, delay)
                try:
                    self._sleep(delay)
                except Exception:  # noqa: BLE001 - contained
                    logger.debug(
                        "Workflow-tuning batch backoff sleep failed",
                        exc_info=True)
        raise RuntimeError(
            "could not write {0}: {1}".format(key, last_error))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jobs_of(section: Any) -> Dict[str, Dict[str, Any]]:
    """The ``jobs`` map of a shadow section, tolerating every malformed
    shape (an unusable entry is simply not a job)."""
    if not isinstance(section, Mapping):
        return {}
    jobs = section.get(JOBS_KEY)
    if not isinstance(jobs, Mapping):
        return {}
    parsed: Dict[str, Dict[str, Any]] = {}
    for job_id, entry in jobs.items():
        key = _text(job_id)
        if not key:
            continue
        parsed[key] = dict(entry) if isinstance(entry, Mapping) else {}
    return parsed


def _truthy(value: Any) -> bool:
    """A shadow flag's truth: ``True`` or the string ``"true"`` only
    (shadow documents carry both forms)."""
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _invocation_error(
    outcome: Dict[str, Any], label: str, reason: str
) -> Dict[str, Any]:
    """Complete an outcome as ``invocation_error`` (Requirement 6.5)."""
    outcome["category"] = categorize_outcome(label, None, reason)
    outcome["isAnomalous"] = None
    outcome["confidence"] = None
    outcome.setdefault("rawAnswer", None)
    outcome.setdefault("outputTokens", None)
    outcome["error"] = reason
    outcome["parseError"] = None
    return outcome


def _elapsed_ms(started: float, finished: float) -> int:
    return int(max(0.0, (finished - started)) * 1000.0)


def _output_tokens(metrics: List[Dict[str, Any]]) -> Optional[Any]:
    """The reported output token count, when the API reported one."""
    if not metrics:
        return None
    value = metrics[-1].get("output_tokens")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _default_session_factory():
    """The device's SQLAlchemy session factory (imported lazily: the DAO
    layer needs the on-device environment)."""
    from dao.sqlite_db.sqlite_db_operations import SessionLocal

    return SessionLocal


def _graph_document(registration: Any) -> Optional[dict]:
    """The registration's ``workflow.json`` graph document, or ``None``
    (contained, mirroring the backfill's own reader)."""
    from workflow_engine import run_artifacts
    from workflow_engine.discovery import WORKFLOW_FILE

    try:
        artifact_path = getattr(registration, "artifact_path", None)
        if not artifact_path:
            return None
        return run_artifacts.read_workflow_graph(
            os.path.join(artifact_path, WORKFLOW_FILE))
    except Exception:  # noqa: BLE001 - best-effort
        logger.debug(
            "Could not read the workflow graph for registration %s",
            getattr(registration, "id", None), exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Delta subscription (the CameraBindingStore pattern)
# ---------------------------------------------------------------------------

def tuning_delta_topic_prefix(
    thing_name: str, shadow_name: str = TUNING_SHADOW_NAME
) -> str:
    """The shadow update topic prefix for the MQTT ``SubscriptionHandler``
    (its ``#`` wildcard covers the ``delta`` subtopic)."""
    return "$aws/things/{}/shadow/name/{}/update/".format(
        thing_name, shadow_name)


def make_tuning_shadow_handler(runner: "JobRunner"):
    """A ``SubscribeToIoTCoreStreamHandler`` for the tuning shadow,
    following ``camera_binding_store.make_bindings_shadow_handler``: a
    ``delta`` notification re-reads ``desired.jobs`` and accepts new
    Device_Score_Jobs.

    The awsiot import is deferred so this module stays importable without
    the Greengrass IPC runtime (tests use fakes).
    """
    import awsiot.greengrasscoreipc.client as client

    from dao.iotshadow.ShadowUtils import decode_shadow_payload, remove_prefix

    prefix = tuning_delta_topic_prefix(runner.thing_name, runner.shadow_name)

    class _WorkflowTuningShadowHandler(client.SubscribeToIoTCoreStreamHandler):
        def on_stream_event(self, event) -> None:
            try:
                subtopic = remove_prefix(event.message.topic_name, prefix)
                if subtopic == "delta":
                    message = decode_shadow_payload(event.message.payload)
                    runner.on_delta(message)
                # accepted/documents notifications need no edge-side action
            except Exception:  # noqa: BLE001 - handler isolation (11.2)
                logger.exception(
                    "Error handling a workflow-tuning shadow message")

        def on_stream_error(self, error: Exception) -> bool:
            logger.error("Workflow-tuning shadow stream error: %s", error)
            return True  # close the stream; the wiring layer resubscribes

        def on_stream_closed(self) -> None:
            logger.info("Workflow-tuning shadow stream closed")

    return _WorkflowTuningShadowHandler()


# ---------------------------------------------------------------------------
# Process-wide runner (installed at startup when tuning is configured)
# ---------------------------------------------------------------------------

_runner_lock = threading.Lock()
_runner: Optional[JobRunner] = None


def job_runner() -> Optional[JobRunner]:
    """The process-wide runner, or ``None`` when tuning is unconfigured."""
    return _runner


def set_job_runner(runner: Optional[JobRunner]) -> None:
    """Install (or clear) the process-wide runner."""
    global _runner  # noqa: WPS420 - process-wide singleton, see module doc
    with _runner_lock:
        _runner = runner


def start_job_runner(
    exporter: Any = None,
    shadow_accessor: Any = None,
    s3_factory: Optional[Callable[[], Any]] = None,
    thing_name: Optional[str] = None,
    sync: bool = True,
    **kwargs: Any
) -> Optional[JobRunner]:
    """Install the Device_Score_Job runner for a configured device.

    Returns ``None`` — installing nothing, reading no shadow and creating
    no client — when tuning sample export is not configured, so a device
    that has not enabled tuning behaves exactly as before the feature
    (Requirements 2.6, 11.3). ``sync`` performs the initial reconciliation
    so jobs delivered while the device was down are picked up without
    waiting for a delta.
    """
    exporter = exporter if exporter is not None else sample_exporter()
    config = getattr(exporter, "config", None)
    if config is None:
        logger.debug(
            "Workflow-tuning job runner not started: tuning is not "
            "configured")
        set_job_runner(None)
        return None
    runner = JobRunner(
        config,
        shadow_accessor=shadow_accessor,
        s3_factory=s3_factory,
        thing_name=(
            thing_name if thing_name is not None
            else getattr(exporter, "thing_name", None)
        ),
        **kwargs
    )
    set_job_runner(runner)
    logger.info(
        "Workflow-tuning job runner ready for device '%s' (shadow '%s')",
        runner.thing_name, runner.shadow_name)
    if sync:
        runner.on_delta(None)
    return runner


def shutdown_job_runner(timeout: Optional[float] = 5.0) -> None:
    """Stop and clear the process-wide runner (test/shutdown helper)."""
    runner = _runner
    set_job_runner(None)
    if runner is not None:
        runner.stop(timeout)


__all__: List[str] = [
    "BATCH_BACKOFF_BASE_SECONDS",
    "JOBS_KEY",
    "JOB_CONCURRENCY",
    "JobManifest",
    "JobRunner",
    "MANIFEST_OBJECT",
    "MAX_BATCH_ATTEMPTS",
    "MAX_REPEATS",
    "MIN_REPEATS",
    "ManifestSample",
    "OUTCOME_BATCH_SIZE",
    "OUTCOME_OBJECT_TEMPLATE",
    "OUTCOME_SCHEMA_VERSION",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "TUNING_SHADOW_NAME",
    "job_runner",
    "make_tuning_shadow_handler",
    "manifest_key",
    "outcomes_key",
    "outcomes_prefix",
    "set_job_runner",
    "shutdown_job_runner",
    "start_job_runner",
    "tuning_delta_topic_prefix",
    "tuning_root",
]
