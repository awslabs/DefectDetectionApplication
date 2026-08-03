"""
Dispatcher planning pure logic (Build_Manager)

Pure decision module for the build dispatcher: dispatch eligibility of
queued Build_Jobs (sequential chaining within a request), the server
allocation decision (at most one running Build_Job per server), queue
promotion (oldest queued job first) when a server's job reaches a terminal
status, the ephemeral provisioning plan (exactly one runner per dispatched
job, sized from the job's own config snapshot), the pre-dispatch
verification decision (pgrep-based build-process gate with 5-minute
re-verification deferral), and the watchdog decisions (runtime timeout,
termination retry cadence, pending fleet action deadline, serialization
check scheduling, serialization-violation stop-all/fail-all, and the
dead-server queue sweep).

This module deliberately has NO AWS clients and NO side effects: it is
imported by the dispatcher handler (build_dispatcher.py) and is fully unit-
and property-testable in isolation. The dispatcher executes the decisions
returned here with DynamoDB conditional updates; nothing in this module
mutates its inputs.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates
Requirements: 1.3, 2.2, 2.3, 3.1, 3.3, 3.8, 3.9, 6.11, 7.1, 7.2, 7.3, 7.4,
7.5, 7.6, 7.7, 7.8, 7.9, 9.3
"""
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

import build_domain

# ---------------------------------------------------------------------------
# Dispatch eligibility (Requirement 1.3)
# ---------------------------------------------------------------------------


def is_dispatch_eligible(
    job: Dict[str, Any],
    predecessor_status: Optional[str] = None,
) -> bool:
    """True iff a queued Build_Job may be dispatched (Req 1.3).

    A queued job is dispatchable iff its ``predecessor_job_id`` is null
    (first job of its request) or the predecessor has reached ANY terminal
    status (succeeded, failed, interrupted, or cancelled). Jobs that are not
    in the queued status are never dispatch candidates.

    ``predecessor_status`` is the current status of the job referenced by
    ``predecessor_job_id`` (ignored when the job has no predecessor). A job
    with a predecessor reference but an unknown/unavailable predecessor
    status is NOT eligible: eligibility must be positively established.
    """
    if job.get('status') != build_domain.STATUS_QUEUED:
        return False
    if job.get('predecessor_job_id') is None:
        return True
    if predecessor_status is None:
        return False
    return build_domain.is_terminal(predecessor_status)


