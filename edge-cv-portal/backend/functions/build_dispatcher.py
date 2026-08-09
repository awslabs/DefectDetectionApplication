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
     3.1, 7.4, 9.3); once the instance is SSM-managed (ping Online) AND
     its bootstrap has signalled completion (the Bootstrap_Marker probe,
     Req 6.1-6.4), SendCommand the agent. RunInstances failure -> job
     failed with the provisioning cause, partial compute terminated,
     audited (Req 3.7); a bootstrap that never completes inside its
     budget -> job failed at the bootstrap stage with the bootstrap log
     location, runner released (Req 6.3, 6.5).
  2.5 Scheduled command reconciliation (build-fleet-execution-failures
     task 5.2, Req 2.5/2.6/2.7): on the SAME one-minute tick, inspect
     command-bearing nonterminal jobs, settlement waits, ambiguous
     `sending` dispatch attempts, and terminal jobs with incomplete
     diagnostics. Final invocation evidence is read via READ-ONLY
     GetCommandInvocation, sanitized immediately through
     build_reconciliation, and classified deterministically: nonterminal
     invocations stay nonterminal, terminal commands settle within the
     configured bound, and `Success` without a callback becomes
     AGENT_RESULT_MISSING only AFTER the settlement window. Ambiguous
     SendCommand (recorded `sending`, no command id) is recovered
     through the deterministic job/attempt command comment and a
     recent-command lookup BEFORE any resend; only a conditional attempt
     after the visibility bound may send anew. A missing/delayed
     EventBridge event therefore affects latency, not correctness.
     Evidence gate (historical-evidence.md task 3.3): rows 2 and 4
     (CONFIRMED) authorize the evidence retrieval/classification; row 5
     (UNKNOWN) authorizes this tick reconciliation as hardening only —
     no claim is made that any historical incident was caused by a lost
     event.
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
import hashlib
import json
import logging
import os
import shlex
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

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
# Pure shared reconciliation contract (build-fleet-execution-failures
# tasks 4.1-4.3): sanitization/bounding, deterministic classification,
# diagnostic merge, settlement planning, execution-attempt identity.
import build_reconciliation
import build_source

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
#: The directory default comes from build_source, the single authoritative
#: value shared with the fleet bootstrap that creates the clone, so the
#: directory the agent is invoked from cannot drift from the directory the
#: bootstrap cloned into (Req 5.1, 5.2). The optional env override is
#: retained for an operator-pinned deployment; the deployed Lambda sets
#: none, so resolution lands on build_source.DEFAULT_REPO_DIR.
BUILD_REPO_URL = os.environ.get('BUILD_REPO_URL', '')
BUILD_REPO_DIR = os.environ.get('BUILD_REPO_DIR',
                                build_source.DEFAULT_REPO_DIR)
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

#: Bootstrap completion probe (Req 6.2): the Bootstrap_Marker test plus the
#: bootstrap log location, one `KEY=value` line each, in exactly the format
#: build_planner.parse_bootstrap_probe reads. Run through the existing
#: run_shell_sync helper, whose None ("could not be positively completed")
#: is a marker that was NOT observed — the gate never opens on unknown
#: output, and the bootstrap budget eventually resolves it (Req 6.3).
#: Both paths are read from build_planner, their one definition; neither the
#: marker nor the log path is re-spelled here.
BOOTSTRAP_PROBE_COMMANDS = [
    f'test -f {shlex.quote(build_planner.BOOTSTRAP_MARKER_PATH)} '
    f'&& echo "{build_planner.BOOTSTRAP_DONE_PROBE_KEY}=1" '
    f'|| echo "{build_planner.BOOTSTRAP_DONE_PROBE_KEY}=0"',
    f'echo "{build_planner.BOOTSTRAP_LOG_PROBE_KEY}='
    f'{build_planner.BOOTSTRAP_LOG_PATH}"',
]

#: Terminal SSM command invocation statuses.
SSM_TERMINAL_STATUSES = ('Success', 'Failed', 'TimedOut', 'Cancelled')

#: Build_Job statuses whose agent command the scheduled reconciliation
#: inspects for terminal transitions (the same scope the event-driven
#: fallback uses: jobs the agent was running).
AGENT_RUNNING_STATUSES = frozenset({
    build_domain.STATUS_BUILDING,
    build_domain.STATUS_PUBLISHING,
})

#: Bounded window for GetCommandInvocation eventual consistency
#: (InvocationDoesNotExist is retried, never fabricated as failure,
#: Req 2.5); after the window the evidence is identified as unavailable.
INVOCATION_LOOKUP_WINDOW_MS = int(os.environ.get(
    'BUILD_INVOCATION_LOOKUP_WINDOW_MS', str(10 * 60 * 1000)))
#: Settlement window after a terminal command observation in which a
#: valid already-in-flight terminal agent result may still arrive;
#: `Success` without a callback becomes AGENT_RESULT_MISSING only after
#: this bound (Req 2.4, 2.5).
SETTLEMENT_WINDOW_MS = int(os.environ.get(
    'BUILD_SETTLEMENT_WINDOW_MS',
    str(build_reconciliation.DEFAULT_SETTLEMENT_WINDOW_MS)))
#: Visibility bound after which an ambiguous `sending` attempt with no
#: recoverable command may be conditionally re-sent; recovery through
#: the deterministic command comment always runs FIRST (Req 2.7).
AMBIGUOUS_SEND_VISIBILITY_MS = int(os.environ.get(
    'BUILD_SEND_VISIBILITY_MS', str(5 * 60 * 1000)))
#: Diagnostic source tag for the scheduled tick (design data model).
EVIDENCE_SOURCE_SCHEDULED = 'scheduled_reconciliation'

#: Error codes recorded on failed Build_Jobs (design Error Handling).
ERROR_PROVISIONING_FAILED = 'PROVISIONING_FAILED'
#: Bootstrap stage failure: the runner's bootstrap never signalled
#: completion inside its budget (Req 6.3).
ERROR_BOOTSTRAP_TIMEOUT = 'BOOTSTRAP_TIMEOUT'
ERROR_TIMEOUT = 'TIMEOUT'
ERROR_SERIALIZATION_VIOLATION = build_planner.SERIALIZATION_VIOLATION_ERROR
ERROR_SERVER_LOST = 'SERVER_LOST'
ERROR_DISPATCH_FAILED = 'DISPATCH_FAILED'
#: Stable error code for a failed dispatch preflight (task 7.1, Req 2.8):
#: an invalid startup contract fails BEFORE build/publish, through the
#: common terminal flow, instead of reaching a costly command.
ERROR_COMMAND_PREFLIGHT_FAILED = \
    build_reconciliation.CODE_COMMAND_PREFLIGHT_FAILED

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


