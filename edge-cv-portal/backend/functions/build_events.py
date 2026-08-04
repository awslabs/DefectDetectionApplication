"""
Build events Lambda function (Build_Manager, portal build fleet)

EventBridge consumer for every asynchronous signal about Build_Jobs and
Dedicated_Build_Servers (design §4 "Event consumer"). One Lambda behind
four EventBridge rules:

  - Custom ``dda.portal.builds`` / ``BuildPhaseChange`` phase events from
    the build agent (scripts/portal-build-agent.sh + portal-build.sh):
    conditional status transitions along the build_domain state machine,
    start/end time recording, the succeeded event's result metadata
    (component version, image refs) recorded verbatim on the Build_Job
    with a ``build_published`` Audit_Log entry (Req 5.1, 5.3, 5.5), and
    publishing-stage failures recorded with a PUBLISHING_FAILED error
    kind distinct from a build failure plus the per-artifact
    published/unpublished lists exactly as reported (``publish_partial``,
    Req 5.4).
  - EC2 Spot Instance Interruption Warning, and EC2 instance state-change
    to stopped/terminated, for an instance whose Build_Job is
    non-terminal: the job becomes ``interrupted`` (logs are already
    durable in CloudWatch; the retry action is served by build_jobs.py)
    (Req 3.5).
  - EC2 instance state-change for fleet instances: the BuildServers
    record's ``lifecycle_state`` and ``last_state_change_at`` are
    updated, ``terminated_at`` recorded on termination, and the
    ``pending_action`` marker cleared when the accepted fleet action's
    expected state is reached (Req 6.2, 6.3, 6.9, 6.11).
  - SSM command status change to Failed/TimedOut/Cancelled for a job's
    agent command, while the job is still building/publishing and no
    agent terminal phase event arrived: fallback to ``failed``
    (Failed/TimedOut) or ``interrupted`` (Cancelled — the command was
    torn down under the agent, e.g. by instance loss).

Idempotence (Req 4.1): every job status transition is a DynamoDB
conditional update (ConditionExpression on the expected current status)
computed by build_domain.next_status with terminal absorption, and every
server state write is conditional on the state actually changing —
duplicate or stale EventBridge delivery is a no-op; a terminal Build_Job
is never resurrected.

The event-application decisions are PURE functions in this module
(``apply_phase_event``, ``decide_ssm_fallback``,
``apply_server_state_change``): event payload -> job/server field
updates, with no AWS clients, so design Property 10 (result and failure
recording on completion events) tests ``apply_phase_event`` directly.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates
Requirements: 3.5, 5.1, 5.3, 5.4, 5.5, 6.2, 6.3, 6.9, 6.11
"""
import json
import logging
import os
import time
from decimal import Decimal
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import log_audit_event

# Pure decision module (no AWS clients): every status transition is
# computed by the build_domain state machine (terminal absorption,
# Req 4.1).
import build_domain

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')

# Environment variables (build-fleet-stack.ts lambdaEnvironment)
BUILD_JOBS_TABLE = os.environ.get('BUILD_JOBS_TABLE')
BUILD_SERVERS_TABLE = os.environ.get('BUILD_SERVERS_TABLE')

#: Audit_Log actor for event-driven transitions (no requesting user).
SYSTEM_USER = 'system'

# ---------------------------------------------------------------- constants

#: Custom phase events emitted by the build agent (design §5).
PHASE_EVENT_SOURCE = 'dda.portal.builds'
PHASE_EVENT_DETAIL_TYPE = 'BuildPhaseChange'

#: AWS event sources / detail types routed to this consumer.
EC2_EVENT_SOURCE = 'aws.ec2'
SSM_EVENT_SOURCE = 'aws.ssm'
SPOT_INTERRUPTION_DETAIL_TYPE = 'EC2 Spot Instance Interruption Warning'
INSTANCE_STATE_CHANGE_DETAIL_TYPE = 'EC2 Instance State-change Notification'
SSM_COMMAND_DETAIL_TYPES = (
    'EC2 Command Status-change Notification',
    'EC2 Command Invocation Status-change Notification',
)

