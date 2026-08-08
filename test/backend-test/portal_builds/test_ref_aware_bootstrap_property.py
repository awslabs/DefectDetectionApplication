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
Ref-aware bootstrap properties (build-source-selection, task 5.4).

**Validates: Requirements 4.1, 4.2, 4.3, 4.4**

Three design properties, restated here independently of the
implementation:

* **Property 3 — the runner obtains the selected source before the agent
  runs.** For any selected ``(repository, ref)`` the Source_Sync for that
  pair happens STRICTLY before any agent invocation, so a ref whose
  content carries ``scripts/portal-build-agent.sh`` still executes the
  agent even when the repository default branch does not carry it. No
  dispatch path depends on the agent script being present on the default
  branch.
* **Property 4 — one sync mechanism, idempotent under repetition.** The
  pre-agent preamble text IS the Sync_Generator's output for the same
  inputs (no second, divergent copy), and applying the preamble and then
  the agent's own Step 2 sync leaves the same ``HEAD`` as the agent's
  sync alone.
* **Property 5 — source failures are named and classified.** Each of the
  two failure classes carries its own marker line, its own exit code
  (65 repository unreachable / 66 ref not found) and its own message
  naming BOTH the repository and the ref, and the one emitted phase event
  has the same detail keys, in the same order, as the agent's
  ``emit_failed`` — plus ``source_error=<class>`` and
  ``error_kind=source_sync``. Never a bare 127.

Scope, and what is deliberately NOT here
----------------------------------------
The Sync_Generator itself (``build_source.source_sync_commands`` /
``bootstrap_commands``) is covered at unit level by the sibling
``test_source_sync_commands_unit.py``: the two checkout arms, the
clone-only sequence, marker/exit-code structure, injection safety, and
executed-against-a-local-origin behavior. Those are NOT repeated here.
This file covers the DISPATCHER-side composition (preamble before agent,
preamble == generator, the emitted event) and the executed
bootstrap+preamble+agent sequence end to end.

The agent's argument/exit-code contract itself is the task 2 preservation
baseline (``test_source_selection_preservation.py``); only the dispatcher's
side of it (the four argument tokens it still passes) is asserted here.

Safety
------
No network, no AWS, no EC2, no SSM, no builds. Concretely:

* the only git remote is a LOCAL origin created in a temp directory and
  cloned over a filesystem path; the real DDA remote is never contacted;
* ``scripts/portal-build-agent.sh`` and ``setup-build-server.sh`` in the
  fixture are inert stubs, and ``apt-get`` is shadowed by a no-op stub on
  ``PATH``, so the generated bootstrap runs unprivileged and starts no
  build;
* ``aws`` is a recording stub on ``PATH`` (or genuinely absent from it):
  the emitted PutEvents payload is captured on disk and never sent;
* ``build_dispatcher`` is imported with dummy credentials and a stub
  ``shared_utils``; it creates boto3 clients at import (no calls) and no
  test in this file invokes any handler that would touch AWS;
* every temp directory is removed in fixture teardown.

Run with ``--noconftest`` like the rest of the ``portal_builds`` suite::

    python3 -m pytest \\
        test/backend-test/portal_builds/test_ref_aware_bootstrap_property.py \\
        --noconftest -q
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Environment BEFORE importing build_dispatcher: it binds boto3 clients and
# env-derived settings at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
# Nothing else is set or unset here on purpose: this module never calls a
# handler, never touches a table, and passes the repository directory
# explicitly, so it needs no build_* environment of its own — and mutating
# one would leak into whichever sibling module imports build_dispatcher
# first when the whole suite runs.

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

#: The agent script this spec must NOT modify; it is only read here (its
#: Step 2 sync block and its emit_failed detail shape are the contract the
#: preamble mirrors).
AGENT_SCRIPT_PATH = os.path.join(
    _REPO_ROOT, "scripts", "portal-build-agent.sh")
AGENT_SCRIPT_RELPATH = "scripts/portal-build-agent.sh"


def _fake_shared_utils():
    """Stand-in for the Lambda layer module build_dispatcher imports."""
    module = types.ModuleType("shared_utils")
    module.log_audit_event = lambda **kwargs: None
    module.create_response = lambda status_code, body: {
        "statusCode": status_code, "body": body}
    module.get_user_from_event = lambda event: {
        "user_id": "refaware-test", "role": "PortalAdmin"}
    return module


sys.modules.setdefault("shared_utils", _fake_shared_utils())

import build_domain  # noqa: E402
import build_source  # noqa: E402
import build_dispatcher  # noqa: E402

#: The bus the dispatcher hands both the preamble and the agent, read from
#: the module rather than assumed, so this file is insensitive to whichever
#: sibling module imported build_dispatcher first.
EVENT_BUS = build_dispatcher.BUILD_EVENT_BUS

REPO_DIR = "/home/ubuntu/DefectDetectionApplication"
NOW = 1_700_000_000_000  # ms epoch anchor

#: The branch that carries the agent script in the fixture, standing in for
#: commit 479ab7f on origin/feature/portal-build-fleet-and-workflow-gates —
#: reachable from that ref only, which is the live failure's shape.
AGENT_BRANCH = "feature/portal-build-fleet-and-workflow-gates"
DEFAULT_BRANCH = "main"
FIXTURE_TAG = "v1.0.0"


