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
BUG CONDITION EXPLORATION for the three blocking portal-build defects
(build-source-selection, task 1 — Properties 1, 3 and 6).

**Validates: Requirements 4.1, 4.2, 5.1, 5.4, 6.1, 6.2**

**CRITICAL**: every test in this file MUST FAIL on the current (unfixed)
code. The failures ARE the result: they reproduce
`isBugCondition(input)`'s three facets from the design — `pathMismatch`,
`agentUnobtainable` and `bootstrapRace`. Do NOT "fix" these tests, and do
NOT touch any source file to make them pass. Task 6 re-runs this exact
file after the Increment A fixes land, where the same assertions must
then PASS.

Encoded expected behavior (design "Correct Result Predicate"):

    agentPathPrefix(dispatch) = bootstrapRepoDir(dispatch)   -- Property 1
    sourceSyncedBeforeAgent(dispatch)                        -- Property 3
    agentSentOnlyAfterMarkerObserved(dispatch)               -- Property 6

--------------------------------------------------------------------------
Counterexamples, verbatim from the 2026-08-06 live evidence
--------------------------------------------------------------------------

1. Path mismatch (root cause 2, Property 1, Req 5.1/5.4)

   `build_dispatcher.py:112-113`:

       BUILD_REPO_DIR = os.environ.get('BUILD_REPO_DIR',
                                       '/opt/dda/DefectDetectionApplication')

   `build_fleet.py:179` (inside `USER_DATA_TEMPLATE`):

       sudo -u ubuntu -H git clone {repo_url} /home/ubuntu/DefectDetectionApplication

   Verified fact: the deployed BuildDispatcherHandler sets **no**
   `BUILD_REPO_DIR` override, so the dedicated agent path
   (`/opt/dda/DefectDetectionApplication/...`) is never where that
   server's bootstrap put the repository
   (`/home/ubuntu/DefectDetectionApplication`). Two independent literals,
   no shared source of truth.

   Observed counterexample: job `a25bb078` (AMD64 dedicated), SSM command
   `d75f1ea2` — Failed, ResponseCode 127.

2. Agent unobtainable (root cause 1, Property 3, Req 4.1/4.2)

   Both bootstrap paths run a bare `git clone <repo_url>` with no ref, so
   the tree on disk is the remote default branch (`origin/main`).
   `scripts/portal-build-agent.sh` was added in commit `479ab7f`, which
   is reachable only from
   `origin/feature/portal-build-fleet-and-workflow-gates` — it is absent
   from `origin/main`. The agent is therefore invoked from a tree that
   cannot contain it, and because the agent is what performs the
   `SOURCE_REF` checkout, the sync can never run.

   Observed counterexamples — both execution modes, same class:

       SSM e9281bdc (job 1ce014a3, JP5 ephemeral)   Failed, ResponseCode 127
       SSM d75f1ea2 (job a25bb078, AMD64 dedicated) Failed, ResponseCode 127

       stderr (both):
       bash: /opt/dda/DefectDetectionApplication/scripts/portal-build-agent.sh: No such file or directory

   Live inspection of runner `i-0b8221f5ed2ebc2a9` confirmed
   `/opt/dda/DefectDetectionApplication` exists (the clone succeeded)
   while `scripts/portal-build-agent.sh` does not.

3. Bootstrap race (root cause 3, Property 6, Req 6.1/6.2)

   `runner_bootstrap_user_data()` clones and runs `setup-build-server.sh`
   in user-data but writes no completion marker, and
   `provision_ephemeral`'s only gate is `instance_ssm_online()`. The SSM
   agent pings Online long before the bootstrap finishes.

   Observed counterexample (job `1ce014a3`, JP5 ephemeral):

       agent command requested   2026-08-06T21:36:59Z
       cloud-init finished       2026-08-06T21:38:54Z  ("Up 140.42 seconds")

   i.e. the agent command was sent ~115 seconds before the runner was
   ready. The dedicated path already writes
   `/var/log/dda-build-server-bootstrap.done`; the ephemeral path has no
   equivalent marker and no wait.

--------------------------------------------------------------------------
Safety
--------------------------------------------------------------------------

ABSOLUTE constraint, honored here: **no test launches EC2 compute, sends a
real SSM command, calls real AWS, or starts a real build.**

