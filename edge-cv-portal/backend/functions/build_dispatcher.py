"""
Build dispatcher Lambda function (Build_Manager, portal build fleet)

Executes the pure planning decisions of build_planner.py with AWS side
effects. Invoked two ways (design §3):

  - asynchronously by build_jobs.py on submit ({"action": "dispatch",
    "build_job_ids": [...]}), so ephemeral provisioning starts well within
    60 seconds of dispatch (Req 3.1), and
  - by a 1-minute EventBridge schedule, which bounds every "within
    5 minutes" requirement (queue promotion Req 7.3, pre-dispatch
    re-verification Req 7.6, serialization checks Req 7.7) and drives the
    watchdogs.

Both invocations run the same full tick, in order:

  0. Release server allocations held by terminal Build_Jobs so queue
     promotion (oldest queued job first) can happen in the same tick
     (Req 7.3).
  1. Dispatch eligible queued jobs (predecessor null or terminal,
     Req 1.3). Dedicated mode: allocate the selected server with a
     DynamoDB conditional update (attribute_not_exists(
     running_build_job_id) — the authoritative serialization lock,
     Req 7.1, 7.2, 2.2), run the pre-dispatch pgrep SSM verification
     (patterns per .kiro/steering/builds.md), and start the build agent
     via SSM SendCommand only when no build process is found; otherwise
     defer the job to the head of its queue with re-verification at
     >= 5-minute intervals (Req 7.5, 7.6).
  2. Provision ephemeral runners: exactly one RunInstances per dispatched
     job, arch and sizing from the job's own config_snapshot (Req 2.3,
     3.1, 7.4, 9.3); once the instance is SSM-managed (ping Online),
     SendCommand the agent. RunInstances failure -> job failed with the
     provisioning cause, partial compute terminated, audited (Req 3.7).
  3. Runtime timeout watchdog: building/publishing jobs past their
     config_snapshot max runtime -> SSM stop, failed with a timeout
     error, logs retained (Req 3.8).
  4. Serialization watchdog: pgrep build-process count on every server
     with a running job (interval <= 5 min, Req 7.7); count >= 2 ->
     pkill within 60 s, every associated job failed with
     SERIALIZATION_VIOLATION, audited (Req 7.8).
  5. Termination watchdog: terminate the runners of terminal ephemeral
     jobs (target <= 10 min of the terminal status, Req 3.2); failed
     terminations retried at <= 10-minute intervals for up to 1 hour,
     then SNS notification to Portal_Admins + orphaned_runner audit
     entry (Req 3.9).
  6. Queue-orphan sweep (dedicated server stopped/terminated with queued
     jobs -> each failed with a server-state error, audited, Req 7.9)
     and pending-fleet-action deadline sweep (10-minute deadline passed
     -> failure audited, marker cleared, Req 6.11).

Every status transition is a DynamoDB conditional update
(ConditionExpression on the expected current status), so a stale writer
can never resurrect a terminal job or double-transition (Req 4.1).

All planning decisions come from the pure build_planner.py /
build_domain.py modules; this handler only executes them.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates
Requirements: 3.1, 3.2, 3.3, 3.7, 3.8, 3.9, 6.11, 7.1, 7.2, 7.3, 7.4,
7.5, 7.6, 7.7, 7.8, 7.9
"""
import json
import logging
import os
import shlex
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import log_audit_event

# Pure decision modules (no AWS clients): every dispatch/watchdog decision
# this handler acts on comes from build_planner / build_domain.
import build_domain
import build_planner

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
ec2 = boto3.client('ec2')
ssm = boto3.client('ssm')
sns = boto3.client('sns')

# Environment variables (build-fleet-stack.ts lambdaEnvironment)
BUILD_JOBS_TABLE = os.environ.get('BUILD_JOBS_TABLE')
BUILD_SERVERS_TABLE = os.environ.get('BUILD_SERVERS_TABLE')
#: CloudWatch Logs group the build agent streams to (90-day retention set
#: by the infrastructure stack, Req 3.4).
BUILD_LOG_GROUP = os.environ.get('BUILD_LOG_GROUP', '/dda/portal-builds')
#: EventBridge bus the agent emits dda.portal.builds phase events to.
BUILD_EVENT_BUS = os.environ.get('BUILD_EVENT_BUS', 'default')
#: SNS topic for Portal_Admin notifications (orphaned runners, Req 3.9).
BUILD_ALERT_TOPIC_ARN = os.environ.get('BUILD_ALERT_TOPIC_ARN', '')
#: Instance profile attached to build compute (extended dda-build-role:
#: SSM core + events:PutEvents + logs + publish permissions).
BUILD_INSTANCE_PROFILE_ARN = os.environ.get('BUILD_INSTANCE_PROFILE_ARN', '')
BUILD_INSTANCE_PROFILE_NAME = os.environ.get('BUILD_INSTANCE_PROFILE_NAME', '')
#: Optional network placement for ephemeral runners (no inbound rules).
BUILD_SECURITY_GROUP_ID = os.environ.get('BUILD_SECURITY_GROUP_ID', '')
BUILD_SUBNET_ID = os.environ.get('BUILD_SUBNET_ID', '')
#: Repository the runners build from and its on-server clone location.
BUILD_REPO_URL = os.environ.get('BUILD_REPO_URL', '')
BUILD_REPO_DIR = os.environ.get('BUILD_REPO_DIR',
                                '/opt/dda/DefectDetectionApplication')