# ---------------------------------------------------------------------------
# Generated inputs
# ---------------------------------------------------------------------------

#: Refs an operator can select: branches, tags, a 40-hex SHA, and values
#: whose text would change the meaning of the script if it were ever
#: interpolated unquoted (the repository and the ref are operator input in
#: Increment B).
CURATED_REFS = [
    "main",
    AGENT_BRANCH,
    "feature/portal-build-fleet-and-workflow-gates",
    "release/2.1",
    "v1.2.3",
    "0" * 40,
    "a3f5c19b2e7d48016cba9f3d2e5178094bcdef12",
    "main; rm -rf /",
    "main && touch /tmp/dda-pwned",
    "main`touch /tmp/dda-pwned`",
    "main$(touch /tmp/dda-pwned)",
    "main'; touch /tmp/dda-pwned; '",
    'main" ; touch /tmp/dda-pwned ; "',
    "main | tee /tmp/dda-pwned",
    "${REPO_DIR}-injected",
    "--upload-pack=touch /tmp/dda-pwned",
]

CURATED_REPOSITORIES = [
    "https://github.com/awslabs/DefectDetectionApplication",
    "https://github.com/awslabs/DefectDetectionApplication.git",
    "https://github.com/an-operator/dda-fork",
    "https://github.com/awslabs/DefectDetectionApplication; rm -rf /",
    "https://github.com/awslabs/$(touch /tmp/dda-pwned)",
]

_NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEZ0123456789-_./"

refs = st.one_of(
    st.sampled_from(CURATED_REFS),
    st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=40).filter(
        lambda value: value.strip() != ""),
)

repositories = st.one_of(
    st.sampled_from(CURATED_REPOSITORIES),
    st.builds(
        "https://github.com/{}/{}".format,
        st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=20),
        st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=20),
    ),
)

build_targets = st.sampled_from([
    build_domain.TARGET_JP5, build_domain.TARGET_JP6,
    build_domain.TARGET_AMD64, build_domain.TARGET_AMD64_NVIDIA,
])

job_ids = st.text(alphabet="abcdef0123456789", min_size=8, max_size=8)


def make_job(source_ref=None, job_id="1ce014a3",
             build_target=None, execution_mode=None, server_id=None):
    """A Build_Job record shaped as build_domain writes it."""
    job = {
        "build_job_id": job_id,
        "build_target": build_target or build_domain.TARGET_JP5,
        "execution_mode": (execution_mode
                           or build_domain.EXECUTION_MODE_EPHEMERAL),
        "status": build_domain.STATUS_QUEUED,
        "requested_by": "operator-1",
        "created_at": NOW,
        "config_snapshot": {
            "arm64_instance_type": "m6g.4xlarge",
            "x86_64_instance_type": "m6i.4xlarge",
            "volume_size_gb": 100,
            "region": "us-east-1",
            "max_runtime_hours": 4,
            "use_spot_for_ephemeral": False,
            "source_ref": source_ref,
        },
    }
    if server_id is not None:
        job["server_id"] = server_id
    return job


def agent_invocation_token(repo_dir):
    """The exact token the agent invocation carries the script path as."""
    return shlex.quote(build_source.agent_script_path(repo_dir))


# ---------------------------------------------------------------------------
# The agent script is READ ONLY here: its Step 2 sync block (the semantics
# the preamble must be idempotent with) and its emit_failed / emit_event
# payload shapes (the shape the preamble's event must match).
# ---------------------------------------------------------------------------

def agent_script_text():
    with open(AGENT_SCRIPT_PATH) as handle:
        return handle.read()


def agent_step2_sync_block():
    """The agent's own Step 2 sync, verbatim from the script.

    Extracted by its section banners rather than by line number so this
    test tracks the script instead of pinning it (the script is NOT
    modified by this spec).
    """
    lines = agent_script_text().splitlines()
    starts = [i for i, line in enumerate(lines)
              if "Step 2: sync the source tree" in line]
    ends = [i for i, line in enumerate(lines) if "Step 3:" in line]
    assert starts and ends, "agent Step 2/Step 3 banners not found"
    block = "\n".join(lines[starts[0] + 1:ends[0]])
    assert 'git checkout --force -B "$SOURCE_REF"' in block, block
    return block


def agent_emit_failed_detail_keys():
    """The detail keys the agent's ``emit_failed`` emits, in order."""
    text = agent_script_text()
    match = re.search(r"detail=\$\(printf '(\{.*?\})'", text, re.S)
    assert match, "agent emit_failed printf format not found"
    keys = re.findall(r'"([a-z_]+)":', match.group(1))
    assert keys[:2] == ["build_job_id", "phase"], keys
    return keys


def agent_event_envelope():
    """The ``Source`` / ``DetailType`` the agent's ``emit_event`` uses."""
    text = agent_script_text()
    match = re.search(r'\[\{"Source":"([^"]+)","DetailType":"([^"]+)"', text)
    assert match, "agent emit_event entries format not found"
    return match.group(1), match.group(2)


# ---------------------------------------------------------------------------
# Local sandbox: a LOCAL git origin whose default branch omits the agent
# script while a feature branch carries it, plus PATH stubs. No network.
# ---------------------------------------------------------------------------

