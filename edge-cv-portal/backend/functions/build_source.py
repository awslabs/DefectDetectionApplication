# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Build source pure logic (Build_Manager)

Pure decision module for the build source a Build_Job is built from: the
one authoritative on-server repository directory, its resolution for a
given Build_Job / Build_Server / runner, the agent script path derived
from it, and the Sync_Generator that produces every Source_Sync command
text used to put a runner's working tree on the selected (repository, ref)
before the agent runs.

Both the dispatcher (build_dispatcher.py) and the fleet handler
(build_fleet.py) import this module so the directory the bootstrap clones
into and the directory the agent is invoked from cannot drift apart
(Req 5.1, 5.2).

This module deliberately has NO AWS clients and NO side effects, mirroring
build_domain.py / build_planner.py: it is fully unit- and
property-testable in isolation and never mutates its inputs.

Increment B adds the containment boundary on top of that: the repository
URL and the source ref an operator submits are validated and normalized
here (normalize_repository_url / normalize_source_ref / parse_owner_repo)
before they reach any command text, any snapshot, or any outbound
discovery call (Req 1.3, 1.4, 2.7, 3.5), and branch discovery
(discover_branches) classifies every upstream outcome into either a
branch list with its default branch or one of six distinct error codes
(Req 3.1, 3.2, 3.3).

Spec: .kiro/specs/build-source-selection
Requirements: 1.3, 1.4, 2.7, 3.1, 3.2, 3.3, 3.5, 4.3, 4.4, 5.1, 5.2,
5.3, 5.4
"""
import json
import os
import re
import shlex
import socket
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# The one authoritative repository directory (Requirements 5.1, 5.2, 5.3)
# ---------------------------------------------------------------------------

#: The single authoritative on-server clone location for the repository.
#:
#: This is deliberately the location build_fleet.py's existing dedicated
#: bootstrap already clones into (build_fleet.py:179), NOT a new directory:
#: every Build_Server bootstrapped before this change has its working tree
#: exactly here and carries no recorded directory of its own, so resolving
#: to this value keeps those servers working untouched — no re-bootstrap,
#: no re-clone, no operator action (Req 5.3).
DEFAULT_REPO_DIR = '/home/ubuntu/DefectDetectionApplication'

#: Path of the build agent script inside the repository working tree.
AGENT_SCRIPT_RELPATH = 'scripts/portal-build-agent.sh'


def _recorded(value: Any) -> Optional[str]:
    """A recorded directory value, or None when nothing was recorded.

    Absent, ``None``, empty and blank values are all "not recorded": a
    Build_Server or runner record written before the bootstrap started
    recording its directory carries no field at all, and a record written
    with an empty value carries no more information than that (Req 5.3).
    Non-string values are treated the same way, keeping resolution total.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


#: The two clone roots proven relevant by retained evidence
#: (historical-evidence.md §1.5, hypothesis row 1 — CONFIRMED): the
#: incident-day dispatcher default rooted the agent path under
#: ``/opt/dda/`` while the fleet bootstrap cloned under
#: ``/home/ubuntu/``. Only these two evidenced segments participate in
#: the fallback correction below — never an arbitrary directory.
_KNOWN_CLONE_ROOT_SEGMENTS = ('/opt/dda/', '/home/ubuntu/')


def _has_agent_script(directory: str) -> bool:
    """True iff ``directory`` carries the agent script on THIS
    filesystem (the positive evidence the correction requires)."""
    try:
        return os.path.isfile(os.path.join(directory,
                                           AGENT_SCRIPT_RELPATH))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return False


def _evidenced_clone_correction(directory: str) -> str:
    """Correct a FALLBACK directory (configured override / default —
    never a recorded machine directory) to the other evidenced clone
    root, only when the filesystem reproduces hypothesis row 1's
    confirmed mismatch: ``directory`` exists but does NOT carry
    ``scripts/portal-build-agent.sh``, while the alternate obtained by
    swapping the two known clone roots DOES. Anything less keeps the
    original value byte-identically (Req 5.3: legacy behavior is
    untouched wherever the mismatch is not in evidence)."""
    try:
        directory_exists = os.path.isdir(directory)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return directory
    if not directory_exists or _has_agent_script(directory):
        return directory
    for segment, alternate_segment in (
            (_KNOWN_CLONE_ROOT_SEGMENTS[0], _KNOWN_CLONE_ROOT_SEGMENTS[1]),
            (_KNOWN_CLONE_ROOT_SEGMENTS[1], _KNOWN_CLONE_ROOT_SEGMENTS[0])):
        if segment not in directory:
            continue
        alternate = directory.replace(segment, alternate_segment, 1)
        if _has_agent_script(alternate):
            return alternate
    return directory


def resolve_repo_dir(
    job: Optional[Dict[str, Any]],
    server: Optional[Dict[str, Any]] = None,
    env_default: Optional[str] = None,
) -> str:
    """Repo_Dir for a Build_Job (Req 5.1, 5.2, 5.3, 5.4).

    Precedence, highest first:

    1. the directory the bootstrap recorded for this server/runner —
       ``server['repo_dir']`` for a dedicated Build_Server, else
       ``job['runner']['repo_dir']`` for an ephemeral runner. This is the
       directory that machine's bootstrap actually used, so it always wins.
    2. the configured environment override (``env_default``), for an
       operator-pinned deployment. The deployed Lambda sets none.
    3. ``DEFAULT_REPO_DIR`` — where every server bootstrapped before this
       change already has its tree (Req 5.3).

    Absent, ``None`` and empty recorded values are indistinguishable: all
    three mean "not recorded" and fall through to the next source.

    Evidence-gated correction (build-fleet-execution-failures task 7.1;
    historical-evidence.md hypothesis row 1 — CONFIRMED): the 2026-08-06
    AMD64 failure was proximately caused by the dispatcher's effective
    `/opt/dda/DefectDetectionApplication` default while the fleet
    bootstrap had cloned to `/home/ubuntu/DefectDetectionApplication`
    (SSM stderr + EC2 user data + incident-day deployed asset). When a
    LEGACY record carries no directory of its own and resolution falls
    to the configured override / default, that value is corrected to the
    OTHER known clone root iff the filesystem proves the mismatch: the
    fallback directory exists WITHOUT `scripts/portal-build-agent.sh`
    while the known-alternate directory carries it. Recorded server /
    runner directories are that machine's own bootstrap record and are
    never second-guessed. Row 1's caveat applies: this is contract
    hardening of the dispatch path — fixing the path alone is NOT proven
    sufficient for a successful build (the agent script's presence on
    the default branch at the incident time is unproven).
    """
    server_dir = _recorded((server or {}).get('repo_dir'))
    if server_dir is not None:
        return server_dir
    runner = (job or {}).get('runner') or {}
    runner_dir = _recorded(runner.get('repo_dir') if isinstance(runner, dict)
                           else None)
    if runner_dir is not None:
        return runner_dir
    configured = _recorded(env_default)
    if configured is not None:
        return _evidenced_clone_correction(configured)
    return _evidenced_clone_correction(DEFAULT_REPO_DIR)