#: Build_Job phases reported by the agent (portal-build-agent.sh /
#: portal-build.sh phase events).
PHASE_BUILDING = 'building'
PHASE_PUBLISHING = 'publishing'
PHASE_SUCCEEDED = 'succeeded'
PHASE_FAILED = 'failed'
KNOWN_PHASES = frozenset({
    PHASE_BUILDING, PHASE_PUBLISHING, PHASE_SUCCEEDED, PHASE_FAILED,
})

#: error_kind reported on a phase=failed event for a publish-stage
#: failure (portal-build-agent.sh, Req 5.4).
ERROR_KIND_PUBLISHING = 'publishing'

#: Error codes recorded on failed Build_Jobs (design Error Handling):
#: a publishing failure is DISTINCT from a build failure (Req 5.4).
ERROR_BUILD_FAILED = 'BUILD_FAILED'
ERROR_PUBLISHING_FAILED = 'PUBLISHING_FAILED'
ERROR_AGENT_COMMAND_FAILED = 'AGENT_COMMAND_FAILED'
ERROR_AGENT_COMMAND_TIMED_OUT = 'AGENT_COMMAND_TIMED_OUT'

#: Audit_Log actions recorded by this consumer.
AUDIT_BUILD_PUBLISHED = 'build_published'            # Req 5.5
AUDIT_BUILD_PUBLISHING_FAILED = 'build_publishing_failed'  # Req 5.4
AUDIT_BUILD_FAILED = 'build_failed'
AUDIT_BUILD_INTERRUPTED = 'build_interrupted'

#: SSM command statuses handled by the fallback path.
SSM_STATUS_FAILED = 'Failed'
SSM_STATUS_TIMED_OUT = 'TimedOut'
SSM_STATUS_CANCELLED = 'Cancelled'
SSM_FAILURE_STATUSES = frozenset({
    SSM_STATUS_FAILED, SSM_STATUS_TIMED_OUT, SSM_STATUS_CANCELLED,
})

#: EC2 instance states that mean the compute is gone: a non-terminal
#: Build_Job on that instance is interrupted (Req 3.5).
INSTANCE_LOST_STATES = frozenset({
    build_domain.SERVER_STATE_STOPPED,
    build_domain.SERVER_STATE_TERMINATED,
})

#: EC2 lifecycle states tracked on BuildServers records (Req 6.1).
EC2_LIFECYCLE_STATES = frozenset({
    build_domain.SERVER_STATE_PENDING,
    build_domain.SERVER_STATE_RUNNING,
    build_domain.SERVER_STATE_STOPPING,
    build_domain.SERVER_STATE_STOPPED,
    build_domain.SERVER_STATE_SHUTTING_DOWN,
    build_domain.SERVER_STATE_TERMINATED,
})

#: Statuses in which a Build_Job is actually occupying build compute:
#: instance loss interrupts exactly these (queued jobs on a dead server
#: are the dispatcher's dead-server sweep concern, Req 7.9).
ACTIVE_ON_COMPUTE_STATUSES = frozenset({
    build_domain.STATUS_PROVISIONING,
    build_domain.STATUS_BUILDING,
    build_domain.STATUS_PUBLISHING,
})

#: Statuses in which the agent SSM command failing means the job's
#: outcome was never reported (the fallback path).
AGENT_RUNNING_STATUSES = frozenset({
    build_domain.STATUS_BUILDING,
    build_domain.STATUS_PUBLISHING,
})


# =========================================================================
# PURE event-application functions (no AWS clients)
# =========================================================================

class PhaseApplication(NamedTuple):
    """Pure application of one agent phase event to a Build_Job.

    - ``steps``: the ordered chain of ``(expected_status, next_status)``
      conditional transitions to execute, each a defined edge of the
      build_domain state machine (empty for a no-op: duplicate delivery,
      stale event, or terminal job — Req 4.1 idempotence).
    - ``updates``: the Build_Job field updates applied together with the
      FINAL transition of the chain (start/end times, verbatim result
      metadata, error record, ``publish_partial`` lists).
    - ``audit_action`` / ``audit_details``: the Audit_Log entry recorded
      when the final transition actually applied (``build_published`` on
      success Req 5.5, the publishing failure Req 5.4).
    """
    steps: Tuple[Tuple[str, str], ...]
    updates: Dict[str, Any]
    audit_action: Optional[str]
    audit_details: Dict[str, Any]

    @property
    def is_noop(self) -> bool:
        return not self.steps

    @property
    def final_status(self) -> Optional[str]:
        return self.steps[-1][1] if self.steps else None