AWS_STUB_SUCCESS = """#!/bin/bash
# Recording stand-in for the AWS CLI. Records the invocation and the
# --entries payload, then prints what `--query FailedEntryCount --output
# text` prints on success. NOTHING is sent anywhere.
printf 'CALL %s\\n' "$*" >> "$PORTAL_TEST_AWS_LOG"
prev=""
for arg in "$@"; do
    if [ "$prev" = "--entries" ]; then
        printf '%s\\n' "$arg" >> "$PORTAL_TEST_AWS_ENTRIES"
    fi
    prev="$arg"
done
echo 0
"""

AWS_STUB_FAILURE = """#!/bin/bash
# A failing AWS CLI: the invocation is still recorded, but it errors out
# exactly like a bus/permission problem would.
printf 'CALL %s\\n' "$*" >> "$PORTAL_TEST_AWS_LOG"
echo "stub aws: PutEvents refused" >&2
exit 1
"""

APT_GET_STUB = """#!/bin/bash
echo "apt-get stub (no-op): $*"
exit 0
"""


class Sandbox:
    """Temp-directory sandbox: the local origin, the PATH stubs, and the
    helpers that execute generated command text against them."""

    def __init__(self, root):
        self.root = root
        self.home = os.path.join(root, "home")
        self.base_bin = os.path.join(root, "bin-base")
        self.aws_ok_bin = os.path.join(root, "bin-aws-ok")
        self.aws_bad_bin = os.path.join(root, "bin-aws-bad")
        for path in (self.home, self.base_bin, self.aws_ok_bin,
                     self.aws_bad_bin):
            os.makedirs(path)
        self.aws_log = os.path.join(root, "aws-calls.log")
        self.aws_entries = os.path.join(root, "aws-entries.log")
        write_file(os.path.join(self.base_bin, "apt-get"), APT_GET_STUB,
                   executable=True)
        write_file(os.path.join(self.aws_ok_bin, "aws"), AWS_STUB_SUCCESS,
                   executable=True)
        write_file(os.path.join(self.aws_bad_bin, "aws"), AWS_STUB_FAILURE,
                   executable=True)
        self.origin = self._make_origin()

    # ----------------------------------------------------------- git origin

    def git(self, *args, cwd=None):
        result = subprocess.run(
            ("git",) + args, cwd=cwd or self.root, capture_output=True,
            text=True, env=self.git_env())
        assert result.returncode == 0, (
            f"git {' '.join(args)} failed ({result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}")
        return result.stdout.strip()

    def git_env(self):
        env = dict(os.environ)
        env.update({
            "HOME": self.home,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "DDA Test",
            "GIT_AUTHOR_EMAIL": "dda-test@example.invalid",
            "GIT_COMMITTER_NAME": "DDA Test",
            "GIT_COMMITTER_EMAIL": "dda-test@example.invalid",
        })
        return env

    def _make_origin(self):
        """`main` WITHOUT scripts/portal-build-agent.sh, a feature branch
        WITH it, plus a tag on the default branch."""
        work = os.path.join(self.root, "origin-work")
        os.makedirs(work)
        self.git("init", "--quiet", work)
        # git 2.25 has no `init -b`: point HEAD at main before committing.
        self.git("symbolic-ref", "HEAD", f"refs/heads/{DEFAULT_BRANCH}",
                 cwd=work)
        write_file(os.path.join(work, "setup-build-server.sh"),
                   "#!/bin/bash\n"
                   "echo 'setup-build-server.sh stub (no-op)'\n",
                   executable=True)
        write_file(os.path.join(work, "README.md"),
                   "default branch without the portal build agent\n")
        self.git("add", "-A", cwd=work)
        self.git("commit", "--quiet", "-m",
                 "base tree without the portal agent", cwd=work)
        self.git("tag", FIXTURE_TAG, cwd=work)

        self.git("checkout", "--quiet", "-b", AGENT_BRANCH, cwd=work)
        write_file(
            os.path.join(work, AGENT_SCRIPT_RELPATH),
            "#!/bin/bash\n"
            "# Inert stand-in for scripts/portal-build-agent.sh: echoes a\n"
            "# marker plus its arguments and exits 0. It starts NO build\n"
            "# and calls NO AWS.\n"
            'echo "PORTAL_AGENT_STUB_RAN args: $*"\n'
            "exit 0\n",
            executable=True)
        self.git("add", "-A", cwd=work)
        self.git("commit", "--quiet", "-m",
                 "add scripts/portal-build-agent.sh (stands in for 479ab7f)",
                 cwd=work)
        self.git("checkout", "--quiet", DEFAULT_BRANCH, cwd=work)

        origin = os.path.join(self.root, "origin.git")
        self.git("clone", "--quiet", "--bare", work, origin)
        self.git("--git-dir", origin, "symbolic-ref", "HEAD",
                 f"refs/heads/{DEFAULT_BRANCH}")
        return origin

    # ------------------------------------------------------------ execution

    def path_for(self, aws="absent"):
        """A PATH with git/coreutils available and ``aws`` present as a
        recording stub, present but failing, or genuinely absent.

        Absence is real, not simulated: the system CLI lives outside
        /usr/bin (verified with ``shutil.which``), so restricting PATH to
        the stub directories plus /usr/bin:/bin removes it.
        """
        parts = {"absent": [], "success": [self.aws_ok_bin],
                 "failure": [self.aws_bad_bin]}[aws]
        return os.pathsep.join(parts + [self.base_bin, "/usr/bin", "/bin"])

    def run_bash(self, script_text, name="script", cwd=None, aws="absent"):
        cwd = cwd or self.root
        path = os.path.join(self.root, f"{name}.sh")
        write_file(path, script_text)
        env = self.git_env()
        env.update({
            "PATH": self.path_for(aws),
            "DEBIAN_FRONTEND": "noninteractive",
            "PORTAL_TEST_AWS_LOG": self.aws_log,
            "PORTAL_TEST_AWS_ENTRIES": self.aws_entries,
        })
        return subprocess.run(["bash", path], cwd=cwd, capture_output=True,
                              text=True, env=env)

    # -------------------------------------------------------- observations

    def clone_at_default_branch(self, dest):
        """A working tree as today's ref-less bootstrap leaves it: the
        repository default branch, which does NOT carry the agent."""
        self.git("clone", "--quiet", self.origin, dest)
        assert not os.path.exists(os.path.join(dest, AGENT_SCRIPT_RELPATH))
        return dest

    def head(self, repo_dir):
        return self.git("-C", repo_dir, "rev-parse", "HEAD")

    def branch(self, repo_dir):
        return self.git("-C", repo_dir, "rev-parse", "--abbrev-ref", "HEAD")

    def aws_calls(self):
        if not os.path.exists(self.aws_log):
            return []
        with open(self.aws_log) as handle:
            return [line for line in handle.read().splitlines()
                    if line.startswith("CALL ")]

    def clear_aws_records(self):
        for path in (self.aws_log, self.aws_entries):
            if os.path.exists(path):
                os.remove(path)

    def aws_payloads(self):
        """The ``--entries`` payloads the stub captured, parsed."""
        if not os.path.exists(self.aws_entries):
            return []
        with open(self.aws_entries) as handle:
            return [json.loads(line) for line in handle.read().splitlines()
                    if line.strip()]


