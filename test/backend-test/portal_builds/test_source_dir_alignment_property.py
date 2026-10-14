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
Repository-directory alignment properties
(build-source-selection, task 3.4).

**Property 1** — Agent path equals the bootstrap clone directory
(Req 5.1, 5.2, 5.4). For any Build_Job in either execution mode and any
configured repository directory, the path prefix of
`build_dispatcher.agent_command(job, repo_dir)` equals the directory that
job's machine bootstrapped its clone into, and both come from the one
shared resolver rather than from independent literals.

**Structural (Req 5.2)** — no independent `DefectDetectionApplication`
*directory* literal remains in `build_dispatcher.py` or `build_fleet.py`,
so the dispatcher's agent path and the bootstrap's clone path cannot drift
apart again. A *repository URL* containing that word is legitimate (it is
the repository's name); a filesystem path literal is not. The one
path-shaped definition lives in `build_source.DEFAULT_REPO_DIR`.

**Property 2** — Legacy dedicated servers need no re-bootstrap (Req 5.3).
For any Build_Server record carrying no recorded repository directory —
absent, `None`, or empty, i.e. every server bootstrapped before this
change — resolution yields `build_source.DEFAULT_REPO_DIR` and the
dedicated dispatch path is otherwise identical across those variants.

Both bootstrap directories are PARSED out of the generated bootstrap text
(`build_fleet.render_user_data` / `build_dispatcher.runner_bootstrap_user_data`)
rather than re-hard-coded here, so this file tracks the templates instead
of pinning a second copy of the value — which is the whole point of
Req 5.2.

`agent_command` now emits a Source_Sync preamble before the agent
invocation whenever a ref is selected (task 5.2), so the agent invocation
is EXTRACTED from the command text (`_agent_invocation`, the same approach
as `test_source_selection_preservation.py::_agent_invocation`) instead of
comparing whole command strings.

NOT DUPLICATED HERE: unit coverage of `build_source.resolve_repo_dir`
precedence (recorded -> env override -> default) and of
`build_source.agent_script_path` already exists in
`test/backend-test/portal_builds/test_build_source_unit.py` (36 tests).
This file covers the dispatcher/fleet ALIGNMENT the resolver exists for.

SAFETY (absolute): nothing here launches EC2 compute, sends a real SSM
command, calls real AWS, or starts a build.
* Every AWS interaction runs against moto (`mock_aws` started at module
  scope before any handler import).
* `BUILD_REPO_URL` points at a non-resolvable `example.invalid` URL and is
  only ever interpolated into generated *text* — no clone is performed.
* The one dispatch pass driven end to end
  (`verify_and_start_dedicated`) runs with `build_dispatcher.ssm` replaced
  by a recording stub and `run_shell_sync` canned, so no SendCommand of
  any kind leaves the process; `provision_ephemeral` is deliberately NOT
  driven — the ephemeral half of Property 1 is asserted against the pure
  bootstrap-text and command-text generators, so no RunInstances call
  exists even under moto.
"""
import ast
import contextlib
import inspect
import json
import os
import re
import shlex
import sys
import types
import uuid
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Environment BEFORE any import: the handlers bind their boto3
# resources/clients, table names and repository configuration at import
# time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "source-align"
_JOBS_TABLE = f"dda-portal-build-jobs-{_SUFFIX}"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
_SETTINGS_TABLE = f"dda-portal-settings-{_SUFFIX}"
_USER_ROLES_TABLE = f"dda-portal-user-roles-{_SUFFIX}"
_AUDIT_TABLE = f"dda-portal-audit-log-{_SUFFIX}"

os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
os.environ["SETTINGS_TABLE"] = _SETTINGS_TABLE
os.environ["USER_ROLES_TABLE"] = _USER_ROLES_TABLE
os.environ["AUDIT_LOG_TABLE"] = _AUDIT_TABLE
# Nothing may be dispatched from a submit path.
os.environ.pop("BUILD_DISPATCHER_FUNCTION_NAME", None)
# A deliberately non-resolvable repository: the ephemeral bootstrap text is
# only GENERATED here, never executed, and `runner_bootstrap_user_data`
# returns '' when no repository is configured.
_REPO_URL = "https://example.invalid/dda.git"
os.environ["BUILD_REPO_URL"] = _REPO_URL
# Deliberately NOT set, so BUILD_REPO_DIR keeps the resolver's default —
# the deployed Lambda sets no override either (design A1). The configured
# override is exercised by PATCHING the module constants below.
os.environ.pop("BUILD_REPO_DIR", None)

import boto3  # noqa: E402

# Some verification containers ship a python build without the _bz2 C
# extension while moto's request path imports moto.s3 -> bz2 (sibling
# shim in test_source_selection_preservation.py).
try:
    import bz2  # noqa: F401
except ImportError:  # pragma: no cover - depends on the runner's build
    _bz2_stub = types.ModuleType("_bz2")

    class _Bz2Unavailable:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("bz2 is unavailable in this environment")

    _bz2_stub.BZ2Compressor = _Bz2Unavailable
    _bz2_stub.BZ2Decompressor = _Bz2Unavailable
    sys.modules["_bz2"] = _bz2_stub

from moto import mock_aws  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_BACKEND = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend")

FUNCTIONS_DIR = os.environ.get(
    "DDA_BUILD_FN_DIR", os.path.join(_BACKEND, "functions"))
LAYER_DIR = os.environ.get(
    "DDA_BUILD_LAYER_DIR",
    os.path.join(_BACKEND, "layers", "shared", "python"))

for _p in (LAYER_DIR, FUNCTIONS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

for _module in ("build_jobs", "build_dispatcher", "build_fleet",
                "build_domain", "build_planner", "build_source",
                "rbac_middleware", "shared_utils"):
    sys.modules.pop(_module, None)

_MOCK = mock_aws()
_MOCK.start()

_DDB = boto3.resource("dynamodb", region_name="us-east-1")

# BuildJobs with the DEPLOYED schema, GSIs included, so a stored item with
# a wrong-typed indexed key is a real DynamoDB ValidationException.
_DDB.create_table(
    TableName=_JOBS_TABLE,
    KeySchema=[{"AttributeName": "build_job_id", "KeyType": "HASH"}],
    AttributeDefinitions=[
        {"AttributeName": "build_job_id", "AttributeType": "S"},
        {"AttributeName": "status", "AttributeType": "S"},
        {"AttributeName": "created_at", "AttributeType": "N"},
        {"AttributeName": "server_id", "AttributeType": "S"},
        {"AttributeName": "request_id", "AttributeType": "S"},
        {"AttributeName": "request_order", "AttributeType": "N"},
    ],
    GlobalSecondaryIndexes=[
        {
            "IndexName": "status-index",
            "KeySchema": [
                {"AttributeName": "status", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
        {
            "IndexName": "server-index",
            "KeySchema": [
                {"AttributeName": "server_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
        {
            "IndexName": "request-index",
            "KeySchema": [
                {"AttributeName": "request_id", "KeyType": "HASH"},
                {"AttributeName": "request_order", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
    ],
    BillingMode="PAY_PER_REQUEST",
)
for _name, _key in ((_SERVERS_TABLE, "server_id"),
                    (_SETTINGS_TABLE, "setting_key")):
    _DDB.create_table(
        TableName=_name,
        KeySchema=[{"AttributeName": _key, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": _key, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
_DDB.create_table(
    TableName=_USER_ROLES_TABLE,
    KeySchema=[
        {"AttributeName": "user_id", "KeyType": "HASH"},
        {"AttributeName": "usecase_id", "KeyType": "RANGE"},
    ],
    AttributeDefinitions=[
        {"AttributeName": "user_id", "AttributeType": "S"},
        {"AttributeName": "usecase_id", "AttributeType": "S"},
    ],
    BillingMode="PAY_PER_REQUEST",
)
_DDB.create_table(
    TableName=_AUDIT_TABLE,
    KeySchema=[
        {"AttributeName": "event_id", "KeyType": "HASH"},
        {"AttributeName": "timestamp", "KeyType": "RANGE"},
    ],
    AttributeDefinitions=[
        {"AttributeName": "event_id", "AttributeType": "S"},
        {"AttributeName": "timestamp", "AttributeType": "N"},
    ],
    BillingMode="PAY_PER_REQUEST",
)

_JOBS = _DDB.Table(_JOBS_TABLE)
_SERVERS = _DDB.Table(_SERVERS_TABLE)
_AUDIT = _DDB.Table(_AUDIT_TABLE)

import build_dispatcher  # noqa: E402
import build_domain  # noqa: E402
import build_fleet  # noqa: E402
import build_jobs  # noqa: E402
import build_planner  # noqa: E402
import build_source  # noqa: E402

TARGETS = (build_domain.TARGET_JP5, build_domain.TARGET_JP6,
           build_domain.TARGET_AMD64, build_domain.TARGET_AMD64_NVIDIA)
MODE_EPHEMERAL = build_domain.EXECUTION_MODE_EPHEMERAL
MODE_DEDICATED = build_domain.EXECUTION_MODE_DEDICATED

AGENT_SCRIPT_RELPATH = build_source.AGENT_SCRIPT_RELPATH

#: The module whose ONE path-shaped `DefectDetectionApplication` literal is
#: the authoritative definition (Req 5.2).
SOURCE_OF_TRUTH_MODULE = os.path.join(FUNCTIONS_DIR, "build_source.py")
ALIGNED_MODULES = (os.path.join(FUNCTIONS_DIR, "build_dispatcher.py"),
                   os.path.join(FUNCTIONS_DIR, "build_fleet.py"))

#: The three "nothing recorded" variants Property 2 ranges over: a record
#: written before the bootstrap started recording its directory carries no
#: field at all; a record written with None or '' carries no more
#: information than that (Req 5.3).
LEGACY_FIELD_VARIANTS = ("absent", None, "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _observation(**fields):
    fields["artifact"] = {
        "build_dispatcher": getattr(build_dispatcher, "__file__", "?"),
        "build_fleet": getattr(build_fleet, "__file__", "?"),
        "build_source": getattr(build_source, "__file__", "?"),
        "dispatcher_repo_dir": build_dispatcher.BUILD_REPO_DIR,
        "fleet_repo_dir": build_fleet.BUILD_REPO_DIR,
        "default_repo_dir": build_source.DEFAULT_REPO_DIR,
    }
    return json.dumps(fields, indent=2, default=str)


_ARG_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]*=")


def _agent_invocation(command_text):
    """The agent invocation inside a dispatcher command: the interpreter,
    the script path and the KEY=VALUE arguments that follow it, plus the
    line it sits on.

    Extracted rather than compared as a whole string because
    `agent_command` prepends a Source_Sync preamble whenever a ref is
    selected (task 5.2) — same approach as
    `test_source_selection_preservation.py::_agent_invocation`."""
    lines = command_text.splitlines()
    for line_index, line in enumerate(lines):
        tokens = shlex.split(line)
        for index, token in enumerate(tokens):
            if token.endswith(AGENT_SCRIPT_RELPATH):
                args = []
                for candidate in tokens[index + 1:]:
                    if not _ARG_TOKEN.match(candidate):
                        break
                    args.append(candidate)
                return {
                    "script": token,
                    "args": args,
                    "arg_keys": [a.split("=", 1)[0] for a in args],
                    "arg_map": dict(a.split("=", 1) for a in args),
                    "interpreter": tokens[index - 1] if index else None,
                    "line_index": line_index,
                }
    return None


def _agent_path_prefix(command_text):
    """`agentPathPrefix(command)` from the design: the repository directory
    the agent is invoked out of."""
    invocation = _agent_invocation(command_text)
    assert invocation is not None, _observation(
        command=command_text, note="no agent invocation in the command text")
    suffix = "/" + AGENT_SCRIPT_RELPATH
    script = invocation["script"]
    assert script.endswith(suffix), _observation(script=script)
    return script[: -len(suffix)]


def _assigned_value(script_text, variable):
    """The value a generated script assigns to `variable` on its own line
    (the Sync_Generator emits `REPO_DIR=<shlex-quoted dir>`)."""
    prefix = f"{variable}="
    for line in script_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            tokens = shlex.split(stripped)
            if tokens and tokens[0].startswith(prefix):
                return tokens[0].split("=", 1)[1]
    return None


def _clone_target(script_text):
    """The directory the first `git clone` line of a bootstrap clones into,
    dereferenced when it is written as `"$REPO_DIR"`."""
    for line in script_text.splitlines():
        stripped = line.strip()
        if "git clone" in stripped and not stripped.startswith("#"):
            # Drop any `|| <failure guard>` so the clone target is the last
            # token of the clone statement itself.
            head = stripped.split("||", 1)[0].strip()
            target = shlex.split(head)[-1]
            if target in ("$REPO_DIR", "${REPO_DIR}"):
                return _assigned_value(script_text,
                                       build_source.VAR_REPO_DIR)
            return target
    return None


def _bootstrap_clone_dir(script_text):
    """`bootstrapRepoDir(...)` from the design: the directory a generated
    bootstrap puts the working tree in.

    Both statements that name it — the `git clone` target and the
    Sync_Generator's `REPO_DIR` assignment — must agree, otherwise the
    bootstrap itself is internally inconsistent and the alignment question
    is meaningless."""
    assigned = _assigned_value(script_text, build_source.VAR_REPO_DIR)
    cloned = _clone_target(script_text)
    observed = _observation(script=script_text, assigned=assigned,
                            cloned=cloned)
    assert assigned, observed
    assert cloned, observed
    assert assigned == cloned, observed
    return assigned


def _fleet_bootstrap(repo_dir=None, source_ref=None):
    """The dedicated bootstrap text a fleet launch generates for a server
    whose bootstrap directory is `repo_dir` (None = the module's resolved
    directory, which is what a legacy launch used)."""
    return build_fleet.render_user_data(_REPO_URL, repo_dir, source_ref)


def _runner_bootstrap(job, repo_dir=None):
    """The ephemeral bootstrap text `provision_ephemeral` would generate
    for `job` (it passes the directory it then records on the runner)."""
    text = build_dispatcher.runner_bootstrap_user_data(job, repo_dir)
    assert text, _observation(
        job=job, note="the ephemeral bootstrap generated no text; "
                      "BUILD_REPO_URL must be configured")
    return text


@contextlib.contextmanager
def _configured_repo_dir(env_default):
    """The operator-configured directory override, applied to BOTH modules
    exactly as the environment would at import time. `None` is the deployed
    reality: no override, so resolution lands on DEFAULT_REPO_DIR."""
    resolved = build_source.resolve_repo_dir(None, env_default=env_default)
    with mock.patch.object(build_dispatcher, "BUILD_REPO_DIR", resolved), \
            mock.patch.object(build_fleet, "BUILD_REPO_DIR", resolved):
        yield resolved


def _server_record(target, repo_dir_field="absent", server_id=None,
                   extra=None):
    """A running Build_Server. `repo_dir_field` "absent" means the field is
    not present at all — every server bootstrapped before this change."""
    record = {
        "server_id": server_id or f"srv-{uuid.uuid4()}",
        "name": "dedicated-1",
        "instance_id": "i-0b8221f5ed2ebc2a9",
        "lifecycle_state": build_domain.SERVER_STATE_RUNNING,
        "cpu_architecture": build_domain.required_arch_for_target(target),
    }
    if repo_dir_field != "absent":
        record["repo_dir"] = repo_dir_field
    record.update(extra or {})
    return record


def _job_record(target, execution_mode, source_ref=None, server_id=None,
                runner_repo_dir="absent", job_id=None, now=None):
    now = now if now is not None else 1_754_500_000_000
    job = {
        "build_job_id": job_id or str(uuid.uuid4()),
        "request_id": "req-alignment",
        "request_order": 0,
        "predecessor_job_id": None,
        "build_target": target,
        "component_name": build_domain.target_definition(
            target)["component_name"],
        "required_arch": build_domain.required_arch_for_target(target),
        "execution_mode": execution_mode,
        "server_id": server_id,
        "status": build_domain.STATUS_QUEUED,
        "requested_by": "alignment-user",
        "created_at": now,
        "config_snapshot": dict(build_domain.DEFAULT_BUILD_CONFIG,
                                source_ref=source_ref),
    }
    if execution_mode == MODE_EPHEMERAL and runner_repo_dir != "absent":
        job["runner"] = {"instance_id": "i-0aaa11bb22cc33dd4",
                         "repo_dir": runner_repo_dir}
    return job


def _clear_tables():
    for item in _JOBS.scan().get("Items", []):
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})
    for item in _AUDIT.scan().get("Items", []):
        _AUDIT.delete_item(Key={"event_id": item["event_id"],
                                "timestamp": item["timestamp"]})


def _raw_job(job_id):
    return _JOBS.get_item(Key={"build_job_id": job_id}).get("Item")


# ---------------------------------------------------------------------------
# Structural scan helpers (Req 5.2)
# ---------------------------------------------------------------------------

_REPO_NAME = "DefectDetectionApplication"
_URL_PREFIX = re.compile(r"^(https?|ssh|git)://", re.IGNORECASE)


def _code_string_literals(path):
    """(lineno, value) for every string literal in a module's CODE.

    Module/class/function docstrings are excluded: a docstring naming a
    path documents behavior, it cannot make two modules drift. Comments are
    not literals at all and never reach the AST."""
    with open(path) as handle:
        tree = ast.parse(handle.read(), filename=path)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings]


def _is_repository_url(value):
    """A repository URL naming the repository is legitimate: the word is
    the repository's NAME there, not a directory."""
    return bool(_URL_PREFIX.match(value)) or value.startswith("git@")


def _is_filesystem_path(value):
    """A filesystem path literal is what must not exist independently: it
    is a second, drift-capable statement of where the clone lives."""
    if _is_repository_url(value):
        return False
    return (value.startswith("/") or value.startswith("~")
            or value.startswith("./") or "/home/" in value
            or "/opt/" in value)


def _directory_literals(path):
    """Path-shaped literals naming the repository directory in a module."""
    return [(lineno, value) for lineno, value in _code_string_literals(path)
            if _REPO_NAME in value and _is_filesystem_path(value)]


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

#: Shell-safe absolute directories an operator could configure or a
#: bootstrap could have recorded. Deliberately space-free: the fleet
#: template interpolates the directory into `git clone <url> <dir>`
#: unquoted, so a directory with shell metacharacters is a separate
#: (unrelated) concern and not the alignment question under test.
_directories = st.one_of(
    st.sampled_from([
        "/home/ubuntu/DefectDetectionApplication",
        "/opt/dda/DefectDetectionApplication",
        "/srv/dda-build/tree",
        "/mnt/build/dda",
    ]),
    st.builds("/mnt/{}/dda".format,
              st.from_regex(r"[a-z][a-z0-9_-]{0,8}", fullmatch=True)),
)

#: The configured environment override: None is the deployed reality.
configured_dirs = st.one_of(st.none(), _directories)

#: What the machine's own record carries: nothing recorded (the three
#: legacy variants) or a recorded directory.
recorded_dirs = st.one_of(st.sampled_from(LEGACY_FIELD_VARIANTS),
                          _directories)

#: Refs a submission can select. A selected ref is what makes
#: `agent_command` emit its Source_Sync preamble, so both the no-preamble
#: and the preamble shape are covered.
source_refs = st.one_of(
    st.none(),
    st.sampled_from(["main", "feature/portal-build-fleet-and-workflow-gates",
                     "v1.2.3", "a" * 40]),
)

execution_modes = st.sampled_from([MODE_DEDICATED, MODE_EPHEMERAL])
targets = st.sampled_from(TARGETS)


# ===========================================================================
# Property 1 — the agent path equals the bootstrap clone directory
# (Req 5.1, 5.2, 5.4), over jobs x server/runner records x configured
# directories, in BOTH execution modes.
# ===========================================================================

class TestAgentPathEqualsBootstrapDirectory:
    """`agentPathPrefix(dispatch) = bootstrapRepoDir(dispatch)` — the
    design's expectedBehavior conjunct for Req 5.1/5.4.

    The bootstrap directory is not asserted against a literal: it is parsed
    out of the very bootstrap text the fleet / dispatcher would put in
    user-data for that machine, so the two sides of the equality are the
    two real artifacts that used to disagree
    (`/opt/dda/...` vs `/home/ubuntu/...`, SSM d75f1ea2 exit 127)."""

    @given(execution_mode=execution_modes, target=targets,
           source_ref=source_refs, env_default=configured_dirs,
           recorded=recorded_dirs)
    @settings(max_examples=200, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_agent_path_prefix_equals_the_bootstrap_clone_dir(
            self, execution_mode, target, source_ref, env_default, recorded):
        """For any Build_Job, any machine record and any configured
        directory: the agent is invoked out of exactly the directory that
        machine's bootstrap cloned into.

        `recorded` is the directory the machine's own record carries — a
        real directory for a machine bootstrapped after this change, and
        one of the three "nothing recorded" variants for a machine
        bootstrapped before it. The bootstrap text is generated with the
        SAME input, which is what makes this an alignment assertion rather
        than a restatement of the resolver: an unrecorded machine's
        bootstrap resolves through the configured override / default, and
        so must the dispatch."""
        with _configured_repo_dir(env_default) as resolved_default:
            recorded_dir = None if recorded in LEGACY_FIELD_VARIANTS \
                else recorded
            server = None
            if execution_mode == MODE_DEDICATED:
                server = _server_record(target, repo_dir_field=recorded)
                job = _job_record(target, execution_mode,
                                  source_ref=source_ref,
                                  server_id=server["server_id"])
                bootstrap = _fleet_bootstrap(recorded_dir, source_ref)
            else:
                job = _job_record(target, execution_mode,
                                  source_ref=source_ref,
                                  runner_repo_dir=recorded)
                # provision_ephemeral resolves the directory from the job
                # (no runner record yet), generates the bootstrap with it
                # and records it on the runner; a runner provisioned before
                # that recording existed carries nothing.
                bootstrap = _runner_bootstrap(
                    _job_record(target, execution_mode,
                                source_ref=source_ref),
                    recorded_dir)

            bootstrap_dir = _bootstrap_clone_dir(bootstrap)
            # What the dispatch paths do: resolve from the machine record,
            # then hand the directory to agent_command.
            dispatch_dir = build_source.resolve_repo_dir(
                job, server, env_default=build_dispatcher.BUILD_REPO_DIR)
            command = build_dispatcher.agent_command(job, dispatch_dir)
            prefix = _agent_path_prefix(command)
            observed = _observation(
                execution_mode=execution_mode, target=target,
                source_ref=source_ref, env_default=env_default,
                recorded=recorded, resolved_default=resolved_default,
                bootstrap_dir=bootstrap_dir, dispatch_dir=dispatch_dir,
                prefix=prefix, command=command)

            assert prefix == bootstrap_dir, observed
            assert dispatch_dir == bootstrap_dir, observed
            # The path is composed by the shared resolver, not by the
            # dispatcher (Req 5.2).
            assert _agent_invocation(command)["script"] == \
                build_source.agent_script_path(bootstrap_dir), observed
            # An unrecorded machine lands on the configured override, else
            # the authoritative default (Req 5.3, 5.4).
            if recorded in LEGACY_FIELD_VARIANTS:
                assert bootstrap_dir == resolved_default, observed
                if env_default is None:
                    assert bootstrap_dir == \
                        build_source.DEFAULT_REPO_DIR, observed
            else:
                assert bootstrap_dir == recorded, observed

    @given(execution_mode=execution_modes, target=targets,
           source_ref=source_refs, env_default=configured_dirs,
           recorded=recorded_dirs)
    @settings(max_examples=150, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_source_sync_preamble_targets_the_same_directory(
            self, execution_mode, target, source_ref, env_default, recorded):
        """The pre-agent Source_Sync preamble syncs the SAME directory the
        agent is then invoked from, and strictly before it.

        Without this the alignment would hold for the invocation while the
        preamble prepared a different tree — the drift the preamble exists
        to close (Req 4.1, 5.1)."""
        with _configured_repo_dir(env_default):
            server = None
            if execution_mode == MODE_DEDICATED:
                server = _server_record(target, repo_dir_field=recorded)
                job = _job_record(target, execution_mode,
                                  source_ref=source_ref,
                                  server_id=server["server_id"])
            else:
                job = _job_record(target, execution_mode,
                                  source_ref=source_ref,
                                  runner_repo_dir=recorded)
            dispatch_dir = build_source.resolve_repo_dir(
                job, server, env_default=build_dispatcher.BUILD_REPO_DIR)
            command = build_dispatcher.agent_command(job, dispatch_dir)
            # The mode-independent text the sync-then-agent sequence lives
            # in: the body IS the whole command for ephemeral jobs and is
            # carried verbatim (executed as ubuntu) inside the dedicated
            # command's build-user wrapper.
            run_body = build_dispatcher.agent_run_body(job, dispatch_dir)
            assert run_body in command
            invocation = _agent_invocation(run_body)
            preamble_dir = _assigned_value(run_body,
                                           build_source.VAR_REPO_DIR)
            observed = _observation(
                execution_mode=execution_mode, source_ref=source_ref,
                recorded=recorded, dispatch_dir=dispatch_dir,
                preamble_dir=preamble_dir, command=command,
                invocation=invocation)

            # The additive environment exports (HOME + region + PATH)
            # precede everything (the SSM shell runs as root with HOME
            # unset, a fresh runner has no region of its own, and
            # $HOME/.local/bin is off the SSM shell's PATH); they carry
            # no directory.
            exports = build_dispatcher.runner_env_export_commands(job)
            if not source_ref:
                # No selection: no preamble — the environment exports and
                # the additive dispatch preflight guard (which must target
                # the SAME dispatch directory: no drift) precede today's
                # invocation, which stays the last line of the body
                # (Req 7.1).
                assert preamble_dir is None, observed
                guard = list(build_dispatcher.preflight_guard_commands(
                    dispatch_dir))
                body_lines = run_body.splitlines()
                assert body_lines[len(exports):len(exports) + len(guard)] \
                    == guard, observed
                assert invocation["line_index"] == \
                    len(exports) + len(guard), observed
                assert len(body_lines) == \
                    len(exports) + len(guard) + 1, observed
                return
            assert preamble_dir == dispatch_dir, observed
            assert invocation["line_index"] > 0, observed
            # The sync statements all precede the invocation line, right
            # after the region exports.
            sync_lines = build_source.source_sync_commands(
                build_dispatcher.BUILD_REPO_URL, dispatch_dir, source_ref,
                event_bus=build_dispatcher.BUILD_EVENT_BUS,
                build_job_id=job["build_job_id"],
                build_target=job["build_target"])
            after_exports = run_body.splitlines()[len(exports):]
            assert after_exports[:len(sync_lines)] == list(sync_lines), \
                observed

    @given(target=targets, source_ref=source_refs,
           env_default=configured_dirs, recorded=recorded_dirs)
    @settings(max_examples=100, deadline=None)
    def test_agent_command_self_resolution_agrees_with_the_callers(
            self, target, source_ref, env_default, recorded):
        """`agent_command(job)` with no directory handed in resolves the
        same directory the ephemeral dispatch path hands it, so the two
        entry points cannot disagree (Req 5.1, 5.4)."""
        with _configured_repo_dir(env_default):
            job = _job_record(target, MODE_EPHEMERAL, source_ref=source_ref,
                              runner_repo_dir=recorded)
            handed = build_dispatcher.agent_command(
                job, build_source.resolve_repo_dir(
                    job, env_default=build_dispatcher.BUILD_REPO_DIR))
            self_resolved = build_dispatcher.agent_command(job)
            assert _agent_path_prefix(self_resolved) == \
                _agent_path_prefix(handed), _observation(
                    handed=handed, self_resolved=self_resolved,
                    recorded=recorded, env_default=env_default)


# ===========================================================================
# Structural: no independent DefectDetectionApplication DIRECTORY literal
# remains in the two modules (Req 5.2) — the two cannot drift again.
# ===========================================================================

class TestNoIndependentDirectoryLiteral:

    @pytest.mark.parametrize("module_path", ALIGNED_MODULES,
                             ids=lambda p: os.path.basename(p))
    def test_module_has_no_repository_directory_literal(self, module_path):
        """A repository URL naming the repository is legitimate (the word is
        the repository's name); a filesystem path literal is not — it is a
        second, drift-capable statement of where the clone lives, which is
        exactly what produced `/opt/dda/...` vs `/home/ubuntu/...`."""
        literals = _directory_literals(module_path)
        assert literals == [], _observation(
            module=module_path, directory_literals=literals,
            note="the repository directory must come from "
                 "build_source.resolve_repo_dir / DEFAULT_REPO_DIR, "
                 "never from a literal in this module")

    @pytest.mark.parametrize("module_path", ALIGNED_MODULES,
                             ids=lambda p: os.path.basename(p))
    def test_repository_name_literals_are_only_repository_urls(
            self, module_path):
        """Precision check on the assertion above: every remaining literal
        mentioning the repository name in these modules is a repository
        URL, so the structural test is passing for the right reason rather
        than because the name disappeared entirely."""
        mentions = [(lineno, value) for lineno, value
                    in _code_string_literals(module_path)
                    if _REPO_NAME in value]
        for lineno, value in mentions:
            assert _is_repository_url(value), _observation(
                module=module_path, lineno=lineno, value=value)

    def test_the_directory_has_exactly_one_definition(self):
        """The one path-shaped definition lives in `build_source`, and it is
        `DEFAULT_REPO_DIR` — the location every server bootstrapped before
        this change already uses (Req 5.2, 5.3)."""
        literals = _directory_literals(SOURCE_OF_TRUTH_MODULE)
        observed = _observation(literals=literals,
                               default=build_source.DEFAULT_REPO_DIR)
        assert len(literals) == 1, observed
        assert literals[0][1] == build_source.DEFAULT_REPO_DIR, observed
        assert build_source.DEFAULT_REPO_DIR == \
            "/home/ubuntu/DefectDetectionApplication", observed
        # Both modules read that one value rather than re-spelling it.
        assert build_dispatcher.BUILD_REPO_DIR == \
            build_source.DEFAULT_REPO_DIR, observed
        assert build_fleet.BUILD_REPO_DIR == \
            build_source.DEFAULT_REPO_DIR, observed


# ===========================================================================
# Property 2 — Preservation: legacy dedicated servers need no re-bootstrap
# (Req 5.3). Input domain: Build_Server records with the repository
# directory absent, None, or empty.
# ===========================================================================

class TestLegacyServerResolvesToTheDefault:
    """Every server bootstrapped before this change carries no recorded
    directory and has its tree at `/home/ubuntu/DefectDetectionApplication`
    — so resolution for such a record must land exactly there, with no
    re-bootstrap, no re-clone and no operator action."""

    @given(repo_dir_field=st.sampled_from(LEGACY_FIELD_VARIANTS),
           target=targets, source_ref=source_refs,
           noise=st.dictionaries(
               st.sampled_from(["name", "pending_action", "ami_id",
                                "instance_type", "running_build_job_id"]),
               st.text(min_size=0, max_size=12), max_size=5))
    @settings(max_examples=200, deadline=None)
    def test_resolution_is_the_default_and_matches_the_legacy_bootstrap(
            self, repo_dir_field, target, source_ref, noise):
        """Resolution yields `DEFAULT_REPO_DIR`, which is the directory the
        legacy fleet bootstrap actually cloned into, and the agent is
        invoked out of it. Unrelated fields on the record cannot influence
        it (only `repo_dir` is consulted)."""
        server = _server_record(target, repo_dir_field=repo_dir_field,
                                server_id="srv-legacy", extra=noise)
        job = _job_record(target, MODE_DEDICATED, source_ref=source_ref,
                          server_id="srv-legacy")

        resolved = build_source.resolve_repo_dir(
            job, server, env_default=build_dispatcher.BUILD_REPO_DIR)
        legacy_bootstrap_dir = _bootstrap_clone_dir(_fleet_bootstrap())
        command = build_dispatcher.agent_command(job, resolved)
        observed = _observation(repo_dir_field=repo_dir_field, server=server,
                                resolved=resolved,
                                legacy=legacy_bootstrap_dir, command=command)

        assert resolved == build_source.DEFAULT_REPO_DIR, observed
        assert resolved == "/home/ubuntu/DefectDetectionApplication", observed
        assert legacy_bootstrap_dir == resolved, observed
        assert _agent_path_prefix(command) == resolved, observed

    @given(target=targets, source_ref=source_refs)
    @settings(max_examples=60, deadline=None)
    def test_agent_invocation_is_byte_equal_across_the_three_variants(
            self, target, source_ref):
        """Absent / None / empty are the same input as far as dispatch is
        concerned: the whole agent command text is byte-equal for all
        three."""
        job = _job_record(target, MODE_DEDICATED, source_ref=source_ref,
                          server_id="srv-legacy", job_id="job-legacy")
        commands = []
        for field in LEGACY_FIELD_VARIANTS:
            server = _server_record(target, repo_dir_field=field,
                                    server_id="srv-legacy")
            commands.append(build_dispatcher.agent_command(
                job, build_source.resolve_repo_dir(
                    job, server,
                    env_default=build_dispatcher.BUILD_REPO_DIR)))
        assert commands[0] == commands[1] == commands[2], _observation(
            commands=commands)

    @given(target=targets)
    @settings(max_examples=20, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_dedicated_dispatch_effects_are_identical_across_the_variants(
            self, target):
        """The whole dedicated dispatch pass — clean pgrep verification ->
        queued->building transition -> exactly one agent SendCommand ->
        command/log bookkeeping — is identical for a record with the
        directory absent, None, or empty (Req 5.3).

        SAFETY: `build_dispatcher.ssm` is a recording stub and
        `run_shell_sync` is canned, so no SSM call and no build occur.
        The same job id and the same `now` are reused per variant on a
        cleared table, so "identical" is byte equality rather than a
        normalized comparison."""
        now = 1_754_500_000_000
        job_id = "job-legacy-dispatch"
        captures = []
        for field in LEGACY_FIELD_VARIANTS:
            _clear_tables()
            server = _server_record(target, repo_dir_field=field,
                                    server_id="srv-legacy")
            _SERVERS.put_item(Item=server)
            job = _job_record(target, MODE_DEDICATED, server_id="srv-legacy",
                              job_id=job_id, now=now)
            build_jobs.put_new_job(job)

            ssm_stub = mock.MagicMock()
            ssm_stub.send_command.return_value = {
                "Command": {"CommandId": "cmd-legacy-alignment"}}
            with mock.patch.object(build_dispatcher, "ssm", ssm_stub), \
                    mock.patch.object(build_dispatcher, "run_shell_sync",
                                      return_value=""):
                build_dispatcher.verify_and_start_dedicated(job, server, now)

            assert ssm_stub.send_command.call_count == 1, _observation(
                field=field, calls=ssm_stub.send_command.call_args_list)
            capture = {
                "send_command": dict(ssm_stub.send_command.call_args.kwargs),
                "stored": build_jobs.to_native(_raw_job(job_id)),
            }
            # The execution-attempt identity is INTENTIONALLY unique per
            # dispatch attempt (build-fleet-execution-failures task 5.2:
            # the deterministic `dda-build:<job>:<attempt>` SendCommand
            # comment supports ambiguous-send recovery), and `sent_at`
            # is a wall-clock stamp. Assert their FORM per variant, then
            # normalize them so the equality below still proves the
            # repo-dir variants change nothing else.
            comment = capture["send_command"].get("Comment")
            assert isinstance(comment, str) and comment.startswith(
                f"dda-build:{job_id}:"), _observation(
                field=field, comment=comment)
            attempt = capture["stored"].get("execution_attempt") or {}
            assert attempt.get("command_comment") == comment, _observation(
                field=field, comment=comment, attempt=attempt)
            assert attempt.get("command_id") == "cmd-legacy-alignment", \
                _observation(field=field, attempt=attempt)
            capture["send_command"]["Comment"] = f"dda-build:{job_id}:<a>"
            # The command text carries the SAME per-attempt identity as
            # an additive `ATTEMPT_ID=<uuid>` agent argument
            # (build-fleet-execution-failures attempt correlation).
            # Assert the binding to THIS attempt, then normalize the
            # unique value exactly like the comment so the equality
            # below still proves the repo-dir variants change nothing
            # else.
            attempt_id = attempt.get("attempt_id")
            commands = capture["send_command"]["Parameters"]["commands"]
            assert attempt_id and \
                f"ATTEMPT_ID={attempt_id}" in commands[0], _observation(
                    field=field, attempt=attempt)
            commands[0] = commands[0].replace(
                f"ATTEMPT_ID={attempt_id}", "ATTEMPT_ID=<a>")
            for volatile in ("attempt_id", "command_comment", "sent_at"):
                attempt.pop(volatile, None)
            captures.append(capture)

        first = captures[0]
        for index, capture in enumerate(captures[1:], start=1):
            assert capture == first, _observation(
                variant=LEGACY_FIELD_VARIANTS[index], capture=capture,
                first=first)

        commands = first["send_command"]["Parameters"]["commands"]
        invocation = _agent_invocation(commands[0])
        observed = _observation(capture=first, invocation=invocation)
        assert len(commands) == 1, observed
        assert first["send_command"]["DocumentName"] == \
            "AWS-RunShellScript", observed
        assert invocation["interpreter"] == "bash", observed
        assert invocation["script"] == build_source.agent_script_path(
            build_source.DEFAULT_REPO_DIR), observed
        assert first["stored"]["status"] == build_domain.STATUS_BUILDING, \
            observed
        assert first["stored"]["ssm"]["command_id"] == \
            "cmd-legacy-alignment", observed
        _clear_tables()
