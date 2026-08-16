"""
Build domain pure logic (Build_Manager)

Pure decision module for the portal build fleet: Build_Target definitions,
the Build_Job status state machine with terminal absorption, interruption
event handling, and the retry-clone function for interrupted jobs.

This module deliberately has NO AWS clients and NO side effects: it is
imported by the build handlers (build_jobs.py, build_dispatcher.py,
build_events.py) and is fully unit- and property-testable in isolation.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates
Requirements: 1.2, 1.3, 1.4, 1.5, 1.8, 1.9, 2.4, 2.6, 2.7, 2.8, 3.5, 3.6,
4.1, 9.2, 9.3, 9.5
"""
import copy
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

# Pure sibling module (also NO AWS clients): repository URL validation and
# normalization. Configuration validation of `default_repository` delegates
# to it (build-source-selection Req 1.5) so the accepted repository shape
# has exactly one definition.
import build_source

# ---------------------------------------------------------------------------
# Build_Target definitions (Requirement 1.4)
# ---------------------------------------------------------------------------

# CPU architectures required by Build_Targets
ARCH_ARM64 = 'arm64'
ARCH_X86_64 = 'x86_64'

# Ubuntu OS releases required by Build_Targets on the build host
# (jp7-ephemeral-runner-provisioning Req 2.1, 2.2). JP7 builds require an
# Ubuntu 24.04 (noble) arm64 host; every other target builds on 22.04.
OS_RELEASE_JAMMY = '22.04'
OS_RELEASE_NOBLE = '24.04'

# Build_Target names
TARGET_JP5 = 'JP5'
TARGET_JP6 = 'JP6'
TARGET_JP7 = 'JP7'
TARGET_AMD64 = 'AMD64'
TARGET_AMD64_NVIDIA = 'AMD64_NVIDIA'

# Target -> component name / recipe / required build-compute architecture /
# required build-host OS release
BUILD_TARGETS: Dict[str, Dict[str, str]] = {
    TARGET_JP5: {
        'component_name': 'aws.edgeml.dda.LocalServer.arm64JP5',
        'recipe': 'recipe-arm64-jp5.yaml',
        'required_arch': ARCH_ARM64,
        'required_os_release': OS_RELEASE_JAMMY,
    },
    TARGET_JP6: {
        'component_name': 'aws.edgeml.dda.LocalServer.arm64JP6',
        'recipe': 'recipe-arm64-jp6.yaml',
        'required_arch': ARCH_ARM64,
        'required_os_release': OS_RELEASE_JAMMY,
    },
    TARGET_JP7: {
        'component_name': 'aws.edgeml.dda.LocalServer.arm64JP7',
        'recipe': 'recipe-arm64-jp7.yaml',
        'required_arch': ARCH_ARM64,
        'required_os_release': OS_RELEASE_NOBLE,
    },
    TARGET_AMD64: {
        'component_name': 'aws.edgeml.dda.LocalServer.amd64',
        'recipe': 'recipe-amd64.yaml',
        'required_arch': ARCH_X86_64,
        'required_os_release': OS_RELEASE_JAMMY,
    },
    TARGET_AMD64_NVIDIA: {
        'component_name': 'aws.edgeml.dda.LocalServer.amd64Nvidia',
        'recipe': 'recipe-amd64-nvidia.yaml',
        'required_arch': ARCH_X86_64,
        'required_os_release': OS_RELEASE_JAMMY,
    },
}

SUPPORTED_BUILD_TARGETS = frozenset(BUILD_TARGETS.keys())


def is_supported_target(target: Any) -> bool:
    """True iff target is one of the supported Build_Targets (Req 1.4)."""
    return target in SUPPORTED_BUILD_TARGETS


def target_definition(target: str) -> Dict[str, str]:
    """Return the definition (component_name, recipe, required_arch) for a
    supported Build_Target. Raises ValueError for unsupported targets so
    callers cannot silently build an undefined target (Req 1.4)."""
    if target not in BUILD_TARGETS:
        raise ValueError(
            f"Unsupported Build_Target '{target}'. Supported Build_Targets: "
            f"{', '.join(sorted(SUPPORTED_BUILD_TARGETS))}"
        )
    return dict(BUILD_TARGETS[target])


def required_arch_for_target(target: str) -> str:
    """CPU architecture required by a Build_Target (arm64 or x86_64)."""
    return target_definition(target)['required_arch']


def required_os_release_for_target(target: str) -> str:
    """Ubuntu OS release required by a Build_Target's build host
    ('24.04' for JP7, '22.04' for every other supported target).

    Delegates to target_definition so unsupported targets keep raising
    ValueError (jp7-ephemeral-runner-provisioning Req 2.1, 2.2, 2.7)."""
    return target_definition(target)['required_os_release']


# ---------------------------------------------------------------------------
# Build_Job statuses (Requirement 4.1)
# ---------------------------------------------------------------------------

STATUS_QUEUED = 'queued'
STATUS_PROVISIONING = 'provisioning'
STATUS_BUILDING = 'building'
STATUS_PUBLISHING = 'publishing'
STATUS_SUCCEEDED = 'succeeded'
STATUS_FAILED = 'failed'
STATUS_INTERRUPTED = 'interrupted'
STATUS_CANCELLED = 'cancelled'

ALL_STATUSES = frozenset({
    STATUS_QUEUED,
    STATUS_PROVISIONING,
    STATUS_BUILDING,
    STATUS_PUBLISHING,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_CANCELLED,
})

# Terminal statuses are never left once reached (Req 4.1)
TERMINAL_STATUSES = frozenset({
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_CANCELLED,
})


def is_terminal(status: str) -> bool:
    """True iff the status is a terminal Build_Job status (Req 4.1)."""
    return status in TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# State machine events
# ---------------------------------------------------------------------------

# Dispatch events
EVENT_DISPATCH_EPHEMERAL = 'dispatch_ephemeral'      # queued -> provisioning (3.1)
EVENT_DISPATCH_DEDICATED = 'dispatch_dedicated'      # queued -> building (7.5)
# Runner/agent phase events
EVENT_RUNNER_READY = 'runner_ready'                  # provisioning -> building
EVENT_PROVISIONING_FAILED = 'provisioning_failed'    # provisioning -> failed (3.7)
EVENT_BUILD_SUCCEEDED = 'build_succeeded'            # building -> publishing (5.1)
EVENT_BUILD_FAILED = 'build_failed'                  # building -> failed
EVENT_PUBLISH_SUCCEEDED = 'publish_succeeded'        # publishing -> succeeded (5.3)
EVENT_PUBLISH_FAILED = 'publish_failed'              # publishing -> failed (5.4)
# Watchdog / external events
EVENT_TIMEOUT = 'timeout'                            # building/publishing -> failed (3.8)
EVENT_SERIALIZATION_VIOLATION = 'serialization_violation'  # -> failed (7.8)
EVENT_INTERRUPTION = 'interruption'                  # runner reclaimed -> interrupted (3.5)
EVENT_CANCEL = 'cancel'                              # cancellation confirmed (4.5, 4.6)
EVENT_SERVER_LOST = 'server_lost'                    # queued jobs on dead server -> failed (7.9)