def write_file(path, text, executable=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)
    if executable:
        os.chmod(path, 0o755)


@pytest.fixture
def sandbox():
    root = tempfile.mkdtemp(prefix="dda-refaware-")
    try:
        yield Sandbox(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ===========================================================================
# Property 3 — the runner obtains the selected source before the agent runs
# (Req 4.1, 4.2)
# ===========================================================================

class TestSourceSyncPrecedesTheAgentInvocation:
    """``sourceSyncedBeforeAgent``: for any generated (repository, ref) the
    Source_Sync index is STRICTLY less than the agent-invocation index."""

    @settings(max_examples=200, deadline=None)
    @given(repository=repositories, ref=refs, job_id=job_ids,
           build_target=build_targets)
    def test_sync_index_is_strictly_below_the_agent_index(
            self, repository, ref, job_id, build_target):
        job = make_job(source_ref=ref, job_id=job_id,
                       build_target=build_target)
        dedicated = make_job(
            source_ref=ref, job_id=job_id, build_target=build_target,
            execution_mode=build_domain.EXECUTION_MODE_DEDICATED,
            server_id="srv-1")
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               repository):
            command = build_dispatcher.agent_command(job, REPO_DIR)
            body = build_dispatcher.agent_run_body(job, REPO_DIR)
            preamble = build_dispatcher.agent_preamble_commands(job, REPO_DIR)
            dedicated_command = build_dispatcher.agent_command(
                dedicated, REPO_DIR)

        # A selected ref always yields a preamble, and the body is exactly
        # the environment exports (additive HOME + region + PATH lines, so
        # HOME is set for git's global config, a fresh runner's aws CLI has
        # a region, and $HOME/.local/bin is on PATH before the preamble's
        # own put-events runs), then that preamble, then the agent
        # invocation.
        assert preamble, (repository, ref)
        exports = build_dispatcher.runner_env_export_commands(job)
        prefix = "\n".join(list(exports) + list(preamble)) + "\n"
        assert body.startswith(prefix), body

        agent_index = body.index(agent_invocation_token(REPO_DIR))
        # The invocation is the body's last line: nothing runs after the
        # agent inside the body.
        assert agent_index >= len(prefix), body
        assert body.count("\n", agent_index) == 0, body

        # Every Source_Sync statement precedes it, strictly.
        for statement in ('mkdir -p "$(dirname "$REPO_DIR")"',
                          'git clone "$REPO_URL" "$REPO_DIR"',
                          'cd "$REPO_DIR"',
                          "git fetch --prune origin",
                          'git rev-parse --verify --quiet '
                          '"refs/remotes/origin/$SOURCE_REF"',
                          'git checkout --force -B "$SOURCE_REF"',
                          'git checkout --force "$SOURCE_REF"'):
            index = body.index(statement)
            assert index < agent_index, (statement, body)

        # The ref reaches the sync as data only, in its own quoted
        # assignment, and reaches the agent as its documented argument.
        assert f"SOURCE_REF={shlex.quote(ref)}" in preamble
        assert shlex.quote(f"SOURCE_REF={ref}") in body[agent_index:]

        # ONE environment model, both execution modes: the command runs
        # the SAME body as ubuntu — the body is transported VERBATIM
        # inside the command text (through the quoted here-doc), the
        # root-context ownership heal precedes it, the sudo build-user
        # execution follows, and the command's exit status is the body's
        # (so the classified 65/66 sync failures survive). Ephemeral and
        # dedicated dispatch generate identical text.
        assert command == dedicated_command
        assert body in command
        heal = build_dispatcher.repo_ownership_heal_command(REPO_DIR)
        assert command.index(heal) < command.index(body), command
        assert (f"sudo -H -u {build_dispatcher.BUILD_USER} bash"
                in command)
        assert command.splitlines()[-1] == \
            f'exit "${build_dispatcher.RUN_STATUS_VAR}"', command

    @settings(max_examples=100, deadline=None)
    @given(repository=repositories, job_id=job_ids)
    def test_no_selected_ref_means_no_preamble_at_all(self, repository,
                                                      job_id):
        """Req 7.1: dispatch for a job that selected nothing yields a
        build-user body that is exactly the additive environment exports
        (HOME + region + PATH lines, which contain no sync text) followed
        by today's single-line agent invocation — no sync text anywhere
        in the command."""
        job = make_job(source_ref=None, job_id=job_id)
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               repository):
            command = build_dispatcher.agent_command(job, REPO_DIR)
            body = build_dispatcher.agent_run_body(job, REPO_DIR)
            assert build_dispatcher.agent_preamble_commands(
                job, REPO_DIR) == []
            exports = build_dispatcher.runner_env_export_commands(job)

        lines = body.splitlines()
        # build-fleet-execution-failures task 7.1 reconciliation
        # (evidence rows 1/8): the no-ref body additionally carries the
        # spec-sanctioned dispatch preflight guard between the exports
        # and the invocation; it contains NO sync text, so this test's
        # own contract — no preamble, no sync anywhere — is intact.
        guard = list(build_dispatcher.preflight_guard_commands(REPO_DIR))
        assert lines[:len(exports)] == list(exports)
        assert lines[len(exports):-1] == guard
        assert lines[-1].startswith(
            "bash " + agent_invocation_token(REPO_DIR))
        assert body in command
        assert "git " not in command
        assert build_source.SYNC_MARKER not in command

    def test_bootstrap_then_agent_runs_a_ref_absent_from_the_default_branch(
            self, sandbox):
        """The live failure's shape, executed: the origin's default branch
        omits scripts/portal-build-agent.sh and the selected ref carries
        it. The generated bootstrap plus the generated agent command must
        leave a tree containing the agent AND execute it — never the
        observed `No such file or directory` / exit 127 (SSM e9281bdc,
        d75f1ea2)."""
        repo_dir = os.path.join(sandbox.root, "runner",
                                "DefectDetectionApplication")
        job = make_job(source_ref=AGENT_BRANCH)

        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               sandbox.origin):
            user_data = build_dispatcher.runner_bootstrap_user_data(
                job, repo_dir)
            command = build_dispatcher.agent_command(job, repo_dir)

        bootstrap = sandbox.run_bash(user_data, name="bootstrap")
        assert bootstrap.returncode == 0, (
            f"bootstrap failed:\n{bootstrap.stdout}\n{bootstrap.stderr}")
        assert build_source.SYNC_MARKER not in bootstrap.stdout
        # The bootstrap alone already put the agent script in place.
        assert os.path.exists(os.path.join(repo_dir, AGENT_SCRIPT_RELPATH))
        assert sandbox.branch(repo_dir) == AGENT_BRANCH

        agent = sandbox.run_bash(command, name="agent")
        context = (f"exit={agent.returncode}\n"
                   f"stdout:\n{agent.stdout}\nstderr:\n{agent.stderr}")
        assert agent.returncode == 0, context
        assert agent.returncode != 127, context
        assert "No such file or directory" not in agent.stderr, context
        assert "PORTAL_AGENT_STUB_RAN" in agent.stdout, context
        # The agent received its documented arguments.
        assert f"SOURCE_REF={AGENT_BRANCH}" in agent.stdout
        assert "BUILD_JOB_ID=1ce014a3" in agent.stdout
        assert f"EVENT_BUS={EVENT_BUS}" in agent.stdout

    def test_preamble_alone_makes_the_agent_obtainable_on_a_legacy_tree(
            self, sandbox):
        """No dispatch path may depend on the agent being present on the
        default branch: a server bootstrapped weeks ago on another ref has
        a tree at the default branch, which does NOT carry the script. The
        pre-agent preamble alone must make it obtainable and run it."""
        repo_dir = os.path.join(sandbox.root, "legacy-server",
                                "DefectDetectionApplication")
        sandbox.clone_at_default_branch(repo_dir)
        job = make_job(source_ref=AGENT_BRANCH,
                       execution_mode=build_domain.EXECUTION_MODE_DEDICATED,
                       job_id="a25bb078", server_id="srv-amd64-dedicated")

        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               sandbox.origin):
            command = build_dispatcher.agent_command(job, repo_dir)

        agent = sandbox.run_bash(command, name="legacy-agent")
        context = (f"exit={agent.returncode}\n"
                   f"stdout:\n{agent.stdout}\nstderr:\n{agent.stderr}")
        assert agent.returncode == 0, context
        assert "PORTAL_AGENT_STUB_RAN" in agent.stdout, context
        assert sandbox.branch(repo_dir) == AGENT_BRANCH
        assert os.path.exists(os.path.join(repo_dir, AGENT_SCRIPT_RELPATH))