#: Ubuntu 22.04 AMI resolution: explicit AMI ids win over the public SSM
#: parameters (canonical), per architecture.
BUILD_ARM64_AMI_ID = os.environ.get('BUILD_ARM64_AMI_ID', '')
BUILD_X86_64_AMI_ID = os.environ.get('BUILD_X86_64_AMI_ID', '')
ARM64_AMI_SSM_PARAMETER = os.environ.get(
    'ARM64_AMI_SSM_PARAMETER',
    '/aws/service/canonical/ubuntu/server/22.04/stable/current/arm64/'
    'hvm/ebs-gp2/ami-id')
X86_64_AMI_SSM_PARAMETER = os.environ.get(
    'X86_64_AMI_SSM_PARAMETER',
    '/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/'
    'hvm/ebs-gp2/ami-id')
#: Window within which a synchronous SSM command (pre-dispatch pgrep
#: verification, serialization count, pkill) is polled to completion.
SSM_COMMAND_TIMEOUT_SECONDS = int(os.environ.get(
    'SSM_COMMAND_TIMEOUT_SECONDS', '60'))
#: Safety margin kept from the Lambda's own deadline.
LAMBDA_TIME_RESERVE_MS = 10000

#: Audit_Log actor for dispatcher-initiated events (no requesting user).
SYSTEM_USER = 'system'

# ---------------------------------------------------------------- constants

#: Pre-dispatch verification (Req 7.5): pgrep -af per
#: .kiro/steering/builds.md; output lines are parsed by
#: build_planner.parse_build_processes / build_process_found. `|| true`
#: keeps a no-match exit from failing the SSM invocation.
VERIFY_BUILD_PROCESS_COMMANDS = [
    'pgrep -af "gdk component build" || true',
    'pgrep -af "build-custom.sh" || true',
]

#: Serialization watchdog count (Req 7.7/7.8): per-pattern process counts
#: with machine-readable markers. One healthy build runs BOTH a
#: `gdk component build` process and its `build-custom.sh` child, so the
#: number of concurrent builds is the MAX of the per-pattern counts, not
#: their sum (two concurrent builds -> two gdk processes).
COUNT_BUILD_PROCESS_COMMANDS = [
    'G=$(pgrep -cf "gdk component build" 2>/dev/null || true)',
    'B=$(pgrep -cf "build-custom.sh" 2>/dev/null || true)',
    'echo "GDK_BUILD_COUNT=${G:-0}"',
    'echo "CUSTOM_BUILD_COUNT=${B:-0}"',
]

#: Stop commands (timeout watchdog Req 3.8, serialization violation
#: Req 7.8): kill the build process trees the serialization rules name
#: (.kiro/steering/builds.md) plus the portal agent entry points.
STOP_BUILD_COMMANDS = [
    'pkill -f "gdk component build" || true',
    'pkill -f "build-custom.sh" || true',
    'pkill -f "portal-build.sh" || true',
    'pkill -f "portal-build-agent.sh" || true',
]

#: Terminal SSM command invocation statuses.
SSM_TERMINAL_STATUSES = ('Success', 'Failed', 'TimedOut', 'Cancelled')

#: Error codes recorded on failed Build_Jobs (design Error Handling).
ERROR_PROVISIONING_FAILED = 'PROVISIONING_FAILED'
ERROR_TIMEOUT = 'TIMEOUT'
ERROR_SERIALIZATION_VIOLATION = build_planner.SERIALIZATION_VIOLATION_ERROR
ERROR_SERVER_LOST = 'SERVER_LOST'
ERROR_DISPATCH_FAILED = 'DISPATCH_FAILED'

#: EC2 tag namespace for build compute (IAM condition-keyed, design §10).
TAG_EPHEMERAL = 'dda-build:ephemeral'
TAG_JOB_ID = 'dda-build:job-id'


# ------------------------------------------------------------ pure helpers

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


def parse_build_count(stdout: str) -> Optional[int]:
    """Concurrent-build count from the COUNT_BUILD_PROCESS_COMMANDS
    output: the MAX of the per-pattern counts (one build runs both a gdk
    process and its build-custom.sh child). None when no marker parses —
    unparseable output never drives a stop-all action (fail-safe)."""
    counts: List[int] = []
    for line in (stdout or '').splitlines():
        line = line.strip()
        for marker in ('GDK_BUILD_COUNT=', 'CUSTOM_BUILD_COUNT='):
            if line.startswith(marker):
                try:
                    counts.append(int(line[len(marker):].strip()))
                except ValueError:
                    pass
    if not counts:
        return None
    return max(counts)


def ssm_log_stream(command_id: str, instance_id: str) -> str:
    """CloudWatch Logs stream name SSM CloudWatchOutputConfig writes the
    AWS-RunShellScript stdout of one command invocation to."""
    return f"{command_id}/{instance_id}/aws-runShellScript/stdout"


def agent_command(job: Dict[str, Any]) -> str:
    """Shell command executing the build agent on a Build_Server for one
    Build_Job (design §5: scripts/portal-build-agent.sh with
    BUILD_JOB_ID / BUILD_TARGET / EVENT_BUS / SOURCE_REF)."""
    snapshot = job.get('config_snapshot') or {}
    parts = [
        'bash',
        shlex.quote(f"{BUILD_REPO_DIR}/scripts/portal-build-agent.sh"),
        shlex.quote(f"BUILD_JOB_ID={job['build_job_id']}"),
        shlex.quote(f"BUILD_TARGET={job['build_target']}"),
        shlex.quote(f"EVENT_BUS={BUILD_EVENT_BUS}"),
    ]
    source_ref = snapshot.get('source_ref')
    if source_ref:
        parts.append(shlex.quote(f"SOURCE_REF={source_ref}"))
    return ' '.join(parts)


