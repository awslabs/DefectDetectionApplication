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

"""One-shot Sample_Export backfill from existing Run_Artifacts
(Requirement 2.7).

When a Use_Case enables tuning sample export, the samples a device would
have exported for the runs it has ALREADY performed are still on disk:
every ``bedrock_inference`` / ``llm_inference`` node's frames were
persisted as run artifacts and every verdict was persisted in the run's
metadata JSON. This module reconstructs those Tuning_Samples once, so a
Tuning_Session has production images to work with immediately instead of
waiting for new runs.

What it does, per the design ("``backfill.py``" §2):

- runs on the **enabled transition** — only when an exporter is
  configured, and only once: the marker file
  :data:`BACKFILL_MARKER_PATH` is written when the walk ends and its
  presence makes every later start a no-op (Property 4's "a second start
  SHALL export nothing");
- for every registration on the device, reads its ``workflow.json``
  graph document and selects the Tunable_Nodes with the SHARED rule
  (``workflow_core.anomaly_invocation.is_tunable_node``), so backfill
  covers exactly the nodes live export covers (Property 1);
- takes the newest :data:`MAX_EXECUTIONS_PER_NODE` executions of that
  registration (``started_at DESC, id DESC`` — the ordering
  ``api.list_registration_executions`` uses) and, per tunable node,
  pairs the persisted node frames by the EXECUTOR's own artifact rules:
  the ``original`` frame (the exact Detection_Crop bytes sent) when the
  node's recorded outcome carries a ``detection_id``, else the ``in``
  frame, plus the ``reference`` frame when one is present;
- skips an execution whose node outcome is an error or whose input frame
  is missing/unreadable (Requirement 2.7), and enqueues everything else
  through the normal :class:`SampleExporter` with
  ``source: backfill``.

Containment (Requirements 11.1-11.3): the whole walk is read-only —
registrations, executions, run metadata and node frames are only read,
never written — every step is wrapped so a defect can only reduce what is
backfilled, and :func:`start_backfill` runs it on a daemon thread so
LocalServer startup is never delayed. Without a configured exporter
nothing here reads the database, touches the filesystem or writes the
marker, so the backfill still happens on the day export is enabled.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
)

from workflow_engine import run_artifacts
from workflow_engine.discovery import WORKFLOW_FILE
from workflow_engine.output_bindings import sanitize_node_id_for_artifact
from workflow_engine.tuning.sample_export import (
    SOURCE_BACKFILL,
    ExportedSample,
    SampleExporter,
    metadata_snippet,
    sample_exporter,
)
from workflow_engine.vendor.workflow_core.anomaly_invocation import (
    NODE_TYPE_BEDROCK_INFERENCE,
    NODE_TYPE_LLM_INFERENCE,
    is_tunable_node,
    prompt_fingerprint,
)

logger = logging.getLogger(__name__)

#: Marker whose presence means the one-shot backfill already ran on this
#: device; it lives beside the Greengrass work directories under
#: ``/aws_dda`` so it survives component restarts and redeployments.
BACKFILL_MARKER_PATH = "/aws_dda/workflow-tuning/backfilled.json"

#: Marker document schema version.
MARKER_SCHEMA_VERSION = 1

#: Newest executions considered per Tunable_Node (Requirement 2.7). The
#: per-registration execution query is bounded by this, so a node can
#: never yield more than this many samples either.
MAX_EXECUTIONS_PER_NODE = 500

#: Run_Metadata section carrying a node type's per-node outcomes, and the
#: key inside that outcome holding the model's raw answer — the two
#: shapes ``output_bindings`` merges (``bedrock.{nodeId}.text`` /
#: ``llm.{nodeId}.generated_text``).
_METADATA_SECTION = {
    NODE_TYPE_BEDROCK_INFERENCE: "bedrock",
    NODE_TYPE_LLM_INFERENCE: "llm",
}
_ANSWER_KEY = {
    NODE_TYPE_BEDROCK_INFERENCE: "text",
    NODE_TYPE_LLM_INFERENCE: "generated_text",
}

#: Node-frame ports the pairing rule chooses between: the Detection_Crop
#: bytes actually sent (``original``, written by
#: ``BedrockInferenceProcessor._persist_original_frame``) when the
#: outcome names a detection, else the captured primary frame (``in``),
#: plus the optional reference frame.
PORT_ORIGINAL = "original"
PORT_IN = "in"
PORT_REFERENCE = "reference"

#: Skip reasons counted in the summary and the marker document.
SKIP_NO_ARTIFACTS = "no_artifacts"
SKIP_NO_OUTCOME = "no_outcome"
SKIP_ERROR_OUTCOME = "error_outcome"
SKIP_MISSING_INPUT = "missing_input"
SKIP_UNREADABLE_INPUT = "unreadable_input"
SKIP_UNREADABLE_REFERENCE = "unreadable_reference"
SKIP_REASONS = (
    SKIP_NO_ARTIFACTS,
    SKIP_NO_OUTCOME,
    SKIP_ERROR_OUTCOME,
    SKIP_MISSING_INPUT,
    SKIP_UNREADABLE_INPUT,
    SKIP_UNREADABLE_REFERENCE,
)

#: Queue-pacing bounds. The exporter's queue is deliberately small (200
#: entries, oldest dropped) because it protects live runs; a backfill of
#: hundreds of executions would otherwise drop most of itself before the
#: uploader drains it. So the walk waits for the queue to fall below
#: half its depth before enqueueing more — bounded, so a stalled uploader
#: (no network) can never stall the backfill thread: after the wait times
#: out the sample is enqueued anyway and the exporter's normal
#: drop-oldest bound applies.
PACE_WAIT_SECONDS = 30.0
PACE_POLL_SECONDS = 0.2


@dataclass
class TunableNode:
    """A Tunable_Node of one registration, as the graph document
    declares it."""

    node_id: str
    node_type: str
    parameters: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BackfillSummary:
    """What one :func:`run_backfill` call did — logged, and recorded in
    the marker document for diagnosis."""

    #: True when the marker already existed, i.e. nothing was exported
    #: because the one-shot backfill already ran (Property 4).
    already_done: bool = False
    #: True when no exporter is configured: the walk did not run AND no
    #: marker was written, so it still happens once export is enabled.
    not_configured: bool = False
    registrations: int = 0
    nodes: int = 0
    executions_scanned: int = 0
    exported: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)
    #: A contained failure of the walk itself, if any.
    error: Optional[str] = None

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    @property
    def ran(self) -> bool:
        return not (self.already_done or self.not_configured)

    def as_document(self) -> Dict[str, Any]:
        return {
            "registrations": self.registrations,
            "nodes": self.nodes,
            "executionsScanned": self.executions_scanned,
            "exported": self.exported,
            "skipped": dict(self.skipped),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Marker (the one-shot guard)
# ---------------------------------------------------------------------------

def marker_exists(marker_path: str = BACKFILL_MARKER_PATH) -> bool:
    """True when the one-shot backfill already ran on this device.

    Any error inspecting the path is read as "already ran": a backfill
    that cannot prove it is due must not run again and flood the
    Sample_Store with duplicates.
    """
    try:
        return os.path.exists(marker_path)
    except Exception:  # noqa: BLE001 - fail safe, see docstring
        logger.debug(
            "Could not check the tuning backfill marker at %s; treating the "
            "backfill as already done", marker_path, exc_info=True)
        return True


def write_marker(
    summary: BackfillSummary,
    marker_path: str = BACKFILL_MARKER_PATH,
    config: Any = None,
    clock: Callable[[], float] = time.time,
) -> bool:
    """Record that the one-shot backfill ran; ``True`` when written.

    Written when the walk ENDS — including when it ended in a contained
    error (the error is recorded in the document). "Backfill once"
    (Requirement 2.7) is taken literally: a partially completed walk is
    not retried on every restart, because the Portal's indexing already
    tolerates a partial set (and marks duplicates), while a repeating
    backfill would keep re-uploading hundreds of objects.

    Best-effort: a write failure is logged and returns ``False`` (the
    backfill then runs again on the next start).
    """
    document = {
        "schemaVersion": MARKER_SCHEMA_VERSION,
        "backfilledAt": int(clock()),
        "bucket": getattr(config, "bucket", None),
        "prefix": getattr(config, "prefix", None),
    }
    document.update(summary.as_document())
    try:
        directory = os.path.dirname(marker_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(marker_path, "w", encoding="utf-8") as marker_file:
            json.dump(document, marker_file, indent=2, sort_keys=True)
        return True
    except OSError:
        logger.warning(
            "Could not write the tuning backfill marker at %s; the backfill "
            "may run again on the next start", marker_path, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Graph document -> Tunable_Nodes
# ---------------------------------------------------------------------------

def tunable_nodes(graph_document: Any) -> List[TunableNode]:
    """The graph document's Tunable_Nodes, in document order.

    Reads the ``run_artifacts.read_workflow_graph`` document shape
    (``{"nodes": [{"id", "type", "parameters", ...}]}``) and applies the
    SHARED classification rule, so the set backfilled is exactly the set
    live export covers and the Portal offers (Property 1). Malformed
    documents/nodes yield no nodes rather than raising.
    """
    if not isinstance(graph_document, Mapping):
        return []
    nodes = graph_document.get("nodes")
    if not isinstance(nodes, list):
        return []
    selected: List[TunableNode] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        node_type = node.get("type")
        parameters = node.get("parameters")
        parameters = (
            dict(parameters) if isinstance(parameters, Mapping) else {}
        )
        if not is_tunable_node(node_type, parameters.get("anomaly_mode")):
            continue
        node_id = node.get("id")
        if node_id is None or str(node_id) == "":
            continue
        selected.append(TunableNode(
            node_id=str(node_id),
            node_type=str(node_type),
            parameters=parameters,
        ))
    return selected


# ---------------------------------------------------------------------------
# Run_Artifacts -> ExportedSample
# ---------------------------------------------------------------------------

def node_outcome(metadata: Any, node: TunableNode) -> Optional[Mapping]:
    """The node's recorded outcome from the run metadata, or ``None``.

    ``bedrock.{nodeId}`` / ``llm.{nodeId}`` — the nested per-node entries
    ``output_bindings`` merges. ``None`` when the node did not run in
    that execution (or the metadata is malformed), which the caller
    counts as ``no_outcome``.
    """
    if not isinstance(metadata, Mapping):
        return None
    section = metadata.get(_METADATA_SECTION.get(node.node_type, ""))
    if not isinstance(section, Mapping):
        return None
    outcome = section.get(node.node_id)
    return outcome if isinstance(outcome, Mapping) else None


def _read_frame(path: Optional[str]) -> Tuple[Optional[bytes], bool]:
    """``(bytes, readable)`` for a node-frame path.

    ``(None, True)`` for an absent path (nothing to read), ``(None,
    False)`` when the file exists but could not be read.
    """
    if not path:
        return None, True
    try:
        with open(path, "rb") as frame_file:
            return frame_file.read(), True
    except OSError:
        logger.debug(
            "Tuning backfill could not read the node frame at %s", path,
            exc_info=True)
        return None, False


def build_sample(
    node: TunableNode,
    workflow_id: str,
    version: Any,
    execution_id: str,
    output_dir: Optional[str],
    capture_id: Optional[str],
    metadata: Mapping,
    outcome: Mapping,
    started_at: Optional[int] = None,
) -> Tuple[Optional[ExportedSample], Optional[str]]:
    """Reconstruct one Tuning_Sample from a run's artifacts:
    ``(sample, skip_reason)`` — exactly one of the two is set.

    The pairing rule is the EXECUTOR's (Requirement 2.7, Property 4):
    the ``original`` frame — the exact Detection_Crop bytes the
    invocation sent — when the outcome carries a ``detection_id``, else
    the captured ``in`` frame; the ``reference`` frame iff one was
    persisted. The recorded answer/verdict come from the run metadata,
    and the fingerprint from the node's registered Prompt_Set, so the
    sample is indistinguishable from a live one apart from its
    ``source``.
    """
    if outcome.get("error"):
        return None, SKIP_ERROR_OUTCOME
    safe_node_id = sanitize_node_id_for_artifact(node.node_id)
    detection_id = outcome.get("detection_id")
    detection_id = (
        str(detection_id)
        if detection_id is not None and str(detection_id) != ""
        else None
    )
    input_port = PORT_ORIGINAL if detection_id is not None else PORT_IN
    input_path = run_artifacts.node_image_path(
        output_dir, capture_id, safe_node_id, input_port)
    if not input_path:
        return None, SKIP_MISSING_INPUT
    input_bytes, readable = _read_frame(input_path)
    if not readable:
        return None, SKIP_UNREADABLE_INPUT
    if not input_bytes:
        # An empty frame file carries no image: nothing to replay.
        return None, SKIP_MISSING_INPUT
    reference_path = run_artifacts.node_image_path(
        output_dir, capture_id, safe_node_id, PORT_REFERENCE)
    reference_bytes, reference_readable = _read_frame(reference_path)
    if not reference_bytes:
        reference_bytes = None
    answer = outcome.get(_ANSWER_KEY.get(node.node_type, ""))
    verdict: Optional[Dict[str, Any]] = None
    if "is_anomalous" in outcome:
        verdict = {
            "is_anomalous": outcome.get("is_anomalous"),
            "confidence": outcome.get("confidence"),
        }
    snippet = None
    if node.node_type == NODE_TYPE_LLM_INFERENCE:
        snippet = metadata_snippet(
            node.parameters.get("prompt_template"), metadata)
    sample = ExportedSample(
        workflow_id=workflow_id,
        node_id=node.node_id,
        node_type=node.node_type,
        execution_id=execution_id,
        input_bytes=input_bytes,
        version=version,
        reference_bytes=reference_bytes,
        answer=str(answer) if answer is not None else None,
        verdict=verdict,
        prompt_fingerprint=prompt_fingerprint(node.parameters),
        detection_id=detection_id,
        detection_slot=(
            _coerce_slot(node.parameters.get("crop_detection_index"))
            if detection_id is not None else None
        ),
        metadata_snippet=snippet,
        source=SOURCE_BACKFILL,
        # The run's start time, not the upload time: the Portal indexes
        # the newest samples by ``exportedAt``, and a backfilled sample
        # describes an old run — dating it "now" would let a backfill
        # crowd genuinely newer live samples out of the index bound.
        exported_at=int(started_at) if started_at is not None else None,
    )
    if reference_path and not reference_readable:
        # Exported single-image (the executor's own degradation for an
        # unreadable reference frame), counted so the summary shows it.
        return sample, SKIP_UNREADABLE_REFERENCE
    return sample, None


def _coerce_slot(raw: Any) -> Optional[int]:
    """The Detection_Crop's slot (its ``crop_detection_index``), or
    ``None`` — the same coercion the live export path applies."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------