#: Shared no-op application (duplicate/stale/terminal, Req 4.1).
NO_APPLICATION = PhaseApplication((), {}, None, {})


def apply_phase_event(current_status: str, detail: Dict[str, Any],
                      now: int) -> PhaseApplication:
    """Pure event-application function: agent phase event payload ->
    Build_Job field updates (design §4, Property 10).

    ``current_status`` is the Build_Job's status at delivery time;
    ``detail`` is the EventBridge detail emitted by the build agent
    (``phase`` plus phase-specific fields); ``now`` is the ms-epoch
    receipt time used for start/end timestamps.

    Every transition in the returned chain is computed by
    build_domain.next_status, so terminal absorption holds by
    construction (Req 4.1): a terminal ``current_status`` or an unknown
    phase yields the no-op application, making duplicate EventBridge
    delivery a no-op. A lost intermediate event is healed by chaining
    the skipped defined edges (e.g. a succeeded event arriving while the
    job is still ``building`` chains building -> publishing ->
    succeeded).

    Field updates:
      - entering ``building`` records the start time (Req 4.3);
      - a terminal final status records the end time (Req 4.3);
      - a succeeded event records the agent-reported result metadata
        (component version, image references) VERBATIM on the Build_Job
        and produces the ``build_published`` Audit_Log entry
        (Req 5.3, 5.5);
      - a failed event with ``error_kind=publishing`` marks the job
        failed with the PUBLISHING_FAILED error kind — distinct from a
        build failure — and preserves the published/unpublished artifact
        lists exactly as reported (``publish_partial``), with the
        publishing failure audited (Req 5.4); any other failed event
        records a BUILD_FAILED error.

    Raises ValueError for a status outside the defined set (programming
    error), mirroring build_domain.next_status.
    """
    if current_status not in build_domain.ALL_STATUSES:
        raise ValueError(f"Unknown Build_Job status '{current_status}'")
    detail = detail or {}
    phase = detail.get('phase')
    if phase not in KNOWN_PHASES or build_domain.is_terminal(current_status):
        return NO_APPLICATION

    steps: List[Tuple[str, str]] = []
    status = current_status

    def advance(event: str) -> None:
        """Append the (status, event) edge when it is a defined move."""
        nonlocal status
        next_ = build_domain.next_status(status, event)
        if next_ != status:
            steps.append((status, next_))
            status = next_

    error_kind = detail.get('error_kind')

    if phase == PHASE_BUILDING:
        # provisioning -> building (ephemeral runner ready). A dedicated
        # job is already `building` (the dispatcher transitions before
        # the agent SendCommand): no-op. `queued` is the dispatcher's
        # transition to make: no-op here.
        if status == build_domain.STATUS_PROVISIONING:
            advance(build_domain.EVENT_RUNNER_READY)
    elif phase == PHASE_PUBLISHING:
        # building -> publishing (build step succeeded, Req 5.1); a lost
        # building event is healed from provisioning.
        if status == build_domain.STATUS_PROVISIONING:
            advance(build_domain.EVENT_RUNNER_READY)
        if status == build_domain.STATUS_BUILDING:
            advance(build_domain.EVENT_BUILD_SUCCEEDED)
    elif phase == PHASE_SUCCEEDED:
        # publishing -> succeeded (Req 5.3), healing lost intermediates.
        if status == build_domain.STATUS_PROVISIONING:
            advance(build_domain.EVENT_RUNNER_READY)
        if status == build_domain.STATUS_BUILDING:
            advance(build_domain.EVENT_BUILD_SUCCEEDED)
        if status == build_domain.STATUS_PUBLISHING:
            advance(build_domain.EVENT_PUBLISH_SUCCEEDED)
    elif phase == PHASE_FAILED:
        if error_kind == ERROR_KIND_PUBLISHING:
            # A publish-stage failure implies the build step succeeded:
            # heal lost intermediates, then publishing -> failed (5.4).
            if status == build_domain.STATUS_PROVISIONING:
                advance(build_domain.EVENT_RUNNER_READY)
            if status == build_domain.STATUS_BUILDING:
                advance(build_domain.EVENT_BUILD_SUCCEEDED)
            if status == build_domain.STATUS_PUBLISHING:
                advance(build_domain.EVENT_PUBLISH_FAILED)
        else:
            # Build-stage failure: building -> failed. A build failure
            # reported while the job is already `publishing` is out of
            # order (stale): no-op.
            if status == build_domain.STATUS_PROVISIONING:
                advance(build_domain.EVENT_RUNNER_READY)
            if status == build_domain.STATUS_BUILDING:
                advance(build_domain.EVENT_BUILD_FAILED)

    if not steps:
        return NO_APPLICATION

    updates: Dict[str, Any] = {}
    audit_action: Optional[str] = None
    audit_details: Dict[str, Any] = {}
    final_status = steps[-1][1]

    # Start time from the moment the job enters `building` (Req 4.3).
    if any(next_ == build_domain.STATUS_BUILDING for _, next_ in steps):
        updates['started_at'] = now
    # End time from the moment the job reaches a terminal status (Req 4.3).
    if build_domain.is_terminal(final_status):
        updates['ended_at'] = now

    if final_status == build_domain.STATUS_SUCCEEDED:
        # Result metadata recorded VERBATIM as the agent reported it:
        # published component version identifier + pushed image
        # references (Req 5.3).
        result = detail.get('result')
        updates['result'] = result if isinstance(result, dict) else {}
        audit_action = AUDIT_BUILD_PUBLISHED  # Req 5.5
        audit_details = {
            'component_name': updates['result'].get('component_name'),
            'component_version': (
                updates['result'].get('published_version')
                or updates['result'].get('component_version')),
            'image_refs': (
                updates['result'].get('pushed_image_refs')
                or updates['result'].get('image_refs') or []),
        }
    elif final_status == build_domain.STATUS_FAILED:
        message = detail.get('error_message') or 'The build failed.'
        if error_kind == ERROR_KIND_PUBLISHING:
            # Distinct error kind + the per-artifact lists EXACTLY as
            # reported (Req 5.4).
            updates['error'] = {
                'code': ERROR_PUBLISHING_FAILED,
                'message': message,
            }
            updates['publish_partial'] = {
                'published': list(detail.get('published_artifacts') or []),
                'unpublished': list(detail.get('unpublished_artifacts') or []),
            }
            audit_action = AUDIT_BUILD_PUBLISHING_FAILED  # Req 5.4
            audit_details = {
                'error_kind': ERROR_KIND_PUBLISHING,
                'error_code': ERROR_PUBLISHING_FAILED,
                'message': message,
                'published': updates['publish_partial']['published'],
                'unpublished': updates['publish_partial']['unpublished'],
            }
        else:
            updates['error'] = {
                'code': ERROR_BUILD_FAILED,
                'message': message,
            }
            audit_action = AUDIT_BUILD_FAILED
            audit_details = {
                'error_kind': error_kind or 'building',
                'error_code': ERROR_BUILD_FAILED,
                'message': message,
            }

    return PhaseApplication(tuple(steps), updates, audit_action,
                            audit_details)