def agent_script_path(repo_dir: str) -> str:
    """Path of the build agent script inside ``repo_dir`` (Req 5.1, 5.4).

    The single origin of the agent path: the dispatcher builds its agent
    command from this instead of composing the path itself, so the path it
    invokes is always rooted in the resolved bootstrap directory.
    """
    return f'{repo_dir}/{AGENT_SCRIPT_RELPATH}'

# ---------------------------------------------------------------------------
# Sync_Generator — the single origin of all Source_Sync command text
# (Requirement 4.3; spec .kiro/specs/build-source-selection design A3)
# ---------------------------------------------------------------------------

#: Marker line prefix every Source_Sync failure echoes before exiting, so
#: the failure is identifiable in SSM/CloudWatch output and classifiable by
#: the reader without SSM invocation reads (Req 4.4).
SYNC_MARKER = 'PORTAL_SOURCE_SYNC_FAILED'

#: Dedicated exit codes. A Source_Sync failure must NEVER surface as a bare
#: 127 ("No such file or directory" from invoking an agent script the tree
#: could not contain) — that opaque code is the live failure this spec
#: exists to eliminate (SSM e9281bdc / d75f1ea2).
EXIT_REPO_UNREACHABLE = 65   # the repository itself could not be obtained
EXIT_REF_NOT_FOUND = 66      # the repository was obtained, the ref was not

#: Failure classes carried by the marker line's ``kind=`` field. These are
#: the two values Req 4.4 distinguishes and are reused verbatim as the
#: ``source_error`` value on the emitted phase event (task 5.3).
SYNC_KIND_REPOSITORY_UNREACHABLE = 'repository_unreachable'
SYNC_KIND_REF_NOT_FOUND = 'ref_not_found'

#: Shell variable names the generated sequence assigns once, from
#: ``shlex.quote``-ed literals, and references everywhere after. Every
#: interpolation of caller-supplied text happens on those three assignment
#: lines and nowhere else: in Increment B the repository and the ref are
#: typed by an operator, so the generated text must be injection-safe by
#: construction rather than by review.
VAR_REPO_DIR = 'REPO_DIR'
VAR_REPO_URL = 'REPO_URL'
VAR_SOURCE_REF = 'SOURCE_REF'

#: Build-environment bootstrap script at the root of the working tree, run
#: by both user-data paths after the source is in place.
SETUP_SCRIPT_RELPATH = './setup-build-server.sh'

# ---------------------------------------------------------------------------
# Source_Sync failure surfacing (Requirement 4.4)
# ---------------------------------------------------------------------------
#
# A nonzero SSM exit alone leaves the Build_Job sitting in
# provisioning/building with no named cause, so the failure branches also
# emit ONE phase event onto the EXISTING phase-event pipeline
# (build_events.handle_phase_event) before exiting with their dedicated
# code. No SSM invocation reads are involved — that reconciliation belongs
# to .kiro/specs/build-fleet-execution-failures.
#
# The emission is an inline `aws events put-events` call, a deliberate
# minimal duplication of scripts/portal-build-agent.sh's `emit_event` /
# `emit_failed`: that helper CANNOT be reused here because it lives in the
# very tree this sync is fetching (a failed sync is exactly the case where
# it is absent). The detail shape below is therefore kept identical to the
# agent's `emit_failed`, key order included, so build_events consumes both
# paths identically:
#
#   {"build_job_id":..,"phase":"failed","build_target":..,
#    "error_kind":"source_sync","error_message":..,"source_error":..}
#
# ``source_error`` sits where the agent's `emit_failed` splices its
# ``extra`` fields (the same slot its publish-stage failure uses for the
# artifact lists), so the shape is an extension of the agent's, not a
# different one.

#: EventBridge envelope the agent uses; the reader (build_events) is
#: subscribed on both values, so they must match exactly.
EVENT_SOURCE = 'dda.portal.builds'
EVENT_DETAIL_TYPE = 'BuildPhaseChange'

#: Phase and error_kind carried by a Source_Sync failure event.
#:
#: ``error_kind`` is deliberately NOT ``publishing``: that is the one value
#: build_events.apply_phase_event branches on, so ``source_sync`` takes the
#: build-stage failure edge (-> failed, a BUILD_FAILED error record, and a
#: build_failed audit entry carrying error_kind=source_sync) with no change
#: to build_events.py.
EVENT_PHASE_FAILED = 'failed'
SYNC_ERROR_KIND = 'source_sync'

#: Shell variables and function names the emission block defines, prefixed
#: to stay clear of the agent's own names (the agent runs as a separate
#: process, but the preamble and the agent invocation share one SSM
#: command text).
VAR_EVENT_BUS = 'PORTAL_EVENT_BUS'
VAR_BUILD_JOB_ID = 'PORTAL_BUILD_JOB_ID'
VAR_BUILD_TARGET = 'PORTAL_BUILD_TARGET'
FN_JSON_ESCAPE = 'portal_source_sync_json_escape'
FN_EMIT_FAILED = 'portal_source_sync_emit_failed'

#: ``error_message`` text per failure class. Both name BOTH the repository
#: and the ref (Req 4.4), and both name them by *quoted shell expansion*:
#: the operator-supplied values reach the payload through the three
#: shlex.quote-ed assignments only, never re-interpolated into command
#: position.
_SYNC_FAILURE_MESSAGES = {
    SYNC_KIND_REPOSITORY_UNREACHABLE: (
        f"Source sync failed: repository '${{{VAR_REPO_URL}}}' could not be "
        f"obtained (requested ref '${{{VAR_SOURCE_REF}}}')"
    ),
    SYNC_KIND_REF_NOT_FOUND: (
        f"Source sync failed: ref '${{{VAR_SOURCE_REF}}}' was not found in "
        f"repository '${{{VAR_REPO_URL}}}'"
    ),
}