* DynamoDB / EC2 / SSM are moto-backed for the whole module (`mock_aws()`
  started before every import that binds a boto3 handle), with dummy
  credentials and no real endpoint reachable.
* The `provision_ephemeral` case patches `send_shell_command` with a
  recorder, so the agent SendCommand is observed but never issued — not
  even to moto.
* The agent-unobtainable case uses a **purely local** git fixture: a bare
  origin created in a temp directory, cloned over a filesystem path. No
  network, and the real DDA remote is never contacted. Its
  `setup-build-server.sh` and `scripts/portal-build-agent.sh` are inert
  stubs, and `apt-get` is shadowed by a no-op stub on `PATH`, so the
  bootstrap text runs without root, without package installs, and without
  starting any build.

The BuildJobs table is created with the EXACT deployed schema INCLUDING
the `status-index` / `server-index` / `request-index` GSIs, matching the
sibling `test_build_authorization_preservation.py` fixture, which
documents why the GSIs matter: suites that omit them miss defects in the
None-valued indexed-key write path (Req 7.3) that only surface against a
table carrying the indexes.

Run from the repository root:

    python3 -m pytest \\
        test/backend-test/portal_builds/test_source_selection_exploration.py \\
        --noconftest -q
"""
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Environment BEFORE any import: build_dispatcher / build_fleet bind their
# boto3 clients and env-derived settings at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "source-selection-explore"
_JOBS_TABLE = f"dda-portal-build-jobs-{_SUFFIX}"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
_SETTINGS_TABLE = f"dda-portal-settings-{_SUFFIX}"

os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
os.environ["SETTINGS_TABLE"] = _SETTINGS_TABLE
# The deployed BuildDispatcherHandler sets NO BUILD_REPO_DIR override
# (verified). The path-mismatch case depends on that fact, so the override
# must be absent here too.
os.environ.pop("BUILD_REPO_DIR", None)
os.environ.pop("BUILD_ALERT_TOPIC_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_NAME", None)
os.environ.pop("BUILD_SECURITY_GROUP_ID", None)
os.environ.pop("BUILD_SUBNET_ID", None)

# Import boto3 (and thus botocore/urllib3) from the test environment BEFORE
# the Lambda function directory joins sys.path.
import boto3  # noqa: E402

# Some verification containers ship a python build without the _bz2 C
# extension while moto's request path imports moto.s3 -> bz2 (sibling
# shim in test_dispatcher_tick_integration.py).
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
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

# ---------------------------------------------------------------------------
# Minimal stand-ins for the Lambda layer modules the handlers import
# (build_dispatcher takes log_audit_event; build_fleet additionally takes
# create_response / get_user_from_event and the RBAC decorators). Audit
# entries are captured in-process; nothing leaves the test.
# ---------------------------------------------------------------------------
AUDIT_EVENTS = []


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        AUDIT_EVENTS.append(kwargs)

    def create_response(status_code, body):
        return {"statusCode": status_code, "body": body}

    def get_user_from_event(event):
        return {"user_id": "explore-user", "role": "PortalAdmin"}

    module.log_audit_event = log_audit_event
    module.create_response = create_response
    module.get_user_from_event = get_user_from_event
    return module


def _fake_rbac_middleware():
    module = types.ModuleType("rbac_middleware")

    def _identity_decorator_factory(*d_args, **d_kwargs):
        def decorator(func):
            return func
        return decorator

    def super_user_only(func):
        return func

    module.require_builds_read = _identity_decorator_factory
    module.require_builds_submit = _identity_decorator_factory
    module.require_builds_cancel = _identity_decorator_factory
    module.super_user_only = super_user_only
    return module


for _module in ("build_dispatcher", "build_fleet", "build_planner",
                "build_domain", "shared_utils", "rbac_middleware"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()
sys.modules["rbac_middleware"] = _fake_rbac_middleware()

# Module-scope moto: active for every import below and for the whole run,
# so no boto3 handle can ever reach a real AWS endpoint.
_MOCK = mock_aws()
_MOCK.start()

_DDB = boto3.resource("dynamodb", region_name="us-east-1")

# BuildJobs with the deployed schema, GSIs included (verified against
# `aws dynamodb describe-table --table-name dda-portal-build-jobs`); the
# sibling test_build_authorization_preservation.py documents why the GSIs
# matter.
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

_JOBS = _DDB.Table(_JOBS_TABLE)
_SERVERS = _DDB.Table(_SERVERS_TABLE)

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_dispatcher  # noqa: E402
import build_fleet  # noqa: E402


# ---------------------------------------------------------------------------
# Evidence constants (quoted verbatim in failure messages)
# ---------------------------------------------------------------------------

#: The two live SSM invocations, both Failed with ResponseCode 127.
LIVE_INVOCATIONS = (
    "SSM e9281bdc (job 1ce014a3, JP5 ephemeral) Failed ResponseCode 127",
    "SSM d75f1ea2 (job a25bb078, AMD64 dedicated) Failed ResponseCode 127",
)
#: The stderr both invocations produced, verbatim.
LIVE_STDERR = ("bash: /opt/dda/DefectDetectionApplication/scripts/"
               "portal-build-agent.sh: No such file or directory")
#: The commit that adds scripts/portal-build-agent.sh, and the only ref it
#: is reachable from.
AGENT_SCRIPT_COMMIT = "479ab7f"
AGENT_SCRIPT_BRANCH = "feature/portal-build-fleet-and-workflow-gates"
#: The observed bootstrap race, verbatim.
RACE_COMMAND_AT = "2026-08-06T21:36:59Z"
RACE_BOOTSTRAP_DONE_AT = "2026-08-06T21:38:54Z (Up 140.42 seconds)"
#: The readiness signal the dedicated bootstrap already writes and the
#: ephemeral bootstrap does not.
BOOTSTRAP_MARKER = "/var/log/dda-build-server-bootstrap.done"

AGENT_SCRIPT_RELPATH = "scripts/portal-build-agent.sh"
NOW = 1_700_000_000_000  # ms epoch anchor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_tables():
    for item in _JOBS.scan().get("Items", []):
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})
    del AUDIT_EVENTS[:]


def _get_job(job_id):
    return build_dispatcher.to_native(
        _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))


def _job(job_id, execution_mode, server_id=None, status=None,
         source_ref=None, **extra):
    """A Build_Job record shaped exactly as build_domain writes it."""
    snapshot = {
        "arm64_instance_type": "m6g.4xlarge",
        "x86_64_instance_type": "m6i.4xlarge",
        "volume_size_gb": 100,
        "region": "us-east-1",
        "max_runtime_hours": 4,
        "use_spot_for_ephemeral": False,
        "source_ref": source_ref,
    }
    item = {
        "build_job_id": job_id,
        "build_target": build_domain.TARGET_JP5,
        "execution_mode": execution_mode,
        "status": status or build_domain.STATUS_QUEUED,
        "requested_by": "operator-1",
        "created_at": NOW,
        "config_snapshot": snapshot,
    }
    if server_id is not None:
        item["server_id"] = server_id
    item.update(extra)
    return item


def _fleet_bootstrap_repo_dir(repo_url="https://example.invalid/dda.git"):
    """The directory `build_fleet.USER_DATA_TEMPLATE` actually clones into
    (build_fleet.py:179) — parsed from the rendered template rather than
    re-hard-coded, so this test tracks the template."""
    rendered = build_fleet.USER_DATA_TEMPLATE.format(repo_url=repo_url)
    for line in rendered.splitlines():
        stripped = line.strip()
        if "git clone" in stripped:
            return shlex.split(stripped)[-1]
    raise AssertionError(
        "build_fleet.USER_DATA_TEMPLATE contains no `git clone` line:\n"
        f"{rendered}")


def _agent_script_path(job):
    """The script path `build_dispatcher.agent_command` targets.

    The invocation is the LAST line of the agent run body: environment
    exports (region and PATH lines, and, with a selected ref, the
    Source_Sync preamble) may precede it. The body IS the whole command
    for ephemeral jobs and is carried verbatim (executed as ubuntu)
    inside the dedicated command, so it is the mode-independent place
    the invocation lives.
    """
    body = build_dispatcher.agent_run_body(job)
    assert body in build_dispatcher.agent_command(job)
    invocation = body.splitlines()[-1]
    tokens = shlex.split(invocation)
    assert tokens[0] == "bash", tokens
    return tokens[1]


def _agent_path_prefix(job):
    """The agent command's path prefix, i.e. the repository directory the
    dispatcher assumes (design: `agentPathPrefix(command)`)."""
    script = _agent_script_path(job)
    suffix = "/" + AGENT_SCRIPT_RELPATH
    assert script.endswith(suffix), script
    return script[: -len(suffix)]


# ---------------------------------------------------------------------------
# Local git fixture (NO network, NO real remote): a bare origin whose
# default branch omits scripts/portal-build-agent.sh while a feature
# branch carries it — the exact shape of the live repository, where
# commit 479ab7f is reachable only from
# origin/feature/portal-build-fleet-and-workflow-gates.
# ---------------------------------------------------------------------------

def _git(*args, cwd=None):
    result = subprocess.run(
        ("git",) + args, cwd=cwd, capture_output=True, text=True,
        env={**os.environ,
             "GIT_CONFIG_NOSYSTEM": "1",
             "HOME": cwd or os.getcwd(),
             "GIT_TERMINAL_PROMPT": "0",
             "GIT_ALLOW_PROTOCOL": "file"},
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed ({result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}")
    return result.stdout


def _write(path, text, executable=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)
    if executable:
        os.chmod(path, 0o755)


def _make_local_origin(root):
    """A bare local origin mirroring the live repository's shape."""
    work = os.path.join(root, "work")
    os.makedirs(work)
    _git("init", "--quiet", work)
    _git("config", "user.email", "explore@example.invalid", cwd=work)
    _git("config", "user.name", "Exploration Fixture", cwd=work)
    # git 2.25 has no `init -b`; point HEAD at main before the first commit.
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=work)

    # Default branch (main): the build-environment bootstrap exists, the
    # portal build agent does NOT — exactly like origin/main today.
    _write(os.path.join(work, "setup-build-server.sh"),
           "#!/bin/bash\necho 'setup-build-server.sh stub (no-op)'\n",
           executable=True)
    _write(os.path.join(work, "README.md"), "local exploration fixture\n")
    _git("add", "-A", cwd=work)
    _git("commit", "--quiet", "-m", "base tree without the portal agent",
         cwd=work)

    # Feature branch: adds scripts/portal-build-agent.sh, standing in for
    # commit 479ab7f on origin/feature/portal-build-fleet-and-workflow-gates.
    _git("checkout", "--quiet", "-b", AGENT_SCRIPT_BRANCH, cwd=work)
    _write(os.path.join(work, AGENT_SCRIPT_RELPATH),
           "#!/bin/bash\n"
           "# Inert stand-in for scripts/portal-build-agent.sh: prints its\n"
           "# arguments and exits 0. It starts NO build and calls NO AWS.\n"
           'echo "portal-build-agent stub args: $*"\n'
           "exit 0\n",
           executable=True)
    _git("add", "-A", cwd=work)
    _git("commit", "--quiet", "-m",
         "add scripts/portal-build-agent.sh (stands in for 479ab7f)",
         cwd=work)
    _git("checkout", "--quiet", "main", cwd=work)

    origin = os.path.join(root, "origin.git")
    _git("clone", "--quiet", "--bare", work, origin)
    _git("--git-dir", origin, "symbolic-ref", "HEAD", "refs/heads/main")
    return origin