def agent_execution_timeout_seconds(job: Dict[str, Any]) -> int:
    """SSM executionTimeout for the agent command: the job's own
    config_snapshot max runtime plus a 30-minute margin, capped at the
    SSM maximum (172800 s). The runtime watchdog (Req 3.8) remains the
    authoritative timeout."""
    limit_ms = build_planner.max_runtime_ms(job.get('config_snapshot'))
    return int(min(limit_ms / 1000 + 1800, 172800))


def runner_bootstrap_user_data() -> str:
    """User-data bootstrap for an ephemeral runner: clone the source
    repository and run the build-environment setup
    (setup-build-server.sh: docker, GDK, Python, AWS CLI). A pre-baked
    AMI (BUILD_*_AMI_ID) may make this a no-op re-run; the script is
    idempotent-guarded on the clone."""
    if not BUILD_REPO_URL:
        # No repo URL configured: assume a pre-provisioned AMI.
        return ''
    return '\n'.join([
        '#!/bin/bash',
        'set -uo pipefail',
        'export DEBIAN_FRONTEND=noninteractive',
        f'REPO_DIR={shlex.quote(BUILD_REPO_DIR)}',
        f'REPO_URL={shlex.quote(BUILD_REPO_URL)}',
        'apt-get update -y && apt-get install -y git',
        'mkdir -p "$(dirname "$REPO_DIR")"',
        'if [ ! -d "$REPO_DIR/.git" ]; then',
        '  git clone "$REPO_URL" "$REPO_DIR"',
        'fi',
        'cd "$REPO_DIR"',
        'bash ./setup-build-server.sh',
        '',
    ])


# ------------------------------------------------------------- persistence

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


def transition_job(build_job_id: str, expected_status: str,
                   new_status: str,
                   extra: Optional[Dict[str, Any]] = None) -> bool:
    """Conditionally transition a Build_Job's status (ConditionExpression
    on the expected current status, Req 4.1: terminal jobs are never
    resurrected, duplicate transitions are no-ops). ``extra`` carries
    additional top-level attributes to SET. Returns False when the job
    moved in the meantime."""
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


def update_job_fields(build_job_id: str, fields: Dict[str, Any]) -> None:
    """Unconditional SET of non-status Build_Job bookkeeping attributes
    (runner info, ssm markers, deferred_at)."""
    names = {}
    values = {}
    sets = []
    for index, (key, value) in enumerate(sorted(fields.items())):
        names[f'#f{index}'] = key
        values[f':f{index}'] = value
        sets.append(f'#f{index} = :f{index}')
    jobs_table().update_item(
        Key={'build_job_id': build_job_id},
        UpdateExpression='SET ' + ', '.join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def allocate_server(server_id: str, build_job_id: str) -> bool:
    """Take a Dedicated_Build_Server's single running slot for a
    Build_Job: conditional update on attribute_not_exists(
    running_build_job_id) — the authoritative serialization lock
    (Req 7.1, 7.2). Returns False when the server is already allocated."""
    try:
        servers_table().update_item(
            Key={'server_id': server_id},
            UpdateExpression='SET running_build_job_id = :job',
            ConditionExpression=(
                'attribute_not_exists(running_build_job_id) '
                'OR running_build_job_id = :none'),
            ExpressionAttributeValues={':job': build_job_id, ':none': None},
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
    slot another job took)."""
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


def clear_pending_action(server_id: str) -> None:
    """Remove a server's pending_action marker (deadline sweep, Req 6.11)"""
    servers_table().update_item(
        Key={'server_id': server_id},
        UpdateExpression='REMOVE pending_action',
    )


def audit(action: str, resource_id: str, result: str,
          details: Optional[Dict[str, Any]] = None) -> None:
    """Best-effort Audit_Log entry for a dispatcher-initiated event; an
    audit failure never fails the tick."""
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


# -------------------------------------------------------------- SSM helpers

def send_shell_command(instance_id: str, commands: List[str],
                       cloudwatch: bool = False,
                       execution_timeout: Optional[int] = None) -> str:
    """SSM SendCommand (AWS-RunShellScript) returning the command id.
    ``cloudwatch`` enables CloudWatchOutputConfig streaming to the build
    log group (agent commands, Req 3.4/4.4)."""
    parameters: Dict[str, Any] = {'commands': commands}
    if execution_timeout is not None:
        parameters['executionTimeout'] = [str(execution_timeout)]
    kwargs: Dict[str, Any] = {
        'InstanceIds': [instance_id],
        'DocumentName': 'AWS-RunShellScript',
        'Parameters': parameters,
    }
    if cloudwatch:
        kwargs['CloudWatchOutputConfig'] = {
            'CloudWatchLogGroupName': BUILD_LOG_GROUP,
            'CloudWatchOutputEnabled': True,
        }
    response = ssm.send_command(**kwargs)
    return response['Command']['CommandId']


def wait_for_command(command_id: str, instance_id: str,
                     deadline: float) -> Optional[Dict]:
    """Poll one SSM command invocation to a terminal status before the
    deadline; returns the invocation or None on window expiry."""
    while time.time() < deadline:
        try:
            invocation = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id)
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') == \
                    'InvocationDoesNotExist':
                time.sleep(1)  # registration lag right after send_command
                continue
            raise
        if invocation.get('Status') in SSM_TERMINAL_STATUSES:
            return invocation
        time.sleep(min(3.0, max(1.0, deadline - time.time())))
    return None