# ===========================================================================
# Property 4 — one sync mechanism, idempotent under repetition (Req 4.3, 7.6)
# ===========================================================================

class TestPreambleIsTheOneSyncGenerator:
    """``syncCommandsComeFromOneGenerator``: the preamble does not contain
    sync text of its own — it IS the generator's output for the same
    inputs, including the failure-surfacing parameters the dispatcher is
    the only caller to pass."""

    @settings(max_examples=200, deadline=None)
    @given(repository=repositories, ref=refs, job_id=job_ids,
           build_target=build_targets)
    def test_preamble_equals_the_generator_for_the_same_inputs(
            self, repository, ref, job_id, build_target):
        job = make_job(source_ref=ref, job_id=job_id,
                       build_target=build_target)
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               repository):
            preamble = build_dispatcher.agent_preamble_commands(job, REPO_DIR)

        expected = build_source.source_sync_commands(
            repository, REPO_DIR, ref,
            event_bus=EVENT_BUS,
            build_job_id=job_id,
            build_target=build_target)

        assert preamble == expected

        # The emission parameters are what make the two lists differ from
        # the user-data flavor, so assert they are genuinely carried: the
        # preamble is the ONLY caller that passes them (Req 4.4).
        user_data_flavor = build_source.source_sync_commands(
            repository, REPO_DIR, ref)
        assert len(preamble) > len(user_data_flavor)
        assert f"PORTAL_BUILD_JOB_ID={shlex.quote(job_id)}" in preamble
        assert f"PORTAL_BUILD_TARGET={shlex.quote(build_target)}" in preamble
        assert f"PORTAL_EVENT_BUS={shlex.quote(EVENT_BUS)}" in preamble

    @settings(max_examples=100, deadline=None)
    @given(repository=repositories, ref=refs, job_id=job_ids,
           build_target=build_targets)
    def test_agent_argument_contract_is_untouched_by_the_preamble(
            self, repository, ref, job_id, build_target):
        """Req 7.6: the dispatcher still passes exactly the four documented
        arguments, on one line — the LAST line of the body — whatever the
        preamble does, in BOTH execution modes (the sudo wrapper carries
        the body verbatim, so the contract is byte-identical)."""
        job = make_job(source_ref=ref, job_id=job_id,
                       build_target=build_target)
        dedicated = make_job(
            source_ref=ref, job_id=job_id, build_target=build_target,
            execution_mode=build_domain.EXECUTION_MODE_DEDICATED,
            server_id="srv-1")
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               repository):
            command = build_dispatcher.agent_command(job, REPO_DIR)
            body = build_dispatcher.agent_run_body(job, REPO_DIR)
            dedicated_command = build_dispatcher.agent_command(
                dedicated, REPO_DIR)

        assert command == dedicated_command
        assert body in command
        invocation = body.splitlines()[-1]
        tokens = shlex.split(invocation)
        assert tokens[0] == "bash"
        assert tokens[1] == build_source.agent_script_path(REPO_DIR)
        assert tokens[2:] == [
            f"BUILD_JOB_ID={job_id}",
            f"BUILD_TARGET={build_target}",
            f"EVENT_BUS={EVENT_BUS}",
            f"SOURCE_REF={ref}",
        ]

    @pytest.mark.parametrize("ref", [AGENT_BRANCH, FIXTURE_TAG])
    def test_preamble_then_agent_sync_leaves_the_same_head_as_agent_alone(
            self, sandbox, ref):
        """Idempotence, executed: the agent re-runs its own Step 2 sync
        after the preamble, so preamble+agent-sync must land exactly where
        agent-sync alone lands. The agent's block is used VERBATIM from
        scripts/portal-build-agent.sh (never modified by this spec)."""
        agent_sync = agent_step2_sync_block()

        def agent_sync_script(repo_dir):
            return "\n".join([
                "#!/bin/bash",
                "set -uo pipefail",
                # The agent's block calls emit_failed on failure; stub it,
                # since the emission contract is Property 5's subject.
                'emit_failed() { echo "AGENT_EMIT_FAILED $*"; }',
                f"SOURCE_REF={shlex.quote(ref)}",
                f"cd {shlex.quote(repo_dir)} || exit 64",
                agent_sync,
            ])

        job = make_job(source_ref=ref)

        # Tree A: the preamble, then the agent's own sync.
        both = sandbox.clone_at_default_branch(
            os.path.join(sandbox.root, "tree-both"))
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               sandbox.origin):
            preamble = build_dispatcher.agent_preamble_commands(job, both)
        first = sandbox.run_bash(
            "#!/bin/bash\nset -uo pipefail\n" + "\n".join(preamble),
            name="preamble-both")
        assert first.returncode == 0, first.stdout + first.stderr
        second = sandbox.run_bash(agent_sync_script(both),
                                  name="agent-sync-both")
        assert second.returncode == 0, second.stdout + second.stderr

        # Tree B: the agent's own sync alone.
        alone = sandbox.clone_at_default_branch(
            os.path.join(sandbox.root, "tree-alone"))
        only = sandbox.run_bash(agent_sync_script(alone),
                                name="agent-sync-alone")
        assert only.returncode == 0, only.stdout + only.stderr

        assert sandbox.head(both) == sandbox.head(alone)
        assert sandbox.branch(both) == sandbox.branch(alone)
        # And the preamble's own repetition is a no-op too.
        repeated = sandbox.run_bash(
            "#!/bin/bash\nset -uo pipefail\n" + "\n".join(preamble),
            name="preamble-repeat")
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        assert sandbox.head(both) == sandbox.head(alone)


