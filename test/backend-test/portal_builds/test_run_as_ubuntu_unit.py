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
Run-as-ubuntu unit tests for the build dispatch, BOTH execution modes
(``edge-cv-portal/backend/functions/build_dispatcher.py``).

Root cause (user-confirmed against edge-cv-portal/
launch-arm64-build-server.sh, the documented setup contract): the build
environment is designed entirely FOR THE UBUNTU USER (SSH as ubuntu,
clone, ./setup-build-server.sh) — the clone, the gdk binary
(/home/ubuntu/.local/bin/gdk) and gdk's Python module (ubuntu's pip
user-site) all belong to ubuntu. The portal's SSM RunShellScript and the
ephemeral cloud-init user-data run as root, which produced a chain of
live failures ending in "ModuleNotFoundError: No module named 'gdk'"
(job ff6d89fe): root's python cannot see ubuntu's user-site packages.
The durable, user-approved fix ("the build should run as ubuntu, root
is not needed") is ONE environment model: root keeps only the
privileged prologue (ownership heals, parent-dir preparation, the
Bootstrap_Marker write), and every sync/setup/agent body executes as
ubuntu via a quoted-here-doc temp script and ``sudo -H -u ubuntu`` —
on dedicated servers AND ephemeral runners.

Asserted here:

* agent command structure, mode-independent, in order: the resilient
  root HOME export, the guarded ownership heals (chown -R ubuntu:ubuntu
  on the clone, only when the directory exists; chown on
  /var/lock/dda-build.lock, only when the file exists — job 01b18948's
  exit-75 defer loop; the docker-socket access heal — job c828f479),
  then the ``sudo -H -u ubuntu`` build-user execution of the body
  (which carries its own exports, so they survive sudo), with the
  body's exit status — including the classified Source_Sync codes
  65/66 — propagated as the command's;
* sync-before-agent ordering inside the wrapped body, and the agent
  invocation's argument contract untouched by the wrapper (Req 7.6);
* the ephemeral user-data runs its sync + setup body the same way,
  with root creating/chowning the parent directory first and writing
  the Bootstrap_Marker as the LAST statement, outside the body;
* a classified sync failure inside the user-data body (65/66)
  propagates out BEFORE the marker write, while any other nonzero
  setup status still reaches the marker (Req 6.3/6.4 semantics
  preserved);
* injection safety: operator-derived values reach the generated text
  only via shlex-quoted assignments; the here-doc transport delimiter
  can never collide with a body line; the repo directory in the heal
  and the parent preparation is shlex-quoted;
* executed (unprivileged, no AWS, no network): the wrapper propagates
  the body's exit status verbatim and delivers the body
  byte-identically.

Safety: text generation plus a few local bash subprocesses running inert
bodies — no AWS calls, no git remotes, no builds.

Run with ``--noconftest`` like the rest of the ``portal_builds`` suite::

    python3 -m pytest \\
        test/backend-test/portal_builds/test_run_as_ubuntu_unit.py \\
        --noconftest -q
"""
import os
import shlex
import subprocess
import sys
import types
from unittest import mock

import pytest

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

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")
    module.log_audit_event = lambda **kwargs: None
    module.create_response = lambda status_code, body: {
        "statusCode": status_code, "body": body}
    module.get_user_from_event = lambda event: {
        "user_id": "run-as-ubuntu-test", "role": "PortalAdmin"}
    return module


sys.modules.setdefault("shared_utils", _fake_shared_utils())

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_source  # noqa: E402
import build_dispatcher  # noqa: E402

REPO_DIR = "/home/ubuntu/DefectDetectionApplication"
REPO_URL = "https://github.com/awslabs/DefectDetectionApplication"
EVENT_BUS = build_dispatcher.BUILD_EVENT_BUS
JOB_ID = "0a1b2c3d"
SOURCE_REF = "feature/portal-build-fleet-and-workflow-gates"

SUDO_LINE = (f'  sudo -H -u {build_dispatcher.BUILD_USER} '
             f'bash "${build_dispatcher.RUN_SCRIPT_VAR}"')
EXIT_LINE = f'exit "${build_dispatcher.RUN_STATUS_VAR}"'

MODES = (build_domain.EXECUTION_MODE_DEDICATED,
         build_domain.EXECUTION_MODE_EPHEMERAL)


def _job(execution_mode, source_ref=None):
    job = {
        "build_job_id": JOB_ID,
        "build_target": build_domain.TARGET_JP5,
        "execution_mode": execution_mode,
        "config_snapshot": {"source_ref": source_ref,
                            "max_runtime_hours": 4},
    }
    if execution_mode == build_domain.EXECUTION_MODE_DEDICATED:
        job["server_id"] = "srv-1"
    return job


def _expected_invocation(job, repo_dir):
    """The agent invocation constructed independently of the module's own
    helpers: the byte-exact argument contract (design §5, Req 7.6)."""
    parts = [
        "bash",
        shlex.quote(build_source.agent_script_path(repo_dir)),
        shlex.quote(f"BUILD_JOB_ID={job['build_job_id']}"),
        shlex.quote(f"BUILD_TARGET={job['build_target']}"),
        shlex.quote(f"EVENT_BUS={EVENT_BUS}"),
    ]
    ref = (job.get("config_snapshot") or {}).get("source_ref")
    if ref:
        parts.append(shlex.quote(f"SOURCE_REF={ref}"))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Agent command: root heals + sudo -H -u ubuntu wrapper, both modes
# ---------------------------------------------------------------------------

class TestAgentCommandRunsAsUbuntu:

    @pytest.mark.parametrize("mode", MODES)
    def test_structure_home_heals_sudo_exit_in_order(self, mode):
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", REPO_URL):
            command = build_dispatcher.agent_command(
                _job(mode, SOURCE_REF), REPO_DIR)
            body = build_dispatcher.agent_run_body(
                _job(mode, SOURCE_REF), REPO_DIR)
        lines = command.splitlines()

        # Root context first: the resilient HOME export, verbatim, on top.
        assert lines[0] == build_dispatcher.HOME_EXPORT_COMMAND, command

        heal = build_dispatcher.repo_ownership_heal_command(REPO_DIR)

        # Heal (root) before the build-user execution, which is before
        # the propagating exit — and the exit line is LAST.
        assert command.index(heal) < command.index(SUDO_LINE) \
            < command.index(EXIT_LINE), command
        assert lines[-1] == EXIT_LINE, command

        # The body is transported VERBATIM (quoted here-doc: no outer
        # expansion, no re-quoting of the shlex-disciplined text).
        assert body in command

    def test_both_modes_generate_identical_text(self):
        """ONE environment model: agent_command no longer branches on the
        execution mode — including unrecognized modes."""
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", REPO_URL):
            commands = [
                build_dispatcher.agent_command(_job(mode, SOURCE_REF),
                                               REPO_DIR)
                for mode in MODES + ("mystery",)
            ]
        assert commands[0] == commands[1] == commands[2]

    def test_heal_is_guarded_on_directory_existence_and_tolerated(self):
        heal = build_dispatcher.repo_ownership_heal_command(REPO_DIR)
        assert heal == (
            f"if [ -d {REPO_DIR} ]; then "
            f"chown -R ubuntu:ubuntu {REPO_DIR} "
            "2>/dev/null || true; fi")

    def test_lock_heal_sits_between_the_repo_heal_and_the_sudo_wrapper(self):
        """Live evidence (job 01b18948, SSM 9602a0e8, srv-3f963f3b): the
        interim ROOT-run attempts left /var/lock/dda-build.lock owned by
        root, so the ubuntu-run agent's `exec 9>"$LOCK_FILE"` failed
        with "Permission denied" / "flock: 9: Bad file descriptor" and
        took the held-lock defer path (exit 75). The command heals the
        lock's ownership in root context — alongside the repo heal,
        before the sudo wrapper."""
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", REPO_URL):
            command = build_dispatcher.agent_command(
                _job(MODES[0], SOURCE_REF), REPO_DIR)
        repo_heal = build_dispatcher.repo_ownership_heal_command(REPO_DIR)
        lock_heal = build_dispatcher.lock_ownership_heal_command()
        assert command.index(repo_heal) < command.index(lock_heal) \
            < command.index(SUDO_LINE), command

    def test_lock_heal_exact_text_guarded_on_file_existence(self):
        """Pinned byte-exact: guarded on `[ -f ... ]` (the agent creates
        the lock ubuntu-owned when absent), non-recursive, silenced and
        tolerated. The path mirrors the agent's LOCK_FILE, pinned as
        AGENT_LOCK_FILE by the task-2 preservation tests."""
        assert build_dispatcher.BUILD_LOCK_FILE == \
            "/var/lock/dda-build.lock"
        assert build_dispatcher.lock_ownership_heal_command() == (
            "if [ -f /var/lock/dda-build.lock ]; then "
            "chown ubuntu:ubuntu /var/lock/dda-build.lock "
            "2>/dev/null || true; fi")

    def test_docker_socket_heal_sits_before_the_sudo_wrapper(self):
        """Live evidence (job c828f479, srv-3f963f3b): the ubuntu-run
        gdk build failed with "permission denied while trying to connect
        to the docker API at unix:///var/run/docker.sock" — ubuntu IS in
        the docker group, but the socket's group resets to root on
        daemon restart, defeating the membership. The command heals the
        socket in root context — after the other heals, before the sudo
        wrapper."""
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", REPO_URL):
            command = build_dispatcher.agent_command(
                _job(MODES[0], SOURCE_REF), REPO_DIR)
        repo_heal = build_dispatcher.repo_ownership_heal_command(REPO_DIR)
        lock_heal = build_dispatcher.lock_ownership_heal_command()
        socket_heal = build_dispatcher.docker_socket_heal_command()
        assert command.index(repo_heal) < command.index(lock_heal) \
            < command.index(socket_heal) < command.index(SUDO_LINE), command

    def test_docker_socket_heal_exact_text_guarded_on_socket_existence(
            self):
        """Pinned byte-exact: guarded on `[ -S ... ]` (no socket means
        no daemon — nothing to grant), `chgrp docker` first (the tight
        fix), `chmod 666` as the documented setup-script fallback,
        silenced and tolerated."""
        assert build_dispatcher.DOCKER_SOCKET_PATH == \
            "/var/run/docker.sock"
        assert build_dispatcher.docker_socket_heal_command() == (
            "if [ -S /var/run/docker.sock ]; then "
            "chgrp docker /var/run/docker.sock 2>/dev/null || "
            "chmod 666 /var/run/docker.sock 2>/dev/null || true; fi")

    def test_heal_shlex_quotes_a_hostile_repo_dir(self):
        hostile = "/tmp/x; rm -rf /"
        heal = build_dispatcher.repo_ownership_heal_command(hostile)
        assert shlex.quote(hostile) in heal
        # The hostile text never appears unquoted.
        assert "; rm -rf /" not in heal.replace(shlex.quote(hostile), "")

    def test_env_exports_survive_sudo_because_the_body_re_emits_them(self):
        """The wrapper needs no `env` passthrough: the region/PATH/HOME
        exports are IN the body, re-established inside the build-user
        shell (sudo -H additionally sets HOME=/home/ubuntu, where the
        provisioning flow's gdk lives)."""
        job = _job(MODES[0], SOURCE_REF)
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", REPO_URL):
            body = build_dispatcher.agent_run_body(job, REPO_DIR)
        exports = build_dispatcher.runner_env_export_commands(job)
        assert body.splitlines()[:len(exports)] == exports

    def test_sync_precedes_the_agent_inside_the_wrapped_body(self):
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", REPO_URL):
            body = build_dispatcher.agent_run_body(
                _job(MODES[0], SOURCE_REF), REPO_DIR)
        checkout_at = body.index("git checkout --force")
        agent_at = body.index(
            shlex.quote(build_source.agent_script_path(REPO_DIR)))
        assert checkout_at < agent_at, body

    @pytest.mark.parametrize("mode", MODES)
    def test_agent_argument_contract_is_untouched_by_the_wrapper(
            self, mode):
        """Req 7.6: the body's last line IS the documented invocation —
        the wrapper changed WHO runs the agent, never WHAT it is
        passed."""
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", REPO_URL):
            body = build_dispatcher.agent_run_body(
                _job(mode, SOURCE_REF), REPO_DIR)
        invocation = _expected_invocation(_job(mode, SOURCE_REF), REPO_DIR)
        assert body.splitlines()[-1] == invocation

    def test_failure_guard_codes_reach_the_command_status(self):
        """The classified Source_Sync exit codes (65/66) and the
        PORTAL_SOURCE_SYNC_FAILED emission live in the body, which runs
        as ubuntu; the wrapper's final line propagates the body's status
        verbatim, so the codes still surface as the SSM command status."""
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", REPO_URL):
            command = build_dispatcher.agent_command(
                _job(MODES[0], SOURCE_REF), REPO_DIR)
            body = build_dispatcher.agent_run_body(
                _job(MODES[0], SOURCE_REF), REPO_DIR)
        assert f"exit {build_source.EXIT_REPO_UNREACHABLE}" in body
        assert f"exit {build_source.EXIT_REF_NOT_FOUND}" in body
        assert build_source.SYNC_MARKER in body
        assert command.splitlines()[-1] == EXIT_LINE


# ---------------------------------------------------------------------------
# Ephemeral user-data: root prologue + ubuntu body + root marker write
# ---------------------------------------------------------------------------

class TestEphemeralUserDataRunsAsUbuntu:

    def _user_data(self, source_ref=SOURCE_REF, repo_dir=REPO_DIR):
        job = _job(build_domain.EXECUTION_MODE_EPHEMERAL, source_ref)
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", REPO_URL):
            return build_dispatcher.runner_bootstrap_user_data(
                job, repo_dir=repo_dir)

    def test_body_is_exports_plus_sync_plus_setup_run_via_sudo(self):
        """The build-user body is exactly the environment exports plus
        the Sync_Generator's bootstrap sequence WITHOUT its final marker
        write, and it is executed via sudo -H -u ubuntu — so gdk's pip3
        --user install lands in /home/ubuntu/.local, matching the
        dedicated servers."""
        job = _job(build_domain.EXECUTION_MODE_EPHEMERAL, SOURCE_REF)
        user_data = self._user_data()
        commands = build_source.bootstrap_commands(
            REPO_URL, REPO_DIR, SOURCE_REF)
        body = "\n".join(
            list(build_dispatcher.runner_env_export_commands(job))
            + commands[:-1])
        assert body in user_data
        assert SUDO_LINE in user_data.splitlines()
        assert build_source.SETUP_SCRIPT_RELPATH in body

    def test_root_creates_and_hands_over_the_parent_directory(self):
        """Root prepares the clone's parent (mkdir -p + chown to ubuntu,
        tolerated) and heals a pre-existing root-owned clone BEFORE the
        build-user body, so the ubuntu-run git clone can create the
        tree."""
        user_data = self._user_data()
        prepare = build_dispatcher.repo_parent_prepare_commands(REPO_DIR)
        heal = build_dispatcher.repo_ownership_heal_command(REPO_DIR)
        for statement in prepare + [heal]:
            assert statement in user_data, (statement, user_data)
            assert user_data.index(statement) < user_data.index(SUDO_LINE)

    def test_parent_preparation_shlex_quotes_a_hostile_repo_dir(self):
        hostile = "/tmp/x; rm -rf /"
        for statement in \
                build_dispatcher.repo_parent_prepare_commands(hostile):
            assert shlex.quote(hostile) in statement
            assert "; rm -rf /" not in statement.replace(
                shlex.quote(hostile), "")

    def test_marker_write_is_root_run_last_and_outside_the_body(self):
        """The Bootstrap_Marker write stays the LAST statement of the
        user-data, run by ROOT after the build-user body — unchanged
        readiness-gate semantics (Req 6.1-6.4)."""
        user_data = self._user_data()
        statements = [line for line in user_data.splitlines()
                      if line.strip()]
        marker = build_planner.BOOTSTRAP_MARKER_PATH
        assert statements[-1] == f"touch {shlex.quote(marker)} || true"
        # Outside the body: after the sudo execution and the status
        # capture, and the marker path appears exactly once.
        assert user_data.index(SUDO_LINE) < user_data.index(statements[-1])
        assert user_data.count(marker) == 1

    def test_classified_sync_exit_gates_the_marker(self):
        """The 65/66 propagation sits between the body execution and the
        marker write: a failed sync never writes the marker (Req 6.3),
        while a nonzero setup-inner-step status still does (Req 6.4)."""
        user_data = self._user_data()
        gate = "\n".join(build_dispatcher.classified_sync_exit_commands())
        assert gate in user_data
        assert user_data.index(SUDO_LINE) < user_data.index(gate) \
            < user_data.index("touch ")

    def test_heredoc_delimiter_is_quoted_in_the_user_data(self):
        """The body is transported through a QUOTED here-doc: the root
        shell must not expand the body's $-laden, shlex-disciplined
        text."""
        user_data = self._user_data()
        opener = next(
            line for line in user_data.splitlines()
            if line.startswith(
                f'cat > "${build_dispatcher.RUN_SCRIPT_VAR}" <<'))
        delimiter_token = opener.split("<<", 1)[1]
        assert delimiter_token.startswith("'") and \
            delimiter_token.endswith("'"), opener

    def test_no_repo_url_still_generates_no_user_data(self):
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", ""):
            assert build_dispatcher.runner_bootstrap_user_data(
                _job(build_domain.EXECUTION_MODE_EPHEMERAL)) == ""


# ---------------------------------------------------------------------------
# The build-user wrapper itself: transport fidelity and status propagation
# (executed locally, unprivileged, inert bodies — no AWS, no git, no build)
# ---------------------------------------------------------------------------

def _run_wrapped(body):
    script = "\n".join(
        build_dispatcher.run_as_build_user_commands(body)
        + [EXIT_LINE])
    return subprocess.run(["bash", "-c", script],
                          capture_output=True, text=True)


class TestBuildUserWrapperMechanics:

    def test_wrapper_propagates_the_body_exit_status_verbatim(self):
        for code in (0, 7, build_source.EXIT_REPO_UNREACHABLE,
                     build_source.EXIT_REF_NOT_FOUND):
            result = _run_wrapped(f"exit {code}")
            assert result.returncode == code, (code, result.stderr)

    def test_wrapper_delivers_the_body_byte_identically(self):
        """The quoted here-doc transports the body verbatim: no outer
        expansion, no word splitting — a body echoing quoted, $-laden
        text produces exactly that text."""
        canary = "canary $HOME `id` $(id) 'single' \"double\""
        result = _run_wrapped(f"printf '%s\\n' {shlex.quote(canary)}")
        assert result.returncode == 0, result.stderr
        assert result.stdout == canary + "\n"

    def test_heredoc_delimiter_never_collides_with_a_body_line(self):
        base = build_dispatcher.RUN_HEREDOC_DELIMITER
        body = "\n".join([base, base + "_", "echo ok", "exit 3"])
        delimiter = build_dispatcher.build_user_heredoc_delimiter(body)
        assert delimiter not in body.splitlines()
        # And the wrapper still executes the WHOLE body (the delimiter
        # lines are inert words to bash), propagating its status.
        result = _run_wrapped(body)
        assert result.returncode == 3, result.stderr
        assert "ok" in result.stdout

    def test_unprivileged_execution_takes_the_direct_arm(self):
        """Run without root the wrapper executes the body directly (the
        sudo arm is gated on `id -u` = 0), which is how the property
        tests execute generated commands end to end."""
        assert os.geteuid() != 0, "this suite must not run as root"
        result = _run_wrapped('printf "%s\\n" "user=$(id -u)"')
        assert result.returncode == 0, result.stderr
        assert f"user={os.geteuid()}" in result.stdout

    def test_classified_exit_gate_propagates_65_and_66_only(self):
        """Executed: the user-data's classified-sync gate exits for 65
        and 66 (marker never reached) and falls through for 0 and for a
        setup-style nonzero status (marker still reached)."""
        gate = "\n".join(build_dispatcher.classified_sync_exit_commands())
        for status, expect_marker in ((0, True), (1, True),
                                      (65, False), (66, False)):
            script = "\n".join([
                "set -uo pipefail",
                *build_dispatcher.run_as_build_user_commands(
                    f"exit {status}"),
                gate,
                'echo MARKER_REACHED',
            ])
            result = subprocess.run(["bash", "-c", script],
                                    capture_output=True, text=True)
            reached = "MARKER_REACHED" in result.stdout
            assert reached == expect_marker, (status, result.stdout,
                                              result.stderr)
            assert result.returncode == (0 if expect_marker else status)