def run_shell_sync(instance_id: str,
                   commands: List[str]) -> Optional[str]:
    """Run a short shell command on a Build_Server and return its stdout,
    or None when the command could not be positively completed
    (fail-safe: callers treat None as 'not verified')."""
    deadline = time.time() + SSM_COMMAND_TIMEOUT_SECONDS
    try:
        command_id = send_shell_command(instance_id, commands)
        invocation = wait_for_command(command_id, instance_id, deadline)
    except ClientError as e:
        logger.warning(f"SSM command on {instance_id} failed: {e}")
        return None
    if invocation and invocation.get('Status') == 'Success':
        return invocation.get('StandardOutputContent') or ''
    return None


def instance_ssm_online(instance_id: str) -> bool:
    """True iff the instance is SSM-managed and pinging Online (ephemeral
    runner readiness gate before the agent SendCommand)."""
    try:
        response = ssm.describe_instance_information(
            Filters=[{'Key': 'InstanceIds', 'Values': [instance_id]}])
    except ClientError as e:
        logger.warning(f"DescribeInstanceInformation({instance_id}): {e}")
        return False
    for info in response.get('InstanceInformationList', []):
        if info.get('InstanceId') == instance_id:
            return info.get('PingStatus') == 'Online'
    return False


def send_agent(job: Dict[str, Any], instance_id: str) -> Tuple[str, str]:
    """SendCommand the build agent for one Build_Job with CloudWatch log
    streaming; returns (command_id, log_stream)."""
    command_id = send_shell_command(
        instance_id,
        [agent_command(job)],
        cloudwatch=True,
        execution_timeout=agent_execution_timeout_seconds(job),
    )
    return command_id, ssm_log_stream(command_id, instance_id)


# -------------------------------------------------------------- EC2 helpers

_AMI_CACHE: Dict[str, str] = {}


def resolve_ami(arch: str) -> str:
    """Ubuntu 22.04 AMI id for a runner architecture: explicit env AMI id
    when set, otherwise the public canonical SSM parameter (cached per
    invocation container)."""
    explicit = BUILD_ARM64_AMI_ID if arch == build_domain.ARCH_ARM64 \
        else BUILD_X86_64_AMI_ID
    if explicit:
        return explicit
    if arch in _AMI_CACHE:
        return _AMI_CACHE[arch]
    parameter = ARM64_AMI_SSM_PARAMETER if arch == build_domain.ARCH_ARM64 \
        else X86_64_AMI_SSM_PARAMETER
    value = ssm.get_parameter(Name=parameter)['Parameter']['Value']
    _AMI_CACHE[arch] = value
    return value


def run_runner_instance(plan: 'build_planner.RunnerPlan') -> str:
    """RunInstances for exactly one Ephemeral_Build_Runner serving exactly
    one Build_Job (Req 2.3, 7.4): arch-selected Ubuntu 22.04 AMI, sizing
    from the job's config_snapshot (Req 3.1, 9.3), hardened profile (SSM
    instance profile, no key pair, IMDSv2 required), dda-build tags."""
    kwargs: Dict[str, Any] = {
        'ImageId': resolve_ami(plan.arch),
        'InstanceType': plan.instance_type,
        'MinCount': 1,
        'MaxCount': 1,
        'BlockDeviceMappings': [{
            'DeviceName': '/dev/sda1',
            'Ebs': {
                'VolumeSize': int(plan.volume_size_gb),
                'VolumeType': 'gp3',
                'DeleteOnTermination': True,
            },
        }],
        'MetadataOptions': {'HttpTokens': 'required',
                            'HttpEndpoint': 'enabled'},
        'TagSpecifications': [{
            'ResourceType': 'instance',
            'Tags': [
                {'Key': TAG_EPHEMERAL, 'Value': 'true'},
                {'Key': TAG_JOB_ID, 'Value': plan.build_job_id},
                {'Key': 'Name',
                 'Value': f'dda-build-runner-{plan.build_job_id[:8]}'},
            ],
        }],
    }
    if BUILD_INSTANCE_PROFILE_ARN:
        kwargs['IamInstanceProfile'] = {'Arn': BUILD_INSTANCE_PROFILE_ARN}
    elif BUILD_INSTANCE_PROFILE_NAME:
        kwargs['IamInstanceProfile'] = {'Name': BUILD_INSTANCE_PROFILE_NAME}
    if BUILD_SECURITY_GROUP_ID:
        kwargs['SecurityGroupIds'] = [BUILD_SECURITY_GROUP_ID]
    if BUILD_SUBNET_ID:
        kwargs['SubnetId'] = BUILD_SUBNET_ID
    user_data = runner_bootstrap_user_data()
    if user_data:
        kwargs['UserData'] = user_data
    if plan.spot:
        kwargs['InstanceMarketOptions'] = {
            'MarketType': 'spot',
            'SpotOptions': {'SpotInstanceType': 'one-time',
                            'InstanceInterruptionBehavior': 'terminate'},
        }
    response = ec2.run_instances(**kwargs)
    return response['Instances'][0]['InstanceId']


def terminate_partial_compute(build_job_id: str) -> List[str]:
    """Terminate any compute tagged for a Build_Job (partial provisioning
    cleanup on a RunInstances failure, Req 3.7). Returns the terminated
    instance ids (best effort)."""
    try:
        response = ec2.describe_instances(Filters=[
            {'Name': f'tag:{TAG_JOB_ID}', 'Values': [build_job_id]},
            {'Name': 'instance-state-name',
             'Values': ['pending', 'running', 'stopping', 'stopped']},
        ])
    except ClientError as e:
        logger.warning(f"Partial-compute lookup for {build_job_id}: {e}")
        return []
    instance_ids = [
        instance['InstanceId']
        for reservation in response.get('Reservations', [])
        for instance in reservation.get('Instances', [])
    ]
    if instance_ids:
        try:
            ec2.terminate_instances(InstanceIds=instance_ids)
        except ClientError as e:
            logger.warning(
                f"Partial-compute termination for {build_job_id}: {e}")
    return instance_ids