def _stub_bin(root):
    """A PATH shim so the generated bootstrap text runs unprivileged: the
    package steps become no-ops. git is the real thing (local paths only)."""
    stub = os.path.join(root, "stubbin")
    os.makedirs(stub, exist_ok=True)
    _write(os.path.join(stub, "apt-get"),
           "#!/bin/bash\necho \"apt-get stub: $*\"\nexit 0\n",
           executable=True)
    return stub


def _run_bash(script_text, stub_bin, cwd):
    path = os.path.join(cwd, "script.sh")
    _write(path, script_text, executable=True)
    return subprocess.run(
        ["bash", path], cwd=cwd, capture_output=True, text=True,
        env={**os.environ,
             "PATH": stub_bin + os.pathsep + os.environ.get("PATH", ""),
             "HOME": cwd,
             "GIT_TERMINAL_PROMPT": "0",
             "DEBIAN_FRONTEND": "noninteractive"},
    )


# ===========================================================================
# Property 1 — Bug Condition: the agent path equals the bootstrap clone
# directory (Req 5.1, 5.4). EXPECTED TO FAIL on unfixed code.
# ===========================================================================

class TestAgentPathEqualsBootstrapDirectory:
    """`pathMismatch := agentPathPrefix(command) != bootstrapRepoDir(job,
    server)` — design isBugCondition facet (b)."""

    def setup_method(self):
        _clear_tables()

    @pytest.mark.parametrize("execution_mode", [
        build_domain.EXECUTION_MODE_DEDICATED,
        build_domain.EXECUTION_MODE_EPHEMERAL,
    ])
    def test_agent_path_prefix_equals_fleet_bootstrap_clone_dir(
            self, execution_mode):
        """A dedicated Build_Job plus the fleet-bootstrapped Build_Server
        record: the path prefix of `build_dispatcher.agent_command(...)`
        MUST equal the directory `build_fleet.USER_DATA_TEMPLATE` clones
        into (the ephemeral row is the same assertion for the runner
        user-data, Req 5.4).

        COUNTEREXAMPLE (expected failure on unfixed code):

            agent_command prefix : /opt/dda/DefectDetectionApplication
                                   (build_dispatcher.py:112-113 default,
                                    and the deployed
                                    BuildDispatcherHandler sets NO
                                    BUILD_REPO_DIR override - verified)
            bootstrap clone dir  : /home/ubuntu/DefectDetectionApplication
                                   (build_fleet.py:179)

        Live evidence: job a25bb078 (AMD64 dedicated), SSM command
        d75f1ea2 - Failed, ResponseCode 127, stderr:
        `bash: /opt/dda/DefectDetectionApplication/scripts/portal-build-agent.sh: No such file or directory`
        """
        repo_url = "https://github.com/awslabs/DefectDetectionApplication"
        bootstrap_dir = _fleet_bootstrap_repo_dir(repo_url)

        server_id = "srv-amd64-dedicated"
        server = {
            "server_id": server_id,
            "name": "amd64-dedicated-1",
            "instance_id": "i-0b8221f5ed2ebc2a9",
            "lifecycle_state": build_domain.SERVER_STATE_RUNNING,
            "cpu_architecture": build_domain.ARCH_X86_64,
        }
        _SERVERS.put_item(Item=server)

        is_dedicated = (execution_mode ==
                        build_domain.EXECUTION_MODE_DEDICATED)
        job = _job("a25bb078" if is_dedicated else "1ce014a3",
                   execution_mode,
                   server_id=server_id if is_dedicated else None)
        _JOBS.put_item(Item=job)

        agent_prefix = _agent_path_prefix(_get_job(job["build_job_id"]))

        assert agent_prefix == bootstrap_dir, (
            "PATH MISMATCH (design isBugCondition facet (b), Property 1)\n"
            f"  execution_mode        : {execution_mode}\n"
            f"  agent_command prefix  : {agent_prefix}\n"
            "                          (build_dispatcher.py:112-113 "
            "BUILD_REPO_DIR default; the deployed "
            "BuildDispatcherHandler sets no override)\n"
            f"  bootstrap clone dir   : {bootstrap_dir}\n"
            "                          (build_fleet.py:179, "
            "USER_DATA_TEMPLATE)\n"
            f"  agent script targeted : "
            f"{_agent_script_path(_get_job(job['build_job_id']))}\n"
            f"  live evidence         : {LIVE_INVOCATIONS[1]}\n"
            f"  live stderr           : {LIVE_STDERR}\n"
            "  expected (Req 5.1/5.2/5.4): one authoritative repository "
            "directory shared by the bootstrap that creates the clone and "
            "the dispatcher that invokes the agent")