# ===========================================================================
# Property 5 — source failures are named and classified (Req 4.4)
# ===========================================================================

#: The two failure classes, with the exit code and the message phrase that
#: identify each. Restated here, independently of the implementation.
FAILURE_CLASSES = {
    "repository_unreachable": {
        "exit_code": 65,
        "phrase": "could not be obtained",
    },
    "ref_not_found": {
        "exit_code": 66,
        "phrase": "was not found",
    },
}

#: ``__CANARY__`` is replaced with a path inside the sandbox: if the ref
#: text is ever executed rather than treated as data, that file appears.
BEHAVIORAL_REFS = [
    "no-such-ref-42",
    "feature/typo",
    'bogus" ; touch __CANARY__ ; "',
]


def failure_lines_by_class(commands):
    """The generated failure guards, grouped by their ``kind=`` class."""
    grouped = {}
    pattern = re.compile(
        re.escape(build_source.SYNC_MARKER) + r" kind=([a-z_]+)")
    for line in commands:
        match = pattern.search(line)
        if not match:
            continue
        grouped.setdefault(match.group(1), []).append(line)
    return grouped


def run_failing_preamble(sandbox, failure_class, ref, aws="success",
                         name="failure", job_id="1ce014a3",
                         build_target=None):
    """Execute the pre-agent preamble on an input that fails in the given
    class, and return ``(repository, result)``.

    ``repository_unreachable`` is a repository path that does not exist;
    ``ref_not_found`` is the working local origin plus a ref it does not
    carry. Both are local paths — no network.
    """
    if failure_class == "repository_unreachable":
        repository = os.path.join(sandbox.root, "no-such-origin.git")
        assert not os.path.exists(repository)
    else:
        repository = sandbox.origin
    repo_dir = os.path.join(sandbox.root, f"tree-{name}",
                            "DefectDetectionApplication")
    job = make_job(source_ref=ref, job_id=job_id,
                   build_target=build_target or build_domain.TARGET_JP5)
    with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", repository):
        preamble = build_dispatcher.agent_preamble_commands(job, repo_dir)
    body = "#!/bin/bash\nset -uo pipefail\n" + "\n".join(preamble) + "\n"
    result = sandbox.run_bash(body, name=name, aws=aws)
    return repository, result