#: Detail JSON: the agent's `emit_failed` printf format with its two fixed
#: values filled in and ``source_error`` in the ``extra`` slot.
_DETAIL_FORMAT = (
    '{"build_job_id":"%s","phase":"' + EVENT_PHASE_FAILED + '",'
    '"build_target":"%s","error_kind":"' + SYNC_ERROR_KIND + '",'
    '"error_message":"%s","source_error":"%s"}'
)

#: PutEvents entries JSON: the agent's `emit_event` format.
_ENTRIES_FORMAT = (
    '[{"Source":"' + EVENT_SOURCE + '","DetailType":"' + EVENT_DETAIL_TYPE
    + '","Detail":"%s","EventBusName":"%s"}]'
)


def _text(value: Any) -> str:
    """A caller-supplied value as trimmed text ('' when absent).

    Keeps generation total: ``None``, a blank string and a non-string all
    collapse to ``''``, which is what "not selected" means for a ref
    (clone-only, today's behavior) and for a repository URL.
    """
    if not isinstance(value, str):
        return ''
    return value.strip()


def _fail_guard(kind: str, exit_code: int, emit: bool = False) -> str:
    """A failure branch: echo the marker line, then exit with its code.

    The marker line names both the repository and the ref
    (``PORTAL_SOURCE_SYNC_FAILED kind=<class> repository=<url> ref=<ref>``)
    by *variable reference*, so the values appear in the output without
    being re-interpolated into the command text (Req 4.4).

    With ``emit`` the guard additionally calls the emission helper between
    the marker line and the exit, so exactly ONE phase event names this
    failure before the classified exit code is taken (Req 4.4). The
    helper always returns 0, and the ``exit`` is sequenced after it with
    ``;``, so a failed emission can never mask or change the exit code.
    Without ``emit`` the text is byte-identical to the pre-emission
    generator, keeping the user-data callers unchanged.
    """
    marker = (
        f'echo "{SYNC_MARKER} kind={kind} '
        f'repository=${VAR_REPO_URL} ref=${VAR_SOURCE_REF}"'
    )
    if not emit:
        return f'{{ {marker}; exit {exit_code}; }}'
    return (
        f'{{ {marker}; '
        f'{FN_EMIT_FAILED} {shlex.quote(kind)} '
        f'"{_SYNC_FAILURE_MESSAGES[kind]}"; '
        f'exit {exit_code}; }}'
    )


def _emits_events(event_bus: Any, build_job_id: Any) -> bool:
    """Whether a generated sync emits phase events (Req 4.4).

    Both a bus and a Build_Job id are required: an event with no
    ``build_job_id`` names no job and the reader would drop it, and an
    empty bus is the agent's own "standalone/debug run" case. Either one
    missing means marker-line-plus-exit-code only, i.e. exactly the
    pre-emission text — which is what the user-data callers want, since a
    user-data sync failure happens before any job-scoped agent command.
    """
    return bool(_text(event_bus)) and bool(_text(build_job_id))


def sync_failure_event_commands(
    event_bus: Any,
    build_job_id: Any,
    build_target: Any = None,
) -> List[str]:
    """The emission block the failure guards call (Req 4.4), or ``[]``.

    Defines two shell functions mirroring
    ``scripts/portal-build-agent.sh``:

    * ``portal_source_sync_json_escape`` — the agent's ``json_escape``
      verbatim (backslashes, double quotes, control characters), so an
      operator-typed repository or ref cannot break the JSON payload;
    * ``portal_source_sync_emit_failed <kind> <message>`` — the agent's
      ``emit_failed`` composed with ``emit_event``: the same detail keys
      in the same order, the same entries envelope, the same
      ``FailedEntryCount`` check, and the same degrade-to-a-warning
      behavior. It always returns 0: the classified exit code is the
      contract, the event is the diagnosis.

    The bus, Build_Job id and Build_Target are interpolated only into
    ``shlex.quote``-ed assignments, like the repository/directory/ref.
    """
    if not _emits_events(event_bus, build_job_id):
        return []
    bus = _text(event_bus)
    job_id = _text(build_job_id)
    target = _text(build_target)
    return [
        f'{VAR_EVENT_BUS}={shlex.quote(bus)}',
        f'{VAR_BUILD_JOB_ID}={shlex.quote(job_id)}',
        f'{VAR_BUILD_TARGET}={shlex.quote(target)}',
        f'{FN_JSON_ESCAPE}() {{',
        "  printf '%s' \"$1\" | tr '\\n\\r\\t' '   ' "
        "| tr -d '\\000-\\010\\013\\014\\016-\\037' "
        "| sed -e 's/\\\\/\\\\\\\\/g' -e 's/\"/\\\\\"/g'",
        '}',
        f'{FN_EMIT_FAILED}() {{',
        f'  [ -n "${VAR_EVENT_BUS}" ] || return 0',
        '  _PORTAL_SYNC_KIND="$1"',
        '  _PORTAL_SYNC_MESSAGE="$2"',
        f"  _PORTAL_SYNC_DETAIL=$(printf '{_DETAIL_FORMAT}' "
        f'"${VAR_BUILD_JOB_ID}" "${VAR_BUILD_TARGET}" '
        f'"$({FN_JSON_ESCAPE} "$_PORTAL_SYNC_MESSAGE")" '
        '"$_PORTAL_SYNC_KIND")',
        f"  _PORTAL_SYNC_ENTRIES=$(printf '{_ENTRIES_FORMAT}' "
        f'"$({FN_JSON_ESCAPE} "$_PORTAL_SYNC_DETAIL")" '
        f'"${VAR_EVENT_BUS}")',
        '  aws events put-events --entries "$_PORTAL_SYNC_ENTRIES" '
        "--query 'FailedEntryCount' --output text 2>/dev/null "
        "| grep -q '^0$' "
        f'|| echo "WARNING: could not emit the {SYNC_MARKER} '
        f'phase event to bus ${VAR_EVENT_BUS}"',
        '  return 0',
        '}',
    ]


def _bootstrap_marker_path() -> str:
    """The Bootstrap_Marker path, read from its one definition.

    ``build_planner.BOOTSTRAP_MARKER_PATH`` (task 4.1) is the single
    definition of this literal and is NOT duplicated here. The import is
    function-local on purpose: ``build_source`` is imported by
    ``build_dispatcher`` / ``build_fleet`` and, once the readiness gate is
    wired, the planner sits on the same import graph — a module-level
    ``import build_planner`` here would make the two pure modules a cycle
    away from each other for no benefit.
    """
    import build_planner  # local import: keeps the pure modules acyclic
    return build_planner.BOOTSTRAP_MARKER_PATH