def eligible_queued_jobs(jobs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter a collection of Build_Jobs down to the dispatch-eligible ones.

    Predecessor statuses are resolved by ``build_job_id`` lookup within the
    supplied collection; a predecessor missing from the collection leaves
    its successor ineligible (Req 1.3). The result preserves submission
    order (ascending ``created_at``, then job id for determinism).
    """
    jobs = list(jobs)
    status_by_id = {
        job.get('build_job_id'): job.get('status')
        for job in jobs
        if job.get('build_job_id') is not None
    }
    eligible = [
        job for job in jobs
        if is_dispatch_eligible(job, status_by_id.get(job.get('predecessor_job_id')))
    ]
    return sorted(
        eligible,
        key=lambda j: (j.get('created_at', 0), str(j.get('build_job_id'))),
    )


# ---------------------------------------------------------------------------
# Server allocation decision (Requirements 2.2, 7.1, 7.2)
# ---------------------------------------------------------------------------

# Allocation decision actions
ALLOCATION_START = 'start'    # server free: start the job on it (7.1)
ALLOCATION_QUEUE = 'queue'    # server occupied: job waits in that server's queue (7.2)


class AllocationDecision(NamedTuple):
    """Planner decision for one dedicated Build_Job.

    - ``action``: ALLOCATION_START or ALLOCATION_QUEUE
    - ``build_job_id``: the job the decision is about
    - ``server_id``: always exactly the server selected in the job's build
      request (Req 2.2)
    - ``status``: the Build_Job status the decision implies — queued while
      waiting for the occupied server (Req 7.2)
    """
    action: str
    build_job_id: str
    server_id: str
    status: str


def allocate_dedicated(
    job: Dict[str, Any],
    server_running_job_id: Optional[str],
) -> AllocationDecision:
    """Allocation decision for one dispatch-eligible dedicated Build_Job.

    ``server_running_job_id`` is the ``running_build_job_id`` currently held
    by the job's selected server (None/absent when the server is free).

    - The target server is always exactly the Dedicated_Build_Server
      selected in the job's build request; the planner never substitutes a
      different server (Req 2.2).
    - Server free -> start the job there, taking the single running slot
      (Req 7.1: at most one running Build_Job per server).
    - Server occupied -> the job goes to (stays in) that server's
      Build_Queue with the queued status instead of starting (Req 7.2).

    Raises ValueError when the job carries no server selection: a dedicated
    job without a server is a programming error upstream, not a plannable
    state.
    """
    server_id = job.get('server_id')
    if not server_id:
        raise ValueError(
            f"Dedicated Build_Job {job.get('build_job_id')} has no selected "
            f"Dedicated_Build_Server"
        )
    if server_running_job_id:
        return AllocationDecision(
            action=ALLOCATION_QUEUE,
            build_job_id=job['build_job_id'],
            server_id=server_id,
            status=build_domain.STATUS_QUEUED,
        )
    return AllocationDecision(
        action=ALLOCATION_START,
        build_job_id=job['build_job_id'],
        server_id=server_id,
        status=job['status'],
    )


def plan_dedicated_dispatch(
    jobs: Iterable[Dict[str, Any]],
    servers: Iterable[Dict[str, Any]],
) -> List[AllocationDecision]:
    """Plan one dispatch tick for the dedicated Build_Jobs in ``jobs``.

    Produces one AllocationDecision per dispatch-eligible queued dedicated
    job (eligibility per Req 1.3). Within a single plan at most one job is
    started per server: the first eligible job (submission order) for a
    free server takes its slot; every other job targeting that server —
    and every job targeting an already occupied server — is queued with
    the queued status (Req 7.1, 7.2). Each decision targets exactly the
    server selected in the job's request (Req 2.2).

    ``servers`` supplies the fleet state (records with ``server_id`` and,
    when occupied, ``running_build_job_id``). Jobs targeting a server not
    present in ``servers`` are skipped: the planner cannot decide against
    unknown fleet state.
    """
    occupied: Dict[str, Optional[str]] = {}
    for server in servers:
        sid = server.get('server_id')
        if sid is not None:
            occupied[sid] = server.get('running_build_job_id') or None

    decisions: List[AllocationDecision] = []
    for job in eligible_queued_jobs(jobs):
        if job.get('execution_mode') != build_domain.EXECUTION_MODE_DEDICATED:
            continue
        server_id = job.get('server_id')
        if server_id not in occupied:
            continue
        decision = allocate_dedicated(job, occupied[server_id])
        decisions.append(decision)
        if decision.action == ALLOCATION_START:
            # The slot is taken for the rest of this plan (Req 7.1).
            occupied[server_id] = decision.build_job_id
    return decisions


# ---------------------------------------------------------------------------
# Queue promotion (Requirement 7.3)
# ---------------------------------------------------------------------------


def should_promote(current_job_status: str) -> bool:
    """True iff a server's current Build_Job status means its queue should
    be promoted: exactly the terminal statuses (Req 7.3)."""
    return build_domain.is_terminal(current_job_status)


def promote_next(
    server_id: str,
    jobs: Iterable[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Select the Build_Job to start next on a server whose current job
    reached a terminal status (Req 7.3).

    From the server's Build_Queue — the queued jobs with this
    ``server_id`` — the job with the earliest submission time
    (``created_at``) is selected, ties broken by job id for determinism.
    Returns None when the server's queue is empty. Deferred jobs retain
    their original ``created_at`` (design: a 7.6 deferral returns the job
    to the head of the queue), so submission order is preserved here.
    """
    queue = [
        job for job in jobs
        if job.get('server_id') == server_id
        and job.get('status') == build_domain.STATUS_QUEUED
    ]
    if not queue:
        return None
    return min(queue, key=lambda j: (j.get('created_at', 0), str(j.get('build_job_id'))))


# ---------------------------------------------------------------------------
# Ephemeral provisioning plan (Requirements 2.3, 3.1, 3.3, 7.4, 9.3)
# ---------------------------------------------------------------------------

# Documented defaults (Req 9.2) applied when a job's config_snapshot lacks a
# sizing field. The snapshot is normally the effective (default-applied)
# configuration at job creation, so these are a fail-safe only — current
# configuration is NEVER consulted here (Req 9.3).
DEFAULT_ARM64_INSTANCE_TYPE = 'm6g.4xlarge'
DEFAULT_X86_64_INSTANCE_TYPE = 'm6i.4xlarge'
DEFAULT_VOLUME_SIZE_GB = 100

# config_snapshot key carrying the instance type for each CPU architecture.
_INSTANCE_TYPE_SNAPSHOT_KEYS = {
    build_domain.ARCH_ARM64: 'arm64_instance_type',
    build_domain.ARCH_X86_64: 'x86_64_instance_type',
}
_DEFAULT_INSTANCE_TYPES = {
    build_domain.ARCH_ARM64: DEFAULT_ARM64_INSTANCE_TYPE,
    build_domain.ARCH_X86_64: DEFAULT_X86_64_INSTANCE_TYPE,
}


class RunnerPlan(NamedTuple):
    """Provisioning plan for exactly one Ephemeral_Build_Runner serving
    exactly one Build_Job (Req 2.3, 7.4).

    - ``build_job_id``: the single Build_Job this runner is provisioned for
    - ``arch``: CPU architecture required by the job's Build_Target (Req 3.1)
    - ``instance_type``: sizing from the job's own ``config_snapshot``
      (never the current configuration, Req 9.3)
    - ``volume_size_gb``: volume sizing from the job's ``config_snapshot``
    - ``spot``: whether the runner uses spot capacity
      (``use_spot_for_ephemeral`` in the snapshot; default False)
    - ``status``: the Build_Job status the dispatch implies — provisioning
      (Req 3.1)
    """
    build_job_id: str
    arch: str
    instance_type: str
    volume_size_gb: Any
    spot: bool
    status: str


def plan_runner(job: Dict[str, Any]) -> RunnerPlan:
    """Provisioning plan for one dispatched ephemeral Build_Job (pure).

    The runner's CPU architecture derives from the job's Build_Target
    (JP5/JP6 -> arm64, AMD64/AMD64_NVIDIA -> x86_64, Req 3.1), and its
    sizing (instance type per architecture, volume size, spot flag) derives
    from the job's own ``config_snapshot`` — the configuration in effect at
    the job's creation. The current configuration is deliberately not an
    input, so later configuration changes can never leak into an already
    created job's provisioning (Req 9.3).

    Raises ValueError for a job that is not ephemeral or has an unsupported
    Build_Target: planning a runner for such a job is a programming error
    upstream, not a plannable state.
    """
    if job.get('execution_mode') != build_domain.EXECUTION_MODE_EPHEMERAL:
        raise ValueError(
            f"Build_Job {job.get('build_job_id')} is not an ephemeral job; "
            f"an Ephemeral_Build_Runner is only planned for ephemeral mode"
        )
    arch = build_domain.required_arch_for_target(job['build_target'])
    snapshot = job.get('config_snapshot') or {}
    instance_type = snapshot.get(_INSTANCE_TYPE_SNAPSHOT_KEYS[arch]) \
        or _DEFAULT_INSTANCE_TYPES[arch]
    volume_size_gb = snapshot.get('volume_size_gb')
    if volume_size_gb is None:
        volume_size_gb = DEFAULT_VOLUME_SIZE_GB
    return RunnerPlan(
        build_job_id=job['build_job_id'],
        arch=arch,
        instance_type=instance_type,
        volume_size_gb=volume_size_gb,
        spot=bool(snapshot.get('use_spot_for_ephemeral', False)),
        status=build_domain.STATUS_PROVISIONING,
    )


def plan_ephemeral_provisioning(
    jobs: Iterable[Dict[str, Any]],
) -> List[RunnerPlan]:
    """Plan the Ephemeral_Build_Runner provisioning for one dispatch tick.

    Produces exactly one RunnerPlan per dispatch-eligible queued ephemeral
    Build_Job (eligibility per Req 1.3) — one runner per dispatched job,
    never more, never shared (Req 2.3, 7.4). When no ephemeral Build_Job is
    queued (or running — running jobs already hold their runner and are not
    dispatch candidates), the plan provisions zero runners, so no ephemeral
    compute exists while no ephemeral build is queued or running (Req 3.3).

    Each plan's architecture and sizing derive from that job's Build_Target
    and its own ``config_snapshot`` (Req 3.1, 9.3); this function takes no
    current-configuration input at all.
    """
    return [
        plan_runner(job)
        for job in eligible_queued_jobs(jobs)
        if job.get('execution_mode') == build_domain.EXECUTION_MODE_EPHEMERAL
    ]


# ---------------------------------------------------------------------------
# Pre-dispatch verification decision (Requirements 7.5, 7.6)
# ---------------------------------------------------------------------------

# Build-process patterns verified on the Build_Server before a dispatch
# (per .kiro/steering/builds.md: pgrep -af "gdk component build" and
# pgrep -af "build-custom.sh").
BUILD_PROCESS_PATTERNS = (
    'gdk component build',
    'build-custom.sh',
)

# Interval between pre-dispatch re-verification attempts for a deferred
# Build_Job (Req 7.6).
PREDISPATCH_RETRY_INTERVAL_MINUTES = 5
PREDISPATCH_RETRY_INTERVAL_MS = PREDISPATCH_RETRY_INTERVAL_MINUTES * 60 * 1000

# Pre-dispatch decision actions
PREDISPATCH_START = 'start'    # verification clean: start the build (7.5)
PREDISPATCH_DEFER = 'defer'    # build process found: defer, re-verify (7.6)


def parse_build_processes(pgrep_output: Optional[str]) -> List[str]:
    """Extract the build-process lines from ``pgrep -af`` output (pure).

    ``pgrep_output`` is the combined stdout of the verification command run
    on the Build_Server (one process per line, ``<pid> <command line>``).
    A line reports a build process iff it contains one of the
    ``BUILD_PROCESS_PATTERNS``. Blank/whitespace-only lines and unrelated
    process lines are ignored; None (no output at all) parses to no
    processes.
    """
    if not pgrep_output:
        return []
    return [
        line.strip()
        for line in pgrep_output.splitlines()
        if line.strip()
        and any(pattern in line for pattern in BUILD_PROCESS_PATTERNS)
    ]


def build_process_found(pgrep_output: Optional[str]) -> bool:
    """True iff the pre-dispatch verification output reports at least one
    running build process (Req 7.5)."""
    return len(parse_build_processes(pgrep_output)) > 0


def is_reverification_due(
    last_verified_at: Optional[int],
    now: int,
) -> bool:
    """True iff a deferred Build_Job's pre-dispatch verification may be
    re-attempted (Req 7.6).

    ``last_verified_at`` is the time (ms epoch) of the last verification
    attempt (the job's ``deferred_at``); None means the job has never been
    verified, so verification is due immediately. Otherwise re-verification
    is due only when at least the 5-minute retry interval has elapsed.
    """
    if last_verified_at is None:
        return True
    return (now - last_verified_at) >= PREDISPATCH_RETRY_INTERVAL_MS


class PredispatchDecision(NamedTuple):
    """Outcome of the pre-dispatch verification for one dedicated dispatch.

    - ``action``: PREDISPATCH_START (no build process found) or
      PREDISPATCH_DEFER (a build process is running on the server)
    - ``build_job_id``: the job the decision is about
    - ``status``: the Build_Job status the decision implies — queued on a
      deferral (the job returns to its server's Build_Queue, Req 7.6)
    - ``created_at``: the job's ORIGINAL submission time, always retained,
      so a deferred job stays at the head of its server's queue in
      submission order (Req 7.6; promotion orders by ``created_at``)
    - ``deferred_at``: the verification time recorded on a deferral (drives
      the 5-minute re-verification interval); None when starting
    - ``build_processes``: the build-process lines found (empty on start)
    """
    action: str
    build_job_id: str
    status: str
    created_at: Any
    deferred_at: Optional[int]
    build_processes: Tuple[str, ...]


def decide_predispatch(
    job: Dict[str, Any],
    pgrep_output: Optional[str],
    now: int,
) -> PredispatchDecision:
    """Decide whether a dedicated Build_Job's build may start (pure).

    ``pgrep_output`` is the output of the verification command run on the
    job's Dedicated_Build_Server (patterns per ``BUILD_PROCESS_PATTERNS``);
    ``now`` is the verification time (ms epoch).

    - No build process found -> start the build (Req 7.5). The dispatcher
      executes the queued -> building transition and sends the agent
      command.
    - A build process found -> defer: the job returns to the head of its
      server's Build_Queue with the queued status and its ORIGINAL
      ``created_at`` (submission order preserved, Req 7.6), and
      ``deferred_at`` records this attempt so re-verification happens only
      after the 5-minute retry interval (``is_reverification_due``).
    """
    processes = tuple(parse_build_processes(pgrep_output))
    if processes:
        return PredispatchDecision(
            action=PREDISPATCH_DEFER,
            build_job_id=job['build_job_id'],
            status=build_domain.STATUS_QUEUED,
            created_at=job.get('created_at'),
            deferred_at=now,
            build_processes=processes,
        )
    return PredispatchDecision(
        action=PREDISPATCH_START,
        build_job_id=job['build_job_id'],
        status=job.get('status', build_domain.STATUS_QUEUED),
        created_at=job.get('created_at'),
        deferred_at=None,
        build_processes=(),
    )


# ---------------------------------------------------------------------------
# Watchdog decisions (Requirements 3.8, 3.9, 6.11, 7.7, 7.8, 7.9)
# ---------------------------------------------------------------------------

_MS_PER_MINUTE = 60 * 1000
_MS_PER_HOUR = 60 * _MS_PER_MINUTE

# Documented default maximum Build_Job runtime in hours (Req 9.2), applied
# when a job's config_snapshot lacks ``max_runtime_hours``. The snapshot is
# normally the effective (default-applied) configuration at job creation,
# so this is a fail-safe only — current configuration is NEVER consulted
# here (Req 9.3).
DEFAULT_MAX_RUNTIME_HOURS = 4


def max_runtime_ms(config_snapshot: Optional[Dict[str, Any]]) -> float:
    """Maximum Build_Job runtime in ms from a job's own ``config_snapshot``
    (``max_runtime_hours``, default 4 per Req 9.2) — never the current
    configuration (Req 9.3)."""
    snapshot = config_snapshot or {}
    hours = snapshot.get('max_runtime_hours')
    if hours is None:
        hours = DEFAULT_MAX_RUNTIME_HOURS
    return hours * _MS_PER_HOUR


# Statuses subject to the runtime watchdog: an actual build/publish is
# running on build compute (design dispatcher step 3).
RUNNING_WATCHDOG_STATUSES = frozenset({
    build_domain.STATUS_BUILDING,
    build_domain.STATUS_PUBLISHING,
})


class TimeoutDecision(NamedTuple):
    """Runtime-watchdog decision for one running Build_Job (Req 3.8).

    - ``timed_out``: True iff the job's elapsed runtime exceeds its
      ``config_snapshot`` maximum
    - ``build_job_id``: the job the decision is about
    - ``status``: the Build_Job status the decision implies — failed on a
      timeout, the unchanged current status otherwise
    - ``error``: the timeout error recorded on the job (None when not
      timed out); logs produced up to termination are retained
    """
    timed_out: bool
    build_job_id: str
    status: str
    error: Optional[str]


def decide_runtime_timeout(job: Dict[str, Any], now: int) -> TimeoutDecision:
    """Runtime-watchdog decision for one Build_Job (pure, Req 3.8).

    A job is timed out if and only if it is running (building or
    publishing), its start time is known, and its elapsed runtime
    strictly exceeds the ``max_runtime_hours`` of its OWN
    ``config_snapshot`` (default 4 hours per Req 9.2; never the current
    configuration, Req 9.3). On a timeout the job is marked failed with a
    timeout error naming the limit; the dispatcher stops the build and
    retains the logs produced up to termination.

    ``started_at`` (ms epoch, recorded when the job entered the building
    status) missing/None means elapsed runtime cannot be established, so
    the job is not timed out (fail-safe: the watchdog never kills a job on
    unknown arithmetic).
    """
    status = job.get('status')
    started_at = job.get('started_at')
    limit_ms = max_runtime_ms(job.get('config_snapshot'))
    timed_out = (
        status in RUNNING_WATCHDOG_STATUSES
        and started_at is not None
        and (now - started_at) > limit_ms
    )
    if not timed_out:
        return TimeoutDecision(
            timed_out=False,
            build_job_id=job['build_job_id'],
            status=status,
            error=None,
        )
    limit_hours = limit_ms / _MS_PER_HOUR
    return TimeoutDecision(
        timed_out=True,
        build_job_id=job['build_job_id'],
        status=build_domain.next_status(status, build_domain.EVENT_TIMEOUT),
        error=(
            f"Build_Job exceeded its maximum runtime of "
            f"{limit_hours:g} hours (timeout)."
        ),
    )


# Termination retry cadence for an Ephemeral_Build_Runner whose
# termination failed (Req 3.9): retries at intervals of no more than
# 10 minutes, for up to 1 hour since the FIRST failure.
TERMINATION_RETRY_INTERVAL_MINUTES = 10
TERMINATION_RETRY_INTERVAL_MS = TERMINATION_RETRY_INTERVAL_MINUTES * _MS_PER_MINUTE
TERMINATION_RETRY_WINDOW_HOURS = 1
TERMINATION_RETRY_WINDOW_MS = TERMINATION_RETRY_WINDOW_HOURS * _MS_PER_HOUR


class TerminationRetryDecision(NamedTuple):
    """Termination-watchdog decision for one failed runner termination
    (Req 3.9).

    - ``retry``: True iff a termination retry is due now
    - ``notify_orphaned``: True iff the Portal_Admins orphaned-runner
      notification (and the Audit_Log termination-failure entry) fires now
    """
    retry: bool
    notify_orphaned: bool


def decide_termination_retry(
    first_failed_at: int,
    last_attempt_at: Optional[int],
    now: int,
    already_notified: bool = False,
) -> TerminationRetryDecision:
    """Decide the termination retry / orphaned-runner notification for an
    Ephemeral_Build_Runner whose termination failed (pure, Req 3.9).

    ``first_failed_at`` is the time (ms epoch) of the FIRST termination
    failure; ``last_attempt_at`` is the time of the most recent retry
    attempt (None when no retry has been attempted yet);
    ``already_notified`` reports whether the orphaned-runner notification
    was already sent.

    Semantics (design Property 8):
      - A retry is due if and only if LESS than the 1-hour window has
        passed since the first failure AND at least the 10-minute retry
        interval has elapsed since the last attempt (immediately due when
        no retry was attempted yet) — so attempts are at most 10 minutes
        apart, for up to 1 hour.
      - The orphaned-runner notification fires exactly when the retry
        window is exhausted: at the first decision at or past the 1-hour
        mark that has not already notified. No retry is due once the
        window is exhausted.
    """
    if (now - first_failed_at) < TERMINATION_RETRY_WINDOW_MS:
        due = (
            last_attempt_at is None
            or (now - last_attempt_at) >= TERMINATION_RETRY_INTERVAL_MS
        )
        return TerminationRetryDecision(retry=due, notify_orphaned=False)
    return TerminationRetryDecision(
        retry=False,
        notify_orphaned=not already_notified,
    )


# Deadline for an accepted fleet management action to reach its expected
# lifecycle state (Req 6.11).
FLEET_ACTION_DEADLINE_MINUTES = 10
FLEET_ACTION_DEADLINE_MS = FLEET_ACTION_DEADLINE_MINUTES * _MS_PER_MINUTE


def fleet_action_deadline(initiated_at: int) -> int:
    """Deadline (ms epoch) by which an accepted fleet management action
    must have reached its expected lifecycle state: 10 minutes after the
    action was initiated (Req 6.11)."""
    return initiated_at + FLEET_ACTION_DEADLINE_MS


def is_pending_action_failed(deadline: int, now: int) -> bool:
    """True iff a pending fleet action has failed: its 10-minute deadline
    has passed (strictly, Req 6.11). At or before the deadline the action
    is still pending, not failed."""
    return now > deadline


class PendingActionFailure(NamedTuple):
    """Expected-state watchdog decision for one pending fleet action
    (Req 6.11).

    - ``failed``: True iff the action's 10-minute deadline has passed
      without the server reaching the expected lifecycle state
    - ``action``: the fleet action (start/stop/terminate/launch)
    - ``server_id``: the target Dedicated_Build_Server
    - ``lifecycle_state``: the server's CURRENT lifecycle state
    - ``error``: user-readable failure identifying the action, the target
      server, and the current lifecycle state (None while still pending)
    """
    failed: bool
    action: Optional[str]
    server_id: Any
    lifecycle_state: Any
    error: Optional[str]


def decide_pending_action(
    pending_action: Dict[str, Any],
    server: Dict[str, Any],
    now: int,
) -> PendingActionFailure:
    """Decide whether a pending fleet action has failed (pure, Req 6.11).

    ``pending_action`` is the marker recorded when the action was
    accepted: ``action`` plus either an explicit ``deadline`` (ms epoch)
    or the ``initiated_at`` time from which the 10-minute deadline is
    derived. ``server`` is the target server record (its CURRENT
    ``lifecycle_state`` names the observed state in the error).

    The action is reported failed if and only if its deadline has passed
    (``is_pending_action_failed``); the error identifies the action, the
    target server, and the server's current lifecycle state.
    """
    action = pending_action.get('action')
    deadline = pending_action.get('deadline')
    if deadline is None:
        deadline = fleet_action_deadline(pending_action['initiated_at'])
    server_id = server.get('server_id')
    state = server.get('lifecycle_state')
    if not is_pending_action_failed(deadline, now):
        return PendingActionFailure(
            failed=False,
            action=action,
            server_id=server_id,
            lifecycle_state=state,
            error=None,
        )
    return PendingActionFailure(
        failed=True,
        action=action,
        server_id=server_id,
        lifecycle_state=state,
        error=(
            f"Fleet action '{action}' on Dedicated_Build_Server "
            f"'{server_id}' did not reach the expected lifecycle state "
            f"within {FLEET_ACTION_DEADLINE_MINUTES} minutes; the server's "
            f"current lifecycle state is '{state}'."
        ),
    )


# Interval between serialization checks on a server with a running
# Build_Job (Req 7.7: intervals not exceeding 5 minutes).
SERIALIZATION_CHECK_INTERVAL_MINUTES = 5
SERIALIZATION_CHECK_INTERVAL_MS = (
    SERIALIZATION_CHECK_INTERVAL_MINUTES * _MS_PER_MINUTE
)


def is_serialization_check_due(
    last_checked_at: Optional[int],
    now: int,
) -> bool:
    """True iff a running server's serialization check is due (Req 7.7).

    ``last_checked_at`` is the time (ms epoch) of the server's last
    serialization check; None means the server has never been checked, so
    a check is due immediately. Otherwise the check is due if and only if
    at least the check interval (5 minutes — so checks happen at intervals
    not exceeding 5 minutes via the 1-minute dispatcher tick) has elapsed
    since the last check.
    """
    if last_checked_at is None:
        return True
    return (now - last_checked_at) >= SERIALIZATION_CHECK_INTERVAL_MS


# Serialization violation handling (Req 7.8).
SERIALIZATION_VIOLATION_ERROR = 'SERIALIZATION_VIOLATION'
# Every detected build process must be stopped within this window of the
# detection (Req 7.8).
SERIALIZATION_STOP_WINDOW_SECONDS = 60
# Build-process count at which concurrent builds are detected.
SERIALIZATION_VIOLATION_MIN_PROCESSES = 2


class SerializationDecision(NamedTuple):
    """Serialization-watchdog decision for one server check (Req 7.8).

    - ``violation``: True iff two or more build processes were detected
      running concurrently on the server
    - ``server_id``: the checked Build_Server
    - ``process_count``: the detected build-process count
    - ``stop_all``: True iff every detected build process must be stopped
      (within ``SERIALIZATION_STOP_WINDOW_SECONDS`` of detection)
    - ``failed_job_ids``: every associated Build_Job, each to be marked
      failed with the serialization-violation error (logs retained)
    - ``error``: the error code recorded on each failed job
      (``SERIALIZATION_VIOLATION``; None when no violation)
    """
    violation: bool
    server_id: Any
    process_count: int
    stop_all: bool
    failed_job_ids: Tuple[str, ...]
    error: Optional[str]


def decide_serialization_violation(
    server_id: Any,
    process_count: int,
    associated_job_ids: Iterable[str],
) -> SerializationDecision:
    """Decide the serialization-watchdog action for one server (pure).

    ``process_count`` is the number of build processes detected running on
    the server (the ``pgrep`` count for ``BUILD_PROCESS_PATTERNS``);
    ``associated_job_ids`` are the Build_Jobs associated with the server's
    detected build activity.

    The stop-all/fail-all action is taken if and only if the detected
    build-process count is two or more (design Property 12): every
    detected build process is stopped within 60 seconds, and EVERY
    associated Build_Job is marked failed with the
    ``SERIALIZATION_VIOLATION`` error, its logs produced up to the stop
    retained, and the event audited (Req 7.8). A count of zero or one is
    no violation: nothing is stopped and no job fails.
    """
    if process_count >= SERIALIZATION_VIOLATION_MIN_PROCESSES:
        return SerializationDecision(
            violation=True,
            server_id=server_id,
            process_count=process_count,
            stop_all=True,
            failed_job_ids=tuple(associated_job_ids),
            error=SERIALIZATION_VIOLATION_ERROR,
        )
    return SerializationDecision(
        violation=False,
        server_id=server_id,
        process_count=process_count,
        stop_all=False,
        failed_job_ids=(),
        error=None,
    )


# Dead-server sweep (Req 7.9): server lifecycle states in which queued
# Build_Jobs for the server can never run.
DEAD_SERVER_STATES = frozenset({
    build_domain.SERVER_STATE_STOPPED,
    build_domain.SERVER_STATE_TERMINATED,
})


class DeadServerSweepDecision(NamedTuple):
    """Queue-orphan sweep decision for one Dedicated_Build_Server
    (Req 7.9).

    - ``sweep``: True iff the server's lifecycle state is stopped or
      terminated (its queued jobs can never run)
    - ``server_id``: the swept server
    - ``lifecycle_state``: the server's observed lifecycle state
    - ``failed_job_ids``: every queued Build_Job for the server, each to
      be marked failed with the server-state error
    - ``error``: user-readable error identifying the server state (None
      when no sweep)
    """
    sweep: bool
    server_id: Any
    lifecycle_state: Any
    failed_job_ids: Tuple[str, ...]
    error: Optional[str]


def sweep_dead_server(
    server: Dict[str, Any],
    jobs: Iterable[Dict[str, Any]],
) -> DeadServerSweepDecision:
    """Decide the queue-orphan sweep for one server (pure, Req 7.9).

    ``server`` is the Dedicated_Build_Server record (``server_id``,
    ``lifecycle_state``); ``jobs`` is the Build_Job collection from which
    the server's Build_Queue (its queued jobs) is derived.

    Every queued Build_Job for the server is marked failed with an error
    identifying the server state if and only if the server's lifecycle
    state is stopped or terminated (design Property 12); the event is
    recorded in the Audit_Log. In any other lifecycle state (including
    the transitional stopping/shutting-down states, where the observed
    state is not yet final) no job is failed.
    """
    server_id = server.get('server_id')
    state = server.get('lifecycle_state')
    if state not in DEAD_SERVER_STATES:
        return DeadServerSweepDecision(
            sweep=False,
            server_id=server_id,
            lifecycle_state=state,
            failed_job_ids=(),
            error=None,
        )
    queued_ids = tuple(
        job['build_job_id']
        for job in sorted(
            (
                job for job in jobs
                if job.get('server_id') == server_id
                and job.get('status') == build_domain.STATUS_QUEUED
            ),
            key=lambda j: (j.get('created_at', 0), str(j.get('build_job_id'))),
        )
    )
    return DeadServerSweepDecision(
        sweep=True,
        server_id=server_id,
        lifecycle_state=state,
        failed_job_ids=queued_ids,
        error=(
            f"Dedicated_Build_Server '{server_id}' entered lifecycle state "
            f"'{state}' while Build_Jobs remained in its Build_Queue; the "
            f"queued Build_Jobs cannot run on this server."
        ),
    )