class SsmFallback(NamedTuple):
    """Pure fallback decision for a Failed/TimedOut/Cancelled agent SSM
    command (design §4 last row).

    ``apply`` is True iff the Build_Job must be moved; then
    ``next_status`` is ``failed`` (command Failed/TimedOut) or
    ``interrupted`` (command Cancelled — the agent was torn down under
    the job, e.g. by instance loss), and ``error`` carries the error
    record for the failed case.
    """
    apply: bool
    next_status: Optional[str]
    error: Optional[Dict[str, str]]


def decide_ssm_fallback(current_status: str,
                        command_status: str) -> SsmFallback:
    """Fallback for a job's agent command reaching Failed/TimedOut/
    Cancelled while the job is still building/publishing and no agent
    terminal phase event arrived (pure).

    Any other job status is a no-op: a terminal job already has its
    outcome (the agent's own event won the race — idempotence, Req 4.1),
    and a queued/provisioning job has no agent outcome to lose.
    """
    if current_status not in build_domain.ALL_STATUSES:
        raise ValueError(f"Unknown Build_Job status '{current_status}'")
    if current_status not in AGENT_RUNNING_STATUSES:
        return SsmFallback(False, None, None)
    if command_status == SSM_STATUS_CANCELLED:
        return SsmFallback(
            True, build_domain.apply_interruption(current_status), None)
    if command_status in (SSM_STATUS_FAILED, SSM_STATUS_TIMED_OUT):
        code = ERROR_AGENT_COMMAND_TIMED_OUT \
            if command_status == SSM_STATUS_TIMED_OUT \
            else ERROR_AGENT_COMMAND_FAILED
        event = build_domain.EVENT_BUILD_FAILED \
            if current_status == build_domain.STATUS_BUILDING \
            else build_domain.EVENT_PUBLISH_FAILED
        return SsmFallback(
            True,
            build_domain.next_status(current_status, event),
            {'code': code,
             'message': (
                 f"The build agent SSM command ended with status "
                 f"'{command_status}' before reporting a build result.")},
        )
    return SsmFallback(False, None, None)


