"""
Shared compilation-status vocabulary and aggregation logic.

Single source of truth for every consumer of a training record's
`compilation_jobs` entries: poller A (`compilation.py::get_compilation_status`),
poller B (`models.py::get_model`'s inline sync), and anything else that needs
to classify an entry or derive the record's overall compilation status.

This module lives in the shared Lambda layer (mounted at /opt/python by both
`CompilationHandler` and `ModelsHandler`) alongside the other cross-handler
modules (`rbac_utils.py`, `s3_browse_utils.py`, `manifest_transformer.py`,
`user_roles_dao.py`).

Pure functions only — no boto3, no I/O. The only side effect is a log warning
when `derive_compilation_status` meets a status value outside the modeled
vocabulary (never a silent catch-all).

Spec: .kiro/specs/onnx-compile-error-diagnostics/ (design Fix Implementation
step 1).
"""
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Poll_Kind: the describe API an entry needs (see classify_poll_kind).
# ---------------------------------------------------------------------------
POLL_KIND_NONE = 'none'                # no live SageMaker job — never describe
POLL_KIND_TRAINING = 'training'        # ONNX export runs as a *training* job
POLL_KIND_COMPILATION = 'compilation'  # SageMaker Neo compilation job

# ---------------------------------------------------------------------------
# Per-job status vocabulary. SageMaker emits uppercase statuses; the portal
# synthesizes mixed-case values ('InProgress', 'Failed') at start time, so
# every membership test goes through normalize_status().
# ---------------------------------------------------------------------------
RUNNING_STATUSES = {'STARTING', 'INPROGRESS', 'IN_PROGRESS'}
COMPLETED_STATUSES = {'COMPLETED'}
FAILED_STATUSES = {'FAILED', 'STOPPING', 'STOPPED'}
# 'ERROR' is a *transient poll fault*, not a compile outcome: the job's true
# status is unknown and it must be re-polled, never latched terminal.
TRANSIENT_STATUSES = {'ERROR'}
TERMINAL_STATUSES = COMPLETED_STATUSES | FAILED_STATUSES

# The status a poller writes on a transient ClientError. Kept as the literal
# 'ERROR' because legacy records already carry it.
STATUS_POLL_ERROR = 'ERROR'

# Bound on the "status unknown, keep polling" window: after this many
# consecutive failed polls the entry is latched genuinely terminal (FAILED),
# with the poll reason promoted into failure_reason only where no
# Originating_Reason exists.
POLL_ERROR_MAX_ATTEMPTS = 5


def normalize_status(value):
    """Uppercase a status value; None/empty become ''."""
    return str(value or '').upper()


def is_terminal_status(value):
    """True when the (normalized) status is a genuine compile outcome that
    must never be overwritten by a poll: COMPLETED, FAILED, STOPPING,
    STOPPED."""
    return normalize_status(value) in TERMINAL_STATUSES


def is_transient_status(value):
    """True when the (normalized) status is a transient poll fault ('ERROR'):
    the job's true status is unknown and it should be re-polled."""
    return normalize_status(value) in TRANSIENT_STATUSES


def classify_poll_kind(job):
    """Classify a compilation_jobs entry into the describe API it needs.

    TOTAL over every entry shape the system writes — no exception path:
      - job_started is False        -> POLL_KIND_NONE (no live SageMaker job)
      - falsy compilation_job_name  -> POLL_KIND_NONE (nothing to describe)
      - export_format == 'onnx'     -> POLL_KIND_TRAINING (torch.onnx.export
                                       runs as a SageMaker *training* job)
      - otherwise                   -> POLL_KIND_COMPILATION (SageMaker Neo)
    """
    if job.get('job_started') is False:
        return POLL_KIND_NONE
    if not job.get('compilation_job_name'):
        return POLL_KIND_NONE
    if job.get('export_format') == 'onnx':
        return POLL_KIND_TRAINING
    return POLL_KIND_COMPILATION


def entry_reason(job):
    """The entry's recorded diagnostic reason: `failure_reason` (captured from
    a describe response) or `error` (captured at start time), in that order.
    Returns None when neither is present. Poll faults (`poll_error`) are
    deliberately excluded — they are Poll_Diagnostics, not the
    Originating_Reason."""
    return job.get('failure_reason') or job.get('error') or None


def derive_compilation_status(compilation_jobs):
    """Aggregate per-target compilation job statuses into a single overall
    status for the model/training record.

    Codomain: None (no jobs) | 'InProgress' | 'Completed' | 'Failed'.

    Inputs are NOT limited to SageMaker Neo compilation statuses: entries can
    carry Neo `CompilationJobStatus` values, ONNX export `TrainingJobStatus`
    values (the export runs as a SageMaker *training* job), the portal's
    mixed-case start-time values ('InProgress', 'Failed'), and the
    poller-written transient 'ERROR'.

    Per-job vocabulary the pollers can emit (compared case-insensitively):
      - running:   STARTING, INPROGRESS, IN_PROGRESS
      - completed: COMPLETED
      - failed:    FAILED, STOPPING, STOPPED
      - transient: ERROR — a poll fault, not a compile outcome; the job's
        true status is unknown and it must be re-polled

    Rules, in precedence order:
      1. no jobs -> None
      2. any job still running -> 'InProgress'
      3. all jobs COMPLETED -> 'Completed'
      4. any genuine failure (FAILED/STOPPING/STOPPED) -> 'Failed'
      5. only transient poll faults (plus completed jobs) remain ->
         'InProgress': a transient fault must never latch the record to a
         terminal state, because the underlying job may still be running
      6. an unmodeled status value -> log a warning naming the values and
         return the non-latching 'InProgress'. NEVER a silent catch-all:
         collapsing unknown values to 'Failed' is exactly Defect 3.
    """
    if not compilation_jobs:
        return None

    statuses = {normalize_status(j.get('status')) for j in compilation_jobs}

    if statuses & RUNNING_STATUSES:
        return 'InProgress'
    if statuses <= COMPLETED_STATUSES:
        return 'Completed'
    if statuses & FAILED_STATUSES:
        return 'Failed'
    if statuses <= (COMPLETED_STATUSES | TRANSIENT_STATUSES):
        # Transient poll fault(s) with no genuine failure: status unknown,
        # keep polling — do not latch.
        return 'InProgress'

    unmodeled = sorted(
        statuses - RUNNING_STATUSES - COMPLETED_STATUSES
        - FAILED_STATUSES - TRANSIENT_STATUSES)
    logger.warning(
        "derive_compilation_status: unmodeled status value(s) %s — "
        "returning non-latching 'InProgress'; extend the vocabulary in "
        "compilation_status.py if a poller now emits these", unmodeled)
    return 'InProgress'