def source_sync_commands(
    repo_url: Any,
    repo_dir: Any,
    source_ref: Any = None,
    event_bus: Any = None,
    build_job_id: Any = None,
    build_target: Any = None,
) -> List[str]:
    """Shell commands that put ``repo_dir`` on ``source_ref`` (Req 4.3).

    Returns one shell statement (or block line) per element; callers join
    with newlines. This is the ONLY origin of Source_Sync command text —
    there is deliberately no second, divergent sync mechanism.

    The semantics are exactly the ones ``scripts/portal-build-agent.sh``
    already implements in its Step 2, because the agent re-runs its own
    sync after this preamble and the two must be idempotent together:

    1. clone when the tree is absent, guarded on ``$REPO_DIR/.git`` like
       the existing bootstrap, so a pre-baked AMI or an already-cloned
       server is untouched;
    2. ``git fetch --prune origin``;
    3. ``git checkout --force -B <ref> origin/<ref>`` when
       ``refs/remotes/origin/<ref>`` verifies (a branch: the local branch
       is recreated at the remote tip), else ``git checkout --force <ref>``
       (a tag or commit SHA).

    Every failure branch echoes
    ``PORTAL_SOURCE_SYNC_FAILED kind=<class> repository=<url> ref=<ref>``
    and exits ``EXIT_REPO_UNREACHABLE`` (65) when the repository could not
    be obtained or entered, or ``EXIT_REF_NOT_FOUND`` (66) when the
    requested ref could not be checked out — never a bare 127 (Req 4.4).

    An empty or ``None`` ``source_ref`` yields the clone-only sequence:
    no fetch, no checkout, i.e. exactly today's behavior on the
    currently checked-out tree.

    ``event_bus`` / ``build_job_id`` / ``build_target`` are the OPTIONAL
    failure-surfacing parameters (Req 4.4). Given a bus and a Build_Job id,
    every failure guard emits one ``dda.portal.builds`` /
    ``BuildPhaseChange`` event (``phase=failed``,
    ``error_kind=source_sync``, ``source_error=<class>``, a message naming
    both the repository and the ref) before taking its dedicated exit
    code. Omitted — the two user-data callers, whose sync runs before any
    job-scoped agent command exists — the generated text is byte-identical
    to the pre-emission generator.

    Injection safety: the repository, directory, ref, bus, Build_Job id and
    Build_Target are interpolated only into ``shlex.quote``-ed variable
    assignments; every later reference, including the ones inside the event
    payload, is a quoted shell expansion. The repository and the ref come
    from operator input in Increment B.
    """
    directory = _text(repo_dir) or DEFAULT_REPO_DIR
    url = _text(repo_url)
    ref = _text(source_ref)

    emit = _emits_events(event_bus, build_job_id)
    unreachable = _fail_guard(SYNC_KIND_REPOSITORY_UNREACHABLE,
                              EXIT_REPO_UNREACHABLE, emit)
    not_found = _fail_guard(SYNC_KIND_REF_NOT_FOUND, EXIT_REF_NOT_FOUND,
                            emit)

    # The three assignments are emitted unconditionally, including an empty
    # ref: the failure branches reference all three and the generated body
    # runs under `set -u` in the ephemeral user-data.
    commands: List[str] = [
        f'{VAR_REPO_DIR}={shlex.quote(directory)}',
        f'{VAR_REPO_URL}={shlex.quote(url)}',
        f'{VAR_SOURCE_REF}={shlex.quote(ref)}',
    ]
    # The emission block is defined before the first guard that calls it.
    commands.extend(sync_failure_event_commands(
        event_bus, build_job_id, build_target))
    commands.extend([
        # Dedicated servers: the pre-agent preamble runs as root via SSM
        # against a tree the ubuntu user created during manual/fleet
        # bootstrap, and git refuses to operate on it ("detected dubious
        # ownership", SSM 30327734 / job 19f270c2 / srv-3f963f3b). Marking
        # the resolved directory safe before any git statement lets the
        # sync proceed; `|| true` keeps a read-only home or a missing git
        # from introducing a new failure mode, and the agent's own Step 2
        # sync (also root on dedicated servers) inherits the same global
        # config. Ephemeral runners are unaffected: root created the
        # clone there, so the entry is redundant but harmless.
        f'git config --global --add safe.directory "${VAR_REPO_DIR}" '
        '2>/dev/null || true',
        f'mkdir -p "$(dirname "${VAR_REPO_DIR}")"',
        f'if [ ! -d "${VAR_REPO_DIR}/.git" ]; then',
        f'  git clone "${VAR_REPO_URL}" "${VAR_REPO_DIR}" || {unreachable}',
        'fi',
        f'cd "${VAR_REPO_DIR}" || {unreachable}',
    ])
    if not ref:
        return commands

    commands.extend([
        f'git fetch --prune origin || {unreachable}',
        f'if git rev-parse --verify --quiet '
        f'"refs/remotes/origin/${VAR_SOURCE_REF}" >/dev/null 2>&1; then',
        f'  git checkout --force -B "${VAR_SOURCE_REF}" '
        f'"origin/${VAR_SOURCE_REF}" || {not_found}',
        'else',
        f'  git checkout --force "${VAR_SOURCE_REF}" || {not_found}',
        'fi',
    ])
    return commands


def bootstrap_commands(
    repo_url: Any,
    repo_dir: Any,
    source_ref: Any = None,
) -> List[str]:
    """The shared body of both user-data scripts (Req 4.3).

    ``source_sync_commands`` (which leaves the shell inside ``$REPO_DIR``
    on the selected ref) plus the build-environment bootstrap plus the
    Bootstrap_Marker write as the LAST statement, so the marker means
    "bootstrap ran to completion" and the readiness gate
    (``build_planner.decide_runner_readiness``) has an authoritative
    signal to observe.

    The setup script's own exit status is deliberately not a guard: the
    live runner logged inner-step failures (``chmod 666
    /var/run/docker.sock``, the Python 3.11 default) and still finished
    usefully, and Req 6.4 makes the marker — not the inner steps —
    authoritative. The generated body runs without ``set -e``, so the
    marker is written either way.

    The marker path is read from ``build_planner.BOOTSTRAP_MARKER_PATH``,
    its one definition; it is not re-spelled here.

    User-data deliberately passes NO event-emission parameters: it runs at
    instance boot, before any job-scoped agent command exists, and a
    Build_Job phase event from that point would race the dispatcher's own
    provisioning bookkeeping. A user-data sync failure surfaces through the
    marker line in the bootstrap log plus the absent Bootstrap_Marker,
    which the readiness gate already turns into a bootstrap-stage failure
    (Req 6.3). The generated body is therefore byte-identical to the
    pre-emission generator.
    """
    commands = list(source_sync_commands(repo_url, repo_dir, source_ref))
    commands.append(f'bash {SETUP_SCRIPT_RELPATH}')
    commands.append(f'touch {shlex.quote(_bootstrap_marker_path())}')
    return commands