class ServerStateApplication(NamedTuple):
    """Pure application of an EC2 instance state-change to a BuildServers
    record (Req 6.2, 6.3, 6.9, 6.11).

    ``changed`` is False for a duplicate/stale delivery (state already
    recorded) or an unknown state — a no-op. ``updates`` carries the
    fields to SET; ``clear_pending`` is True when the accepted fleet
    action's expected lifecycle state was reached, so the
    ``pending_action`` marker is removed (Req 6.11).
    """
    changed: bool
    updates: Dict[str, Any]
    clear_pending: bool


def apply_server_state_change(server: Dict[str, Any], new_state: str,
                              now: int) -> ServerStateApplication:
    """EC2 instance state-change -> BuildServers field updates (pure).

    Records the observed lifecycle state and the time of the state
    change (Req 6.1, 6.9), ``terminated_at`` on the first transition to
    terminated, and clears the ``pending_action`` marker when its
    ``expected_state`` is reached (Req 6.2, 6.3, 6.11). A state equal to
    the stored one (duplicate delivery) or outside the EC2 lifecycle
    state set is a no-op.
    """
    server = server or {}
    if new_state not in EC2_LIFECYCLE_STATES \
            or new_state == server.get('lifecycle_state'):
        return ServerStateApplication(False, {}, False)

    updates: Dict[str, Any] = {
        'lifecycle_state': new_state,
        'last_state_change_at': now,
    }
    if new_state == build_domain.SERVER_STATE_TERMINATED \
            and not server.get('terminated_at'):
        updates['terminated_at'] = now

    pending = server.get('pending_action') or {}
    clear_pending = bool(pending) and \
        pending.get('expected_state') == new_state

    return ServerStateApplication(True, updates, clear_pending)


# =========================================================================
# Persistence (DynamoDB) — same conditional-update conventions as
# build_dispatcher.py
# =========================================================================

def now_ms() -> int:
    """Current epoch milliseconds (BuildJobs timestamps are ms epoch)"""
    return int(time.time() * 1000)


def to_native(value: Any) -> Any:
    """Convert DynamoDB Decimals to native ints/floats (deep)"""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: to_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_native(v) for v in value]
    return value


def jobs_table():
    """BuildJobs DynamoDB table accessor"""
    return dynamodb.Table(BUILD_JOBS_TABLE)


def servers_table():
    """BuildServers DynamoDB table accessor"""
    return dynamodb.Table(BUILD_SERVERS_TABLE)