ALL_EVENTS = frozenset({
    EVENT_DISPATCH_EPHEMERAL,
    EVENT_DISPATCH_DEDICATED,
    EVENT_RUNNER_READY,
    EVENT_PROVISIONING_FAILED,
    EVENT_BUILD_SUCCEEDED,
    EVENT_BUILD_FAILED,
    EVENT_PUBLISH_SUCCEEDED,
    EVENT_PUBLISH_FAILED,
    EVENT_TIMEOUT,
    EVENT_SERIALIZATION_VIOLATION,
    EVENT_INTERRUPTION,
    EVENT_CANCEL,
    EVENT_SERVER_LOST,
})

# (current status, event) -> next status.
# Edges exactly follow the design state machine:
#   queued       -> provisioning | building | cancelled | failed
#   provisioning -> building | failed
#   building     -> publishing | failed | interrupted | cancelled
#   publishing   -> succeeded | failed | interrupted | cancelled
_TRANSITIONS: Dict[tuple, str] = {
    (STATUS_QUEUED, EVENT_DISPATCH_EPHEMERAL): STATUS_PROVISIONING,
    (STATUS_QUEUED, EVENT_DISPATCH_DEDICATED): STATUS_BUILDING,
    (STATUS_QUEUED, EVENT_CANCEL): STATUS_CANCELLED,
    (STATUS_QUEUED, EVENT_SERVER_LOST): STATUS_FAILED,

    (STATUS_PROVISIONING, EVENT_RUNNER_READY): STATUS_BUILDING,
    (STATUS_PROVISIONING, EVENT_PROVISIONING_FAILED): STATUS_FAILED,

    (STATUS_BUILDING, EVENT_BUILD_SUCCEEDED): STATUS_PUBLISHING,
    (STATUS_BUILDING, EVENT_BUILD_FAILED): STATUS_FAILED,
    (STATUS_BUILDING, EVENT_TIMEOUT): STATUS_FAILED,
    (STATUS_BUILDING, EVENT_SERIALIZATION_VIOLATION): STATUS_FAILED,
    (STATUS_BUILDING, EVENT_INTERRUPTION): STATUS_INTERRUPTED,
    (STATUS_BUILDING, EVENT_CANCEL): STATUS_CANCELLED,

    (STATUS_PUBLISHING, EVENT_PUBLISH_SUCCEEDED): STATUS_SUCCEEDED,
    (STATUS_PUBLISHING, EVENT_PUBLISH_FAILED): STATUS_FAILED,
    (STATUS_PUBLISHING, EVENT_TIMEOUT): STATUS_FAILED,
    (STATUS_PUBLISHING, EVENT_SERIALIZATION_VIOLATION): STATUS_FAILED,
    (STATUS_PUBLISHING, EVENT_INTERRUPTION): STATUS_INTERRUPTED,
    (STATUS_PUBLISHING, EVENT_CANCEL): STATUS_CANCELLED,
}


def next_status(current_status: str, event: str) -> str:
    """Compute the next Build_Job status for (current status, event).

    - Terminal absorption (Req 4.1): any event applied to a terminal status
      returns the terminal status unchanged. Terminal jobs are never
      resurrected or double-transitioned (stale/duplicate events are no-ops).
    - Undefined (status, event) pairs return the current status unchanged
      (no-op), so the function is total over valid statuses and a job always
      holds exactly one status from the defined set.

    Raises ValueError for a status or event outside the defined sets, so
    programming errors cannot silently pass through as no-ops.
    """
    if current_status not in ALL_STATUSES:
        raise ValueError(f"Unknown Build_Job status '{current_status}'")
    if event not in ALL_EVENTS:
        raise ValueError(f"Unknown Build_Job event '{event}'")
    if current_status in TERMINAL_STATUSES:
        return current_status
    return _TRANSITIONS.get((current_status, event), current_status)


def is_valid_transition(current_status: str, event: str) -> bool:
    """True iff (current status, event) is a defined state-machine edge
    (i.e. next_status would actually move the job)."""
    return (current_status, event) in _TRANSITIONS


# ---------------------------------------------------------------------------
# Interruption handling (Requirement 3.5)
# ---------------------------------------------------------------------------

def apply_interruption(current_status: str) -> str:
    """Status resulting from the compute provider reclaiming/interrupting the
    job's runner: any non-terminal status becomes interrupted; terminal
    statuses are unchanged (Req 3.5, 4.1)."""
    if current_status not in ALL_STATUSES:
        raise ValueError(f"Unknown Build_Job status '{current_status}'")
    if current_status in TERMINAL_STATUSES:
        return current_status
    return STATUS_INTERRUPTED


# ---------------------------------------------------------------------------
# Retry clone (Requirement 3.6)
# ---------------------------------------------------------------------------