# ---------------------------------------------------------------------------
# Repository / ref validation and normalization
# (Requirements 1.3, 1.4, 2.7, 3.5; spec design B2)
# ---------------------------------------------------------------------------
#
# In Increment B the repository and the ref are OPERATOR INPUT: they arrive in
# a request body or a query string and end up (a) inside generated shell
# command text, (b) on the Build_Job's config_snapshot, and (c) as the basis
# of an outbound discovery call. This section is the single containment
# boundary in front of all three.
#
# Two shapes matter and are kept distinct:
#
#   * the REJECTION — a dict carrying ``rule`` and ``field`` (plus a
#     user-readable ``message``), so the caller can splice it straight into
#     the existing validation envelope with the offending field named
#     (Req 1.4). ``rule``/``message`` match build_domain's error shape; the
#     added ``field`` is what names the form control;
#   * the NORMALIZED_REPOSITORY — ``https://<allowed-host>/<owner>/<repo>``
#     with no userinfo, no port, no query, no fragment, no ``.git`` suffix
#     and no trailing slash. Discovery URLs are built ONLY from the
#     ``<owner>/<repo>`` this yields (``parse_owner_repo``), never from the
#     raw input, so no input can direct a request at a non-repository
#     endpoint (Req 3.5).
#
# Both functions are TOTAL: for every input, including non-strings, they
# return either an accepted value or a rejection — never an exception.

#: Repository hosts a build may be made from. A single-element allowlist is
#: deliberate: discovery (task 10.1) speaks the GitHub API, and Req 3.2's
#: "no credentials needed" only holds for public GitHub repositories.
ALLOWED_REPOSITORY_HOSTS = ('github.com',)

#: The only accepted URL scheme (Req 1.4: "a well-formed HTTPS Git remote").
REPOSITORY_SCHEME = 'https'

#: Field names the rejections carry, matching the submitted body keys so the
#: frontend can attach the message to the control the operator typed into.
FIELD_REPOSITORY = 'repository'
FIELD_SOURCE_REF = 'source_ref'

#: Rejection rule identifiers. build_domain re-exports these from its own
#: validation surface (tasks 8 and 9) rather than inventing second spellings.
RULE_REPOSITORY_INVALID = 'repository_invalid'
RULE_SOURCE_REF_INVALID = 'source_ref_invalid'

#: Length ceilings. Both are far above any real value (GitHub caps owners at
#: 39 and repository names at 100 characters; git refs are conventionally
#: well under 255) and exist so a megabyte-long body cannot be walked
#: character by character.
MAX_REPOSITORY_LENGTH = 512
MAX_SOURCE_REF_LENGTH = 255

#: The example the form defaults to, restated in rejection messages so the
#: operator is told the accepted shape rather than only what failed.
_REPOSITORY_SHAPE = f'https://{ALLOWED_REPOSITORY_HOSTS[0]}/<owner>/<repo>'

_CONTROL_CHARS_RE = re.compile(r'[\x00-\x1f\x7f]')
_WHITESPACE_RE = re.compile(r'\s')

#: GitHub owner (user or organisation): alphanumerics and hyphens, starting
#: with an alphanumeric.
_OWNER_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]*$')

#: Repository name: alphanumerics, ``.``, ``_`` and ``-``, starting with an
#: alphanumeric or ``_``. Refusing a leading ``.`` keeps ``.`` and ``..``
#: out of the segment by construction.
_REPO_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]*$')

#: The Normalized_Repository invariant, asserted on the composed result as a
#: belt-and-braces check: whatever path the staged validation took, the value
#: handed back always satisfies this.
_NORMALIZED_REPOSITORY_RE = re.compile(
    r'^https://(?:'
    + '|'.join(re.escape(host) for host in ALLOWED_REPOSITORY_HOSTS)
    + r')/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*$'
)

#: Accepted ref character set: letters, digits, ``.`` ``_`` ``-`` ``/`` ``+``.
#: This is a tightening of ``git check-ref-format`` (git also allows ``#``,
#: ``=``, ``,`` and more), chosen because the value reaches git through
#: generated shell text: every character git's own rules forbid — whitespace,
#: control characters, ``~ ^ : ? * [ \`` — is outside the set, so the
#: catch-all below cannot be bypassed by a form the named checks miss.
_SOURCE_REF_RE = re.compile(r'^[A-Za-z0-9._/+-]+$')

#: A 40-character hex commit SHA, accepted verbatim (Req 2.7): the agent's
#: existing sync takes the detached ``git checkout --force <ref>`` arm for it.
SOURCE_REF_SHA_RE = re.compile(r'^[0-9a-fA-F]{40}$')


def _repository_rejection(message: str) -> Dict[str, str]:
    """A repository rejection naming its rule and its field (Req 1.4)."""
    return {
        'rule': RULE_REPOSITORY_INVALID,
        'field': FIELD_REPOSITORY,
        'message': message,
    }


def _source_ref_rejection(message: str) -> Dict[str, str]:
    """A ref rejection naming its rule and its field (Req 1.4)."""
    return {
        'rule': RULE_SOURCE_REF_INVALID,
        'field': FIELD_SOURCE_REF,
        'message': message,
    }