def _default_session_factory():
    """The device's SQLAlchemy session factory.

    Imported lazily: the DAO layer needs the on-device
    ``COMPONENT_WORK_PATH`` environment, so this module stays importable
    everywhere (mirrors ``WorkflowWatcher.__init__``).
    """
    from dao.sqlite_db.sqlite_db_operations import SessionLocal

    return SessionLocal


def _pace(exporter: Any, wait_seconds: float, sleep: Callable[[float], None],
          clock: Callable[[], float]) -> None:
    """Wait (bounded) for the export queue to drain below half its depth.

    Keeps a large backfill from dropping most of itself against the
    exporter's 200-entry bound while never blocking indefinitely: after
    ``wait_seconds`` the caller enqueues anyway. Inert for any exporter
    that does not report a queue depth (and for a stalled one, after the
    bound).
    """
    depth = getattr(exporter, "queue_depth", None)
    if not isinstance(depth, int):
        return
    limit = max(1, int(getattr(exporter, "queue_size", 0) or 0) // 2)
    if depth < limit:
        return
    deadline = clock() + max(0.0, float(wait_seconds))
    while clock() < deadline:
        sleep(PACE_POLL_SECONDS)
        depth = getattr(exporter, "queue_depth", None)
        if not isinstance(depth, int) or depth < limit:
            return


def run_backfill(
    exporter: Optional[SampleExporter] = None,
    session_factory: Optional[Callable] = None,
    marker_path: str = BACKFILL_MARKER_PATH,
    max_executions: int = MAX_EXECUTIONS_PER_NODE,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    pace_wait_seconds: float = PACE_WAIT_SECONDS,
) -> BackfillSummary:
    """Export, once, the Tuning_Samples derivable from existing
    Run_Artifacts (Requirement 2.7).

    No-op — and no marker written — when export is not configured, so the
    backfill happens on the day the Use_Case enables it. No-op when the
    marker already exists (a second start exports nothing). Never
    raises: any failure is contained, recorded in the summary and
    logged.
    """
    summary = BackfillSummary()
    try:
        exporter = exporter if exporter is not None else sample_exporter()
        if exporter is None or not getattr(exporter, "enabled", False):
            summary.not_configured = True
            logger.debug(
                "Tuning sample backfill skipped: export is not configured")
            return summary
        if marker_exists(marker_path):
            summary.already_done = True
            logger.debug(
                "Tuning sample backfill already ran (marker %s); nothing to "
                "export", marker_path)
            return summary
        logger.info(
            "Tuning sample backfill starting: reconstructing samples from "
            "existing run artifacts (newest %d executions per node)",
            max_executions)
        try:
            _walk(
                exporter, summary,
                session_factory=session_factory or _default_session_factory(),
                max_executions=max_executions,
                clock=clock,
                sleep=sleep,
                pace_wait_seconds=pace_wait_seconds,
            )
        except Exception as error:  # noqa: BLE001 - contained per 11.2
            summary.error = str(error)
            logger.exception(
                "Tuning sample backfill failed; workflow runs and live "
                "sample export are unaffected")
        logger.info(
            "Tuning sample backfill finished: %d sample(s) enqueued from %d "
            "execution(s) across %d registration(s) / %d tunable node(s); "
            "skipped %s", summary.exported, summary.executions_scanned,
            summary.registrations, summary.nodes, summary.skipped or "{}")
        write_marker(
            summary, marker_path,
            config=getattr(exporter, "config", None), clock=clock)
        return summary
    except Exception as error:  # noqa: BLE001 - never touches the runtime
        summary.error = str(error)
        logger.exception(
            "Tuning sample backfill could not run; workflow runs are "
            "unaffected")
        return summary


def _walk(
    exporter: Any,
    summary: BackfillSummary,
    session_factory: Callable,
    max_executions: int,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    pace_wait_seconds: float,
) -> None:
    """Read-only walk of every registration's newest executions,
    enqueueing one sample per (tunable node, usable execution)."""
    from workflow_engine.models import WorkflowExecution, WorkflowRegistration

    with session_factory() as session:
        registrations = (
            session.query(WorkflowRegistration)
            .order_by(
                WorkflowRegistration.registered_at.desc(),
                WorkflowRegistration.id.desc(),
            )
            .all()
        )
        for registration in registrations:
            nodes = tunable_nodes(_graph_document(registration))
            if not nodes:
                continue
            summary.registrations += 1
            summary.nodes += len(nodes)
            executions = (
                session.query(WorkflowExecution)
                .filter(
                    WorkflowExecution.registration_id == registration.id
                )
                .order_by(
                    WorkflowExecution.started_at.desc(),
                    WorkflowExecution.id.desc(),
                )
                .limit(max(0, int(max_executions)))
                .all()
            )
            for execution in executions:
                summary.executions_scanned += 1
                _backfill_execution(
                    exporter, summary, registration, execution, nodes,
                    clock=clock, sleep=sleep,
                    pace_wait_seconds=pace_wait_seconds,
                )


def _graph_document(registration: Any) -> Optional[dict]:
    """The registration's ``workflow.json`` graph document, or ``None``
    (contained, mirroring ``executor._load_graph_document``)."""
    try:
        artifact_path = getattr(registration, "artifact_path", None)
        if not artifact_path:
            return None
        return run_artifacts.read_workflow_graph(
            os.path.join(artifact_path, WORKFLOW_FILE))
    except Exception:  # noqa: BLE001 - best-effort
        logger.debug(
            "Tuning backfill could not read the workflow graph for %s",
            getattr(registration, "id", None), exc_info=True)
        return None


def _backfill_execution(
    exporter: Any,
    summary: BackfillSummary,
    registration: Any,
    execution: Any,
    nodes: List[TunableNode],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    pace_wait_seconds: float,
) -> None:
    """Enqueue this execution's samples for every tunable node.

    Contained per execution: an unreadable run keeps the rest of the
    backfill going.
    """
    try:
        output_dir = getattr(execution, "output_dir", None)
        capture_id = getattr(execution, "capture_id", None)
        if not output_dir or not capture_id:
            for _ in nodes:
                summary.skip(SKIP_NO_ARTIFACTS)
            return
        metadata = run_artifacts.read_run_metadata(output_dir, capture_id)
        for node in nodes:
            outcome = node_outcome(metadata, node)
            if outcome is None:
                summary.skip(SKIP_NO_OUTCOME)
                continue
            workflow_id = str(
                getattr(registration, "workflow_id", "") or "")
            sample, reason = build_sample(
                node,
                workflow_id=workflow_id,
                version=getattr(registration, "version", None),
                execution_id=str(getattr(execution, "id", "") or ""),
                output_dir=output_dir,
                capture_id=capture_id,
                metadata=metadata,
                outcome=outcome,
                started_at=getattr(execution, "started_at", None),
            )
            if reason is not None:
                summary.skip(reason)
            if sample is None:
                continue
            _pace(exporter, pace_wait_seconds, sleep, clock)
            exporter.enqueue(sample)
            summary.exported += 1
    except Exception:  # noqa: BLE001 - contained per 11.2
        logger.debug(
            "Tuning backfill skipped execution %s; the remaining executions "
            "are unaffected", getattr(execution, "id", None), exc_info=True)


def start_backfill(
    exporter: Optional[SampleExporter] = None,
    **kwargs: Any,
) -> Optional[threading.Thread]:
    """Run :func:`run_backfill` on a daemon thread; returns the thread.

    Startup must never wait on the backfill (it reads the run database
    and hundreds of frames), and the thread is a daemon so it can never
    hold LocalServer shut-down. ``None`` when the thread could not be
    started — logged, and the device simply runs without a backfill.
    """
    try:
        thread = threading.Thread(
            target=lambda: run_backfill(exporter, **kwargs),
            name="tuning-sample-backfill",
            daemon=True,
        )
        thread.start()
        return thread
    except Exception:  # noqa: BLE001 - never take LocalServer down
        logger.warning(
            "Could not start the tuning sample backfill thread; live sample "
            "export is unaffected", exc_info=True)
        return None


__all__: List[str] = [
    "BACKFILL_MARKER_PATH",
    "BackfillSummary",
    "MARKER_SCHEMA_VERSION",
    "MAX_EXECUTIONS_PER_NODE",
    "PACE_WAIT_SECONDS",
    "PORT_IN",
    "PORT_ORIGINAL",
    "PORT_REFERENCE",
    "SKIP_ERROR_OUTCOME",
    "SKIP_MISSING_INPUT",
    "SKIP_NO_ARTIFACTS",
    "SKIP_NO_OUTCOME",
    "SKIP_REASONS",
    "SKIP_UNREADABLE_INPUT",
    "SKIP_UNREADABLE_REFERENCE",
    "TunableNode",
    "build_sample",
    "marker_exists",
    "node_outcome",
    "run_backfill",
    "start_backfill",
    "tunable_nodes",
    "write_marker",
]