def scan_all(table) -> List[Dict]:
    """Full paginated scan of a table (native types)"""
    items: List[Dict] = []
    kwargs: Dict[str, Any] = {}
    while True:
        response = table.scan(**kwargs)
        items.extend(to_native(item) for item in response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            return items
        kwargs['ExclusiveStartKey'] = last_key


def get_job(build_job_id: str) -> Optional[Dict]:
    """Fetch one Build_Job record (native types) or None"""
    response = jobs_table().get_item(Key={'build_job_id': build_job_id})
    item = response.get('Item')
    return to_native(item) if item else None


def transition_job(build_job_id: str, expected_status: str,
                   new_status: str,
                   extra: Optional[Dict[str, Any]] = None) -> bool:
    """Conditionally transition a Build_Job's status (ConditionExpression
    on the expected current status, Req 4.1: terminal jobs are never
    resurrected, duplicate EventBridge delivery is a no-op). ``extra``
    carries additional top-level attributes to SET. Returns False when
    the job moved in the meantime."""
    names = {'#status': 'status'}
    values: Dict[str, Any] = {':new': new_status, ':expected': expected_status}
    sets = ['#status = :new']
    for index, (key, value) in enumerate(sorted((extra or {}).items())):
        names[f'#a{index}'] = key
        values[f':a{index}'] = value
        sets.append(f'#a{index} = :a{index}')
    try:
        jobs_table().update_item(
            Key={'build_job_id': build_job_id},
            UpdateExpression='SET ' + ', '.join(sets),
            ConditionExpression='#status = :expected',
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == \
                'ConditionalCheckFailedException':
            return False
        raise


def release_server(server_id: str, build_job_id: str) -> bool:
    """Release a server allocation held by one Build_Job (conditional on
    the job actually holding it, so a stale release can never free a
    slot another job took) — lets the dispatcher promote the oldest
    queued job on its next tick (Req 7.3)."""
    try:
        servers_table().update_item(
            Key={'server_id': server_id},
            UpdateExpression='REMOVE running_build_job_id',
            ConditionExpression='running_build_job_id = :job',
            ExpressionAttributeValues={':job': build_job_id},
        )
        return True
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == \
                'ConditionalCheckFailedException':
            return False
        raise


def write_server_state(server: Dict[str, Any],
                       application: ServerStateApplication) -> bool:
    """Persist a ServerStateApplication onto the BuildServers record.
    Conditional on the lifecycle state actually changing, so duplicate
    EventBridge delivery is a no-op. Returns False on the no-op."""
    updates = application.updates
    names: Dict[str, str] = {}
    values: Dict[str, Any] = {}
    sets: List[str] = []
    for index, (key, value) in enumerate(sorted(updates.items())):
        names[f'#s{index}'] = key
        values[f':s{index}'] = value
        sets.append(f'#s{index} = :s{index}')
    expression = 'SET ' + ', '.join(sets)
    if application.clear_pending:
        # The accepted fleet action reached its expected state: the
        # marker is cleared (Req 6.11).
        expression += ' REMOVE pending_action'
    values[':current'] = updates['lifecycle_state']
    try:
        servers_table().update_item(
            Key={'server_id': server['server_id']},
            UpdateExpression=expression,
            ConditionExpression=(
                'attribute_not_exists(lifecycle_state) '
                'OR lifecycle_state <> :current'),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == \
                'ConditionalCheckFailedException':
            return False  # duplicate delivery: state already recorded
        raise


def audit(action: str, resource_id: str, result: str,
          details: Optional[Dict[str, Any]] = None) -> None:
    """Best-effort Audit_Log entry for an event-driven transition; an
    audit failure never fails the event handling."""
    try:
        log_audit_event(
            user_id=SYSTEM_USER,
            action=action,
            resource_type='build_job' if action.startswith('build') else
                          'build_server',
            resource_id=resource_id,
            result=result,
            details=details or {},
        )
    except Exception as e:
        logger.warning(f"Audit_Log write failed for {action}/{resource_id}: {e}")


# =========================================================================
# Job lookup helpers
# =========================================================================

def find_jobs_on_instance(instance_id: str) -> List[Dict]:
    """Build_Jobs actively occupying the given EC2 instance: ephemeral
    jobs whose runner is the instance, and dedicated jobs assigned to
    the fleet server registered with that instance."""
    jobs = [j for j in scan_all(jobs_table())
            if j.get('status') in ACTIVE_ON_COMPUTE_STATUSES]
    matched = [j for j in jobs
               if (j.get('runner') or {}).get('instance_id') == instance_id]
    dedicated = [j for j in jobs
                 if j.get('execution_mode') ==
                 build_domain.EXECUTION_MODE_DEDICATED and j.get('server_id')]
    if dedicated:
        server_ids = {
            s['server_id'] for s in scan_all(servers_table())
            if s.get('instance_id') == instance_id and s.get('server_id')}
        matched.extend(j for j in dedicated
                       if j['server_id'] in server_ids
                       and j not in matched)
    return matched


def find_job_by_command(command_id: str) -> Optional[Dict]:
    """The Build_Job whose agent SSM command has the given command id."""
    for job in scan_all(jobs_table()):
        if (job.get('ssm') or {}).get('command_id') == command_id:
            return job
    return None


def find_server_by_instance(instance_id: str) -> Optional[Dict]:
    """The fleet BuildServers record registered with an EC2 instance."""
    for server in scan_all(servers_table()):
        if server.get('instance_id') == instance_id:
            return server
    return None


def release_if_dedicated(job: Dict[str, Any]) -> None:
    """Free a terminal dedicated Build_Job's server allocation so queue
    promotion is not delayed to the dispatcher tick (Req 7.3)."""
    if job.get('execution_mode') == build_domain.EXECUTION_MODE_DEDICATED \
            and job.get('server_id'):
        release_server(job['server_id'], job['build_job_id'])


# =========================================================================
# Event handlers
# =========================================================================

def handle_phase_event(detail: Dict[str, Any]) -> None:
    """Custom dda.portal.builds phase event from the build agent:
    conditional transitions along the state machine, start/end times,
    verbatim result metadata, publish_partial lists, and the
    build_published audit entry on success (Req 5.1, 5.3, 5.4, 5.5)."""
    build_job_id = detail.get('build_job_id')
    if not build_job_id:
        logger.warning(f"Phase event without build_job_id ignored: {detail}")
        return
    job = get_job(build_job_id)
    if job is None:
        logger.warning(f"Phase event for unknown Build_Job "
                       f"{build_job_id} ignored")
        return

    application = apply_phase_event(job['status'], detail, now_ms())
    if application.is_noop:
        logger.info(f"Phase event '{detail.get('phase')}' for Build_Job "
                    f"{build_job_id} (status '{job['status']}') is a "
                    f"no-op (duplicate/stale delivery)")
        return

    # Execute the conditional chain; any lost race means another writer
    # (duplicate delivery, cancellation, watchdog) already moved the job:
    # stop without side effects (Req 4.1 idempotence).
    for expected, next_ in application.steps[:-1]:
        if not transition_job(build_job_id, expected, next_):
            logger.info(f"Build_Job {build_job_id} moved during phase "
                        f"application ({expected} -> {next_} raced); "
                        f"treating delivery as stale")
            return
    expected, final = application.steps[-1]
    if not transition_job(build_job_id, expected, final,
                          extra=application.updates):
        logger.info(f"Build_Job {build_job_id} moved during phase "
                    f"application ({expected} -> {final} raced); "
                    f"treating delivery as stale")
        return
    logger.info(f"Build_Job {build_job_id}: phase '{detail.get('phase')}' "
                f"applied, status '{job['status']}' -> '{final}'")

    if build_domain.is_terminal(final):
        release_if_dedicated(job)
    if application.audit_action:
        audit(application.audit_action, build_job_id,
              'success' if final == build_domain.STATUS_SUCCEEDED
              else 'failure',
              application.audit_details)


def interrupt_jobs_on_instance(instance_id: str, cause: str) -> None:
    """Mark every non-terminal Build_Job occupying an instance as
    interrupted (Req 3.5): conditional transition per job (terminal jobs
    unchanged — Req 4.1), end time recorded, logs already durable in
    CloudWatch, retry served by build_jobs.py."""
    for job in find_jobs_on_instance(instance_id):
        current = job['status']
        interrupted = build_domain.apply_interruption(current)
        if interrupted == current:
            continue  # terminal: unchanged
        if transition_job(job['build_job_id'], current, interrupted,
                          extra={'ended_at': now_ms()}):
            release_if_dedicated(job)
            audit(AUDIT_BUILD_INTERRUPTED, job['build_job_id'], 'failure',
                  {'instance_id': instance_id, 'cause': cause,
                   'status_at_interruption': current})
            logger.info(f"Build_Job {job['build_job_id']} interrupted "
                        f"({cause}, instance {instance_id})")


def handle_spot_interruption(detail: Dict[str, Any]) -> None:
    """EC2 Spot Instance Interruption Warning: the compute provider is
    reclaiming the runner — every non-terminal Build_Job on it becomes
    interrupted (Req 3.5)."""
    instance_id = detail.get('instance-id')
    if not instance_id:
        return
    interrupt_jobs_on_instance(instance_id, 'spot_interruption')


def handle_instance_state_change(detail: Dict[str, Any]) -> None:
    """EC2 instance state-change: update the fleet BuildServers record
    (lifecycle state, last_state_change_at, pending_action clearing —
    Req 6.2, 6.3, 6.9, 6.11), and interrupt the non-terminal Build_Jobs
    of an instance that went away (Req 3.5)."""
    instance_id = detail.get('instance-id')
    state = detail.get('state')
    if not instance_id or not state:
        return

    server = find_server_by_instance(instance_id)
    if server is not None:
        application = apply_server_state_change(server, state, now_ms())
        if application.changed:
            if write_server_state(server, application):
                logger.info(
                    f"Build server {server['server_id']} lifecycle state "
                    f"'{server.get('lifecycle_state')}' -> '{state}'"
                    + (' (pending action completed)'
                       if application.clear_pending else ''))

    if state in INSTANCE_LOST_STATES:
        interrupt_jobs_on_instance(instance_id, f'instance_{state}')


def handle_ssm_command_status(detail: Dict[str, Any]) -> None:
    """SSM command status change to Failed/TimedOut/Cancelled for a
    job's agent command: fallback transition when the job is still
    building/publishing and no agent terminal phase event arrived
    (design §4) — failed for Failed/TimedOut, interrupted for
    Cancelled."""
    command_id = detail.get('command-id')
    status = detail.get('status')
    if not command_id or status not in SSM_FAILURE_STATUSES:
        return
    job = find_job_by_command(command_id)
    if job is None:
        return

    fallback = decide_ssm_fallback(job['status'], status)
    if not fallback.apply:
        return  # agent outcome already recorded / job not running
    extra: Dict[str, Any] = {'ended_at': now_ms()}
    if fallback.error:
        extra['error'] = fallback.error
    if not transition_job(job['build_job_id'], job['status'],
                          fallback.next_status, extra=extra):
        return  # raced: another writer recorded the outcome (Req 4.1)
    release_if_dedicated(job)
    if fallback.next_status == build_domain.STATUS_INTERRUPTED:
        audit(AUDIT_BUILD_INTERRUPTED, job['build_job_id'], 'failure',
              {'command_id': command_id, 'command_status': status,
               'status_at_interruption': job['status']})
    else:
        audit(AUDIT_BUILD_FAILED, job['build_job_id'], 'failure',
              {'command_id': command_id, 'command_status': status,
               'error_code': (fallback.error or {}).get('code'),
               'status_at_failure': job['status']})
    logger.info(f"Build_Job {job['build_job_id']}: agent command "
                f"{command_id} ended '{status}' -> job "
                f"'{fallback.next_status}' (fallback)")


# =========================================================================
# Lambda entry point
# =========================================================================

def handler(event: Dict, context: Any) -> Dict:
    """Lambda entry point: route one EventBridge event by source and
    detail-type (design §4). Unrecognized events are logged and ignored
    so a rule misconfiguration cannot poison the event queue."""
    source = event.get('source')
    detail_type = event.get('detail-type')
    detail = event.get('detail') or {}
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Unparseable event detail ignored: {detail!r}")
            return {'statusCode': 400}

    try:
        if source == PHASE_EVENT_SOURCE:
            handle_phase_event(detail)
        elif source == EC2_EVENT_SOURCE and \
                detail_type == SPOT_INTERRUPTION_DETAIL_TYPE:
            handle_spot_interruption(detail)
        elif source == EC2_EVENT_SOURCE and \
                detail_type == INSTANCE_STATE_CHANGE_DETAIL_TYPE:
            handle_instance_state_change(detail)
        elif source == SSM_EVENT_SOURCE and \
                detail_type in SSM_COMMAND_DETAIL_TYPES:
            handle_ssm_command_status(detail)
        else:
            logger.info(f"Ignoring event source='{source}' "
                        f"detail-type='{detail_type}'")
        return {'statusCode': 200}
    except Exception as e:
        # EventBridge retries failed deliveries; every transition here is
        # a conditional update, so the retry is safe (Req 4.1).
        logger.exception(f"Build event handling failed: {e}")
        raise