def normalize_repository_url(
    value: Any,
) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    """Validate and normalize a submitted repository URL (Req 1.3, 1.4, 3.5).

    Returns ``(normalized_url, None)`` on acceptance or ``(None, error)`` on
    rejection, where ``error`` carries ``rule``, ``field`` and ``message``.
    Exactly one element is ever non-``None``, for every input.

    Accepted: ``https``, a host in :data:`ALLOWED_REPOSITORY_HOSTS`, and a
    path of exactly ``<owner>/<repo>`` — with an optional ``.git`` suffix and
    an optional single trailing slash, both of which the normalized form
    drops. Surrounding whitespace is trimmed first, since a pasted URL
    routinely carries it.

    Rejected, each with its own message: non-string input, empty input, a
    value longer than :data:`MAX_REPOSITORY_LENGTH`, embedded whitespace or
    control characters, any scheme other than ``https`` (so ``http://`` and
    ``git@github.com:...`` are out), userinfo, a port, a query string, a
    fragment, a non-allowlisted host, and a path that is not exactly two
    segments (a missing repository, an extra segment such as
    ``/owner/repo/tree/main``, or a traversal attempt).

    The normalized form is idempotent — normalizing it again returns it
    unchanged — and always satisfies the Normalized_Repository invariant:
    HTTPS, host-allowlisted, ``<owner>/<repo>``, no userinfo/port/query/
    fragment. That is what makes :func:`parse_owner_repo` safe to build
    outbound discovery URLs from (Req 3.5).
    """
    if not isinstance(value, str):
        return None, _repository_rejection(
            'The repository must be text: an HTTPS GitHub URL of the form '
            f'{_REPOSITORY_SHAPE}.'
        )
    candidate = value.strip()
    if not candidate:
        return None, _repository_rejection(
            f'The repository must not be empty. Expected {_REPOSITORY_SHAPE}.'
        )
    if len(candidate) > MAX_REPOSITORY_LENGTH:
        return None, _repository_rejection(
            f'The repository is too long ({len(candidate)} characters, the '
            f'maximum is {MAX_REPOSITORY_LENGTH}). Expected '
            f'{_REPOSITORY_SHAPE}.'
        )
    if _CONTROL_CHARS_RE.search(candidate):
        return None, _repository_rejection(
            'The repository must not contain control characters. Expected '
            f'{_REPOSITORY_SHAPE}.'
        )
    if _WHITESPACE_RE.search(candidate):
        return None, _repository_rejection(
            'The repository must not contain spaces. Expected '
            f'{_REPOSITORY_SHAPE}.'
        )

    try:
        parts = urlsplit(candidate)
    except ValueError:
        # Malformed authority (an unterminated IPv6 literal, for example).
        return None, _repository_rejection(
            f"'{candidate}' is not a well-formed URL. Expected "
            f'{_REPOSITORY_SHAPE}.'
        )

    if parts.scheme != REPOSITORY_SCHEME:
        named = f"'{parts.scheme}'" if parts.scheme else 'no scheme'
        return None, _repository_rejection(
            f'The repository must be an HTTPS URL: {named} is not accepted. '
            f'Expected {_REPOSITORY_SHAPE}.'
        )
    if parts.query:
        return None, _repository_rejection(
            'The repository must not carry a query string. Expected '
            f'{_REPOSITORY_SHAPE}.'
        )
    if parts.fragment:
        return None, _repository_rejection(
            'The repository must not carry a fragment. Expected '
            f'{_REPOSITORY_SHAPE}.'
        )

    netloc = parts.netloc
    if '@' in netloc:
        return None, _repository_rejection(
            'The repository must not carry credentials. Expected '
            f'{_REPOSITORY_SHAPE}.'
        )
    if ':' in netloc:
        return None, _repository_rejection(
            'The repository must not carry a port. Expected '
            f'{_REPOSITORY_SHAPE}.'
        )
    host = netloc.lower()
    if host not in ALLOWED_REPOSITORY_HOSTS:
        allowed = ', '.join(ALLOWED_REPOSITORY_HOSTS)
        named = f"'{netloc}'" if netloc else 'no host'
        return None, _repository_rejection(
            f'The repository host {named} is not allowed. Allowed hosts: '
            f'{allowed}.'
        )

    path = parts.path
    if path.endswith('/'):
        path = path[:-1]
    if not path.startswith('/'):
        return None, _repository_rejection(
            'The repository must name an owner and a repository. Expected '
            f'{_REPOSITORY_SHAPE}.'
        )
    segments = path[1:].split('/')
    if len(segments) != 2:
        return None, _repository_rejection(
            f"'{candidate}' does not name exactly one owner and one "
            f'repository. Expected {_REPOSITORY_SHAPE}.'
        )
    owner, repo = segments
    if repo.endswith('.git') and len(repo) > len('.git'):
        repo = repo[:-len('.git')]
    if not _OWNER_RE.match(owner):
        return None, _repository_rejection(
            f"'{owner}' is not a valid repository owner. Expected "
            f'{_REPOSITORY_SHAPE}.'
        )
    if not _REPO_RE.match(repo):
        return None, _repository_rejection(
            f"'{repo}' is not a valid repository name. Expected "
            f'{_REPOSITORY_SHAPE}.'
        )

    normalized = f'{REPOSITORY_SCHEME}://{host}/{owner}/{repo}'
    if not _NORMALIZED_REPOSITORY_RE.match(normalized):
        # Unreachable by construction; kept so the invariant is enforced by
        # the code that produces the value, not only by its tests.
        return None, _repository_rejection(
            f"'{candidate}' is not a well-formed repository URL. Expected "
            f'{_REPOSITORY_SHAPE}.'
        )
    return normalized, None


def parse_owner_repo(normalized_url: Any) -> Tuple[str, str]:
    """The ``(owner, repo)`` pair of a Normalized_Repository (Req 3.5).

    Discovery builds every outbound URL from this pair against its own fixed
    API host, never from operator input, so no submitted value can reach a
    non-repository endpoint. To make that guarantee unconditional the input
    is re-normalized here: a value that is not already a
    Normalized_Repository raises ``ValueError`` instead of being parsed
    best-effort, so there is no path from a rejected string to an outbound
    URL.
    """
    normalized, error = normalize_repository_url(normalized_url)
    if error is not None:
        raise ValueError(error['message'])
    host, owner, repo = normalized.split('://', 1)[1].split('/')
    del host
    return owner, repo