# ===========================================================================
# Property 3 — Bug Condition: the runner obtains the selected source
# before the agent runs (Req 4.1, 4.2). EXPECTED TO FAIL on unfixed code.
# ===========================================================================

class TestRunnerObtainsSelectedSourceBeforeAgent:
    """`agentUnobtainable` — design isBugCondition facet (a). Purely local
    git fixture: no network, no clone of the real remote."""

    def setup_method(self):
        _clear_tables()
        self.root = tempfile.mkdtemp(prefix="dda-source-explore-")
        self.origin = _make_local_origin(self.root)
        self.stub_bin = _stub_bin(self.root)
        self.repo_dir = os.path.join(self.root, "runner",
                                     "DefectDetectionApplication")

    def teardown_method(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_bootstrap_then_agent_invocation_finds_the_agent_script(self):
        """The bootstrap command text `runner_bootstrap_user_data()`
        generates is executed against a local origin whose default branch
        (`main`) omits `scripts/portal-build-agent.sh` while
        `feature/portal-build-fleet-and-workflow-gates` carries it — the
        exact shape of the live repository, where commit 479ab7f is
        reachable only from that feature branch. The job selects that ref
        via `config_snapshot.source_ref`. The agent invocation is then
        attempted.

        COUNTEREXAMPLE (expected failure on unfixed code): the bootstrap
        runs a bare `git clone <repo_url>` with no ref, so the tree is
        `origin/main`, which cannot contain the agent — and the agent is
        what performs the SOURCE_REF checkout, so the sync can never run.
        The invocation reproduces the observed class:

            SSM e9281bdc (job 1ce014a3, JP5 ephemeral)   Failed ResponseCode 127
            SSM d75f1ea2 (job a25bb078, AMD64 dedicated) Failed ResponseCode 127

            bash: /opt/dda/DefectDetectionApplication/scripts/portal-build-agent.sh: No such file or directory

        Live inspection of runner i-0b8221f5ed2ebc2a9 confirmed the clone
        directory exists while the script does not.
        """
        job = _job("1ce014a3", build_domain.EXECUTION_MODE_EPHEMERAL,
                   source_ref=AGENT_SCRIPT_BRANCH)
        _JOBS.put_item(Item=job)
        job = _get_job("1ce014a3")

        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               self.origin), \
                mock.patch.object(build_dispatcher, "BUILD_REPO_DIR",
                                  self.repo_dir):
            user_data = build_dispatcher.runner_bootstrap_user_data()
            agent_invocation = build_dispatcher.agent_command(job)

        assert user_data, "runner_bootstrap_user_data() produced no bootstrap"
        bootstrap = _run_bash(user_data, self.stub_bin, self.root)
        assert bootstrap.returncode == 0, (
            "the local bootstrap fixture itself failed - the test setup, "
            f"not the defect:\nstdout:\n{bootstrap.stdout}\n"
            f"stderr:\n{bootstrap.stderr}")

        agent = subprocess.run(
            ["bash", "-c", agent_invocation], cwd=self.root,
            capture_output=True, text=True,
            env={**os.environ,
                 "PATH": self.stub_bin + os.pathsep +
                         os.environ.get("PATH", ""),
                 "HOME": self.root})

        checked_out = subprocess.run(
            ["git", "-C", self.repo_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True).stdout.strip()
        context = (
            "AGENT UNOBTAINABLE (design isBugCondition facet (a), "
            "Property 3)\n"
            f"  selected repository   : {self.origin} (local fixture)\n"
            f"  selected source_ref   : {AGENT_SCRIPT_BRANCH}\n"
            f"    (carries the agent script; stands in for commit "
            f"{AGENT_SCRIPT_COMMIT})\n"
            f"  bootstrap left HEAD at: {checked_out or '<unknown>'}\n"
            f"  agent invocation      : {agent_invocation}\n"
            f"  exit code             : {agent.returncode}\n"
            f"  stderr                : {agent.stderr.strip()}\n"
            f"  live evidence         : {LIVE_INVOCATIONS[0]}\n"
            f"                          {LIVE_INVOCATIONS[1]}\n"
            f"  live stderr           : {LIVE_STDERR}\n"
            "  bootstrap text generated by runner_bootstrap_user_data():\n"
            + "\n".join(f"      {line}" for line in user_data.splitlines())
            + "\n  expected (Req 4.1/4.2): the bootstrap performs the "
              "Source_Sync for the selected (repository, ref) STRICTLY "
              "before any agent invocation, so a ref that carries "
              "scripts/portal-build-agent.sh still executes the agent even "
              "though the repository default branch does not carry it")

        assert agent.returncode != 127, context
        assert "No such file or directory" not in agent.stderr, context
        assert agent.returncode == 0, context


# ===========================================================================
# Property 6 — Bug Condition: bootstrap completion gates the agent command
# (Req 6.1, 6.2). EXPECTED TO FAIL on unfixed code.
# ===========================================================================

class TestBootstrapCompletionGatesAgentCommand:
    """`bootstrapRace` — design isBugCondition facet (c). SSM and EC2 are
    moto-backed and `send_shell_command` is a recorder, so the agent
    SendCommand is observed but never issued."""

    def setup_method(self):
        _clear_tables()

    def test_no_agent_command_while_the_bootstrap_marker_is_absent(self):
        """`provision_ephemeral` is driven over a provisioning job whose
        runner reports SSM Online while `/var/log/
        dda-build-server-bootstrap.done` does NOT exist. No agent
        SendCommand may be issued in that tick.

        COUNTEREXAMPLE (expected failure on unfixed code): the command is
        sent on the FIRST Online tick, because the only gate is
        `instance_ssm_online()` and `runner_bootstrap_user_data()` writes
        no completion marker at all (the dedicated
        `build_fleet.USER_DATA_TEMPLATE` already writes one).

            agent command requested   2026-08-06T21:36:59Z
            cloud-init finished       2026-08-06T21:38:54Z
                                      ("Up 140.42 seconds")

        i.e. job 1ce014a3's agent command was sent ~115 seconds before its
        runner was ready, and its SSM invocation e9281bdc Failed with
        ResponseCode 127.
        """
        job = _job("1ce014a3", build_domain.EXECUTION_MODE_EPHEMERAL,
                   status=build_domain.STATUS_PROVISIONING,
                   dispatched_at=NOW,
                   runner={
                       "instance_id": "i-0b8221f5ed2ebc2a9",
                       "instance_type": "m6g.4xlarge",
                       "arch": build_domain.ARCH_ARM64,
                       "spot": False,
                       "terminate_attempts": 0,
                       "terminate_first_failed_at": None,
                   })
        _JOBS.put_item(Item=job)

        sent = []
        probes = []

        def _record_send(instance_id, commands, **kwargs):
            sent.append({"instance_id": instance_id,
                         "commands": list(commands), **kwargs})
            return "command-not-actually-sent"

        def _marker_absent(instance_id, commands):
            """Every probe reports the bootstrap marker ABSENT: this runner
            is still inside cloud-init, exactly as at 21:36:59Z."""
            probes.append(list(commands))
            return f"BOOTSTRAP_DONE=0\nBOOTSTRAP_LOG={BOOTSTRAP_MARKER}\n"

        with mock.patch.object(build_dispatcher, "send_shell_command",
                               side_effect=_record_send), \
                mock.patch.object(build_dispatcher, "run_shell_sync",
                                  side_effect=_marker_absent), \
                mock.patch.object(build_dispatcher, "instance_ssm_online",
                                  return_value=True):
            build_dispatcher.provision_ephemeral(
                [build_dispatcher.to_native(job)], NOW)

        agent_sends = [call for call in sent
                       if any(AGENT_SCRIPT_RELPATH in command
                              for command in call["commands"])]
        stored = _get_job("1ce014a3")
        context = (
            "BOOTSTRAP RACE (design isBugCondition facet (c), Property 6)\n"
            f"  job status            : {job['status']}\n"
            f"  runner SSM ping       : Online\n"
            f"  bootstrap marker      : {BOOTSTRAP_MARKER} ABSENT\n"
            f"  marker probes issued  : {len(probes)} "
            "(runner_bootstrap_user_data() writes no marker, and the "
            "dispatcher has no probe: its only gate is "
            "instance_ssm_online())\n"
            f"  agent SendCommands    : {len(agent_sends)}\n"
            f"  commands recorded     : "
            f"{[call['commands'] for call in sent]}\n"
            f"  job.ssm after tick    : {(stored or {}).get('ssm')}\n"
            f"  live evidence         : agent command requested "
            f"{RACE_COMMAND_AT}, cloud-init finished "
            f"{RACE_BOOTSTRAP_DONE_AT}; SSM e9281bdc (job 1ce014a3) "
            "Failed ResponseCode 127\n"
            f"  live stderr           : {LIVE_STDERR}\n"
            "  expected (Req 6.1/6.2): no agent command in any tick where "
            "the Bootstrap_Marker has not been observed; readiness is a "
            "function of the marker probe alone, never elapsed sleep")

        assert agent_sends == [], context
        assert not (stored or {}).get("ssm", {}).get("command_id"), context