def retry_clone(
    interrupted_job: Dict[str, Any],
    new_job_id: str,
    requested_by: str,
    created_at: int,
    config_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create the record for a new Build_Job retrying an interrupted one.

    The new job carries the same Build_Target (with its component name and
    required architecture) and the same execution mode (including the
    selected Dedicated_Build_Server for dedicated mode) as the interrupted
    job, plus a `retry_of` reference to it (Req 3.6). The new job starts in
    the queued status (Req 1.9 semantics) with its own requester, submission
    time, and effective configuration snapshot (defaults to the interrupted
    job's snapshot when the caller does not supply a fresh one).

    Raises ValueError when the source job is not in the interrupted status:
    the retry action exists only for interrupted Build_Jobs.
    """
    source_status = interrupted_job.get('status')
    if source_status != STATUS_INTERRUPTED:
        raise ValueError(
            f"Retry is only available for interrupted Build_Jobs; "
            f"job {interrupted_job.get('build_job_id')} has status '{source_status}'"
        )

    target = interrupted_job['build_target']
    definition = target_definition(target)

    return {
        'build_job_id': new_job_id,
        'build_target': target,
        'component_name': definition['component_name'],
        'required_arch': definition['required_arch'],
        'execution_mode': interrupted_job['execution_mode'],
        'server_id': interrupted_job.get('server_id'),
        'status': STATUS_QUEUED,
        'requested_by': requested_by,
        'created_at': created_at,
        'retry_of': interrupted_job['build_job_id'],
        'config_snapshot': (
            config_snapshot if config_snapshot is not None
            else interrupted_job.get('config_snapshot')
        ),
    }


# ---------------------------------------------------------------------------
# Build request validation (Requirements 1.4, 1.8, 2.4, 2.6, 2.8)
# ---------------------------------------------------------------------------

EXECUTION_MODE_EPHEMERAL = 'ephemeral'
EXECUTION_MODE_DEDICATED = 'dedicated'

EXECUTION_MODES = frozenset({EXECUTION_MODE_EPHEMERAL, EXECUTION_MODE_DEDICATED})

# Dedicated_Build_Server lifecycle state required to accept a dedicated build
# request (Req 2.4).
SERVER_STATE_RUNNING = 'running'

# Validation rule identifiers: every rejection names the rule that failed.
RULE_TARGETS_EMPTY = 'targets_empty'                    # Req 1.8
RULE_UNSUPPORTED_TARGET = 'unsupported_target'          # Req 1.4
RULE_EXECUTION_MODE_MISSING = 'execution_mode_missing'  # Req 2.6
RULE_EXECUTION_MODE_INVALID = 'execution_mode_invalid'  # Req 2.6
RULE_SERVER_ID_MISSING = 'server_id_missing'            # Req 2.6
RULE_SERVER_NOT_FOUND = 'server_not_found'              # Req 2.4
RULE_SERVER_NOT_RUNNING = 'server_not_running'          # Req 2.4
RULE_SERVER_ARCH_MISMATCH = 'server_arch_mismatch'      # Req 2.8
# JP7 dedicated capability gate (jp7-ephemeral-runner-provisioning Req 2.7):
# the selected server's recorded Ubuntu release must match the required
# build-host release of every noble-requiring selected target.
RULE_SERVER_OS_RELEASE_MISMATCH = 'server_os_release_mismatch'
# Per-submission source selection (build-source-selection Req 1.4, 2.7).
# Re-exported from build_source — the one definition of the accepted
# repository/ref shapes — rather than spelled a second time here.
RULE_REPOSITORY_INVALID = build_source.RULE_REPOSITORY_INVALID
RULE_SOURCE_REF_INVALID = build_source.RULE_SOURCE_REF_INVALID


class ValidationResult(NamedTuple):
    """Outcome of a pure validation function.

    ``valid`` is True iff there are no errors. Each error is a dict with:
      - ``rule``: the identifier of the failed validation rule
      - ``message``: a user-readable explanation naming what failed
    """
    valid: bool
    errors: Tuple[Dict[str, str], ...]

    @classmethod
    def ok(cls) -> 'ValidationResult':
        return cls(True, ())

    @classmethod
    def rejected(cls, errors: List[Dict[str, str]]) -> 'ValidationResult':
        return cls(False, tuple(errors))


def _find_server(servers: Any, server_id: Any) -> Optional[Dict[str, Any]]:
    """Locate a server record by id in a fleet list (list of dicts) or a
    mapping of id -> record."""
    if isinstance(servers, dict):
        candidate = servers.get(server_id)
        return candidate if isinstance(candidate, dict) else None
    for server in servers or []:
        if isinstance(server, dict) and server.get('server_id') == server_id:
            return server
    return None


def validate_build_request(
    body: Dict[str, Any],
    servers: Any,
    config: Optional[Dict[str, Any]] = None,
) -> ValidationResult:
    """Validate a build request against the fleet state (pure function).

    ``body`` is the submitted request: ``targets`` (ordered list of
    Build_Targets), ``execution_mode`` ('ephemeral' or 'dedicated'), and
    ``server_id`` (required for dedicated mode). ``servers`` is the fleet
    state: a list of server records (or a mapping of server id -> record),
    each with ``server_id``, ``lifecycle_state``, and ``arch``. ``config``
    is the effective build configuration (accepted for signature parity;
    no configuration value affects request validity).

    The body may additionally carry an OPTIONAL per-submission source
    selection (build-source-selection Req 1.3, 1.4, 2.7):
    ``repository`` (an HTTPS GitHub remote) and ``source_ref`` (a branch,
    tag, or 40-hex commit SHA). Both are validated through build_source —
    the one definition of the accepted shapes — and each rejection carries
    the offending ``field`` alongside its ``rule``, so the existing
    BUILD_REQUEST_INVALID envelope names the form control that failed.
    Omitted values are valid: they mean "the configured default".

    Rules enforced, each rejection naming the failing rule:
      - targets non-empty (Req 1.8)
      - every target supported, error lists the supported Build_Targets
        (Req 1.4)
      - execution mode present and one of ephemeral/dedicated (Req 2.6)
      - dedicated mode identifies a specific server (Req 2.6)
      - the selected server exists and its lifecycle state is running;
        the error names the current state and the action needed (Req 2.4)
      - the server's CPU architecture matches the required architecture of
        every selected target; the error names the server architecture, the
        mismatched Build_Target, and the architecture it requires (Req 2.8)
      - the server's recorded Ubuntu release satisfies every selected
        target that requires a 24.04 build host (JP7); the error names the
        missing Ubuntu 24.04 arm64 capability and the server's actual
        release. A record with no ``ubuntu_version`` field predates the
        field's introduction and is treated as the 22.04 host it is
        (jp7-ephemeral-runner-provisioning Req 2.7)
      - a supplied ``repository`` is a well-formed HTTPS GitHub remote
        (RULE_REPOSITORY_INVALID, build-source-selection Req 1.4)
      - a supplied ``source_ref`` is a valid branch/tag/SHA ref
        (RULE_SOURCE_REF_INVALID, build-source-selection Req 2.7)
    """
    del config  # No configuration value affects request validity.
    body = body or {}
    errors: List[Dict[str, str]] = []

    # --- Targets (Req 1.8, 1.4) ---
    targets = body.get('targets')
    if not isinstance(targets, list) or len(targets) == 0:
        errors.append({
            'rule': RULE_TARGETS_EMPTY,
            'message': 'At least one Build_Target must be selected.',
        })
        targets = []
    supported = ', '.join(sorted(SUPPORTED_BUILD_TARGETS))
    for target in targets:
        if not is_supported_target(target):
            errors.append({
                'rule': RULE_UNSUPPORTED_TARGET,
                'message': (
                    f"Unsupported Build_Target '{target}'. Supported "
                    f"Build_Targets: {supported}."
                ),
            })

    # --- Execution mode (Req 2.6) ---
    execution_mode = body.get('execution_mode')
    if execution_mode is None or execution_mode == '':
        errors.append({
            'rule': RULE_EXECUTION_MODE_MISSING,
            'message': (
                'An execution mode must be selected: ephemeral or dedicated.'
            ),
        })
    elif execution_mode not in EXECUTION_MODES:
        errors.append({
            'rule': RULE_EXECUTION_MODE_INVALID,
            'message': (
                f"Invalid execution mode '{execution_mode}'. The execution "
                f"mode must be one of: ephemeral, dedicated."
            ),
        })

    # --- Dedicated server selection (Req 2.6, 2.4, 2.8) ---
    if execution_mode == EXECUTION_MODE_DEDICATED:
        server_id = body.get('server_id')
        if server_id is None or server_id == '':
            errors.append({
                'rule': RULE_SERVER_ID_MISSING,
                'message': (
                    'The dedicated execution mode requires selecting a '
                    'specific Dedicated_Build_Server.'
                ),
            })
        else:
            server = _find_server(servers, server_id)
            if server is None:
                errors.append({
                    'rule': RULE_SERVER_NOT_FOUND,
                    'message': (
                        f"Dedicated_Build_Server '{server_id}' does not "
                        f"exist in the fleet. Select an existing running "
                        f"server or use the ephemeral execution mode."
                    ),
                })
            else:
                state = server.get('lifecycle_state')
                if state != SERVER_STATE_RUNNING:
                    errors.append({
                        'rule': RULE_SERVER_NOT_RUNNING,
                        'message': (
                            f"Dedicated_Build_Server '{server_id}' is in "
                            f"lifecycle state '{state}', not running. Start "
                            f"the server (or select a running server) before "
                            f"submitting the build request."
                        ),
                    })
                server_arch = server.get('arch')
                for target in targets:
                    if not is_supported_target(target):
                        continue
                    required = required_arch_for_target(target)
                    if server_arch != required:
                        errors.append({
                            'rule': RULE_SERVER_ARCH_MISMATCH,
                            'message': (
                                f"Dedicated_Build_Server '{server_id}' has "
                                f"CPU architecture '{server_arch}', but "
                                f"Build_Target {target} requires "
                                f"'{required}'."
                            ),
                        })
                # JP7 dedicated capability gate (jp7-ephemeral-runner-
                # provisioning Req 2.7): every selected target that requires
                # a 24.04 build host needs the server's RECORDED Ubuntu
                # release to be exactly 24.04. A record with no
                # ubuntu_version field predates the field's introduction
                # (ec1dc38) and is therefore the 22.04 host it was launched
                # as. Targets requiring 22.04 impose NO release constraint,
                # and this gate composes with (never masks) the not-found,
                # not-running, and arch-mismatch rules above.
                server_release = (
                    server.get('ubuntu_version') or OS_RELEASE_JAMMY)
                for target in targets:
                    if not is_supported_target(target):
                        continue
                    required_release = required_os_release_for_target(target)
                    if required_release != OS_RELEASE_NOBLE:
                        continue
                    if server_release != required_release:
                        errors.append({
                            'rule': RULE_SERVER_OS_RELEASE_MISMATCH,
                            'message': (
                                f"Dedicated_Build_Server '{server_id}' runs "
                                f"Ubuntu {server_release}, but Build_Target "
                                f"{target} requires an Ubuntu 24.04 arm64 "
                                f"build host. Select a 24.04 arm64 server "
                                f"(or use the ephemeral execution mode)."
                            ),
                        })

    # --- Optional per-submission source selection
    #     (build-source-selection Req 1.3, 1.4, 2.7) ---
    # An omitted repository means "the configured default repository";
    # a supplied one must be a well-formed HTTPS GitHub remote. The
    # build_source rejection already carries rule, field and message, so
    # it splices straight into the envelope with the offending field named.
    if body.get('repository') is not None:
        _, repository_error = build_source.normalize_repository_url(
            body['repository'])
        if repository_error is not None:
            errors.append(dict(repository_error))
    # None and a blank string both mean "no ref selected" (the configured
    # value applies); anything else must be a valid branch/tag/SHA ref.
    _, source_ref_error = build_source.normalize_source_ref(
        body.get('source_ref'))
    if source_ref_error is not None:
        errors.append(dict(source_ref_error))

    if errors:
        return ValidationResult.rejected(errors)
    return ValidationResult.ok()


# ---------------------------------------------------------------------------
# Fleet action validation (Requirements 6.4, 6.10)
# ---------------------------------------------------------------------------

# Fleet management actions on a Dedicated_Build_Server.
FLEET_ACTION_START = 'start'
FLEET_ACTION_STOP = 'stop'
FLEET_ACTION_TERMINATE = 'terminate'

FLEET_ACTIONS = frozenset({
    FLEET_ACTION_START,
    FLEET_ACTION_STOP,
    FLEET_ACTION_TERMINATE,
})

# Dedicated_Build_Server lifecycle states (the EC2 instance states; the
# design pins fleet lifecycle state to the EC2 state set).
SERVER_STATE_PENDING = 'pending'
SERVER_STATE_STOPPING = 'stopping'
SERVER_STATE_STOPPED = 'stopped'
SERVER_STATE_SHUTTING_DOWN = 'shutting-down'
SERVER_STATE_TERMINATED = 'terminated'

# Fleet action rejection rule identifiers (every rejection names its rule).
RULE_FLEET_STATE_INVALID = 'fleet_state_invalid'        # Req 6.10
RULE_FLEET_JOB_RUNNING = 'fleet_job_running'            # Req 6.4


def _running_job_id(running_job: Any) -> str:
    """Best-effort identifier of a running Build_Job for error messages."""
    if isinstance(running_job, dict):
        job_id = running_job.get('build_job_id')
        if job_id:
            return str(job_id)
        return 'unknown'
    return str(running_job)


def validate_fleet_action(
    action: str,
    server: Dict[str, Any],
    running_job: Optional[Any] = None,
) -> ValidationResult:
    """Validate a fleet management action against a server's state (pure).

    ``action`` is one of start, stop, or terminate. ``server`` is the
    Dedicated_Build_Server record with at least ``lifecycle_state`` (the
    EC2 instance state). ``running_job`` is the Build_Job currently running
    on the server (a record with ``build_job_id``, or a job id), or None
    when no Build_Job is running.

    Rules (design Property 13):
      - start is permitted if and only if the lifecycle state is stopped
        (Req 6.10)
      - stop is permitted if and only if the lifecycle state is running and
        no Build_Job is running on the server (Req 6.10, 6.4)
      - terminate is permitted if and only if the lifecycle state is not
        terminated and no Build_Job is running on the server (Req 6.10, 6.4)

    Every rejection identifies the server's current lifecycle state, and
    rejections caused by a running Build_Job identify that job and instruct
    the user to cancel it or wait for it to finish (Req 6.4).

    Raises ValueError for an action outside the defined set, so programming
    errors cannot silently pass through as rejections.
    """
    if action not in FLEET_ACTIONS:
        raise ValueError(
            f"Unknown fleet action '{action}'. Fleet actions: "
            f"{', '.join(sorted(FLEET_ACTIONS))}"
        )

    server = server or {}
    state = server.get('lifecycle_state')
    errors: List[Dict[str, str]] = []

    # --- Lifecycle state permits the action (Req 6.10) ---
    if action == FLEET_ACTION_START and state != SERVER_STATE_STOPPED:
        errors.append({
            'rule': RULE_FLEET_STATE_INVALID,
            'message': (
                f"The server cannot be started: its current lifecycle "
                f"state is '{state}', not '{SERVER_STATE_STOPPED}'."
            ),
        })
    elif action == FLEET_ACTION_STOP and state != SERVER_STATE_RUNNING:
        errors.append({
            'rule': RULE_FLEET_STATE_INVALID,
            'message': (
                f"The server cannot be stopped: its current lifecycle "
                f"state is '{state}', not '{SERVER_STATE_RUNNING}'."
            ),
        })
    elif action == FLEET_ACTION_TERMINATE and state == SERVER_STATE_TERMINATED:
        errors.append({
            'rule': RULE_FLEET_STATE_INVALID,
            'message': (
                f"The server cannot be terminated: its current lifecycle "
                f"state is already '{SERVER_STATE_TERMINATED}'."
            ),
        })

    # --- No running Build_Job for stop/terminate (Req 6.4) ---
    if action in (FLEET_ACTION_STOP, FLEET_ACTION_TERMINATE) and running_job:
        job_id = _running_job_id(running_job)
        verb = 'stopped' if action == FLEET_ACTION_STOP else 'terminated'
        errors.append({
            'rule': RULE_FLEET_JOB_RUNNING,
            'message': (
                f"The server cannot be {verb}: Build_Job '{job_id}' is "
                f"currently running on it (lifecycle state '{state}'). "
                f"Cancel the Build_Job or wait for it to finish."
            ),
        })

    if errors:
        return ValidationResult.rejected(errors)
    return ValidationResult.ok()


# ---------------------------------------------------------------------------
# Build_Job creation (Requirements 1.2, 1.3, 1.5, 1.9, 2.7, 9.3)
# ---------------------------------------------------------------------------

def create_build_jobs(
    targets: List[str],
    execution_mode: str,
    server_id: Optional[str],
    request_id: str,
    job_ids: List[str],
    requested_by: str,
    created_at: int,
    config_snapshot: Optional[Dict[str, Any]] = None,
    volume_size_gb: Any = None,
) -> List[Dict[str, Any]]:
    """Create the Build_Job records for a validated build request (pure).

    One Build_Job per target, in request order (Req 1.2, 1.3). Every job
    shares the ``request_id`` and carries its 0-based ``request_order``;
    ``predecessor_job_id`` chains job *n* to job *n-1* (None for the first),
    so job *n* is dispatchable only after its predecessor is terminal
    (Req 1.3). The execution mode and (for dedicated mode) the selected
    Dedicated_Build_Server apply to every job (Req 2.7). Each job records
    the requesting user and submission time (Req 1.5), snapshots the
    effective configuration at creation (Req 9.3, each job gets its own
    deep copy so later configuration changes cannot leak in), and starts in
    the queued status (Req 1.9).

    ``job_ids`` must supply exactly one pre-generated id per target so the
    function stays deterministic and side-effect free.

    Ephemeral volume sizing is resolved ONCE here, per job, in design
    order (Req 2.20): the OPTIONAL explicit ``volume_size_gb`` request
    value, the snapshot's OPTIONAL ``volume_size_gb_by_target`` entry for
    the job's own target, then the snapshot's global ``volume_size_gb``
    (documented default 200). The resolved size is written into each
    job's own ``config_snapshot.volume_size_gb``, which ``plan_runner``
    continues to read unchanged; a snapshot supplying neither a map
    entry nor a global value is left untouched. Previously created jobs
    keep their snapshotted sizes without retroactive adoption (Req 3.13).
    Snapshotted ``runtime_budgets`` are deep-copied per job like every
    other snapshot field, making each job's runtime budget immutable at
    submission (Req 2.17).
    """
    if not targets:
        raise ValueError('create_build_jobs requires at least one Build_Target')
    if len(job_ids) != len(targets):
        raise ValueError(
            f'create_build_jobs requires one job id per target '
            f'({len(targets)} targets, {len(job_ids)} job ids)'
        )
    if execution_mode not in EXECUTION_MODES:
        raise ValueError(f"Invalid execution mode '{execution_mode}'")
    if execution_mode == EXECUTION_MODE_DEDICATED and not server_id:
        raise ValueError('Dedicated execution mode requires a server id')

    jobs: List[Dict[str, Any]] = []
    for order, target in enumerate(targets):
        definition = target_definition(target)
        snapshot = copy.deepcopy(config_snapshot)
        if isinstance(snapshot, dict):
            resolved_volume = resolve_volume_size_gb(
                volume_size_gb, target, snapshot)
            if resolved_volume is not None:
                snapshot['volume_size_gb'] = resolved_volume
        jobs.append({
            'build_job_id': job_ids[order],
            'request_id': request_id,
            'request_order': order,
            'predecessor_job_id': job_ids[order - 1] if order > 0 else None,
            'build_target': target,
            'component_name': definition['component_name'],
            'required_arch': definition['required_arch'],
            'execution_mode': execution_mode,
            'server_id': (
                server_id if execution_mode == EXECUTION_MODE_DEDICATED
                else None
            ),
            'status': STATUS_QUEUED,
            'requested_by': requested_by,
            'created_at': created_at,
            'config_snapshot': snapshot,
        })
    return jobs


# ---------------------------------------------------------------------------
# Cancellation decision (Requirements 4.5, 4.6, 4.8, 4.9)
# ---------------------------------------------------------------------------

# Window within which the stop of a running build process must be confirmed
# (via pgrep verification on the Build_Server) for the cancellation of a
# running Build_Job to succeed (Req 4.6, 4.9).
CANCELLATION_CONFIRMATION_WINDOW_MINUTES = 5

# Statuses whose cancellation goes through the SSM stop + pgrep confirmation
# path: an actual build process may be running on the Build_Server (Req 4.6).
RUNNING_STATUSES = frozenset({STATUS_BUILDING, STATUS_PUBLISHING})

# Cancellation rejection rule identifiers (every rejection names its rule).
RULE_CANCEL_TERMINAL_STATUS = 'cancel_terminal_status'          # Req 4.8
RULE_CANCEL_STOP_NOT_CONFIRMED = 'cancel_stop_not_confirmed'    # Req 4.9
RULE_CANCEL_NOT_CANCELLABLE = 'cancel_not_cancellable'          # provisioning


class CancellationDecision(NamedTuple):
    """Outcome of a cancellation request against a Build_Job (pure).

    - ``cancelled``: True iff the Build_Job is to be marked cancelled.
    - ``next_status``: the status the Build_Job holds after the decision
      (``cancelled`` on success, the unchanged current status otherwise).
    - ``remove_from_queue``: True iff the Build_Job must also be removed
      from the Build_Queue (queued jobs only, Req 4.5).
    - ``errors``: rejection details when ``cancelled`` is False; each error
      dict carries ``rule`` and a user-readable ``message``.
    """
    cancelled: bool
    next_status: str
    remove_from_queue: bool
    errors: Tuple[Dict[str, str], ...]


def decide_cancellation(
    current_status: str,
    stop_confirmed: Optional[bool] = None,
    server_id: Optional[str] = None,
) -> CancellationDecision:
    """Decide the outcome of a cancellation request for a Build_Job (pure).

    ``current_status`` is the Build_Job's status at the time of the request.
    ``stop_confirmed`` reports the result of the SSM stop + pgrep
    verification for running jobs: True iff no build process was found on
    the Build_Server within the confirmation window
    (``CANCELLATION_CONFIRMATION_WINDOW_MINUTES``). ``server_id`` names the
    Build_Server for error messages on the running-job path.

    Semantics (design Property 9):
      - queued: the Build_Job becomes cancelled and is removed from the
        Build_Queue (Req 4.5). No stop confirmation is involved.
      - running (building or publishing): the Build_Job becomes cancelled
        if and only if ``stop_confirmed`` is True; otherwise (False or not
        yet known, fail-closed) it keeps its current status and the
        rejection names the affected Build_Server (Req 4.6, 4.9).
      - terminal (succeeded, failed, interrupted, cancelled): the request
        is rejected, the Build_Job is unchanged, and the error identifies
        the current status (Req 4.8).
      - provisioning: the request is rejected with the Build_Job unchanged
        and an error identifying the current status. Rationale: the design
        state machine defines no provisioning -> cancelled edge (the compute
        is mid-launch, there is no build process to stop and no agent to
        confirm a stop), so cancellation is deliberately unavailable until
        the job either starts building (cancellable via the stop +
        confirmation path) or fails/interrupts on its own. This mirrors the
        terminal rejection shape: unchanged job, error naming the status.

    Raises ValueError for a status outside the defined set, so programming
    errors cannot silently pass through.
    """
    if current_status not in ALL_STATUSES:
        raise ValueError(f"Unknown Build_Job status '{current_status}'")

    # Terminal: rejected unchanged, error names the current status (Req 4.8)
    if current_status in TERMINAL_STATUSES:
        return CancellationDecision(
            cancelled=False,
            next_status=current_status,
            remove_from_queue=False,
            errors=({
                'rule': RULE_CANCEL_TERMINAL_STATUS,
                'message': (
                    f"The Build_Job is already in the terminal status "
                    f"'{current_status}' and cannot be cancelled."
                ),
            },),
        )

    # Queued: cancelled immediately and removed from the queue (Req 4.5)
    if current_status == STATUS_QUEUED:
        return CancellationDecision(
            cancelled=True,
            next_status=STATUS_CANCELLED,
            remove_from_queue=True,
            errors=(),
        )

    # Running (building/publishing): cancelled iff the stop is confirmed
    # within the confirmation window (Req 4.6); otherwise the job keeps its
    # status and the error names the Build_Server (Req 4.9). A missing
    # confirmation result is treated as not confirmed (fail-closed).
    if current_status in RUNNING_STATUSES:
        if stop_confirmed is True:
            return CancellationDecision(
                cancelled=True,
                next_status=STATUS_CANCELLED,
                remove_from_queue=False,
                errors=(),
            )
        server_name = server_id if server_id else 'unknown'
        return CancellationDecision(
            cancelled=False,
            next_status=current_status,
            remove_from_queue=False,
            errors=({
                'rule': RULE_CANCEL_STOP_NOT_CONFIRMED,
                'message': (
                    f"The build process on Build_Server '{server_name}' was "
                    f"not confirmed stopped within "
                    f"{CANCELLATION_CONFIRMATION_WINDOW_MINUTES} minutes of "
                    f"the cancellation request. The Build_Job keeps its "
                    f"'{current_status}' status."
                ),
            },),
        )

    # Provisioning: rejected unchanged, error names the current status (see
    # docstring rationale — no state-machine edge, nothing to stop yet).
    return CancellationDecision(
        cancelled=False,
        next_status=current_status,
        remove_from_queue=False,
        errors=({
            'rule': RULE_CANCEL_NOT_CANCELLABLE,
            'message': (
                f"The Build_Job is in the '{current_status}' status and "
                f"cannot be cancelled while its build compute is being "
                f"provisioned. Retry once the build starts, or wait for "
                f"the job to fail or be interrupted."
            ),
        },),
    )


# ---------------------------------------------------------------------------
# Build infrastructure configuration (Requirements 9.2, 9.5)
# ---------------------------------------------------------------------------

# Documented per-field defaults (Req 9.2), matching the current manual
# process. Stored under the PortalSettings key `build_infrastructure_config`;
# every field is optional and the default applies on read when a field is
# absent (or stored as None). build_planner.py carries its own fail-safe
# copies of the sizing defaults for jobs whose config_snapshot lacks a field;
# the values here are the authoritative read-time defaults.
DEFAULT_BUILD_CONFIG: Dict[str, Any] = {
    'arm64_instance_type': 'm6g.4xlarge',
    'x86_64_instance_type': 'm6i.4xlarge',
    # Raised from 100 to 200 (build-fleet-execution-failures storage
    # amendment, Req 2.20): the JP6 target exports two large multi-stage
    # docker images concurrently on a single root volume shared by snap
    # docker layer storage, the repository clone, buildkit cache, and
    # /tmp; 100 GB was evidence-confirmed as undersized (job bd91c5d8).
    # Applies at submission time only: previously created jobs keep
    # their snapshotted sizes (Req 3.13).
    'volume_size_gb': 200,
    'region': 'us-east-1',
    'max_runtime_hours': 4,
    'use_spot_for_ephemeral': False,
    # Operator-controlled default repository for build submissions
    # (build-source-selection Req 1.5). Present in this table so
    # build_config.KNOWN_PARAMETERS makes it an operator-settable,
    # audited parameter with no build_config.py change.
    'default_repository': 'https://github.com/awslabs/DefectDetectionApplication',
    # None means "the repository's default branch" (resolved at build time).
    'source_ref': None,
    # OPTIONAL target/mode runtime budgets (build-fleet-execution-failures
    # Req 2.17): {target: {mode-or-'default': {heartbeat_lease_minutes,
    # progress_stall_minutes, hard_runtime_hours, queue_wait_hours,
    # provisioning_minutes}}}. None means "not configured": jobs fall back
    # to their snapshotted max_runtime_hours (Req 3.12). No default value
    # is encoded here — an unevidenced production timeout change is
    # prohibited (Req 2.19, historical-evidence.md gate row 6).
    'runtime_budgets': None,
    # OPTIONAL per-target ephemeral volume sizing (Req 2.20):
    # {target: volume GB}. None means "not configured": every target uses
    # the global volume_size_gb. Any configured JP6 entry must be at
    # least MIN_JP6_VOLUME_SIZE_GB.
    'volume_size_gb_by_target': None,
}

#: Minimum ephemeral volume size for the JP6 target (GB): JP6 exports two
#: large multi-stage images concurrently and is evidence-confirmed to
#: exhaust 100 GB (Req 2.20 — any configured JP6 value must be >= 200).
MIN_JP6_VOLUME_SIZE_GB = 200

#: The budget keys one runtime-budget entry may define, matching the
#: expectations of ``build_reconciliation.effective_budget`` (Req 2.17):
#: soft leases and hard ceiling, plus the independent OPTIONAL queue-wait
#: and provisioning budgets (disabled unless explicitly configured,
#: Req 2.14).
RUNTIME_BUDGET_ENTRY_KEYS = frozenset({
    'heartbeat_lease_minutes',
    'progress_stall_minutes',
    'hard_runtime_hours',
    'queue_wait_hours',
    'provisioning_minutes',
})

#: The per-target keys of the runtime_budgets map: one entry per
#: execution mode plus the target-wide 'default'.
RUNTIME_BUDGET_MODE_KEYS = frozenset(
    {EXECUTION_MODE_EPHEMERAL, EXECUTION_MODE_DEDICATED, 'default'})

# EC2 instance-family -> CPU architecture lookup table (design §7).
# Graviton families are arm64; Intel/AMD families are x86_64. Families not
# listed here are unknown: validation rejects them (fail-closed) because the
# architecture match cannot be verified.
INSTANCE_FAMILY_ARCH: Dict[str, str] = {
    # arm64 (Graviton)
    'm6g': ARCH_ARM64, 'm6gd': ARCH_ARM64,
    'm7g': ARCH_ARM64, 'm7gd': ARCH_ARM64,
    'm8g': ARCH_ARM64,
    'c6g': ARCH_ARM64, 'c6gd': ARCH_ARM64, 'c6gn': ARCH_ARM64,
    'c7g': ARCH_ARM64, 'c7gd': ARCH_ARM64, 'c7gn': ARCH_ARM64,
    'c8g': ARCH_ARM64,
    'r6g': ARCH_ARM64, 'r6gd': ARCH_ARM64,
    'r7g': ARCH_ARM64, 'r7gd': ARCH_ARM64,
    'r8g': ARCH_ARM64,
    't4g': ARCH_ARM64,
    'x2gd': ARCH_ARM64,
    'im4gn': ARCH_ARM64, 'is4gen': ARCH_ARM64,
    # x86_64 (Intel / AMD)
    'm5': ARCH_X86_64, 'm5d': ARCH_X86_64, 'm5n': ARCH_X86_64,
    'm5a': ARCH_X86_64, 'm5zn': ARCH_X86_64,
    'm6i': ARCH_X86_64, 'm6id': ARCH_X86_64, 'm6a': ARCH_X86_64,
    'm7i': ARCH_X86_64, 'm7a': ARCH_X86_64,
    'c5': ARCH_X86_64, 'c5d': ARCH_X86_64, 'c5n': ARCH_X86_64,
    'c5a': ARCH_X86_64,
    'c6i': ARCH_X86_64, 'c6id': ARCH_X86_64, 'c6a': ARCH_X86_64,
    'c7i': ARCH_X86_64, 'c7a': ARCH_X86_64,
    'r5': ARCH_X86_64, 'r5d': ARCH_X86_64, 'r5n': ARCH_X86_64,
    'r5a': ARCH_X86_64,
    'r6i': ARCH_X86_64, 'r6id': ARCH_X86_64, 'r6a': ARCH_X86_64,
    'r7i': ARCH_X86_64, 'r7a': ARCH_X86_64,
    't2': ARCH_X86_64, 't3': ARCH_X86_64, 't3a': ARCH_X86_64,
    'i3': ARCH_X86_64, 'i4i': ARCH_X86_64,
    'd3': ARCH_X86_64,
}

# Configuration parameter -> the CPU architecture its instance type must
# belong to (Req 9.5).
INSTANCE_TYPE_CONFIG_ARCH: Dict[str, str] = {
    'arm64_instance_type': ARCH_ARM64,
    'x86_64_instance_type': ARCH_X86_64,
}

# Configuration validation rule identifiers (every rejection names its rule
# and the invalid parameter, Req 9.5).
RULE_CONFIG_INSTANCE_TYPE_INVALID = 'config_instance_type_invalid'
RULE_CONFIG_INSTANCE_TYPE_ARCH_MISMATCH = 'config_instance_type_arch_mismatch'
RULE_CONFIG_VOLUME_SIZE_INVALID = 'config_volume_size_invalid'
RULE_CONFIG_MAX_RUNTIME_INVALID = 'config_max_runtime_invalid'
RULE_CONFIG_REPOSITORY_INVALID = 'config_repository_invalid'
RULE_CONFIG_RUNTIME_BUDGETS_INVALID = 'config_runtime_budgets_invalid'
RULE_CONFIG_VOLUME_BY_TARGET_INVALID = 'config_volume_by_target_invalid'
RULE_CONFIG_JP6_VOLUME_MINIMUM = 'config_jp6_volume_minimum'


def effective_build_config(stored: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Effective build infrastructure configuration read (pure, Req 9.2).

    ``stored`` is the raw ``build_infrastructure_config`` item from
    PortalSettings (or None when never written). The result contains every
    documented parameter: the stored value when present, the documented
    default otherwise (ARM64 instance type m6g.4xlarge, x86_64 instance
    type m6i.4xlarge, volume size 200 GB, region us-east-1, max runtime
    4 hours, spot for ephemeral runners off, default repository
    https://github.com/awslabs/DefectDetectionApplication, source ref =
    repository default branch). A field stored as None counts as absent.
    Unknown stored fields pass through unchanged.
    """
    config = dict(DEFAULT_BUILD_CONFIG)
    for key, value in (stored or {}).items():
        if value is not None:
            config[key] = value
    return config


def instance_type_family(instance_type: Any) -> Optional[str]:
    """EC2 instance family of an instance type string ('m6g.4xlarge' ->
    'm6g'), or None when the value is not a non-empty 'family.size'
    string."""
    if not isinstance(instance_type, str):
        return None
    family, sep, size = instance_type.partition('.')
    if not sep or not family or not size:
        return None
    return family.lower()


def instance_type_arch(instance_type: Any) -> Optional[str]:
    """CPU architecture of an instance type per the instance-family ->
    architecture lookup table, or None when the family is unknown or the
    value is malformed."""
    family = instance_type_family(instance_type)
    if family is None:
        return None
    return INSTANCE_FAMILY_ARCH.get(family)


def _is_positive_number(value: Any) -> bool:
    """True iff value is a positive, finite number (bool excluded)."""
    if isinstance(value, bool):
        return False
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return False
    if isinstance(value, str):
        return False
    return as_float > 0 and as_float != float('inf')


def validate_runtime_budgets(value: Any) -> List[Dict[str, str]]:
    """Validate an OPTIONAL target/mode runtime-budget map (pure,
    Req 2.17): ``{target: {mode-or-'default': {budget key: positive
    number}}}``, the shape ``build_reconciliation.effective_budget``
    resolves at decision time. Fail-closed: unknown targets, unknown
    mode keys, unknown budget keys, and non-positive values are all
    rejected so a typo can never silently disable or misapply a budget.
    Returns the (possibly empty) list of error dicts."""
    errors: List[Dict[str, str]] = []

    def _error(message: str) -> None:
        errors.append({
            'rule': RULE_CONFIG_RUNTIME_BUDGETS_INVALID,
            'parameter': 'runtime_budgets',
            'message': 'Invalid value for runtime_budgets: ' + message,
        })

    if not isinstance(value, dict):
        _error(f"'{value}' is not a map of Build_Target to per-mode "
               f"budget entries.")
        return errors
    supported = ', '.join(sorted(SUPPORTED_BUILD_TARGETS))
    for target, per_mode in value.items():
        if not is_supported_target(target):
            _error(f"'{target}' is not a supported Build_Target "
                   f"(supported: {supported}).")
            continue
        if not isinstance(per_mode, dict):
            _error(f"the entry for target '{target}' is not a map of "
                   f"execution mode (or 'default') to a budget entry.")
            continue
        for mode, entry in per_mode.items():
            if mode not in RUNTIME_BUDGET_MODE_KEYS:
                _error(f"'{target}.{mode}' is not an execution mode or "
                       f"'default' (expected one of: "
                       + ', '.join(sorted(RUNTIME_BUDGET_MODE_KEYS)) + ').')
                continue
            if not isinstance(entry, dict):
                _error(f"the budget entry '{target}.{mode}' is not a map "
                       f"of budget keys to positive numbers.")
                continue
            for key, budget in entry.items():
                if key not in RUNTIME_BUDGET_ENTRY_KEYS:
                    _error(f"'{target}.{mode}.{key}' is not a known "
                           f"budget key (expected one of: "
                           + ', '.join(sorted(RUNTIME_BUDGET_ENTRY_KEYS))
                           + ').')
                    continue
                if budget is not None and not _is_positive_number(budget):
                    _error(f"'{target}.{mode}.{key}' = '{budget}' is not "
                           f"a positive number.")
    return errors


def validate_volume_size_by_target(value: Any) -> List[Dict[str, str]]:
    """Validate an OPTIONAL per-target volume-size map (pure, Req 2.20):
    ``{target: volume GB}``, each value a positive number, and any
    configured JP6 entry at least ``MIN_JP6_VOLUME_SIZE_GB`` (the JP6
    target is evidence-confirmed to exhaust smaller volumes). Returns
    the (possibly empty) list of error dicts."""
    errors: List[Dict[str, str]] = []
    if not isinstance(value, dict):
        errors.append({
            'rule': RULE_CONFIG_VOLUME_BY_TARGET_INVALID,
            'parameter': 'volume_size_gb_by_target',
            'message': (
                f"Invalid value for volume_size_gb_by_target: '{value}' "
                f"is not a map of Build_Target to volume size (GB)."
            ),
        })
        return errors
    supported = ', '.join(sorted(SUPPORTED_BUILD_TARGETS))
    for target, size in value.items():
        if not is_supported_target(target):
            errors.append({
                'rule': RULE_CONFIG_VOLUME_BY_TARGET_INVALID,
                'parameter': 'volume_size_gb_by_target',
                'message': (
                    f"Invalid value for volume_size_gb_by_target: "
                    f"'{target}' is not a supported Build_Target "
                    f"(supported: {supported})."
                ),
            })
            continue
        if size is None:
            continue
        if not _is_positive_number(size):
            errors.append({
                'rule': RULE_CONFIG_VOLUME_BY_TARGET_INVALID,
                'parameter': 'volume_size_gb_by_target',
                'message': (
                    f"Invalid value for volume_size_gb_by_target."
                    f"{target}: '{size}' is not a positive number."
                ),
            })
            continue
        if target == TARGET_JP6 and float(size) < MIN_JP6_VOLUME_SIZE_GB:
            errors.append({
                'rule': RULE_CONFIG_JP6_VOLUME_MINIMUM,
                'parameter': 'volume_size_gb_by_target',
                'message': (
                    f"Invalid value for volume_size_gb_by_target.JP6: "
                    f"'{size}' is below the required minimum of "
                    f"{MIN_JP6_VOLUME_SIZE_GB} GB — the JP6 target "
                    f"exports two large multi-stage images concurrently "
                    f"and exhausts smaller volumes."
                ),
            })
    return errors


def resolve_volume_size_gb(
    explicit_value: Any,
    build_target: str,
    config: Optional[Dict[str, Any]],
) -> Any:
    """Resolve one Build_Job's ephemeral volume size at submission
    (pure, Req 2.20), in design order: the explicit request value when
    supplied, the OPTIONAL per-target map entry, then the global
    ``volume_size_gb`` (documented default 200). Returns None when no
    level supplies a value (the caller leaves the snapshot untouched and
    ``plan_runner``'s fail-safe applies). Resolution happens ONCE at
    submission and is snapshotted immutably: previously created jobs
    keep their snapshotted sizes without retroactive adoption
    (Req 3.13)."""
    if explicit_value is not None:
        return explicit_value
    config = config or {}
    by_target = config.get('volume_size_gb_by_target')
    if isinstance(by_target, dict) and by_target.get(build_target) is not None:
        return by_target[build_target]
    return config.get('volume_size_gb')


def validate_build_config(update: Optional[Dict[str, Any]]) -> ValidationResult:
    """Validate a build infrastructure configuration update (pure, Req 9.5).

    ``update`` is a partial configuration object; only the fields it
    supplies (with non-None values) are validated. Rules, each rejection
    naming the failing rule and the invalid parameter:

      - ``arm64_instance_type`` / ``x86_64_instance_type``: the instance
        type's family architecture (per ``INSTANCE_FAMILY_ARCH``) must match
        the architecture slot the parameter configures; malformed values and
        unknown families are rejected (fail-closed) because the match cannot
        be verified.
      - ``volume_size_gb``: a positive number.
      - ``max_runtime_hours``: a positive duration (positive number of
        hours).
      - ``default_repository``: a well-formed HTTPS GitHub remote, per
        ``build_source.normalize_repository_url`` (build-source-selection
        Req 1.5).
      - ``runtime_budgets``: an OPTIONAL target/mode runtime-budget map
        (``validate_runtime_budgets``, Req 2.17).
      - ``volume_size_gb_by_target``: an OPTIONAL per-target volume-size
        map with any JP6 entry at least ``MIN_JP6_VOLUME_SIZE_GB``
        (``validate_volume_size_by_target``, Req 2.20).

    A rejected update must be discarded in full (atomic reject): the caller
    keeps the stored configuration unchanged — see ``apply_config_update``.
    """
    update = update or {}
    errors: List[Dict[str, str]] = []

    # --- Instance types must match their architecture slot (Req 9.5) ---
    for parameter, required_arch in INSTANCE_TYPE_CONFIG_ARCH.items():
        if parameter not in update or update[parameter] is None:
            continue
        value = update[parameter]
        family = instance_type_family(value)
        if family is None:
            errors.append({
                'rule': RULE_CONFIG_INSTANCE_TYPE_INVALID,
                'parameter': parameter,
                'message': (
                    f"Invalid value for {parameter}: '{value}' is not an "
                    f"EC2 instance type (expected the form "
                    f"'family.size', e.g. 'm6g.4xlarge')."
                ),
            })
            continue
        arch = INSTANCE_FAMILY_ARCH.get(family)
        if arch is None:
            errors.append({
                'rule': RULE_CONFIG_INSTANCE_TYPE_INVALID,
                'parameter': parameter,
                'message': (
                    f"Invalid value for {parameter}: instance family "
                    f"'{family}' is not in the known instance-family "
                    f"architecture table, so its CPU architecture cannot "
                    f"be verified."
                ),
            })
        elif arch != required_arch:
            errors.append({
                'rule': RULE_CONFIG_INSTANCE_TYPE_ARCH_MISMATCH,
                'parameter': parameter,
                'message': (
                    f"Invalid value for {parameter}: instance type "
                    f"'{value}' (family '{family}') has CPU architecture "
                    f"'{arch}', but {parameter} requires an instance type "
                    f"with CPU architecture '{required_arch}'."
                ),
            })

    # --- Volume size a positive number (Req 9.5) ---
    if 'volume_size_gb' in update and update['volume_size_gb'] is not None:
        value = update['volume_size_gb']
        if not _is_positive_number(value):
            errors.append({
                'rule': RULE_CONFIG_VOLUME_SIZE_INVALID,
                'parameter': 'volume_size_gb',
                'message': (
                    f"Invalid value for volume_size_gb: '{value}' is not a "
                    f"positive number."
                ),
            })

    # --- Default repository a well-formed HTTPS GitHub remote
    #     (build-source-selection Req 1.5) ---
    if 'default_repository' in update and update['default_repository'] is not None:
        value = update['default_repository']
        _, repository_error = build_source.normalize_repository_url(value)
        if repository_error is not None:
            errors.append({
                'rule': RULE_CONFIG_REPOSITORY_INVALID,
                'parameter': 'default_repository',
                'message': (
                    'Invalid value for default_repository: '
                    + repository_error['message']
                ),
            })

    # --- Max runtime a positive duration (Req 9.5) ---
    if 'max_runtime_hours' in update and update['max_runtime_hours'] is not None:
        value = update['max_runtime_hours']
        if not _is_positive_number(value):
            errors.append({
                'rule': RULE_CONFIG_MAX_RUNTIME_INVALID,
                'parameter': 'max_runtime_hours',
                'message': (
                    f"Invalid value for max_runtime_hours: '{value}' is not "
                    f"a positive duration."
                ),
            })

    # --- OPTIONAL target/mode runtime budgets (Req 2.17) ---
    if 'runtime_budgets' in update and update['runtime_budgets'] is not None:
        errors.extend(validate_runtime_budgets(update['runtime_budgets']))

    # --- OPTIONAL per-target volume sizes, JP6 >= 200 GB (Req 2.20) ---
    if 'volume_size_gb_by_target' in update \
            and update['volume_size_gb_by_target'] is not None:
        errors.extend(validate_volume_size_by_target(
            update['volume_size_gb_by_target']))

    if errors:
        return ValidationResult.rejected(errors)
    return ValidationResult.ok()


def apply_config_update(
    stored: Optional[Dict[str, Any]],
    update: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], ValidationResult]:
    """Apply a configuration update to the stored configuration (pure).

    Returns ``(new_stored_config, validation_result)``:

      - When ``validate_build_config(update)`` rejects, the update is
        discarded in full and the returned configuration is the stored
        configuration unchanged (atomic reject, Req 9.5) — no field of a
        rejected update is applied, even fields that are individually valid.
      - When it accepts, the returned configuration is the stored
        configuration with every supplied field overwritten by the update
        (a field supplied as None reverts to its documented read-time
        default, since ``effective_build_config`` treats None as absent).

    The inputs are never mutated; the returned dict is a fresh copy.
    """
    stored_copy: Dict[str, Any] = copy.deepcopy(stored) if stored else {}
    result = validate_build_config(update)
    if not result.valid:
        return stored_copy, result
    for key, value in (update or {}).items():
        stored_copy[key] = copy.deepcopy(value)
    return stored_copy, result