def normalize_source_ref(
    value: Any,
) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    """Validate and normalize a submitted source ref (Req 1.4, 2.7).

    Returns ``(ref, None)`` on acceptance or ``(None, error)`` on rejection,
    where ``error`` carries ``rule``, ``field`` and ``message``.

    ``None`` and a blank string are ACCEPTED as ``(None, None)``: "no ref
    selected", which is the existing ``source_ref = None`` meaning of "the
    repository default branch" (Req 2.4). Callers therefore branch on the
    error, not on the value.

    Accepted otherwise: branch names (``main``,
    ``feature/portal-build-fleet-and-workflow-gates``), tags (``v1.2.3``)
    and 40-hex commit SHAs — all three returned VERBATIM apart from trimmed
    surrounding whitespace, since the value has to match the remote exactly
    and Req 2.7 keeps non-branch refs valid.

    Rejected, each with its own message: a non-string that is not ``None``, a
    value longer than :data:`MAX_SOURCE_REF_LENGTH`, control characters,
    embedded whitespace, a leading ``-`` (which git would read as an option),
    ``..``, a leading or trailing ``/`` or an empty path component, a
    component starting with ``.``, a trailing ``.`` or a ``.lock`` suffix,
    and any character outside the accepted set (which covers git's own
    forbidden ``~ ^ : ? * [ \\`` and ``@{``).
    """
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, _source_ref_rejection(
            'The source ref must be text: a branch name, a tag, or a '
            '40-character commit SHA.'
        )
    candidate = value.strip()
    if not candidate:
        return None, None
    if len(candidate) > MAX_SOURCE_REF_LENGTH:
        return None, _source_ref_rejection(
            f'The source ref is too long ({len(candidate)} characters, the '
            f'maximum is {MAX_SOURCE_REF_LENGTH}).'
        )
    if _CONTROL_CHARS_RE.search(candidate):
        return None, _source_ref_rejection(
            'The source ref must not contain control characters.'
        )
    if _WHITESPACE_RE.search(candidate):
        return None, _source_ref_rejection(
            'The source ref must not contain spaces.'
        )
    if candidate.startswith('-'):
        return None, _source_ref_rejection(
            "The source ref must not start with '-'."
        )
    if '..' in candidate:
        return None, _source_ref_rejection(
            "The source ref must not contain '..'."
        )
    if candidate.startswith('/') or candidate.endswith('/') or '//' in candidate:
        return None, _source_ref_rejection(
            "The source ref must not start or end with '/' or contain an "
            'empty path component.'
        )
    if candidate.endswith('.'):
        return None, _source_ref_rejection(
            "The source ref must not end with '.'."
        )
    for component in candidate.split('/'):
        if component.startswith('.'):
            return None, _source_ref_rejection(
                "No component of the source ref may start with '.'."
            )
        if component.endswith('.lock'):
            return None, _source_ref_rejection(
                "No component of the source ref may end with '.lock'."
            )
    if not _SOURCE_REF_RE.match(candidate):
        return None, _source_ref_rejection(
            f"'{candidate}' is not a valid source ref. A ref may contain "
            'letters, digits, and the characters . _ - / + only.'
        )
    return candidate, None


# ---------------------------------------------------------------------------
# Branch discovery (Requirements 3.1, 3.2, 3.3, 3.5; spec design B4)
# ---------------------------------------------------------------------------
#
# Discovery answers "which branches does the selected repository have, and
# which one is its default?" for the submission form's branch dropdown. It
# is pure classification around an INJECTED fetch, mirroring
# vllm_fit_check._default_hf_fetch: the default fetch is the only line that
# touches the network, so every upstream outcome — success, empty
# repository, 404, rate-limited 403, plain 403, 429, timeout, 5xx,
# malformed payload — is drivable in tests by substituting the callable.
#
# Containment (Req 3.5): every outbound URL is composed from
# ``parse_owner_repo()`` of a Normalized_Repository against the fixed
# :data:`GITHUB_API_HOST` — never from raw operator input, which
# ``parse_owner_repo`` refuses with ``ValueError`` before any call is made.
#
# No credentials (Req 3.2): the default fetch sends no Authorization
# header. Both the DDA repository and typical forks are public, and the
# unauthenticated GitHub API limit is enough for a dropdown.

#: The one fixed API host discovery speaks to. URLs are built ONLY as
#: ``{GITHUB_API_HOST}/repos/<owner>/<repo>...`` from the parsed pair.
GITHUB_API_HOST = 'https://api.github.com'

#: Socket timeout per outbound call, mirroring vllm_fit_check's short
#: HF fetch timeout: the caller is an interactive form, not a batch job.
DISCOVERY_TIMEOUT_SECONDS = 5

#: GitHub's maximum page size for the branches listing.
BRANCHES_PER_PAGE = 100

#: Page cap: 3 pages x 100 -> at most 300 branches, then ``truncated`` is
#: flagged instead of walking an arbitrarily large repository.
MAX_BRANCH_PAGES = 3

#: Distinct, actionable error codes — one per upstream condition (Req 3.3).
#: A failure is NEVER reported as a success with an empty branch list.
REPOSITORY_NOT_FOUND = 'REPOSITORY_NOT_FOUND'          # 404
REPOSITORY_FORBIDDEN = 'REPOSITORY_FORBIDDEN'          # 403, no rate-limit
DISCOVERY_RATE_LIMITED = 'DISCOVERY_RATE_LIMITED'      # 403/429 with it
DISCOVERY_TIMEOUT = 'DISCOVERY_TIMEOUT'                # socket timeout
DISCOVERY_UPSTREAM_ERROR = 'DISCOVERY_UPSTREAM_ERROR'  # 5xx / malformed
REPOSITORY_EMPTY = 'REPOSITORY_EMPTY'                  # reachable, 0 branches