# ---------------------------------------------------------- job resolution

def job_instance_id(job: Dict[str, Any],
                    servers_by_id: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """EC2 instance a Build_Job's build executes on: the ephemeral
    runner's instance, or the dedicated server's registered instance."""
    if job.get('execution_mode') == build_domain.EXECUTION_MODE_EPHEMERAL:
        return (job.get('runner') or {}).get('instance_id')
    server = servers_by_id.get(job.get('server_id') or '')
    return (server or {}).get('instance_id')


def fail_job(job: Dict[str, Any], error_code: str, message: str,
             audit_action: str,
             details: Optional[Dict[str, Any]] = None) -> bool:
    """Conditionally mark a Build_Job failed with an error record and end
    time, then audit the failure. Returns False when the job moved in the
    meantime (the failure then belongs to whoever moved it)."""
    moved = transition_job(
        job['build_job_id'], job['status'], build_domain.STATUS_FAILED,
        extra={
            'error': {'code': error_code, 'message': message},
            'ended_at': now_ms(),
        })
    if moved:
        audit(audit_action, job['build_job_id'], 'failure',
              {**(details or {}),
               'error_code': error_code,
               'message': message,
               'status_at_failure': job['status']})
    return moved


# ----------------------------------------------- step 0: allocation release

def release_stale_allocations(jobs_by_id: Dict[str, Dict[str, Any]],
                              servers: List[Dict[str, Any]]) -> None:
    """Release server allocations held by terminal (or vanished)
    Build_Jobs so the oldest queued job for the server can be promoted in
    the regular dispatch step within the 5-minute bound (Req 7.3)."""
    for server in servers:
        held_by = server.get('running_build_job_id')
        if not held_by:
            continue
        job = jobs_by_id.get(held_by)
        if job is None or build_domain.is_terminal(job.get('status', '')):
            if release_server(server['server_id'], held_by):
                server['running_build_job_id'] = None
                logger.info(
                    f"Released server {server['server_id']} allocation "
                    f"held by terminal Build_Job {held_by}")


# --------------------------------------------- step 1: dedicated dispatch

def verify_and_start_dedicated(job: Dict[str, Any],
                               server: Dict[str, Any],
                               now: int) -> None:
    """Pre-dispatch verification + agent start for one dedicated
    Build_Job that holds its server's allocation (Req 7.5, 7.6).

    Re-verification only when the 5-minute retry interval has elapsed
    since the last deferral (build_planner.is_reverification_due). The
    pgrep output drives build_planner.decide_predispatch: clean -> queued
    -> building transition + agent SendCommand; a build process found (or
    an unverifiable command, fail-safe) -> the job stays queued at the
    head of its queue (original created_at retained) with deferred_at
    recording this attempt.
    """
    if not build_planner.is_reverification_due(job.get('deferred_at'), now):
        return
    instance_id = server.get('instance_id')
    if not instance_id:
        logger.warning(
            f"Dedicated server {server.get('server_id')} has no "
            f"instance_id; deferring Build_Job {job['build_job_id']}")
        update_job_fields(job['build_job_id'], {'deferred_at': now})
        return

    output = run_shell_sync(instance_id, VERIFY_BUILD_PROCESS_COMMANDS)
    if output is None:
        # Verification not positively completed: never start on unknown
        # server state (fail closed against Req 7.5); retry next window.
        update_job_fields(job['build_job_id'], {'deferred_at': now})
        return

    decision = build_planner.decide_predispatch(job, output, now)
    if decision.action == build_planner.PREDISPATCH_DEFER:
        # Return to the head of the queue: status stays queued, the
        # ORIGINAL created_at is retained (Req 7.6); the allocation is
        # kept so no other job can slip onto the busy server.
        update_job_fields(job['build_job_id'],
                          {'deferred_at': decision.deferred_at})
        logger.info(
            f"Deferred Build_Job {job['build_job_id']}: build process "
            f"running on server {server['server_id']}: "
            f"{list(decision.build_processes)}")
        return

    # Clean verification: queued -> building (Req 7.5) + agent dispatch.
    if not transition_job(
            job['build_job_id'], build_domain.STATUS_QUEUED,
            build_domain.next_status(build_domain.STATUS_QUEUED,
                                     build_domain.EVENT_DISPATCH_DEDICATED),
            extra={'dispatched_at': now, 'started_at': now}):
        return  # raced (e.g. cancellation); allocation release next tick
    try:
        command_id, log_stream = send_agent(job, instance_id)
    except ClientError as e:
        logger.error(f"Agent SendCommand for Build_Job "
                     f"{job['build_job_id']} on {instance_id} failed: {e}")
        job = dict(job, status=build_domain.STATUS_BUILDING)
        fail_job(job, ERROR_DISPATCH_FAILED,
                 f"The build agent could not be started on "
                 f"Dedicated_Build_Server '{server.get('name') or server['server_id']}': {e}",
                 'build_dispatch_failed',
                 {'server_id': server['server_id']})
        release_server(server['server_id'], job['build_job_id'])
        return
    update_job_fields(job['build_job_id'], {
        'ssm': {**(job.get('ssm') or {}), 'command_id': command_id},
        'log': {'group': BUILD_LOG_GROUP, 'stream': log_stream},
    })
    logger.info(f"Dispatched Build_Job {job['build_job_id']} to dedicated "
                f"server {server['server_id']} (command {command_id})")


def dispatch_dedicated(jobs: List[Dict[str, Any]],
                       servers: List[Dict[str, Any]],
                       now: int) -> None:
    """Dispatch eligible queued dedicated Build_Jobs (Req 1.3, 2.2, 7.1,
    7.2, 7.5, 7.6): allocation lock first, pre-dispatch verification
    second, agent start last."""
    servers_by_id = {s['server_id']: s for s in servers if s.get('server_id')}

    # Jobs already holding their server's allocation from an earlier
    # deferral resume at the verification step.
    held_job_ids = set()
    for job in build_planner.eligible_queued_jobs(jobs):
        if job.get('execution_mode') != build_domain.EXECUTION_MODE_DEDICATED:
            continue
        server = servers_by_id.get(job.get('server_id') or '')
        if server and server.get('running_build_job_id') == \
                job['build_job_id']:
            held_job_ids.add(job['build_job_id'])
            verify_and_start_dedicated(job, server, now)

    # Fresh allocations for the remaining eligible jobs: the pure planner
    # decides start-vs-queue per server (at most one start per server per
    # plan); the conditional update is the authoritative lock (Req 7.1).
    remaining = [j for j in jobs if j.get('build_job_id') not in held_job_ids]
    for decision in build_planner.plan_dedicated_dispatch(remaining, servers):
        if decision.action != build_planner.ALLOCATION_START:
            continue  # occupied server: the job stays queued (Req 7.2)
        server = servers_by_id[decision.server_id]
        if server.get('lifecycle_state') != build_domain.SERVER_STATE_RUNNING:
            continue  # not startable now; dead-server sweep handles 7.9
        if not allocate_server(decision.server_id, decision.build_job_id):
            continue  # lost the race for the slot: stays queued (Req 7.2)
        server['running_build_job_id'] = decision.build_job_id
        job = next(j for j in remaining
                   if j.get('build_job_id') == decision.build_job_id)
        verify_and_start_dedicated(job, server, now)


# --------------------------------------------- step 2: ephemeral provision

def fail_provisioning(job: Dict[str, Any], cause: str) -> None:
    """RunInstances (or dispatch) failure for an ephemeral Build_Job:
    failed with the provisioning cause, partial compute terminated,
    audited (Req 3.7)."""
    terminated = terminate_partial_compute(job['build_job_id'])
    fail_job(job, ERROR_PROVISIONING_FAILED,
             f"Provisioning the Ephemeral_Build_Runner failed: {cause}",
             'build_provisioning_failed',
             {'terminated_partial_compute': terminated})


def provision_ephemeral(jobs: List[Dict[str, Any]], now: int) -> None:
    """Provision Ephemeral_Build_Runners for the dispatch-eligible queued
    ephemeral Build_Jobs: exactly one runner per job, sizing from the
    job's own config_snapshot (Req 2.3, 3.1, 7.4, 9.3), and start the
    agent on runners of provisioning jobs once SSM-managed."""
    for plan in build_planner.plan_ephemeral_provisioning(jobs):
        if not transition_job(
                plan.build_job_id, build_domain.STATUS_QUEUED,
                plan.status,  # provisioning (Req 3.1)
                extra={'dispatched_at': now}):
            continue  # raced (e.g. cancellation)
        job = next(j for j in jobs
                   if j.get('build_job_id') == plan.build_job_id)
        job = dict(job, status=plan.status)
        try:
            instance_id = run_runner_instance(plan)
        except (ClientError, ValueError) as e:
            fail_provisioning(job, str(e))
            continue
        update_job_fields(plan.build_job_id, {
            'runner': {
                'instance_id': instance_id,
                'instance_type': plan.instance_type,
                'arch': plan.arch,
                'spot': plan.spot,
                'terminate_attempts': 0,
                'terminate_first_failed_at': None,
            },
        })
        logger.info(f"Provisioning runner {instance_id} "
                    f"({plan.arch}/{plan.instance_type}) for Build_Job "
                    f"{plan.build_job_id}")

    # Runners provisioned on earlier ticks: SSM ping, then the agent
    # SendCommand exactly once (ssm.command_id records the send).
    for job in jobs:
        if job.get('status') != build_domain.STATUS_PROVISIONING:
            continue
        if job.get('execution_mode') != build_domain.EXECUTION_MODE_EPHEMERAL:
            continue
        if (job.get('ssm') or {}).get('command_id'):
            continue  # agent already dispatched
        instance_id = (job.get('runner') or {}).get('instance_id')
        if not instance_id or not instance_ssm_online(instance_id):
            continue  # not SSM-managed yet; next tick
        try:
            command_id, log_stream = send_agent(job, instance_id)
        except ClientError as e:
            logger.error(f"Agent SendCommand for ephemeral Build_Job "
                         f"{job['build_job_id']} on {instance_id} "
                         f"failed: {e}")
            fail_provisioning(job, f"the build agent could not be "
                                   f"started on the runner: {e}")
            continue
        update_job_fields(job['build_job_id'], {
            'ssm': {**(job.get('ssm') or {}), 'command_id': command_id},
            'log': {'group': BUILD_LOG_GROUP, 'stream': log_stream},
        })
        logger.info(f"Started agent on runner {instance_id} for Build_Job "
                    f"{job['build_job_id']} (command {command_id})")


# ------------------------------------------------ step 3: runtime timeout

def runtime_timeout_watchdog(jobs: List[Dict[str, Any]],
                             servers_by_id: Dict[str, Dict[str, Any]],
                             now: int) -> None:
    """Fail running Build_Jobs past their config_snapshot max runtime:
    SSM stop of the build processes, failed with a timeout error, logs
    retained in CloudWatch (Req 3.8)."""
    for job in jobs:
        if job.get('status') not in build_planner.RUNNING_WATCHDOG_STATUSES:
            continue
        decision = build_planner.decide_runtime_timeout(job, now)
        if not decision.timed_out:
            continue
        instance_id = job_instance_id(job, servers_by_id)
        if instance_id:
            try:
                send_shell_command(instance_id, STOP_BUILD_COMMANDS)
            except ClientError as e:
                logger.warning(f"Timeout stop for Build_Job "
                               f"{job['build_job_id']} on {instance_id}: {e}")
        fail_job(job, ERROR_TIMEOUT, decision.error or 'Build timed out.',
                 'build_timeout', {'instance_id': instance_id})
        if job.get('execution_mode') == build_domain.EXECUTION_MODE_DEDICATED \
                and job.get('server_id'):
            release_server(job['server_id'], job['build_job_id'])


# -------------------------------------------- step 4: serialization check

def serialization_watchdog(jobs: List[Dict[str, Any]],
                           servers_by_id: Dict[str, Dict[str, Any]],
                           now: int) -> None:
    """Concurrent-build detection on every Build_Server with a running
    Build_Job, at intervals <= 5 minutes (Req 7.7): pgrep count via SSM;
    count >= 2 -> pkill every build process within 60 s, every associated
    Build_Job failed with SERIALIZATION_VIOLATION, audited (Req 7.8)."""
    running = [j for j in jobs
               if j.get('status') in build_planner.RUNNING_WATCHDOG_STATUSES]
    for job in running:
        last = (job.get('ssm') or {}).get('last_serialization_check_at')
        if not build_planner.is_serialization_check_due(last, now):
            continue
        instance_id = job_instance_id(job, servers_by_id)
        if not instance_id:
            continue
        output = run_shell_sync(instance_id, COUNT_BUILD_PROCESS_COMMANDS)
        update_job_fields(job['build_job_id'], {
            'ssm': {**(job.get('ssm') or {}),
                    'last_serialization_check_at': now},
        })
        count = parse_build_count(output) if output is not None else None
        if count is None:
            continue  # not positively counted; re-check next window
        server_key = job.get('server_id') or instance_id
        associated = [j['build_job_id'] for j in running
                      if job_instance_id(j, servers_by_id) == instance_id]
        decision = build_planner.decide_serialization_violation(
            server_key, count, associated)
        if not decision.violation:
            continue
        # Stop every detected build process within 60 s of detection.
        try:
            send_shell_command(instance_id, STOP_BUILD_COMMANDS,
                               execution_timeout=(
                build_planner.SERIALIZATION_STOP_WINDOW_SECONDS))
        except ClientError as e:
            logger.error(f"Serialization pkill on {instance_id}: {e}")
        for failed_id in decision.failed_job_ids:
            failed_job = next((j for j in running
                               if j['build_job_id'] == failed_id), None)
            if failed_job is None:
                continue
            fail_job(failed_job, ERROR_SERIALIZATION_VIOLATION,
                     f"Two or more build processes were detected running "
                     f"concurrently on Build_Server '{server_key}' "
                     f"({decision.process_count} builds); every build "
                     f"process was stopped.",
                     'build_serialization_violation',
                     {'server': server_key,
                      'instance_id': instance_id,
                      'process_count': decision.process_count})
            if failed_job.get('execution_mode') == \
                    build_domain.EXECUTION_MODE_DEDICATED \
                    and failed_job.get('server_id'):
                release_server(failed_job['server_id'], failed_id)


# ------------------------------------------- step 5: runner termination

def notify_orphaned_runner(job: Dict[str, Any], instance_id: str) -> None:
    """SNS notification to Portal_Admins + orphaned_runner Audit_Log
    entry when the 1-hour termination retry window is exhausted
    (Req 3.9)."""
    message = (
        f"Ephemeral build runner {instance_id} for Build_Job "
        f"{job['build_job_id']} could not be terminated within 1 hour of "
        f"the first termination failure. Manual cleanup is required.")
    if BUILD_ALERT_TOPIC_ARN:
        try:
            sns.publish(TopicArn=BUILD_ALERT_TOPIC_ARN,
                        Subject='DDA portal build: orphaned build runner',
                        Message=message)
        except ClientError as e:
            logger.error(f"Orphaned-runner SNS publish failed: {e}")
    else:
        logger.error(f"BUILD_ALERT_TOPIC_ARN unset; orphaned runner "
                     f"notification not sent: {message}")
    audit('orphaned_runner', job['build_job_id'], 'failure',
          {'instance_id': instance_id, 'message': message})


def termination_watchdog(jobs: List[Dict[str, Any]], now: int) -> None:
    """Terminate the Ephemeral_Build_Runner of every terminal ephemeral
    Build_Job (within 10 minutes of the terminal status via the 1-minute
    tick, Req 3.2; logs stay in CloudWatch, Req 3.4). Failed terminations
    are retried at <= 10-minute intervals for up to 1 hour since the
    first failure; when the window is exhausted Portal_Admins are
    notified and the failure audited (Req 3.9)."""
    for job in jobs:
        if job.get('execution_mode') != build_domain.EXECUTION_MODE_EPHEMERAL:
            continue
        if not build_domain.is_terminal(job.get('status', '')):
            continue
        runner = job.get('runner') or {}
        instance_id = runner.get('instance_id')
        if not instance_id or runner.get('terminated_at') \
                or runner.get('orphan_notified'):
            continue

        first_failed_at = runner.get('terminate_first_failed_at')
        if first_failed_at is not None:
            decision = build_planner.decide_termination_retry(
                first_failed_at,
                runner.get('terminate_last_attempt_at'),
                now,
                already_notified=bool(runner.get('orphan_notified')))
            if decision.notify_orphaned:
                notify_orphaned_runner(job, instance_id)
                update_job_fields(job['build_job_id'], {
                    'runner': {**runner, 'orphan_notified': True}})
                continue
            if not decision.retry:
                continue

        try:
            ec2.terminate_instances(InstanceIds=[instance_id])
            update_job_fields(job['build_job_id'], {
                'runner': {**runner, 'terminated_at': now}})
            logger.info(f"Terminated runner {instance_id} of terminal "
                        f"Build_Job {job['build_job_id']}")
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code', '')
            if code in ('InvalidInstanceID.NotFound',
                        'InvalidInstanceID.Malformed'):
                # Already gone: nothing left to terminate.
                update_job_fields(job['build_job_id'], {
                    'runner': {**runner, 'terminated_at': now}})
                continue
            logger.warning(f"Runner termination failed for {instance_id}: {e}")
            update_job_fields(job['build_job_id'], {
                'runner': {**runner,
                           'terminate_attempts':
                               int(runner.get('terminate_attempts') or 0) + 1,
                           'terminate_first_failed_at':
                               first_failed_at or now,
                           'terminate_last_attempt_at': now}})


# ----------------------------------------------------- step 6: sweeps

def dead_server_sweep(jobs: List[Dict[str, Any]],
                      servers: List[Dict[str, Any]]) -> None:
    """Fail the queued Build_Jobs of every stopped/terminated
    Dedicated_Build_Server with a server-state error, audited (Req 7.9)."""
    for server in servers:
        decision = build_planner.sweep_dead_server(server, jobs)
        if not decision.sweep:
            continue
        for failed_id in decision.failed_job_ids:
            job = next((j for j in jobs
                        if j.get('build_job_id') == failed_id), None)
            if job is None:
                continue
            fail_job(job, ERROR_SERVER_LOST,
                     decision.error or 'The selected Dedicated_Build_Server '
                                       'is no longer available.',
                     'build_queue_orphaned',
                     {'server_id': decision.server_id,
                      'lifecycle_state': decision.lifecycle_state})


def pending_action_sweep(servers: List[Dict[str, Any]], now: int) -> None:
    """Fail every pending fleet action past its 10-minute deadline: the
    failure (action, server, current lifecycle state) is audited and the
    marker cleared so the fleet UI surfaces the error (Req 6.11)."""
    for server in servers:
        pending = server.get('pending_action')
        if not pending or not isinstance(pending, dict):
            continue
        if pending.get('deadline') is None \
                and pending.get('initiated_at') is None \
                and pending.get('requested_at') is None:
            continue
        marker = dict(pending)
        if marker.get('deadline') is None and \
                marker.get('initiated_at') is None:
            marker['initiated_at'] = marker.get('requested_at')
        decision = build_planner.decide_pending_action(marker, server, now)
        if not decision.failed:
            continue
        audit('fleet_action_failed', str(decision.server_id), 'failure',
              {'action': decision.action,
               'lifecycle_state': decision.lifecycle_state,
               'error': decision.error,
               'requested_by': pending.get('requested_by')})
        clear_pending_action(server['server_id'])
        logger.error(decision.error)


# ------------------------------------------------------------------- tick

def run_tick(now: Optional[int] = None) -> Dict[str, Any]:
    """One full dispatcher tick over the current BuildJobs/BuildServers
    state, in the design §3 order."""
    now = now if now is not None else now_ms()
    jobs = scan_all(jobs_table())
    servers = scan_all(servers_table())
    jobs_by_id = {j.get('build_job_id'): j for j in jobs
                  if j.get('build_job_id')}
    servers_by_id = {s.get('server_id'): s for s in servers
                     if s.get('server_id')}

    # 0. Queue promotion enabler: free slots held by terminal jobs (7.3).
    release_stale_allocations(jobs_by_id, servers)
    # 1. Dispatch eligible queued dedicated jobs (1.3, 2.2, 7.1-7.6).
    dispatch_dedicated(jobs, servers, now)
    # 2. Provision ephemeral runners + start agents (2.3, 3.1, 3.7, 7.4).
    provision_ephemeral(jobs, now)
    # 3. Runtime timeout watchdog (3.8).
    runtime_timeout_watchdog(jobs, servers_by_id, now)
    # 4. Serialization watchdog (7.7, 7.8).
    serialization_watchdog(jobs, servers_by_id, now)
    # 5. Termination watchdog (3.2, 3.9).
    termination_watchdog(jobs, now)
    # 6. Queue-orphan + pending-action-deadline sweeps (7.9, 6.11).
    dead_server_sweep(jobs, servers)
    pending_action_sweep(servers, now)

    return {'jobs': len(jobs), 'servers': len(servers)}


def handler(event: Dict, context: Any) -> Dict:
    """Lambda entry point. Both invocation shapes run the same full tick:

    - {"action": "dispatch", "build_job_ids": [...]}: the async on-submit
      invoke from build_jobs.py (immediate dispatch, Req 3.1);
    - the EventBridge 1-minute scheduled event (queue promotion,
      re-verification, and every watchdog, Req 7.3/7.6/7.7).
    """
    try:
        summary = run_tick()
        logger.info(f"Dispatcher tick complete: {json.dumps(summary)}")
        return {'statusCode': 200, 'body': json.dumps(summary)}
    except Exception as e:
        # The 1-minute schedule retries the tick; failures must not
        # poison the async invoke queue with Lambda retries of stale
        # state, so the error is logged and swallowed.
        logger.exception(f"Dispatcher tick failed: {e}")
        return {'statusCode': 500, 'body': json.dumps({'error': str(e)})}