def sole_marker_line(result):
    lines = [line for line in result.stdout.splitlines()
             if line.startswith(build_source.SYNC_MARKER)]
    assert len(lines) == 1, result.stdout + result.stderr
    return lines[0]


def sole_emitted_detail(sandbox):
    """The single PutEvents payload's detail object, parsed."""
    calls = sandbox.aws_calls()
    assert len(calls) == 1, calls
    payloads = sandbox.aws_payloads()
    assert len(payloads) == 1, payloads
    entries = payloads[0]
    assert isinstance(entries, list) and len(entries) == 1, entries
    entry = entries[0]
    source, detail_type = agent_event_envelope()
    assert entry["Source"] == source
    assert entry["DetailType"] == detail_type
    assert entry["EventBusName"] == EVENT_BUS
    return json.loads(entry["Detail"])


class TestSourceFailuresAreNamedAndClassified:
    """``sourceUnobtainable`` never surfaces as a bare 127: each class has
    its own marker line, exit code and message, and one phase event whose
    detail shape matches the agent's ``emit_failed``."""

    @settings(max_examples=200, deadline=None)
    @given(repository=repositories, ref=refs, job_id=job_ids,
           build_target=build_targets)
    def test_the_two_classes_are_structurally_distinct(
            self, repository, ref, job_id, build_target):
        job = make_job(source_ref=ref, job_id=job_id,
                       build_target=build_target)
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               repository):
            preamble = build_dispatcher.agent_preamble_commands(job, REPO_DIR)
        text = "\n".join(preamble)

        grouped = failure_lines_by_class(preamble)
        assert set(grouped) == set(FAILURE_CLASSES), grouped

        messages = {}
        for failure_class, lines in grouped.items():
            expected_exit = FAILURE_CLASSES[failure_class]["exit_code"]
            for line in lines:
                # The marker line names BOTH values, by expansion.
                assert "repository=$REPO_URL" in line
                assert "ref=$SOURCE_REF" in line
                # The class-distinct exit code, and only it.
                assert f"exit {expected_exit}" in line
                other = [code["exit_code"] for name, code
                         in FAILURE_CLASSES.items()
                         if name != failure_class]
                for code in other:
                    assert f"exit {code}" not in line
                # One emission per guard, carrying the class and a message
                # naming both values.
                emit = re.search(
                    r"portal_source_sync_emit_failed (\S+) \"(.*?)\"; exit",
                    line)
                assert emit, line
                assert emit.group(1) == failure_class
                message = emit.group(2)
                assert "${REPO_URL}" in message
                assert "${SOURCE_REF}" in message
                messages.setdefault(failure_class, set()).add(message)

        # Class-distinct messages, and the documented phrases.
        assert len(messages["repository_unreachable"]) == 1
        assert len(messages["ref_not_found"]) == 1
        assert (messages["repository_unreachable"]
                != messages["ref_not_found"])
        for failure_class, expected in FAILURE_CLASSES.items():
            assert expected["phrase"] in next(iter(messages[failure_class]))

        # Never a bare 127, and no other exit code anywhere.
        assert "exit 127" not in text
        codes = {token.split(";")[0].strip()
                 for token in text.split("exit ")[1:]}
        assert codes == {"65", "66"}, codes

        # The emitted detail carries the classification, not a generic
        # agent-command failure.
        assert f'"error_kind":"{build_source.SYNC_ERROR_KIND}"' in text
        assert '"source_error":"%s"' in text
        assert '"phase":"failed"' in text

    @pytest.mark.parametrize("failure_class", sorted(FAILURE_CLASSES))
    @pytest.mark.parametrize("ref", BEHAVIORAL_REFS)
    def test_failure_is_classified_named_and_emitted_exactly_once(
            self, sandbox, failure_class, ref):
        """Executed: the classified exit code, one marker line naming both
        values, and exactly ONE PutEvents whose detail keys are the agent's
        emit_failed keys in the agent's order plus ``source_error``."""
        expected = FAILURE_CLASSES[failure_class]
        canary = os.path.join(sandbox.root, "canary")
        ref = ref.replace("__CANARY__", canary)
        repository, result = run_failing_preamble(
            sandbox, failure_class, ref, aws="success",
            name=f"{failure_class}", build_target=build_domain.TARGET_JP6)

        assert result.returncode == expected["exit_code"], (
            f"exit={result.returncode}\n{result.stdout}\n{result.stderr}")
        assert result.returncode != 127
        # No injection: the hostile ref is data, never a command.
        assert not os.path.exists(canary), result.stdout + result.stderr

        marker = sole_marker_line(result)
        assert f"kind={failure_class}" in marker
        assert f"repository={repository}" in marker
        assert f"ref={ref}" in marker

        detail = sole_emitted_detail(sandbox)
        assert list(detail) == agent_emit_failed_detail_keys() + [
            "source_error"]
        assert detail["build_job_id"] == "1ce014a3"
        assert detail["phase"] == "failed"
        assert detail["build_target"] == build_domain.TARGET_JP6
        assert detail["error_kind"] == "source_sync"
        assert detail["source_error"] == failure_class
        assert expected["phrase"] in detail["error_message"]
        # The message names BOTH the repository and the ref (Req 4.4).
        assert repository in detail["error_message"]
        assert ref in detail["error_message"]

    def test_the_two_classes_produce_different_codes_and_messages(
            self, sandbox):
        """Class distinction, executed side by side on the same ref."""
        observed = {}
        for failure_class in sorted(FAILURE_CLASSES):
            sandbox.clear_aws_records()
            repository, result = run_failing_preamble(
                sandbox, failure_class, "no-such-ref-42", aws="success",
                name=f"pair-{failure_class}")
            detail = sole_emitted_detail(sandbox)
            observed[failure_class] = (result.returncode,
                                       detail["source_error"],
                                       detail["error_message"],
                                       repository)

        codes = {value[0] for value in observed.values()}
        classes = {value[1] for value in observed.values()}
        messages = {value[2] for value in observed.values()}
        assert codes == {65, 66}
        assert classes == set(FAILURE_CLASSES)
        assert len(messages) == 2
        # Each names its own repository and the same requested ref.
        for failure_class, value in observed.items():
            assert value[3] in value[2]
            assert "no-such-ref-42" in value[2]

    @pytest.mark.parametrize("aws", ["failure", "absent"])
    @pytest.mark.parametrize("failure_class", sorted(FAILURE_CLASSES))
    def test_a_failing_or_absent_aws_never_changes_the_exit_code(
            self, sandbox, failure_class, aws):
        """The classified exit code is the contract; the event is the
        diagnosis. A refused PutEvents — or no CLI at all — degrades to a
        warning and leaves the exit code alone."""
        if aws == "absent":
            assert shutil.which(
                "aws", path=sandbox.path_for("absent")) is None, (
                "the sandbox PATH still resolves an aws CLI")

        expected = FAILURE_CLASSES[failure_class]["exit_code"]
        _, result = run_failing_preamble(
            sandbox, failure_class, "no-such-ref-42", aws=aws,
            name=f"{failure_class}-{aws}")

        assert result.returncode == expected, (
            f"exit={result.returncode}\n{result.stdout}\n{result.stderr}")
        assert result.returncode != 127
        assert f"kind={failure_class}" in sole_marker_line(result)
        # A failing CLI is still invoked exactly once; an absent one never.
        assert len(sandbox.aws_calls()) == (1 if aws == "failure" else 0)