def _default_github_fetch(url: str) -> Any:
    """Fetch and JSON-decode a GitHub API URL (short timeout).

    Mirrors ``vllm_fit_check._default_hf_fetch``. Deliberately carries NO
    Authorization header and no token plumbing: discovery targets public
    repositories only (Req 3.2), so no credential can leak into a URL or
    a request this module composes.
    """
    request = urllib.request.Request(
        url, headers={'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(
            request, timeout=DISCOVERY_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode('utf-8'))


def _discovery_failure(code: str, message: str) -> Dict[str, Any]:
    """A discovery failure: a distinct code plus an actionable message."""
    return {'error': {'code': code, 'message': message}}


def _rate_limit_indicated(error: Any) -> bool:
    """Whether an HTTP error response carries a rate-limit indication.

    GitHub signals primary rate limiting with ``X-RateLimit-Remaining: 0``
    and secondary limiting with a ``Retry-After`` header; either one on a
    403 means "come back later", not "this repository is off limits".
    """
    headers = getattr(error, 'headers', None)
    if headers is None:
        return False
    remaining = headers.get('X-RateLimit-Remaining')
    if isinstance(remaining, str) and remaining.strip() == '0':
        return True
    return headers.get('Retry-After') is not None


def _classify_http_error(error: Any, repository: str) -> Dict[str, Any]:
    """The distinct failure for one HTTP error status (Req 3.3)."""
    status = getattr(error, 'code', None)
    if status == 404:
        return _discovery_failure(
            REPOSITORY_NOT_FOUND,
            f'Repository {repository} was not found on GitHub. Check the '
            'owner and repository name; a private repository is also '
            'reported as not found.',
        )
    if status == 429 or (status == 403 and _rate_limit_indicated(error)):
        return _discovery_failure(
            DISCOVERY_RATE_LIMITED,
            f'GitHub rate-limited branch discovery for {repository}. Wait '
            'a minute and retry, or enter the ref manually.',
        )
    if status == 403:
        return _discovery_failure(
            REPOSITORY_FORBIDDEN,
            f'GitHub refused access to {repository}. Branch discovery '
            'works on public repositories only; enter the ref manually '
            'for a private one.',
        )
    if status == 409:
        # GitHub answers 409 ("Git Repository is empty") on git-data
        # endpoints of a repository with no commits: reachable, no branches.
        return _discovery_failure(
            REPOSITORY_EMPTY,
            f'Repository {repository} is reachable but has no branches yet.',
        )
    return _discovery_failure(
        DISCOVERY_UPSTREAM_ERROR,
        f'GitHub returned an unexpected response (HTTP {status}) during '
        f'branch discovery for {repository}. Retry, or enter the ref '
        'manually.',
    )


def _discovery_fetch(
    fetch: Callable[[str], Any],
    url: str,
    repository: str,
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """One classified fetch: ``(payload, None)`` or ``(None, failure)``.

    Every exception class the fetch can raise maps to exactly one distinct
    code (Req 3.3); nothing propagates, so no failure can fall through to
    a caller as an empty success.
    """
    try:
        return fetch(url), None
    except urllib.error.HTTPError as error:
        return None, _classify_http_error(error, repository)
    except (socket.timeout, TimeoutError):
        return None, _discovery_failure(
            DISCOVERY_TIMEOUT,
            f'Branch discovery for {repository} timed out after '
            f'{DISCOVERY_TIMEOUT_SECONDS} seconds. Retry, or enter the '
            'ref manually.',
        )
    except urllib.error.URLError as error:
        reason = getattr(error, 'reason', None)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return None, _discovery_failure(
                DISCOVERY_TIMEOUT,
                f'Branch discovery for {repository} timed out after '
                f'{DISCOVERY_TIMEOUT_SECONDS} seconds. Retry, or enter '
                'the ref manually.',
            )
        return None, _discovery_failure(
            DISCOVERY_UPSTREAM_ERROR,
            f'Branch discovery for {repository} could not reach GitHub '
            f'({reason}). Retry, or enter the ref manually.',
        )
    except (ValueError, OSError) as error:
        # json.JSONDecodeError is a ValueError: a non-JSON body is an
        # upstream malfunction, not an empty repository.
        return None, _discovery_failure(
            DISCOVERY_UPSTREAM_ERROR,
            f'GitHub returned a malformed response during branch '
            f'discovery for {repository} ({error}). Retry, or enter the '
            'ref manually.',
        )


_MALFORMED_PAYLOAD_MESSAGE = (
    'GitHub returned a malformed payload during branch discovery for '
    '{repository}. Retry, or enter the ref manually.'
)


def discover_branches(
    normalized_url: Any,
    fetch: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """Branches + default branch for a Normalized_Repository (Req 3.1-3.3).

    Returns exactly one of two shapes:

    * success — ``{'branches': [...], 'default_branch': <name>,
      'truncated': <bool>}`` with at least one branch, the default branch
      present in the list, and ``truncated`` flagged when the repository
      has more branches than :data:`MAX_BRANCH_PAGES` pages of
      :data:`BRANCHES_PER_PAGE` (Req 3.1);
    * failure — ``{'error': {'code': <one of the six distinct codes>,
      'message': <actionable text>}}`` (Req 3.3). A failure is NEVER
      shaped as a success with an empty list.

    ``fetch`` is the injected transport (``fetch(url) -> decoded JSON``,
    raising ``urllib.error.HTTPError`` / timeout / ``URLError`` on
    failure), defaulting to :func:`_default_github_fetch` — the
    ``vllm_fit_check._default_hf_fetch`` pattern, so tests drive every
    upstream outcome without a network.

    Containment (Req 3.5): the input must be a Normalized_Repository.
    ``parse_owner_repo`` re-normalizes it and raises ``ValueError`` for
    anything else BEFORE any outbound call, and every URL is composed
    from the parsed ``(owner, repo)`` against :data:`GITHUB_API_HOST`
    only. No credentials are sent (Req 3.2).
    """
    if fetch is None:
        fetch = _default_github_fetch
    # Raises ValueError on anything but a Normalized_Repository: there is
    # no path from unvalidated input to an outbound URL (Req 3.5).
    owner, repo = parse_owner_repo(normalized_url)
    repository = f'{REPOSITORY_SCHEME}://{ALLOWED_REPOSITORY_HOSTS[0]}/{owner}/{repo}'
    base_url = f'{GITHUB_API_HOST}/repos/{owner}/{repo}'
    malformed = _discovery_failure(
        DISCOVERY_UPSTREAM_ERROR,
        _MALFORMED_PAYLOAD_MESSAGE.format(repository=repository))

    # The repository document names the default branch (Req 3.1).
    metadata, failure = _discovery_fetch(fetch, base_url, repository)
    if failure is not None:
        return failure
    if not isinstance(metadata, dict):
        return malformed
    default_branch = metadata.get('default_branch')
    if not isinstance(default_branch, str) or not default_branch.strip():
        return malformed
    default_branch = default_branch.strip()

    # The branches listing, paged up to the cap.
    branches: List[str] = []
    truncated = False
    for page in range(1, MAX_BRANCH_PAGES + 1):
        page_url = (f'{base_url}/branches'
                    f'?per_page={BRANCHES_PER_PAGE}&page={page}')
        payload, failure = _discovery_fetch(fetch, page_url, repository)
        if failure is not None:
            return failure
        if not isinstance(payload, list):
            return malformed
        page_names: List[str] = []
        for entry in payload:
            name = entry.get('name') if isinstance(entry, dict) else None
            if not isinstance(name, str) or not name:
                return malformed
            page_names.append(name)
        branches.extend(page_names)
        if len(page_names) < BRANCHES_PER_PAGE:
            break
    else:
        # Every allowed page came back full: the listing MAY continue.
        truncated = True

    if not branches:
        # Reachable but branch-less: a distinct condition, never an empty
        # success (Req 3.3).
        return _discovery_failure(
            REPOSITORY_EMPTY,
            f'Repository {repository} is reachable but has no branches '
            'yet.',
        )
    if default_branch not in branches:
        # The default branch is identified even when pagination truncated
        # it out of the listing: exactly one default, always present.
        branches.insert(0, default_branch)
    return {
        'branches': branches,
        'default_branch': default_branch,
        'truncated': truncated,
    }