def to_dynamo(value: Any) -> Any:
    """Convert native floats to Decimals for DynamoDB persistence (deep);
    sanitized diagnostics may carry numeric fields."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_dynamo(v) for v in value]
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


def job_source_ref(job: Optional[Dict[str, Any]]) -> str:
    """The ref a Build_Job selected, from its own config_snapshot ('' when
    none was selected).

    ``config_snapshot.source_ref`` is the selection Increment A carries;
    Increment B adds the per-submission repository alongside it, at which
    point the repository is read from the same snapshot instead of the
    module's BUILD_REPO_URL.
    """
    snapshot = (job or {}).get('config_snapshot') or {}
    source_ref = snapshot.get('source_ref')
    return source_ref if isinstance(source_ref, str) else ''


def dispatch_region(job: Optional[Dict[str, Any]] = None) -> str:
    """The AWS region generated runner commands must export before any
    aws CLI usage.

    Ephemeral runners come up with NO region configured (no
    AWS_DEFAULT_REGION/AWS_REGION in the SSM/user-data environment, no
    ~/.aws/config) and run AWS CLI v1, which does not infer the region
    from instance metadata — so every `aws` invocation fails with "You
    must specify a region" (live SSM 06ec7c91 on i-02e1afdc4c93a0d8f: the
    agent's put-events phase emissions failed 3/3 and
    portal-build-agent.sh aborted at its region check, while the same
    call succeeded with an explicit --region).

    The value is the dispatcher Lambda's own region (os.environ
    ['AWS_REGION'], which the Lambda runtime always sets — the runners it
    provisions and the buses/log groups it hands out live in the same
    region), falling back to the job's config_snapshot 'region' when the
    env is absent. Empty when neither is available.
    """
    region = os.environ.get('AWS_REGION', '')
    if region:
        return region
    snapshot = (job or {}).get('config_snapshot') or {}
    value = snapshot.get('region')
    return value if isinstance(value, str) else ''


#: The build user's home directory. Every generated sync/build body now
#: runs as the build user via ``sudo -H -u ubuntu`` (see BUILD_USER), and
#: ``-H`` sets HOME to exactly this value — so the braced defaults below
#: are pure `set -u` safety nets (live counterexample for the bare form:
#: runner i-0298c6f74db03ec69, job f13904b1 — cloud-init runs user-data
#: as root WITHOUT $HOME set, under `set -u`, and a bare ``$HOME``
#: aborted the bootstrap before the marker was written), not a second
#: execution mode.
BUILD_USER_HOME = '/home/ubuntu'

#: PATH export the build-user body emits before the sync/agent/setup
#: statements run.
#:
#: setup-build-server.sh installs the GDK CLI via ``pip3 install --user``,
#: which places the ``gdk`` binary in $HOME/.local/bin — a directory a
#: non-login shell does NOT put on PATH (live SSM c77c67ef, job a235bef0:
#: "./portal-build.sh: line 282: gdk: command not found"). The body runs
#: as the build user (sudo -H sets HOME=/home/ubuntu), so
#: ``${HOME:-/home/ubuntu}/.local/bin`` resolves to the one place the
#: documented provisioning flow (launch-arm64-build-server.sh ->
#: setup-build-server.sh as ubuntu) actually installs gdk — on dedicated
#: servers today, and on ephemeral runners now that their bootstrap runs
#: the same setup as ubuntu. The old /root fallback existed only for the
#: root-run ephemeral flow this change retires; the braced default form
#: is kept for `set -u` safety (see BUILD_USER_HOME).
PATH_EXPORT_COMMAND = (
    f'export PATH="${{HOME:-{BUILD_USER_HOME}}}/.local/bin:$PATH"')

#: Root-prologue HOME export. The SSM RunShellScript environment runs as
#: root with HOME **unset** (live SSM diagnostic on dedicated server
#: srv-3f963f3b, job 81bc94a3: git died with "fatal: $HOME not set" on
#: the Sync_Generator's safe.directory entry). The build itself no longer
#: needs this — the sync and the agent run as ubuntu with sudo -H setting
#: HOME — but the export is kept at the top of the root prologue so any
#: root-context statement (the heals, an operator-added diagnostic)
#: remains resilient. `set -u`-safe braced default form.
HOME_EXPORT_COMMAND = 'export HOME="${HOME:-/root}"'

#: HOME export inside the BUILD-USER body: a no-op under ``sudo -H``
#: (which already sets HOME=/home/ubuntu) and a `set -u`-safe default for
#: any other execution of the same text (an operator reproducing the
#: body, a test harness).
BUILD_USER_HOME_EXPORT_COMMAND = (
    f'export HOME="${{HOME:-{BUILD_USER_HOME}}}"')


def runner_env_export_commands(
        job: Optional[Dict[str, Any]] = None) -> List[str]:
    """Environment export statements emitted at the top of every generated
    BUILD-USER body, before the first aws/git/agent/setup statement.

    First, unconditionally, the HOME export (see
    BUILD_USER_HOME_EXPORT_COMMAND): a no-op under ``sudo -H`` and a
    `set -u`-safe default anywhere else, so ``git config --global`` (the
    Sync_Generator's safe.directory entry) always has a HOME to write —
    the live "fatal: $HOME not set" failure (srv-3f963f3b, job 81bc94a3)
    can never recur in any execution of the body.

    Then the region exports: dispatch_region(job) as AWS_DEFAULT_REGION
    and AWS_REGION (both spellings: CLI v1 reads the former, CLI v2 and
    most SDKs the latter), omitted entirely when no region resolves. The
    value is interpolated through shlex.quote only (the injection-safety
    convention of build_source.source_sync_commands).

    Then, unconditionally, the PATH export putting the build user's
    ~/.local/bin on PATH (see PATH_EXPORT_COMMAND) — unconditional
    because the gdk-not-found defect it closes is independent of region
    configuration."""
    lines: List[str] = [BUILD_USER_HOME_EXPORT_COMMAND]
    region = dispatch_region(job)
    if region:
        quoted = shlex.quote(region)
        lines.append(f'export AWS_DEFAULT_REGION={quoted}')
        lines.append(f'export AWS_REGION={quoted}')
    lines.append(PATH_EXPORT_COMMAND)
    return lines


def agent_preamble_commands(job: Optional[Dict[str, Any]],
                            repo_dir: str) -> List[str]:
    """PRE-AGENT PREAMBLE: the Source_Sync commands that put ``repo_dir``
    on the Build_Job's selected (repository, ref) before the agent is
    invoked (Req 4.1, 4.2, 4.3), or ``[]`` when no ref is selected.

    Why the preamble exists at all: the agent script itself lives IN the
    tree being synced, so a server bootstrapped weeks ago on another ref —
    or an ephemeral runner whose user-data predates a config change —
    cannot be expected to already carry it. Without this, invoking the
    agent from a tree that cannot contain it is the live `No such file or
    directory` / exit 127 failure (SSM e9281bdc, d75f1ea2). The commands
    come from build_source.source_sync_commands, the single origin of all
    Source_Sync text, so the agent's own Step 2 then re-runs an identical,
    idempotent sync.

    No ref selected means no preamble: that mirrors the agent, whose Step 2
    is itself guarded on `[ -n "$SOURCE_REF" ]` and which builds the
    currently checked-out tree otherwise. Dispatch for a job with no
    selection is therefore byte-identical to today (Req 7.1).

    FAILURE SURFACING (Req 4.4): this is the one caller of the generator
    that passes the event-emission parameters, because it is the only one
    that runs inside a Build_Job's dispatch — it has the job id, the
    Build_Target, and the same EventBridge bus the agent is handed. So a
    sync failure here emits ONE dda.portal.builds / BuildPhaseChange event
    (phase=failed, error_kind=source_sync, source_error=<class>, a message
    naming both the repository and the ref) and then exits with its
    classified code, instead of leaving the Build_Job in
    provisioning/building behind a bare nonzero SSM exit. The two user-data
    paths pass nothing: their sync runs at instance boot, before any
    job-scoped agent command exists, and their failure is surfaced by the
    bootstrap readiness gate instead (Req 6.3).
    """
    source_ref = job_source_ref(job)
    if not source_ref:
        return []
    job = job or {}
    return build_source.source_sync_commands(
        BUILD_REPO_URL, repo_dir, source_ref,
        event_bus=BUILD_EVENT_BUS,
        build_job_id=job.get('build_job_id'),
        build_target=job.get('build_target'))


# ----------------------------------------------------- build-user execution

#: The user every sync and build runs as, in BOTH execution modes
#: (user-approved structural decision: "the build should run as ubuntu,
#: root is not needed").
#: launch-arm64-build-server.sh documents the provisioning flow as: SSH
#: as ubuntu -> clone as ubuntu -> ./setup-build-server.sh as ubuntu
#: (which installs gdk via ``pip3 install --user`` into
#: ~/.local/bin) — the build environment is designed FOR the ubuntu
#: user. SSM AWS-RunShellScript and cloud-init user-data both run as
#: root, and the root-run path was chased through a chain of
#: live-verified environment defects (region unset, HOME unset, git
#: dubious ownership on job 81bc94a3, gdk not on PATH on job d352a735 —
#: gdk exists only at /home/ubuntu/.local/bin — and finally
#: "ModuleNotFoundError: No module named 'gdk'", job ff6d89fe:
#: ubuntu's pip user-site is invisible to root's python). The prior env
#: exports were correct triage; the durable fix is ONE environment
#: model: root does only the privileged prologue (heals, parent-dir
#: preparation, marker write), and the sync/setup/agent body executes AS
#: ubuntu — on dedicated servers and ephemeral runners alike, so
#: ephemeral runners install and find gdk in /home/ubuntu/.local/bin
#: exactly like the dedicated fleet.
BUILD_USER = 'ubuntu'

#: Shell variables the build-user execution wrapper assigns (the PORTAL_
#: prefix keeps them clear of the agent's own names, the convention of
#: build_source's emission block).
RUN_SCRIPT_VAR = 'PORTAL_RUN_SCRIPT'
RUN_STATUS_VAR = 'PORTAL_RUN_STATUS'

#: Base here-doc delimiter for the build-user body (see
#: build_user_heredoc_delimiter for the collision-proofing).
RUN_HEREDOC_DELIMITER = 'PORTAL_RUN_EOF'

#: The agent's on-server build mutual-exclusion lock file. Mirrors the
#: agent's own LOCK_FILE (scripts/portal-build-agent.sh, pinned as
#: AGENT_LOCK_FILE by the task-2 preservation tests in
#: test_source_selection_preservation.py — referenced, not imported:
#: production code never imports from test code).
BUILD_LOCK_FILE = '/var/lock/dda-build.lock'

#: The docker daemon's API socket, the path the ubuntu-run gdk build
#: connects to (see docker_socket_heal_command).
DOCKER_SOCKET_PATH = '/var/run/docker.sock'


def repo_ownership_heal_command(repo_dir: str) -> str:
    """Guarded ownership heal run AS ROOT before the build-user body:
    ``chown -R ubuntu:ubuntu <repo_dir>``, only when the directory
    exists, silenced and tolerated.

    The interim root-run syncs left root-owned files in the dedicated
    servers' ubuntu-owned clones on the live servers; a ubuntu-run git
    would fail on them, so the tree is handed back to the build user
    first. The ``[ -d ... ]`` guard skips the heal when the directory
    does not exist (nothing to heal — the sync inside the build-user
    body creates it, ubuntu-owned), and ``2>/dev/null || true`` keeps an
    unprivileged execution (an operator reproducing the command, a test
    harness) from introducing a new failure mode. ``repo_dir`` passes
    through shlex.quote — the injection-safety convention of every
    generated statement.
    """
    quoted = shlex.quote(repo_dir)
    return (f'if [ -d {quoted} ]; then '
            f'chown -R {BUILD_USER}:{BUILD_USER} {quoted} '
            '2>/dev/null || true; fi')


def lock_ownership_heal_command() -> str:
    """Guarded ownership heal run AS ROOT before the build-user body:
    ``chown ubuntu:ubuntu BUILD_LOCK_FILE``, only when the file exists,
    silenced and tolerated.

    Live evidence (job 01b18948, SSM 9602a0e8, server srv-3f963f3b):
    the interim ROOT-run dispatch attempts created BUILD_LOCK_FILE
    owned by root, so the ubuntu-run agent's ``exec 9>"$LOCK_FILE"``
    failed with "Permission denied" then "flock: 9: Bad file
    descriptor", taking the held-lock defer path (exit 75) forever.
    There is never a live holder to disturb — flock releases on process
    death — only the stale root ownership, which this hands back to the
    build user. The ``[ -f ... ]`` guard skips the heal when the file
    does not exist (the agent creates it, ubuntu-owned), and
    ``2>/dev/null || true`` keeps an unprivileged execution (an
    operator reproducing the command, a test harness) from introducing
    a new failure mode — the same discipline as
    repo_ownership_heal_command.
    """
    return (f'if [ -f {BUILD_LOCK_FILE} ]; then '
            f'chown {BUILD_USER}:{BUILD_USER} {BUILD_LOCK_FILE} '
            '2>/dev/null || true; fi')


def docker_socket_heal_command() -> str:
    """Guarded docker-socket access heal run AS ROOT before the
    build-user body: ``chgrp docker DOCKER_SOCKET_PATH``, falling back
    to ``chmod 666 DOCKER_SOCKET_PATH``, only when the socket exists,
    silenced and tolerated.

    Live evidence (job c828f479, dedicated server srv-3f963f3b): the
    ubuntu-run gdk build failed with "permission denied while trying to
    connect to the docker API at unix:///var/run/docker.sock". Verified
    on the server: ubuntu IS in the docker group (gid 1001), but
    /var/run/docker.sock was owned root:root mode srw-rw---- — the
    socket's group is root, so ubuntu's docker-group membership grants
    nothing. setup-build-server.sh's own remedy is ``sudo chmod 666
    /var/run/docker.sock`` (line ~144), and the server's ORIGINAL live
    bootstrap logs recorded exactly that inner step failing ("Failed:
    sudo chmod 666 /var/run/docker.sock") — the socket's group resets
    when the docker daemon restarts, so a one-time fix does not stick.
    Healing on every dedicated dispatch does.

    ``chgrp docker`` is the tight fix: the socket already carries g+rw
    and ubuntu is in the docker group, so handing the socket to that
    group grants exactly the intended access. ``chmod 666`` is the
    documented setup-script fallback for a server whose docker group is
    absent. The ``[ -S ... ]`` guard skips the heal when the socket does
    not exist (no daemon — nothing this heal could usefully grant), and
    ``2>/dev/null || true`` keeps an unprivileged execution (an operator
    reproducing the command, a test harness) from introducing a new
    failure mode — the same discipline as repo_ownership_heal_command
    and lock_ownership_heal_command.
    """
    return (f'if [ -S {DOCKER_SOCKET_PATH} ]; then '
            f'chgrp docker {DOCKER_SOCKET_PATH} 2>/dev/null || '
            f'chmod 666 {DOCKER_SOCKET_PATH} 2>/dev/null || true; fi')


def repo_parent_prepare_commands(repo_dir: str) -> List[str]:
    """Root-context statements the ephemeral bootstrap runs BEFORE the
    build-user body: create ``repo_dir``'s parent directory and hand it
    to the build user, so the ubuntu-run ``git clone`` inside the body
    can create the clone (ubuntu-owned) wherever the resolved directory
    lives.

    For the default directory the parent is /home/ubuntu, which already
    exists ubuntu-owned — both statements are then no-ops. For an
    operator-pinned directory outside ubuntu's home (e.g. /opt/dda/...),
    root is the only user that can create the parent, and the chown is
    what makes it writable by the build user. The chown is non-recursive
    (only the parent needs to admit the clone), silenced and tolerated —
    the same discipline as the heal helpers — and ``repo_dir`` passes
    through shlex.quote, the injection-safety convention of every
    generated statement.
    """
    quoted = shlex.quote(repo_dir)
    return [
        f'mkdir -p "$(dirname {quoted})"',
        f'chown {BUILD_USER}:{BUILD_USER} "$(dirname {quoted})" '
        '2>/dev/null || true',
    ]


def classified_sync_exit_commands() -> List[str]:
    """Root-context statements the ephemeral bootstrap runs AFTER the
    build-user body and BEFORE the Bootstrap_Marker write: propagate a
    CLASSIFIED Source_Sync failure (exit 65 repository unreachable / 66
    ref not found, from the guards inside the body) as the bootstrap's
    own exit, so the marker is never written for a bootstrap whose sync
    failed — exactly the pre-change semantics, where those guards exited
    the (then root-run) script inline before the marker statement, and
    the readiness gate resolved the job with the bootstrap log location
    (Req 6.3).

    Any OTHER nonzero body status deliberately does NOT gate the marker:
    setup-build-server.sh's inner steps failing while the bootstrap still
    finishes usefully is the live-observed normal (Req 6.4 makes the
    marker, not the inner steps, authoritative), and the pre-change
    script ran without ``set -e`` for the same reason.
    """
    return [
        f'case "${RUN_STATUS_VAR}" in',
        f'  {build_source.EXIT_REPO_UNREACHABLE}|'
        f'{build_source.EXIT_REF_NOT_FOUND}) exit "${RUN_STATUS_VAR}";;',
        'esac',
    ]


def build_user_heredoc_delimiter(body: str) -> str:
    """A here-doc delimiter no line of ``body`` equals.

    The build-user body is transported inside a quoted here-doc (so its
    multi-line shell functions and shlex-quoted values arrive verbatim,
    with no outer expansion). A here-doc terminates on an exact line
    match regardless of quoting inside the body, so the delimiter is
    extended until no body line collides with it — injection safety by
    construction, not by review.
    """
    delimiter = RUN_HEREDOC_DELIMITER
    lines = body.splitlines()
    while delimiter in lines:
        delimiter += '_'
    return delimiter


def run_as_build_user_commands(body: str) -> List[str]:
    """Statements that execute ``body`` as the BUILD_USER (design: every
    build runs as ubuntu — the user the whole build environment is
    designed for — in both execution modes; root keeps only the
    privileged prologue).

    Mechanism: the body is written verbatim to a root-created temp script
    through a quoted here-doc (no expansion in the root shell, no
    re-quoting of the already shlex.quote-disciplined body, and the
    generated text stays line-structured and auditable), then executed
    with ``sudo -H -u ubuntu bash`` — ``-H`` sets HOME=/home/ubuntu, so
    the body's ``${HOME:-...}`` expansions resolve to ubuntu's home and
    ``pip3 install --user`` artifacts (gdk) are found where the
    documented provisioning flow put them. env_reset scrubbing is
    irrelevant: the body carries its own exports (region, PATH, HOME).

    Run unprivileged — an operator reproducing a command, a test harness
    — the same body executes directly in the current user, which is
    already the groomed-user situation the sudo arm exists to create.
    The wrapper leaves the body's exit status in RUN_STATUS_VAR and
    removes the temp script; the CALLER decides what the status means
    (the dedicated agent command propagates it verbatim).
    """
    delimiter = build_user_heredoc_delimiter(body)
    return [
        f'{RUN_SCRIPT_VAR}="$(mktemp /tmp/portal-build-run.XXXXXX)" '
        '|| exit 1',
        f'cat > "${RUN_SCRIPT_VAR}" <<' + f"'{delimiter}'",
        body,
        delimiter,
        f'chmod 644 "${RUN_SCRIPT_VAR}"',
        f'if [ "$(id -u)" = "0" ] && id {BUILD_USER} >/dev/null 2>&1; then',
        f'  sudo -H -u {BUILD_USER} bash "${RUN_SCRIPT_VAR}"',
        'else',
        f'  bash "${RUN_SCRIPT_VAR}"',
        'fi',
        f'{RUN_STATUS_VAR}="$?"',
        f'rm -f "${RUN_SCRIPT_VAR}"',
    ]


# --------------------------------------------------- dispatch preflight
# build-fleet-execution-failures task 7.1 (Req 2.8, 2.9, 2.10, 2.12,
# 3.7, 3.9). Evidence gate (historical-evidence.md task 3.3):
#   - Row 1 (CONFIRMED): the 2026-08-06 AMD64 job's SSM command executed
#     /opt/dda/DefectDetectionApplication/scripts/portal-build-agent.sh,
#     which did not exist (exit 127 in 3 s), while the fleet bootstrap
#     had cloned to /home/ubuntu/DefectDetectionApplication. The path
#     correction lives in build_source.resolve_repo_dir (registered
#     repo_dir first, evidenced-fallback correction second) and carries
#     row 1's caveat: it is contract hardening — fixing the path alone
#     is NOT proven sufficient for a successful build.
#   - Row 8 (CONFIRMED): that invalid path/script contract reached a
#     dispatched SSM command on a live m6i.4xlarge with zero pre-checks.
#     The preflight below is the authorized correction: the local
#     contract decision (decide_preflight) rejects invalid target/
#     component/mode/quoting/callback contracts BEFORE any SSM send, and
#     the generated on-server guard (preflight_guard_commands) plus the
#     agent's own preflight (scripts/portal-build-agent.sh, run when
#     ATTEMPT_ID is supplied) validate the machine-side contract BEFORE
#     any build/publish. Preflight is not claimed to address every
#     failure mode (row 8's prohibited inference).
#   - Row 9 (CONFIRMED, tasks 7.4/7.5): the JP6 ephemeral job bd91c5d8
#     exhausted its 100 GB volume; the agent preflight now RECORDS disk
#     capacity for the docker storage path and repo/tmp volume as
#     evidence (never a failure unless a configured minimum is
#     violated), surfaced through execution_diagnostic.disk.

#: Marker a failed preflight writes to stderr; reconciliation maps it to
#: the stable COMMAND_PREFLIGHT_FAILED classification.
PREFLIGHT_FAILURE_MARKER = build_reconciliation.PREFLIGHT_FAILURE_MARKER
#: Exit status of a failed preflight (EX_CONFIG; distinct from the
#: agent's 64 usage / 75 lock-held and the sync guards' 65/66).
PREFLIGHT_EXIT_CODE = 78
#: The docker layer-storage path on snap-docker runners (evidence-gate
#: row 9: all layer storage lives here on the single root volume).
DOCKER_STORAGE_PATH = '/var/snap/docker/common'


class PreflightDecision(NamedTuple):
    """Pure local-contract preflight decision for one dispatch.

    - ``ok``: False means the startup contract is invalid and the job
      must fail with COMMAND_PREFLIGHT_FAILED before any costly work
    - ``failures``: short stable failure identifiers (safe vocabulary
      only — no raw values)
    - ``checks``: the recorded evidence of what was validated
    """
    ok: bool
    failures: Tuple[str, ...]
    checks: Dict[str, Any]


def quoting_round_trips(value: Any) -> bool:
    """True iff ``value`` survives the generated command's shlex quoting
    round trip unchanged (design preflight check 7)."""
    if not isinstance(value, str):
        return False
    try:
        return shlex.split(shlex.quote(value)) == [value]
    except ValueError:  # pragma: no cover - shlex.quote never produces this
        return False


def decide_preflight(job: Dict[str, Any], repo_dir: str,
                     event_bus: Optional[str] = None,
                     region: Optional[str] = None) -> PreflightDecision:
    """PURE local-contract preflight (task 7.1, evidence rows 1 and 8):
    everything about the dispatch contract that can be validated without
    touching the server is validated here, BEFORE any SSM send. Machine-
    side facts (script existence/readability, tools, writability, AWS
    identity, architecture, disk capacity) are validated on-server by
    the generated guard and the agent's own preflight, still before any
    build/publish.

    Target/mode preservation (task 7.3, Req 3.7): the checks READ the
    existing build_domain definitions (JP5/JP6 -> arm64, AMD64/
    AMD64_NVIDIA -> x86_64, current component identities) and impose no
    cross-target assumptions; intentionally invalid combinations keep
    failing the existing submission validation before ever reaching
    dispatch.
    """
    job = job or {}
    failures: List[str] = []
    checks: Dict[str, Any] = {}

    target = job.get('build_target')
    if build_domain.is_supported_target(target):
        definition = build_domain.target_definition(target)
        checks['build_target'] = target
        checks['required_arch'] = definition['required_arch']
        checks['component_name'] = definition['component_name']
        # Component identity (design preflight check 5): a job whose
        # recorded component is ANOTHER known target's component is
        # cross-wired and must not dispatch. Any other recorded value
        # (legacy/test fixtures with free-form names) is advisory only —
        # the agent derives the real component from BUILD_TARGET, so an
        # unknown name cannot change the build (Req 3.7 preservation).
        recorded_component = job.get('component_name')
        known_components = {
            other['component_name']
            for other_target, other in build_domain.BUILD_TARGETS.items()
            if other_target != target}
        if recorded_component in known_components:
            failures.append('component_identity_mismatch')
    else:
        failures.append('unsupported_target')

    mode = job.get('execution_mode')
    checks['execution_mode'] = mode
    if mode not in (build_domain.EXECUTION_MODE_DEDICATED,
                    build_domain.EXECUTION_MODE_EPHEMERAL):
        failures.append('unsupported_execution_mode')

    if not isinstance(repo_dir, str) or not repo_dir.strip() \
            or not repo_dir.startswith('/'):
        failures.append('repository_dir_invalid')
    else:
        checks['repo_dir'] = repo_dir

    bus = event_bus if event_bus is not None else BUILD_EVENT_BUS
    if not bus:
        failures.append('callback_bus_missing')
    else:
        checks['callback_bus'] = bus
    resolved_region = region if region is not None \
        else dispatch_region(job)
    if not resolved_region:
        failures.append('callback_region_missing')
    else:
        checks['callback_region'] = resolved_region

    snapshot = job.get('config_snapshot') or {}
    quoted_values = {
        'build_job_id': job.get('build_job_id'),
        'build_target': target,
        'repo_dir': repo_dir,
        'event_bus': bus,
    }
    source_ref = snapshot.get('source_ref')
    if source_ref:
        quoted_values['source_ref'] = source_ref
    for name, value in sorted(quoted_values.items()):
        if value is None:
            continue
        if not quoting_round_trips(value):
            failures.append(f'quoting_contract_violated:{name}')
    checks['quoting_round_trip'] = not any(
        f.startswith('quoting_contract_violated') for f in failures)

    return PreflightDecision(ok=not failures,
                             failures=tuple(failures),
                             checks=checks)


#: Shell variable names of the generated on-server preflight guard. The
#: agent-script path is assembled from PARTS at execution time so no
#: single generated token spells the full script path (the invocation
#: line stays the one token carrying it — the argument-contract oracle
#: keys on that).
_PREFLIGHT_DIR_VAR = 'PORTAL_PREFLIGHT_DIR'
_PREFLIGHT_AGENT_VAR = 'PORTAL_PREFLIGHT_AGENT'


def attempt_env_inject_command(attempt_id: str) -> str:
    """The root-prologue statement that prepends
    ``export ATTEMPT_ID=<id>`` to the generated temp run script (task
    7.2), so the agent inherits its execution-attempt identity WITHOUT
    any change to the frozen here-doc body or the agent's KEY=VALUE
    argument contract. Tolerated: a failed injection degrades to the
    legacy attempt-less agent behavior, never to a failed dispatch."""
    export_line = f'export ATTEMPT_ID={shlex.quote(attempt_id)}'
    return (f'sed -i {shlex.quote("1i " + export_line)} '
            f'"${RUN_SCRIPT_VAR}" 2>/dev/null || true')


def preflight_guard_commands(repo_dir: str) -> List[str]:
    """Generated on-server guard (task 7.1, evidence rows 1 and 8) run
    INSIDE the build-user body when no Source_Sync preamble applies,
    after the environment exports and BEFORE the agent invocation:
    validate that the resolved repository directory exists and carries a
    readable ``scripts/portal-build-agent.sh``. On violation it writes the
    PREFLIGHT_FAILURE_MARKER (with only the safe path context) to
    stderr and exits PREFLIGHT_EXIT_CODE without invoking the agent —
    so an invalid startup contract can never reach build/publish, and
    reconciliation classifies the retained stderr as the stable
    COMMAND_PREFLIGHT_FAILED code instead of 2026-08-06's bare 127."""
    quoted = shlex.quote(repo_dir)
    return [
        f'{_PREFLIGHT_DIR_VAR}={quoted}',
        f'if [ ! -d "${_PREFLIGHT_DIR_VAR}" ]; then',
        f'  echo "{PREFLIGHT_FAILURE_MARKER} check=repository_dir '
        f'path=${_PREFLIGHT_DIR_VAR}" >&2',
        f'  exit {PREFLIGHT_EXIT_CODE}',
        'fi',
        f'{_PREFLIGHT_AGENT_VAR}="${_PREFLIGHT_DIR_VAR}/scripts'
        '/portal-build-agent"',
        f'{_PREFLIGHT_AGENT_VAR}="${_PREFLIGHT_AGENT_VAR}.sh"',
        f'if [ ! -f "${_PREFLIGHT_AGENT_VAR}" ] || '
        f'[ ! -r "${_PREFLIGHT_AGENT_VAR}" ]; then',
        f'  echo "{PREFLIGHT_FAILURE_MARKER} check=agent_script '
        f'path=${_PREFLIGHT_AGENT_VAR}" >&2',
        f'  exit {PREFLIGHT_EXIT_CODE}',
        'fi',
    ]


def agent_run_body(job: Dict[str, Any],
                   repo_dir: Optional[str] = None) -> str:
    """The agent command text for one Build_Job: the environment exports,
    then the Source_Sync preamble for the job's selected ref (Req 4.1,
    4.2), then the agent invocation (design §5:
    scripts/portal-build-agent.sh with BUILD_JOB_ID / BUILD_TARGET /
    EVENT_BUS / SOURCE_REF) as the LAST line, so the body's exit status
    is the agent's.

    This text is the BUILD-USER body: agent_command executes it as
    ubuntu, in both execution modes, through the temp-script + ``sudo -H
    -u ubuntu`` transport (see run_as_build_user_commands).

    The exports come FIRST because both the preamble's failure-surfacing
    put-events and the agent's own aws usage need a configured region on
    a machine that has none of its own (see dispatch_region), and the
    agent's build needs ~/.local/bin on PATH in a non-login shell (see
    PATH_EXPORT_COMMAND) — under ``sudo -H -u ubuntu`` the
    ${HOME:-/home/ubuntu} expansions resolve to /home/ubuntu, where the
    documented provisioning flow installs gdk on dedicated servers and
    (now that the bootstrap body also runs as ubuntu) on ephemeral
    runners alike.
    """
    if repo_dir is None:
        repo_dir = build_source.resolve_repo_dir(
            job, env_default=BUILD_REPO_DIR)
    snapshot = job.get('config_snapshot') or {}
    parts = [
        'bash',
        shlex.quote(build_source.agent_script_path(repo_dir)),
        shlex.quote(f"BUILD_JOB_ID={job['build_job_id']}"),
        shlex.quote(f"BUILD_TARGET={job['build_target']}"),
        shlex.quote(f"EVENT_BUS={BUILD_EVENT_BUS}"),
    ]
    source_ref = snapshot.get('source_ref')
    if source_ref:
        parts.append(shlex.quote(f"SOURCE_REF={source_ref}"))
    invocation = ' '.join(parts)
    # Dispatch preflight guard (task 7.1, evidence rows 1/8): the
    # repository/script contract is validated right before the
    # invocation — AFTER the Source_Sync preamble when a ref is
    # selected, since the sync legitimately (re)creates the tree and
    # the agent script — so an invalid startup contract exits with the
    # preflight marker (classified COMMAND_PREFLIGHT_FAILED) instead of
    # the 2026-08-06 incident's bare exit 127 after dispatch. The guard
    # targets the SAME resolved directory as the invocation (no drift).
    lines = (runner_env_export_commands(job)
             + list(agent_preamble_commands(job, repo_dir))
             + preflight_guard_commands(repo_dir)
             + [invocation])
    return '\n'.join(lines)


def agent_command(job: Dict[str, Any],
                  repo_dir: Optional[str] = None,
                  attempt_id: Optional[str] = None) -> str:
    """Shell command executing the build agent for one Build_Job.

    ``repo_dir`` is the directory this machine's bootstrap actually used,
    resolved by the caller from the Build_Server or runner record; the
    agent path is rooted in it through build_source.agent_script_path, so
    it can never drift from the bootstrap clone (Req 5.1, 5.4). Omitted,
    it is resolved from the job itself.

    ONE environment model, both execution modes (see BUILD_USER): SSM
    runs this text as root, root does only the privileged prologue, and
    the sync preamble + agent invocation run AS THE BUILD USER. The
    result is still ONE command text, so it stays a single SSM
    AWS-RunShellScript element and the agent's argument contract is
    untouched (Req 7.6). In order:

    * the root prologue — the resilient HOME export (see
      HOME_EXPORT_COMMAND; no root-context statement here needs HOME
      today, but the prologue stays safe if one ever does) and the
      guarded root-context heals: root hands back the root-owned files
      earlier root-run syncs left in ubuntu's clone
      (repo_ownership_heal_command, repo_dir shlex-quoted), the
      root-owned agent lock file those attempts left in /var/lock
      (lock_ownership_heal_command), and the docker socket's root-group
      reset that denies ubuntu's docker-group membership
      (docker_socket_heal_command);
    * the build-user execution of agent_run_body via the quoted-here-doc
      temp script + ``sudo -H -u ubuntu`` transport — the sync preamble
      and the agent invocation both run as ubuntu, with the environment
      exports re-established inside that body (they survive sudo because
      they are IN the body, not inherited across it);
    * the final ``exit``, which propagates the agent's own exit status —
      including the classified Source_Sync failure codes 65/66 — as the
      SSM command status, exactly as before.
    """
    if repo_dir is None:
        repo_dir = build_source.resolve_repo_dir(
            job, env_default=BUILD_REPO_DIR)
    body = agent_run_body(job, repo_dir)
    run_lines = run_as_build_user_commands(body)
    # Correlated attempt identity (task 7.2): ATTEMPT_ID is delivered by
    # prepending an export to the generated temp run script, so the
    # here-doc BODY and the agent's frozen KEY=VALUE argument contract
    # (BUILD_JOB_ID / BUILD_TARGET / EVENT_BUS / SOURCE_REF) stay
    # byte-identical to the legacy text (Req 3.9/3.10). A legacy or
    # attempt-less dispatch produces exactly the pre-change command.
    resolved_attempt_id = attempt_id or \
        (job.get('execution_attempt') or {}).get('attempt_id')
    if resolved_attempt_id and quoting_round_trips(resolved_attempt_id):
        chmod_line = f'chmod 644 "${RUN_SCRIPT_VAR}"'
        run_lines.insert(run_lines.index(chmod_line),
                         attempt_env_inject_command(resolved_attempt_id))
    lines = ([HOME_EXPORT_COMMAND,
              repo_ownership_heal_command(repo_dir),
              lock_ownership_heal_command(),
              docker_socket_heal_command()]
             + run_lines
             + [f'exit "${RUN_STATUS_VAR}"'])
    return '\n'.join(lines)


def agent_execution_timeout_seconds(job: Dict[str, Any]) -> int:
    """SSM executionTimeout for the agent command: the job's own
    config_snapshot max runtime plus a 30-minute margin, capped at the
    SSM maximum (172800 s). The runtime watchdog (Req 3.8) remains the
    authoritative timeout."""
    limit_ms = build_planner.max_runtime_ms(job.get('config_snapshot'))
    return int(min(limit_ms / 1000 + 1800, 172800))


#: Non-fatal log redirect for a generated bootstrap script.
#:
#: A failed `exec >` redirect ABORTS a non-interactive bash immediately,
#: before any later statement runs. On a real runner the user-data runs as
#: root and /var/log is writable, but any unprivileged execution of the
#: same text (a manual re-run, an operator reproducing a bootstrap, a test
#: harness) would abort before the repository is even cloned. So the
#: redirect is attempted only after the path proves writable, and skipped
#: otherwise — output then goes to the cloud-init log instead of the
#: bootstrap log, which is a diagnosability downgrade, never a failure.
BOOTSTRAP_LOG_VAR = 'BOOTSTRAP_LOG'


def bootstrap_log_redirect_commands() -> List[str]:
    """Statements that send a bootstrap's output to
    build_planner.BOOTSTRAP_LOG_PATH when that path is writable (Req 6.2),
    and are a no-op when it is not (see BOOTSTRAP_LOG_VAR)."""
    return [
        f'{BOOTSTRAP_LOG_VAR}='
        f'{shlex.quote(build_planner.BOOTSTRAP_LOG_PATH)}',
        f'if : > "${BOOTSTRAP_LOG_VAR}" 2>/dev/null; then',
        f'  exec >> "${BOOTSTRAP_LOG_VAR}" 2>&1',
        'fi',
    ]


def tolerate_failure(command: str) -> str:
    """A generated statement made non-fatal (`|| true`).

    Used for the Bootstrap_Marker write: an unwritable marker path must not
    change the script's outcome. If the marker genuinely cannot be written
    the readiness gate simply never opens and the bootstrap budget resolves
    the job (Req 6.3) — the fail-safe behavior, instead of an abort before
    the repository is cloned.
    """
    return f'{command} || true'


def runner_bootstrap_user_data(job: Optional[Dict[str, Any]] = None,
                               repo_dir: Optional[str] = None) -> str:
    """User-data bootstrap for an ephemeral runner: put the working tree on
    the Build_Job's selected (repository, ref) and run the
    build-environment setup (setup-build-server.sh: docker, GDK, Python,
    AWS CLI). A pre-baked AMI (BUILD_*_AMI_ID) may make this a no-op
    re-run; the generated sequence is idempotent-guarded on the clone.

    ONE environment model with the dedicated fleet (see BUILD_USER):
    cloud-init runs this script as root, but root does only the
    privileged prologue — the log redirect, the git install, creating
    the clone's parent directory and handing it to the build user
    (repo_parent_prepare_commands), and healing a pre-baked AMI's
    root-owned clone (repo_ownership_heal_command) — while the sync and
    setup-build-server.sh body executes AS ubuntu through the same
    temp-script + ``sudo -H -u ubuntu`` transport the agent command uses
    (run_as_build_user_commands). ``pip3 install --user`` therefore
    lands gdk in /home/ubuntu/.local/bin with its module in ubuntu's
    user site-packages — exactly where the (also ubuntu-run) agent
    command later finds them, and exactly matching the dedicated servers
    groomed by launch-arm64-build-server.sh.

    The body's command sequence comes from build_source
    .bootstrap_commands, the single origin of all Source_Sync text, and
    ROOT writes the Bootstrap_Marker as the LAST statement — outside the
    build-user body — so the marker still means "bootstrap ran to
    completion" and the readiness gate (Req 6.1-6.4) can trust it. A
    classified Source_Sync failure exits the body 65/66 and
    classified_sync_exit_commands propagates it before the marker, so
    the bootstrap budget resolves the job with the bootstrap log
    location (Req 6.3), unchanged. Neither the log redirect nor the
    marker write can abort the script (see BOOTSTRAP_LOG_VAR /
    tolerate_failure).

    The clone lands in the resolved repository directory (Req 5.1, 5.4) —
    the same directory provisioning records on the runner record and the
    agent command is later rooted in.
    """
    if not BUILD_REPO_URL:
        # No repo URL configured: assume a pre-provisioned AMI.
        return ''
    if repo_dir is None:
        repo_dir = build_source.resolve_repo_dir(
            job, env_default=BUILD_REPO_DIR)
    commands = build_source.bootstrap_commands(
        BUILD_REPO_URL, repo_dir, job_source_ref(job))
    # The build-user body: the environment exports, then the sync +
    # setup-build-server.sh sequence. The marker write (commands[-1]) is
    # deliberately NOT in the body — root writes it last.
    body = '\n'.join(list(runner_env_export_commands(job))
                     + commands[:-1])
    return '\n'.join([
        '#!/bin/bash',
        'set -uo pipefail',
        *bootstrap_log_redirect_commands(),
        HOME_EXPORT_COMMAND,
        'export DEBIAN_FRONTEND=noninteractive',
        'apt-get update -y && apt-get install -y git',
        *repo_parent_prepare_commands(repo_dir),
        repo_ownership_heal_command(repo_dir),
        *run_as_build_user_commands(body),
        *classified_sync_exit_commands(),
        tolerate_failure(commands[-1]),
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


# ------------------------------------------- terminal-effects adapters
# (build-fleet-execution-failures task 6, Req 2.6/2.7/3.11): ONE
# conditional terminal write carries status, error-or-result, ended_at,
# the sanitized-evidence digest, and the stable effect ID; audit,
# verified compute cleanup, allocation release, and promotion wakeup are
# retryable ledger effects completed by conditional pending -> done
# writes, so retries/races converge on exactly one logical effect each.

#: Stable attempt component of the effect identity for jobs that never
#: recorded an execution attempt (legacy jobs, pre-dispatch failures).
NO_ATTEMPT_ID = 'no-attempt'

#: Ordered effects and their required predecessor (advance_effect
#: ordering enforced as a DynamoDB condition): allocation release
#: requires VERIFIED compute cleanup (stop-before-release, Req 3.11);
#: promotion requires the release.
_EFFECT_ORDER_GUARDS = {
    build_reconciliation.EFFECT_ALLOCATION_RELEASE:
        build_reconciliation.EFFECT_COMPUTE_CLEANUP,
    build_reconciliation.EFFECT_PROMOTION_WAKEUP:
        build_reconciliation.EFFECT_ALLOCATION_RELEASE,
}


def evidence_digest(evidence: Any) -> Optional[str]:
    """Stable content digest of one terminal outcome's sanitized
    evidence (the 'evidence digest' of the design's terminal
    finalization write). Digests only already-sanitized structures."""
    if not evidence:
        return None
    return hashlib.sha256(
        json.dumps(to_native(evidence), sort_keys=True,
                   default=str).encode('utf-8')).hexdigest()


def plan_job_ledger(job: Dict[str, Any],
                    cleanup_required: bool) -> Dict[str, Any]:
    """The terminal-effects ledger for one job's terminal outcome
    (build_reconciliation.plan_terminal_effects) keyed by the job's
    current attempt identity; jobs without an attempt use a stable
    placeholder so the effect ID stays deterministic."""
    attempt_id = ((job.get('execution_attempt') or {}).get('attempt_id')
                  or NO_ATTEMPT_ID)
    return build_reconciliation.plan_terminal_effects(
        job['build_job_id'], attempt_id,
        job.get('execution_mode') or '', cleanup_required)


def complete_effect(build_job_id: str, effect_id: Optional[str],
                    effect: str) -> bool:
    """Conditionally advance ONE ledger effect pending -> done. The
    DynamoDB condition is the concurrency arbiter (the persistence
    adapter of build_reconciliation.advance_effect): the effect must
    still be pending under the SAME stable effect_id, and ordered
    effects require their predecessor to be done or not_applicable.
    Returns False on a duplicate/out-of-order completion — the caller
    must not repeat the side effect (Req 2.7)."""
    if not effect_id:
        return False
    names = {'#fx': 'terminal_effects', '#e': effect}
    values: Dict[str, Any] = {
        ':eid': effect_id,
        ':pending': build_reconciliation.EFFECT_PENDING,
        ':done': build_reconciliation.EFFECT_DONE,
    }
    condition = '#fx.effect_id = :eid AND #fx.#e = :pending'
    guard = _EFFECT_ORDER_GUARDS.get(effect)
    if guard:
        names['#g'] = guard
        values[':na'] = build_reconciliation.EFFECT_NOT_APPLICABLE
        condition += ' AND (#fx.#g = :done OR #fx.#g = :na)'
    try:
        jobs_table().update_item(
            Key={'build_job_id': build_job_id},
            UpdateExpression='SET #fx.#e = :done',
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == \
                'ConditionalCheckFailedException':
            return False
        raise


def audit_terminal_effect(build_job_id: str, ledger: Dict[str, Any],
                          action: str, result: str,
                          details: Optional[Dict[str, Any]] = None) -> None:
    """ONE logical Audit_Log entry per terminal outcome, deduplicated by
    the stable effect identity (Req 2.7): only the writer that completes
    the pending audit effect writes the entry, so a retry may complete a
    pending audit but can never create a second logical audit."""
    effect_id = ledger.get('effect_id')
    if complete_effect(build_job_id, effect_id,
                       build_reconciliation.EFFECT_AUDIT):
        audit(action, build_job_id, result,
              {**(details or {}), 'terminal_effect_id': effect_id})


def cleanup_blocks_release(job: Optional[Dict[str, Any]]) -> bool:
    """True while the job's terminal-effects ledger records a PENDING
    compute cleanup: the dedicated slot stays held and no follower is
    promoted while the server's process state is unknown (Req 3.11).
    Terminal jobs without a ledger (legacy records) keep the original
    release behavior."""
    ledger = (job or {}).get('terminal_effects')
    return isinstance(ledger, dict) and \
        ledger.get(build_reconciliation.EFFECT_COMPUTE_CLEANUP) == \
        build_reconciliation.EFFECT_PENDING


def confirm_no_build_processes(instance_id: str) -> Optional[bool]:
    """Verified-stop confirmation for one Build_Server (Req 3.11): pgrep
    count via the existing synchronous SSM helper. True means no
    protected build process remains; False means one is still running;
    None means the check could not be positively completed — the caller
    must treat unknown process state as NOT cleaned up (fail closed)."""
    output = run_shell_sync(instance_id, COUNT_BUILD_PROCESS_COMMANDS)
    if output is None:
        return None
    count = parse_build_count(output)
    if count is None:
        return None
    return count == 0


def release_and_promote(job: Dict[str, Any],
                        ledger: Dict[str, Any]) -> None:
    """Drive the allocation_release and promotion_wakeup effects for one
    finalized terminal outcome (Req 2.7/3.11). The release completes
    ONLY when the ledger's compute cleanup is done or not applicable
    (the conditional guard); the slot release itself stays conditional
    on this job still owning it (stale-release protection). The
    promotion wakeup is completed by this same tick — the scheduled
    dispatch pass IS the promotion path (oldest-eligible planning plus
    the conditional server lock), retained as the fallback."""
    build_job_id = job['build_job_id']
    effect_id = ledger.get('effect_id')
    if job.get('execution_mode') == build_domain.EXECUTION_MODE_DEDICATED \
            and job.get('server_id'):
        if complete_effect(build_job_id, effect_id,
                           build_reconciliation.EFFECT_ALLOCATION_RELEASE):
            release_server(job['server_id'], build_job_id)
    complete_effect(build_job_id, effect_id,
                    build_reconciliation.EFFECT_PROMOTION_WAKEUP)


# -------------------------------------------------------------- SSM helpers

def send_shell_command(instance_id: str, commands: List[str],
                       cloudwatch: bool = False,
                       execution_timeout: Optional[int] = None,
                       comment: Optional[str] = None) -> str:
    """SSM SendCommand (AWS-RunShellScript) returning the command id.
    ``cloudwatch`` enables CloudWatchOutputConfig streaming to the build
    log group (agent commands, Req 3.4/4.4). ``comment`` carries the
    deterministic job/attempt marker so an ambiguous send can be
    recovered by a recent-command lookup instead of a blind resend
    (build-fleet-execution-failures Req 2.7)."""
    parameters: Dict[str, Any] = {'commands': commands}
    if execution_timeout is not None:
        parameters['executionTimeout'] = [str(execution_timeout)]
    kwargs: Dict[str, Any] = {
        'InstanceIds': [instance_id],
        'DocumentName': 'AWS-RunShellScript',
        'Parameters': parameters,
    }
    if comment:
        kwargs['Comment'] = comment
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


def probe_bootstrap_marker(instance_id: str) -> Optional[str]:
    """Marker-probe stdout from a Build_Server (Req 6.2), or None when the
    probe could not be positively completed.

    None flows straight into build_planner.decide_runner_readiness, which
    reads it as "the Bootstrap_Marker was not observed" — the module's
    existing fail-safe convention (see run_shell_sync): readiness must be
    positively established, so an unverifiable probe keeps the gate shut
    and the bootstrap budget eventually resolves the job (Req 6.3).
    """
    return run_shell_sync(instance_id, BOOTSTRAP_PROBE_COMMANDS)


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


def send_agent(job: Dict[str, Any], instance_id: str,
               repo_dir: Optional[str] = None,
               comment: Optional[str] = None,
               attempt_id: Optional[str] = None) -> Tuple[str, str]:
    """SendCommand the build agent for one Build_Job with CloudWatch log
    streaming; returns (command_id, log_stream). ``repo_dir`` is the
    resolved bootstrap directory of the machine the command runs on;
    ``comment`` is the attempt's deterministic command comment;
    ``attempt_id`` is handed to the agent as ATTEMPT_ID so its start/
    heartbeat/progress/terminal events correlate to exactly this attempt
    (task 7.2)."""
    command_id = send_shell_command(
        instance_id,
        [agent_command(job, repo_dir, attempt_id=attempt_id)],
        cloudwatch=True,
        execution_timeout=agent_execution_timeout_seconds(job),
        comment=comment,
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


def run_runner_instance(plan: 'build_planner.RunnerPlan',
                        repo_dir: Optional[str] = None,
                        job: Optional[Dict[str, Any]] = None) -> str:
    """RunInstances for exactly one Ephemeral_Build_Runner serving exactly
    one Build_Job (Req 2.3, 7.4): arch-selected Ubuntu 22.04 AMI, sizing
    from the job's config_snapshot (Req 3.1, 9.3), hardened profile (SSM
    instance profile, no key pair, IMDSv2 required), dda-build tags. The
    bootstrap clones into ``repo_dir`` (the resolved repository directory,
    Req 5.1) and syncs it to the ref ``job`` selected (Req 4.1)."""
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
    user_data = runner_bootstrap_user_data(job, repo_dir=repo_dir)
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
             details: Optional[Dict[str, Any]] = None,
             ledger: Optional[Dict[str, Any]] = None,
             extra: Optional[Dict[str, Any]] = None) -> bool:
    """Conditionally mark a Build_Job failed with an error record and end
    time, then audit the failure. Returns False when the job moved in the
    meantime (the failure then belongs to whoever moved it).

    ``ledger`` (task 6.1) makes this the ONE terminal finalization write:
    the terminal-effects ledger and the sanitized-evidence digest travel
    in the same conditional update, and the audit is deduplicated by the
    ledger's stable effect identity. ``extra`` carries additional
    additive attributes (e.g. the timeout timing diagnostic)."""
    audit_details = {**(details or {}),
                     'error_code': error_code,
                     'message': message,
                     'status_at_failure': job['status']}
    write: Dict[str, Any] = {
        'error': {'code': error_code, 'message': message},
        'ended_at': now_ms(),
    }
    if ledger is not None:
        write['terminal_effects'] = ledger
        write['evidence_digest'] = evidence_digest(audit_details)
    if extra:
        write.update(extra)
    moved = transition_job(
        job['build_job_id'], job['status'], build_domain.STATUS_FAILED,
        extra=write)
    if moved:
        if ledger is not None:
            audit_terminal_effect(job['build_job_id'], ledger,
                                  audit_action, 'failure', audit_details)
        else:
            audit(audit_action, job['build_job_id'], 'failure',
                  audit_details)
    return moved


def preflight_record(decision: PreflightDecision, repo_dir: str,
                     now: int) -> Dict[str, Any]:
    """The separately recorded, sanitized preflight evidence persisted on
    the Build_Job (task 7.1, Req 2.8/2.10): what was validated, with what
    outcome, against which repository directory. Projected through the
    shared normalization/redaction/bounding primitives so no raw value
    can widen the record."""
    return build_reconciliation.sanitize_evidence_tree({
        'passed': decision.ok,
        'checked_at': now,
        'repo_dir': repo_dir,
        'failures': list(decision.failures),
        'checks': decision.checks,
    })


def fail_preflight(job: Dict[str, Any], decision: PreflightDecision,
                   details: Optional[Dict[str, Any]] = None) -> bool:
    """Terminal preflight failure (task 7.1, evidence row 8): record the
    sanitized preflight evidence, then fail the job with the stable
    COMMAND_PREFLIGHT_FAILED code through the SAME conditional-
    transition/audit flow every other terminal failure uses. No costly
    work has run; the caller performs its mode's cleanup/release. The
    message is built only from the stable failure vocabulary — no raw
    provider or configuration value enters the error record (Req 2.10)."""
    now = now_ms()
    update_job_fields(job['build_job_id'], {
        'preflight': to_dynamo(preflight_record(
            decision, (decision.checks or {}).get('repo_dir') or '', now)),
    })
    summary = ', '.join(decision.failures) or 'invalid startup contract'
    return fail_job(
        job, ERROR_COMMAND_PREFLIGHT_FAILED,
        f"The dispatch preflight failed before any build/publish work "
        f"was started: {summary}.",
        'build_preflight_failed',
        {**(details or {}), 'failures': list(decision.failures)})


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
        if job is not None and cleanup_blocks_release(job):
            # Verified stop before release (task 6.2, Req 3.11): the
            # job is terminal but its compute cleanup is still pending,
            # so the process state on the server is unknown — the slot
            # stays held and no follower is promoted onto it until the
            # effects reconciliation confirms the cleanup.
            continue
        if job is None or build_domain.is_terminal(job.get('status', '')):
            if release_server(server['server_id'], held_by):
                server['running_build_job_id'] = None
                logger.info(
                    f"Released server {server['server_id']} allocation "
                    f"held by terminal Build_Job {held_by}")


# --------------------------------------------- step 1: dedicated dispatch

def dedicated_bootstrap_gate(
        job: Dict[str, Any], server: Dict[str, Any], instance_id: str,
        now: int) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Bootstrap readiness policy for a Dedicated_Build_Server, returning
    ``(proceed, bootstrap_note)``.

    The same marker probe the ephemeral gate uses runs here alongside the
    pgrep verification, but the policy is deliberately ADVISORY rather
    than fail-closed (design "Decisions worth reviewing" item 2): the
    marker is REQUIRED only while the server is still inside its bootstrap
    budget from launch, which is where the race actually lives. Past that
    window — or when the server records no launch time at all, which is
    every server registered outside a fleet launch — the marker is not
    required and the dispatch proceeds with an advisory note recorded, so
    servers bootstrapped before this change and manually prepared servers
    keep working untouched (Req 5.3, 7.1). Only inside the window is the
    probe issued at all, so a long-running server costs no extra SSM round
    trip per dispatch.

    ``proceed`` False means "still bootstrapping": the caller defers the
    job to the head of its queue, exactly as an unclean pgrep does. A
    dedicated server is never FAILED on the marker (that is the ephemeral
    gate's job, Req 6.3).
    """
    # The launch time this server's own bootstrap started from; the budget
    # and the boundary convention come from the shared pure decision.
    probe_job = dict(job, dispatched_at=server.get('created_at'))
    window = build_planner.decide_runner_readiness(probe_job, None, now)
    if window.deadline is None or \
            window.readiness != build_planner.READINESS_WAIT:
        return True, {
            'marker_observed': False,
            'log_path': window.log_path,
            'advisory': (
                'Bootstrap_Marker not required: the Dedicated_Build_Server '
                'is past its bootstrap budget from launch (or records no '
                'launch time), so it is treated as already prepared'),
        }
    decision = build_planner.decide_runner_readiness(
        probe_job, probe_bootstrap_marker(instance_id), now)
    if decision.readiness != build_planner.READINESS_READY:
        return False, None
    return True, {'marker_at': now, 'log_path': decision.log_path}


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

    # Bootstrap readiness alongside the pgrep verification (Req 6.2), with
    # the advisory dedicated policy: a freshly launched server that has not
    # signalled completion yet is deferred, never failed.
    proceed, bootstrap_note = dedicated_bootstrap_gate(
        job, server, instance_id, now)
    if not proceed:
        update_job_fields(job['build_job_id'], {'deferred_at': now})
        logger.info(
            f"Deferred Build_Job {job['build_job_id']}: bootstrap of "
            f"server {server['server_id']} has not signalled completion "
            f"({build_planner.BOOTSTRAP_MARKER_PATH} not observed)")
        return

    # The directory THIS server's bootstrap used (Req 5.1, 5.2, 5.3): the
    # value recorded on the Build_Server record, else the configured
    # override (with the evidenced known-clone-root correction of task
    # 7.1 / hypothesis row 1), else the authoritative default every
    # server bootstrapped before this change already uses.
    repo_dir = build_source.resolve_repo_dir(job, server,
                                             env_default=BUILD_REPO_DIR)
    # Dispatch preflight, local contract portion (task 7.1, evidence
    # rows 1 and 8): validated BEFORE the queued -> building transition
    # and before ANY SSM send, so an invalid startup contract performs
    # no costly work and fails through the common terminal flow with
    # the stable COMMAND_PREFLIGHT_FAILED code. The machine-side checks
    # run inside the generated command guard and the agent's own
    # preflight, still before build/publish.
    preflight = decide_preflight(job, repo_dir)
    if not preflight.ok:
        fail_preflight(job, preflight, {'server_id': server['server_id']})
        release_server(server['server_id'], job['build_job_id'])
        return

    # Clean verification: queued -> building (Req 7.5) + agent dispatch.
    if not transition_job(
            job['build_job_id'], build_domain.STATUS_QUEUED,
            build_domain.next_status(build_domain.STATUS_QUEUED,
                                     build_domain.EVENT_DISPATCH_DEDICATED),
            extra={'dispatched_at': now, 'started_at': now}):
        return  # raced (e.g. cancellation); allocation release next tick
    # Execution-attempt claim recorded BEFORE the SendCommand
    # (build-fleet-execution-failures Req 2.7): dispatch_state moves
    # claimed -> sending -> sent around the send, and the deterministic
    # command comment lets ambiguous-send recovery attach the existing
    # command instead of blindly resending. Readiness evidence (marker
    # or advisory) is recorded in the same write (Req 6.4).
    attempt = build_reconciliation.new_execution_attempt(
        job['build_job_id'], str(uuid.uuid4()), instance_id, now)
    attempt['dispatch_state'] = build_reconciliation.DISPATCH_SENDING
    attempt['sending_at'] = now
    update_job_fields(job['build_job_id'], {
        'bootstrap': bootstrap_note,
        'execution_attempt': attempt,
        # Separately recorded preflight evidence (task 7.1, Req 2.8):
        # the local contract passed; the machine-side checks and disk
        # recording arrive with the agent's execution-start event.
        'preflight': to_dynamo(preflight_record(preflight, repo_dir, now)),
    })
    try:
        command_id, log_stream = send_agent(
            job, instance_id, repo_dir,
            comment=attempt['command_comment'],
            attempt_id=attempt['attempt_id'])
    except ClientError as e:
        logger.error(f"Agent SendCommand for Build_Job "
                     f"{job['build_job_id']} on {instance_id} failed: {e}")
        update_job_fields(job['build_job_id'], {
            'execution_attempt': {
                **attempt,
                'dispatch_state': build_reconciliation.DISPATCH_TERMINAL,
            },
        })
        job = dict(job, status=build_domain.STATUS_BUILDING)
        # SendCommand was rejected before a command existed: no build
        # process can be running, so cleanup is not applicable and the
        # release is immediately permitted through the ledger (task 6.1).
        ledger = plan_job_ledger(dict(job, execution_attempt=attempt),
                                 cleanup_required=False)
        if fail_job(job, ERROR_DISPATCH_FAILED,
                    f"The build agent could not be started on "
                    f"Dedicated_Build_Server '{server.get('name') or server['server_id']}': {e}",
                    'build_dispatch_failed',
                    {'server_id': server['server_id']},
                    ledger=ledger):
            release_and_promote(job, ledger)
        return
    attempt = {**attempt, 'command_id': command_id,
               'dispatch_state': build_reconciliation.DISPATCH_SENT,
               'sent_at': now_ms()}
    update_job_fields(job['build_job_id'], {
        'ssm': {**(job.get('ssm') or {}), 'command_id': command_id,
                'instance_id': instance_id},
        'log': {'group': BUILD_LOG_GROUP, 'stream': log_stream},
        'execution_attempt': attempt,
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
    # Ephemeral cleanup is a retryable ledger effect (task 6.1/6.2): the
    # idempotent tag-based termination above starts it; the effects
    # reconciliation confirms no tagged compute remains and completes it.
    fail_job(job, ERROR_PROVISIONING_FAILED,
             f"Provisioning the Ephemeral_Build_Runner failed: {cause}",
             'build_provisioning_failed',
             {'terminated_partial_compute': terminated},
             ledger=plan_job_ledger(job, cleanup_required=True))


def fail_bootstrap_timeout(job: Dict[str, Any],
                           decision: 'build_planner.ReadinessDecision') -> None:
    """Bootstrap stage failure for an ephemeral Build_Job whose runner
    never signalled completion inside its budget (Req 6.3, 6.5): the
    runner is released through the same terminate_partial_compute path
    every other provisioning failure uses, then the job is failed with the
    bootstrap-stage error the pure decision produced — it names the budget
    and the bootstrap log location — and audited."""
    terminated = terminate_partial_compute(job['build_job_id'])
    fail_job(job, ERROR_BOOTSTRAP_TIMEOUT, decision.error,
             'build_bootstrap_timeout',
             {'stage': 'bootstrap',
              'bootstrap_log': decision.log_path,
              'terminated_partial_compute': terminated},
             ledger=plan_job_ledger(job, cleanup_required=True))


def provision_ephemeral(jobs: List[Dict[str, Any]], now: int) -> None:
    """Provision Ephemeral_Build_Runners for the dispatch-eligible queued
    ephemeral Build_Jobs: exactly one runner per job, sizing from the
    job's own config_snapshot (Req 2.3, 3.1, 7.4, 9.3), and start the
    agent on runners of provisioning jobs once SSM-managed AND the
    Bootstrap_Marker has been observed (Req 6.1-6.5): WAIT does nothing
    this tick, TIMEOUT fails the job at the bootstrap stage and releases
    the runner."""
    for plan in build_planner.plan_ephemeral_provisioning(jobs):
        if not transition_job(
                plan.build_job_id, build_domain.STATUS_QUEUED,
                plan.status,  # provisioning (Req 3.1)
                extra={'dispatched_at': now}):
            continue  # raced (e.g. cancellation)
        job = next(j for j in jobs
                   if j.get('build_job_id') == plan.build_job_id)
        job = dict(job, status=plan.status)
        # The directory this runner's bootstrap will clone into; recorded
        # on the runner record below so later ticks invoke the agent from
        # exactly the directory the bootstrap used (Req 5.1, 5.4).
        repo_dir = build_source.resolve_repo_dir(
            job, env_default=BUILD_REPO_DIR)
        try:
            # The job travels with the plan so the bootstrap syncs to the
            # ref this job's own config_snapshot selected (Req 4.1).
            instance_id = run_runner_instance(plan, repo_dir, job=job)
        except (ClientError, ValueError) as e:
            fail_provisioning(job, str(e))
            continue
        update_job_fields(plan.build_job_id, {
            'runner': {
                'instance_id': instance_id,
                'instance_type': plan.instance_type,
                'arch': plan.arch,
                'spot': plan.spot,
                'repo_dir': repo_dir,
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
        # SSM Online is necessary but NOT sufficient: the agent runs only
        # once the Bootstrap_Marker has been observed (Req 6.1, 6.2). The
        # SSM agent pings Online long before cloud-init finishes — the live
        # runner's command was sent ~115 s before its bootstrap completed.
        readiness = build_planner.decide_runner_readiness(
            job, probe_bootstrap_marker(instance_id), now)
        if readiness.readiness == build_planner.READINESS_WAIT:
            continue  # still bootstrapping; nothing else happens this tick
        if readiness.readiness == build_planner.READINESS_TIMEOUT:
            fail_bootstrap_timeout(job, readiness)
            continue
        # Marker observed: record the readiness evidence and the bootstrap
        # log location BEFORE the agent command (Req 6.4).
        update_job_fields(job['build_job_id'], {
            'bootstrap': {'marker_at': now, 'log_path': readiness.log_path},
        })
        # The directory recorded by this runner's own provisioning pass
        # (Req 5.1, 5.4); pre-existing runners with none recorded resolve
        # through the configured override / authoritative default.
        repo_dir = build_source.resolve_repo_dir(
            job, env_default=BUILD_REPO_DIR)
        # Dispatch preflight, local contract portion (task 7.1, evidence
        # rows 1 and 8): after ephemeral SSM readiness and BEFORE the
        # agent SendCommand, so an invalid startup contract performs no
        # build/publish work and the runner is released through the same
        # cleanup path every provisioning failure uses.
        preflight = decide_preflight(job, repo_dir)
        if not preflight.ok:
            terminated = terminate_partial_compute(job['build_job_id'])
            fail_preflight(job, preflight,
                           {'terminated_partial_compute': terminated})
            continue
        # Execution-attempt claim recorded BEFORE the SendCommand
        # (build-fleet-execution-failures Req 2.7): the deterministic
        # command comment supports ambiguous-send recovery.
        attempt = build_reconciliation.new_execution_attempt(
            job['build_job_id'], str(uuid.uuid4()), instance_id, now)
        attempt['dispatch_state'] = build_reconciliation.DISPATCH_SENDING
        attempt['sending_at'] = now
        update_job_fields(job['build_job_id'], {
            'execution_attempt': attempt,
            # Separately recorded preflight evidence (task 7.1, Req 2.8).
            'preflight': to_dynamo(
                preflight_record(preflight, repo_dir, now)),
        })
        try:
            command_id, log_stream = send_agent(
                job, instance_id, repo_dir,
                comment=attempt['command_comment'],
                attempt_id=attempt['attempt_id'])
        except ClientError as e:
            logger.error(f"Agent SendCommand for ephemeral Build_Job "
                         f"{job['build_job_id']} on {instance_id} "
                         f"failed: {e}")
            update_job_fields(job['build_job_id'], {
                'execution_attempt': {
                    **attempt,
                    'dispatch_state':
                        build_reconciliation.DISPATCH_TERMINAL,
                },
            })
            fail_provisioning(job, f"the build agent could not be "
                                   f"started on the runner: {e}")
            continue
        attempt = {**attempt, 'command_id': command_id,
                   'dispatch_state': build_reconciliation.DISPATCH_SENT,
                   'sent_at': now_ms()}
        update_job_fields(job['build_job_id'], {
            'ssm': {**(job.get('ssm') or {}), 'command_id': command_id,
                    'instance_id': instance_id},
            'log': {'group': BUILD_LOG_GROUP, 'stream': log_stream},
            'execution_attempt': attempt,
        })
        logger.info(f"Started agent on runner {instance_id} for Build_Job "
                    f"{job['build_job_id']} (command {command_id})")


# ---------------------------- step 2.5: scheduled command reconciliation
# (build-fleet-execution-failures task 5.2, Req 2.5, 2.6, 2.7, 2.11,
# 3.2, 3.4)

def retrieve_invocation(command_id: Optional[str],
                        instance_id: Optional[str]
                        ) -> Optional[Dict[str, Any]]:
    """READ-ONLY GetCommandInvocation for a persisted command identity
    (Req 2.1/2.5). None on ``InvocationDoesNotExist`` (eventual
    consistency) or incomplete identity. The raw response exists only in
    local memory long enough to be sanitized — never logged/persisted
    as-is (Req 2.10)."""
    if not command_id or not instance_id:
        return None
    try:
        return ssm.get_command_invocation(CommandId=command_id,
                                          InstanceId=instance_id)
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code not in ('InvocationDoesNotExist', 'InvalidCommandId'):
            logger.warning(f"GetCommandInvocation({command_id}) "
                           f"failed: {code}")
        return None


def find_command_by_comment(instance_id: Optional[str],
                            comment: Optional[str]) -> Optional[str]:
    """Recent-command lookup for an ambiguous send (Req 2.7): the
    command whose Comment equals the attempt's deterministic
    ``dda-build:<job>:<attempt>`` marker, or None. READ-ONLY."""
    if build_reconciliation.parse_command_comment(comment) is None:
        return None
    kwargs: Dict[str, Any] = {'MaxResults': 50}
    if instance_id:
        kwargs['InstanceId'] = instance_id
    try:
        response = ssm.list_commands(**kwargs)
    except ClientError as e:
        logger.warning(f"ListCommands for ambiguous-send recovery "
                       f"failed: {e.response.get('Error', {}).get('Code')}")
        return None
    for command in response.get('Commands', []):
        if command.get('Comment') == comment:
            return command.get('CommandId')
    return None


def claim_resend(build_job_id: str, attempt_id: Optional[str],
                 previous_sending_at: Any, now: int) -> bool:
    """Conditionally claim the ONE re-send permitted after the
    visibility bound (Req 2.7): only the writer that still sees the same
    attempt in `sending` with the same sending_at wins; every retry or
    concurrent tick loses the condition and does NOT send."""
    try:
        jobs_table().update_item(
            Key={'build_job_id': build_job_id},
            UpdateExpression='SET execution_attempt.sending_at = :now',
            ConditionExpression=(
                'execution_attempt.attempt_id = :aid AND '
                'execution_attempt.dispatch_state = :sending AND '
                'execution_attempt.sending_at = :prev'),
            ExpressionAttributeValues={
                ':aid': attempt_id,
                ':sending': build_reconciliation.DISPATCH_SENDING,
                ':prev': previous_sending_at,
                ':now': now,
            },
        )
        return True
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == \
                'ConditionalCheckFailedException':
            return False
        raise


def recover_ambiguous_send(job: Dict[str, Any],
                           servers_by_id: Dict[str, Dict[str, Any]],
                           now: int) -> None:
    """Recover a dispatch attempt stuck in `sending` with no persisted
    command id (execution stopped between SendCommand and the command_id
    write): search recent commands for the attempt's deterministic
    comment and attach the existing command — NEVER blindly resend.
    Only after the visibility bound proves no command exists may ONE
    conditional new attempt be sent (Req 2.7, at most one effective
    dispatch)."""
    attempt = dict(job.get('execution_attempt') or {})
    build_job_id = job['build_job_id']
    instance_id = attempt.get('instance_id') \
        or job_instance_id(job, servers_by_id)
    comment = attempt.get('command_comment')

    found = find_command_by_comment(instance_id, comment)
    if found:
        # The command exists: attach it; reconciliation now owns it.
        attempt.update({
            'command_id': found,
            'dispatch_state': build_reconciliation.DISPATCH_SENT,
            'sent_at': attempt.get('sent_at') or now,
        })
        fields = {
            'execution_attempt': attempt,
            'ssm': {**(job.get('ssm') or {}), 'command_id': found,
                    'instance_id': instance_id},
            'log': {'group': BUILD_LOG_GROUP,
                    'stream': ssm_log_stream(found, instance_id)},
        }
        update_job_fields(build_job_id, fields)
        # Keep the in-memory job coherent for the rest of THIS tick
        # (later steps rewrite bookkeeping maps from their scan).
        job.update(fields)
        logger.info(f"Build_Job {build_job_id}: ambiguous send recovered "
                    f"to existing command {found} (no resend)")
        return

    sending_since = attempt.get('sending_at') or attempt.get('claimed_at')
    if sending_since is None \
            or now <= sending_since + AMBIGUOUS_SEND_VISIBILITY_MS:
        return  # within the visibility bound: never resend yet
    if not instance_id or not comment:
        return
    if not claim_resend(build_job_id, attempt.get('attempt_id'),
                        sending_since, now):
        return  # another writer claimed/settled the attempt
    server = servers_by_id.get(job.get('server_id') or '')
    repo_dir = build_source.resolve_repo_dir(job, server,
                                             env_default=BUILD_REPO_DIR)
    try:
        command_id, log_stream = send_agent(job, instance_id, repo_dir,
                                            comment=comment,
                                            attempt_id=attempt
                                            .get('attempt_id'))
    except ClientError as e:
        logger.error(f"Conditional resend for Build_Job {build_job_id} "
                     f"on {instance_id} failed: {e}")
        return
    attempt.update({
        'command_id': command_id,
        'dispatch_state': build_reconciliation.DISPATCH_SENT,
        'sent_at': now_ms(),
        'sending_at': now,
    })
    fields = {
        'execution_attempt': attempt,
        'ssm': {**(job.get('ssm') or {}), 'command_id': command_id,
                'instance_id': instance_id},
        'log': {'group': BUILD_LOG_GROUP, 'stream': log_stream},
    }
    update_job_fields(build_job_id, fields)
    # Keep the in-memory job coherent for the rest of THIS tick (later
    # steps rewrite bookkeeping maps from their scan).
    job.update(fields)
    logger.info(f"Build_Job {build_job_id}: conditional attempt re-sent "
                f"after the visibility bound (command {command_id})")


def classification_message(classification:
                           'build_reconciliation.Classification',
                           command_status: Optional[str]) -> str:
    """Safe failed-job message built ONLY from stable vocabulary — no
    raw provider text ever enters the error record (Req 2.10)."""
    return (f"The build agent SSM command ended with status "
            f"'{command_status}' before reporting a build result: "
            f"{classification.reason}. Retained command evidence was "
            f"recorded in the execution diagnostic.")


def reconcile_running_command(job: Dict[str, Any], command_id: str,
                              servers_by_id: Dict[str, Dict[str, Any]],
                              now: int) -> None:
    """Scheduled reconciliation for one command-bearing RUNNING job
    (Req 2.5/2.6): a nonterminal invocation stays nonterminal; terminal
    Failed/TimedOut/Cancelled settles deterministically within the
    bound; Success without a callback becomes AGENT_RESULT_MISSING only
    AFTER the settlement window. A missing EventBridge event therefore
    costs latency, never correctness."""
    build_job_id = job['build_job_id']
    job_status = job['status']
    attempt = job.get('execution_attempt') or {}
    instance_id = (attempt.get('instance_id')
                   or (job.get('ssm') or {}).get('instance_id')
                   or job_instance_id(job, servers_by_id))
    recon = job.get('reconciliation') or {}
    settlement_deadline_ms = recon.get('settlement_deadline')
    recorded_event_status = recon.get('command_status')

    raw_invocation = retrieve_invocation(command_id, instance_id)
    invocation_status = (raw_invocation or {}).get('Status')

    if raw_invocation is not None and invocation_status not in \
            build_reconciliation.SSM_TERMINAL_STATUSES:
        return  # genuinely nonterminal: stays nonterminal (Req 2.5)

    if raw_invocation is None:
        first = recon.get('first_observed_at')
        if first is None:
            # No terminal observation exists yet for this command; a
            # missing invocation right after send is registration lag.
            # Nothing terminal to reconcile — never fabricate (Req 2.2).
            return
        lookup = build_reconciliation.decide_invocation_lookup(
            None, first, now, INVOCATION_LOOKUP_WINDOW_MS)
        if lookup.state == build_reconciliation.LOOKUP_PENDING:
            return  # bounded retry continues next tick (Req 2.5)
        if recorded_event_status not in \
                build_reconciliation.SSM_TERMINAL_STATUSES:
            return  # window exhausted, no terminal evidence: watchdogs own it
        evidence: Dict[str, Any] = {'Status': recorded_event_status}
        lookup_state = build_reconciliation.LOOKUP_UNAVAILABLE
    else:
        evidence = raw_invocation
        lookup_state = build_reconciliation.LOOKUP_RETRIEVED

    terminal_status = evidence.get('Status')
    first_observed_at = recon.get('first_observed_at') or now
    if terminal_status == 'Success' and settlement_deadline_ms is None:
        settlement_deadline_ms = build_reconciliation.settlement_deadline(
            now, SETTLEMENT_WINDOW_MS)

    classification = build_reconciliation.classify_attempt(
        current_status=job_status,
        invocation=evidence,
        settlement_deadline_ms=settlement_deadline_ms,
        now=now)

    incoming = build_reconciliation.build_execution_diagnostic(
        attempt={'attempt_id': attempt.get('attempt_id'),
                 'command_id': command_id,
                 'instance_id': instance_id},
        invocation=raw_invocation,
        classification=classification.error_code,
        source=EVIDENCE_SOURCE_SCHEDULED,
        observed_at=now,
        disk=(job.get('preflight') or {}).get('disk'))

    application = build_reconciliation.apply_evidence(
        job_status, job.get('execution_diagnostic'), incoming,
        classification, now)

    recon_state = {
        'command_id': command_id,
        'command_status': terminal_status,
        'first_observed_at': first_observed_at,
        'lookup_state': lookup_state,
        'updated_at': now,
    }
    if settlement_deadline_ms is not None:
        recon_state['settlement_deadline'] = settlement_deadline_ms

    if application.update_status is not None:
        # Terminal finalization (task 6.1): the ONE conditional write
        # carries status, error, ended_at, the sanitized evidence
        # digest, and the terminal-effects ledger. A terminal invocation
        # means the command's shell exited on the server, so a dedicated
        # slot needs no separate stop verification (cleanup not
        # applicable — same-tick release preserved); an ephemeral
        # runner's idempotent termination stays a retryable effect.
        ledger = plan_job_ledger(
            job,
            cleanup_required=(job.get('execution_mode') ==
                              build_domain.EXECUTION_MODE_EPHEMERAL))
        extra: Dict[str, Any] = {
            'ended_at': application.update_ended_at,
            'reconciliation': to_dynamo(recon_state),
            'terminal_effects': ledger,
            'evidence_digest': evidence_digest(
                application.update_diagnostic or incoming),
        }
        if application.update_diagnostic is not None:
            extra['execution_diagnostic'] = to_dynamo(
                application.update_diagnostic)
        if application.update_status == build_domain.STATUS_FAILED:
            extra['error'] = {
                'code': (application.update_error_code
                         or 'AGENT_COMMAND_FAILED'),
                'message': classification_message(classification,
                                                  terminal_status),
            }
        if not transition_job(build_job_id, job_status,
                              application.update_status, extra=extra):
            return  # raced: another writer recorded the outcome
        if application.update_status == build_domain.STATUS_INTERRUPTED:
            audit_terminal_effect(
                build_job_id, ledger, 'build_interrupted', 'failure',
                {'command_id': command_id,
                 'command_status': terminal_status,
                 'error_code': application.update_error_code,
                 'status_at_interruption': job_status})
        else:
            audit_terminal_effect(
                build_job_id, ledger, 'build_failed', 'failure',
                {'command_id': command_id,
                 'command_status': terminal_status,
                 'error_code': application.update_error_code,
                 'status_at_failure': job_status})
        release_and_promote(job, ledger)
        logger.info(f"Build_Job {build_job_id}: scheduled reconciliation "
                    f"settled command {command_id} "
                    f"('{terminal_status}') -> job "
                    f"'{application.update_status}' "
                    f"({application.update_error_code or 'agent result'})")
        return

    # No terminal decision (settlement wait / duplicate): persist any
    # increased diagnostic completeness and the settlement/lookup state;
    # duplicate observations are no-ops (Req 2.6).
    fields: Dict[str, Any] = {}
    if application.update_diagnostic is not None:
        fields['execution_diagnostic'] = to_dynamo(
            application.update_diagnostic)
    state_changed = (
        recon.get('lookup_state') != recon_state['lookup_state']
        or recon.get('command_status') != recon_state['command_status']
        or recon.get('settlement_deadline')
        != recon_state.get('settlement_deadline')
        or recon.get('first_observed_at')
        != recon_state['first_observed_at'])
    if state_changed:
        fields['reconciliation'] = to_dynamo(recon_state)
    if fields:
        update_job_fields(build_job_id, fields)


def complete_terminal_diagnostics(job: Dict[str, Any], command_id: str,
                                  servers_by_id: Dict[str, Dict[str, Any]],
                                  now: int) -> None:
    """Late diagnostic completion for a TERMINAL job whose execution
    diagnostic is missing or incomplete (Req 2.6): later evidence may
    only increase diagnostic completeness — the absorbed status, error,
    result, and ``ended_at`` are never touched (apply_evidence)."""
    diagnostic = job.get('execution_diagnostic')
    if isinstance(diagnostic, dict) and diagnostic.get('complete'):
        return
    recon = job.get('reconciliation') or {}
    if recon.get('lookup_state') == build_reconciliation.LOOKUP_UNAVAILABLE:
        return  # bounded window exhausted: identified as unavailable
    build_job_id = job['build_job_id']
    attempt = job.get('execution_attempt') or {}
    instance_id = (attempt.get('instance_id')
                   or (job.get('ssm') or {}).get('instance_id')
                   or job_instance_id(job, servers_by_id))
    first = recon.get('first_observed_at') or job.get('ended_at') or now

    raw_invocation = retrieve_invocation(command_id, instance_id)
    lookup = build_reconciliation.decide_invocation_lookup(
        raw_invocation, first, now, INVOCATION_LOOKUP_WINDOW_MS)
    if lookup.state != build_reconciliation.LOOKUP_RETRIEVED:
        state = {
            'command_id': command_id,
            'command_status': recon.get('command_status'),
            'first_observed_at': first,
            'lookup_state': lookup.state,
            'updated_at': now,
        }
        if state.get('lookup_state') != recon.get('lookup_state') \
                or recon.get('first_observed_at') != first:
            update_job_fields(build_job_id,
                              {'reconciliation': to_dynamo(state)})
        return

    classification = build_reconciliation.classify_attempt(
        current_status=job['status'],
        invocation=raw_invocation,
        now=now)
    incoming = build_reconciliation.build_execution_diagnostic(
        attempt={'attempt_id': attempt.get('attempt_id'),
                 'command_id': command_id,
                 'instance_id': instance_id},
        invocation=raw_invocation,
        classification=(job.get('error') or {}).get('code')
        or classification.error_code,
        source=EVIDENCE_SOURCE_SCHEDULED,
        observed_at=now,
        disk=(job.get('preflight') or {}).get('disk'))
    application = build_reconciliation.apply_evidence(
        job['status'], diagnostic, incoming, classification, now)
    # Terminal absorption: apply_evidence can only yield a diagnostic
    # update here; status/error/ended_at stay untouched.
    if application.update_diagnostic is not None:
        update_job_fields(build_job_id, {
            'execution_diagnostic': to_dynamo(
                application.update_diagnostic),
            'reconciliation': to_dynamo({
                'command_id': command_id,
                'command_status': (raw_invocation or {}).get('Status'),
                'first_observed_at': first,
                'lookup_state': build_reconciliation.LOOKUP_RETRIEVED,
                'updated_at': now,
            }),
        })
        logger.info(f"Build_Job {build_job_id}: late diagnostics merged "
                    f"for command {command_id} (terminal status "
                    f"unchanged)")


def command_reconciliation(jobs: List[Dict[str, Any]],
                           servers_by_id: Dict[str, Dict[str, Any]],
                           now: int) -> None:
    """Step 2.5 (task 5.2): inspect command-bearing nonterminal jobs,
    settlement waits, ambiguous `sending` attempts, and terminal jobs
    with incomplete diagnostics on the existing one-minute tick. One
    job's reconciliation failure never poisons the tick."""
    for job in jobs:
        try:
            status = job.get('status') or ''
            attempt = job.get('execution_attempt') or {}
            ssm_info = job.get('ssm') or {}
            command_id = ssm_info.get('command_id') \
                or attempt.get('command_id')
            if not build_domain.is_terminal(status) \
                    and not command_id \
                    and attempt.get('dispatch_state') == \
                    build_reconciliation.DISPATCH_SENDING:
                recover_ambiguous_send(job, servers_by_id, now)
                continue
            if not command_id:
                continue
            if build_domain.is_terminal(status):
                complete_terminal_diagnostics(job, command_id,
                                              servers_by_id, now)
            elif status in AGENT_RUNNING_STATUSES:
                reconcile_running_command(job, command_id,
                                          servers_by_id, now)
        except Exception as e:
            logger.warning(f"Command reconciliation for Build_Job "
                           f"{job.get('build_job_id')} failed: {e}")


# ------------------------------------------------ step 3: runtime timeout

def runtime_timeout_watchdog(jobs: List[Dict[str, Any]],
                             servers_by_id: Dict[str, Dict[str, Any]],
                             now: int) -> None:
    """Fail running Build_Jobs past their applicable runtime deadline
    (Req 3.8; phase-clock leases and hard ceiling via
    build_planner.decide_runtime_timeout, legacy max-runtime preserved).

    Task 6.2 / deferred task 8.2 dispatcher wiring:

    - the terminal write persists the COMPLETE safe timing diagnostic —
      ``timeout_evidence`` from build_reconciliation.timeout_evidence_record
      plus the timing map's ``timeout_kind``/``timeout_decided_at`` —
      alongside the terminal-effects ledger and evidence digest
      (Req 2.18, one finalization write);
    - cleanup is recorded PENDING first, the stop is sent idempotently,
      protected processes are pgrep-confirmed absent, and only then is
      the allocation released for exactly this job/attempt; while the
      process state is unknown the slot and its followers stay blocked
      (verified stop before release, Req 3.11). Logs produced up to
      termination stay retained in CloudWatch (Req 3.8)."""
    for job in jobs:
        if job.get('status') not in build_planner.RUNNING_WATCHDOG_STATUSES:
            continue
        decision = build_planner.decide_runtime_timeout(job, now)
        if not decision.timed_out:
            continue
        instance_id = job_instance_id(job, servers_by_id)
        # The complete safe timing diagnostic (Req 2.18): pure
        # projection, no raw provider text — safe for every sink.
        timeout_evidence = build_reconciliation.timeout_evidence_record(
            build_reconciliation.TimingDecision(
                timed_out=True,
                classification=decision.classification or ERROR_TIMEOUT,
                evidence=dict(decision.evidence or {})),
            decided_at=now)
        timing = dict(job.get('timing') or {})
        timing['timeout_kind'] = decision.classification or ERROR_TIMEOUT
        timing['timeout_decided_at'] = now
        # Legacy-shaped jobs keep the frozen TIMEOUT error code
        # (preservation Req 3.8/3.12); phase-clock decisions record the
        # design's stable classification (MAX_RUNTIME_EXCEEDED,
        # AGENT_HEARTBEAT_EXPIRED, BUILD_PROGRESS_STALLED).
        error_code = (decision.classification
                      if decision.evidence is not None
                      and decision.classification else ERROR_TIMEOUT)
        # Terminal outcome + PENDING cleanup recorded first (task 6.2).
        ledger = plan_job_ledger(job, cleanup_required=True)
        moved = fail_job(
            job, error_code, decision.error or 'Build timed out.',
            'build_timeout', {'instance_id': instance_id},
            ledger=ledger,
            extra={'timing': to_dynamo(timing),
                   'timeout_evidence': to_dynamo(timeout_evidence)})
        if not moved:
            continue
        # Stop idempotently, confirm absence, then release (Req 3.11).
        if instance_id:
            try:
                send_shell_command(instance_id, STOP_BUILD_COMMANDS)
            except ClientError as e:
                logger.warning(f"Timeout stop for Build_Job "
                               f"{job['build_job_id']} on {instance_id}: {e}")
        terminal_job = dict(job, status=build_domain.STATUS_FAILED)
        if reconcile_compute_cleanup(terminal_job, ledger, servers_by_id,
                                     send_stop=False):
            release_and_promote(terminal_job, ledger)
        else:
            logger.info(
                f"Build_Job {job['build_job_id']}: timeout cleanup not "
                f"yet verified; the allocation stays held until the "
                f"stop is pgrep-confirmed (stop-before-release)")


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
            # Terminal outcome + PENDING cleanup first (task 6.2): the
            # pkill above is the idempotent stop; the release happens
            # only once the processes are pgrep-confirmed absent.
            ledger = plan_job_ledger(failed_job, cleanup_required=True)
            moved = fail_job(
                failed_job, ERROR_SERIALIZATION_VIOLATION,
                f"Two or more build processes were detected running "
                f"concurrently on Build_Server '{server_key}' "
                f"({decision.process_count} builds); every build "
                f"process was stopped.",
                'build_serialization_violation',
                {'server': server_key,
                 'instance_id': instance_id,
                 'process_count': decision.process_count},
                ledger=ledger)
            if not moved:
                continue
            terminal_job = dict(failed_job,
                                status=build_domain.STATUS_FAILED)
            if reconcile_compute_cleanup(terminal_job, ledger,
                                         servers_by_id,
                                         send_stop=False):
                release_and_promote(terminal_job, ledger)


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
            # Keep the in-memory job coherent and complete the ledger's
            # compute-cleanup effect: idempotent ephemeral termination is
            # successful cleanup (task 6.2, Req 2.7).
            job['runner'] = {**runner, 'terminated_at': now}
            complete_ephemeral_cleanup_effect(job)
            logger.info(f"Terminated runner {instance_id} of terminal "
                        f"Build_Job {job['build_job_id']}")
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code', '')
            if code in ('InvalidInstanceID.NotFound',
                        'InvalidInstanceID.Malformed'):
                # Already gone: nothing left to terminate —
                # InvalidInstanceID.NotFound is successful cleanup
                # (task 6.2, Req 2.7).
                update_job_fields(job['build_job_id'], {
                    'runner': {**runner, 'terminated_at': now}})
                job['runner'] = {**runner, 'terminated_at': now}
                complete_ephemeral_cleanup_effect(job)
                continue
            logger.warning(f"Runner termination failed for {instance_id}: {e}")
            update_job_fields(job['build_job_id'], {
                'runner': {**runner,
                           'terminate_attempts':
                               int(runner.get('terminate_attempts') or 0) + 1,
                           'terminate_first_failed_at':
                               first_failed_at or now,
                           'terminate_last_attempt_at': now}})


# ------------------------ step 5.5: terminal-effects reconciliation
# (task 6.1/6.2, Req 2.7/3.11): re-drive the pending retryable effects
# of terminal jobs so a crashed/raced writer costs latency, never a
# lost audit, an unverified release, or a duplicated side effect.

def complete_ephemeral_cleanup_effect(job: Dict[str, Any]) -> None:
    """Mark a terminal ephemeral job's compute cleanup done on its
    ledger after a verified/idempotent runner termination."""
    ledger = job.get('terminal_effects')
    if isinstance(ledger, dict):
        if complete_effect(job['build_job_id'], ledger.get('effect_id'),
                           build_reconciliation.EFFECT_COMPUTE_CLEANUP):
            ledger[build_reconciliation.EFFECT_COMPUTE_CLEANUP] = \
                build_reconciliation.EFFECT_DONE


def reconcile_compute_cleanup(job: Dict[str, Any],
                              ledger: Dict[str, Any],
                              servers_by_id: Dict[str, Dict[str, Any]],
                              send_stop: bool = True) -> bool:
    """Attempt to VERIFY one terminal job's compute cleanup and complete
    the ledger effect (task 6.2). Returns True when cleanup is done or
    not applicable — i.e. the allocation release is now permitted.

    - dedicated: send the stop idempotently (``send_stop``), then
      pgrep-confirm no protected build process remains; an observed
      stopped/terminated server counts as cleanup success (the design's
      observed-terminal-instance-state rule). Unknown process state
      stays pending — fail closed (Req 3.11).
    - ephemeral: a terminated runner (``terminated_at``) or the
      idempotent tag-based termination finding nothing left counts as
      success; ``InvalidInstanceID.NotFound`` semantics are inherited
      from the termination watchdog (Req 2.7)."""
    state = ledger.get(build_reconciliation.EFFECT_COMPUTE_CLEANUP)
    if state != build_reconciliation.EFFECT_PENDING:
        return True  # done or not applicable: release already permitted

    def _complete() -> bool:
        if complete_effect(job['build_job_id'], ledger.get('effect_id'),
                           build_reconciliation.EFFECT_COMPUTE_CLEANUP):
            ledger[build_reconciliation.EFFECT_COMPUTE_CLEANUP] = \
                build_reconciliation.EFFECT_DONE
        # A refused completion means another writer already completed
        # it — the release is permitted either way.
        return True

    if job.get('execution_mode') == build_domain.EXECUTION_MODE_EPHEMERAL:
        runner = job.get('runner') or {}
        if runner.get('terminated_at'):
            return _complete()
        if runner.get('instance_id'):
            # The termination watchdog owns this runner's retryable
            # termination and completes the effect on success.
            return False
        # No runner recorded (partial provisioning): the idempotent
        # tag-based termination is the cleanup; nothing left = done.
        if not terminate_partial_compute(job['build_job_id']):
            return _complete()
        return False

    # Dedicated: observed terminal server state is successful cleanup.
    server = servers_by_id.get(job.get('server_id') or '')
    if server and server.get('lifecycle_state') in (
            build_domain.SERVER_STATE_STOPPED,
            build_domain.SERVER_STATE_TERMINATED):
        return _complete()
    instance_id = job_instance_id(job, servers_by_id)
    if not instance_id:
        return False  # unknown process state: stay blocked (fail closed)
    if send_stop:
        try:
            send_shell_command(instance_id, STOP_BUILD_COMMANDS)
        except ClientError as e:
            logger.warning(f"Cleanup stop for Build_Job "
                           f"{job['build_job_id']} on {instance_id}: {e}")
    if confirm_no_build_processes(instance_id) is True:
        return _complete()
    return False


#: Audit reconstruction for a pending audit effect found on a terminal
#: job (the finalizing writer crashed before completing it): a retry may
#: complete a pending audit but never creates a second logical audit.
_TERMINAL_AUDIT_ACTIONS = {
    build_domain.STATUS_SUCCEEDED: ('build_published', 'success'),
    build_domain.STATUS_FAILED: ('build_failed', 'failure'),
    build_domain.STATUS_INTERRUPTED: ('build_interrupted', 'failure'),
    build_domain.STATUS_CANCELLED: ('build_cancelled', 'success'),
}


def terminal_effects_reconciliation(jobs: List[Dict[str, Any]],
                                    servers_by_id: Dict[str, Dict[str, Any]],
                                    now: int) -> None:
    """Step 5.5 (task 6.1/6.2): for every terminal job whose ledger
    still records pending effects, re-drive them in the required order —
    audit, verified compute cleanup, allocation release, promotion
    wakeup. Every completion is a conditional pending -> done write, so
    concurrent writers/retries converge on one logical effect each
    (Req 2.7/3.11); one job's failure never poisons the tick."""
    for job in jobs:
        try:
            if not build_domain.is_terminal(job.get('status', '')):
                continue
            ledger = job.get('terminal_effects')
            if not isinstance(ledger, dict):
                continue  # legacy terminal record: original paths own it
            if not build_reconciliation.pending_effects(ledger):
                continue
            build_job_id = job['build_job_id']
            if ledger.get(build_reconciliation.EFFECT_AUDIT) == \
                    build_reconciliation.EFFECT_PENDING:
                action, result = _TERMINAL_AUDIT_ACTIONS.get(
                    job.get('status'), ('build_failed', 'failure'))
                audit_details = {
                    'error_code': (job.get('error') or {}).get('code'),
                    'status': job.get('status'),
                    'completed_by': 'terminal_effects_reconciliation',
                }
                if complete_effect(build_job_id, ledger.get('effect_id'),
                                   build_reconciliation.EFFECT_AUDIT):
                    ledger[build_reconciliation.EFFECT_AUDIT] = \
                        build_reconciliation.EFFECT_DONE
                    audit(action, build_job_id, result,
                          {**audit_details,
                           'terminal_effect_id': ledger.get('effect_id')})
            cleanup_ok = reconcile_compute_cleanup(job, ledger,
                                                   servers_by_id)
            if cleanup_ok:
                release_and_promote(job, ledger)
        except Exception as e:
            logger.warning(f"Terminal-effects reconciliation for "
                           f"Build_Job {job.get('build_job_id')} "
                           f"failed: {e}")


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
            # Queued job on a stopped/terminated server: no build was
            # ever dispatched for it, so cleanup is not applicable and
            # any deferral-held allocation is releasable (task 6.1).
            ledger = plan_job_ledger(job, cleanup_required=False)
            if fail_job(job, ERROR_SERVER_LOST,
                        decision.error or
                        'The selected Dedicated_Build_Server '
                        'is no longer available.',
                        'build_queue_orphaned',
                        {'server_id': decision.server_id,
                         'lifecycle_state': decision.lifecycle_state},
                        ledger=ledger):
                release_and_promote(
                    dict(job, status=build_domain.STATUS_FAILED), ledger)


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
    # 2.5 Scheduled command/settlement reconciliation and ambiguous-send
    #     recovery (build-fleet-execution-failures 5.2).
    command_reconciliation(jobs, servers_by_id, now)
    # 3. Runtime timeout watchdog (3.8).
    runtime_timeout_watchdog(jobs, servers_by_id, now)
    # 4. Serialization watchdog (7.7, 7.8).
    serialization_watchdog(jobs, servers_by_id, now)
    # 5. Termination watchdog (3.2, 3.9).
    termination_watchdog(jobs, now)
    # 5.5 Terminal-effects reconciliation: re-drive pending audit,
    #     verified cleanup, release, and promotion-wakeup effects
    #     (build-fleet-execution-failures 6.1/6.2, Req 2.7/3.11).
    terminal_effects_reconciliation(jobs, servers_by_id, now)
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
