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
Environment-export and run-as-ubuntu structure unit tests for the build
dispatcher's generated runner command text
(``edge-cv-portal/backend/functions/build_dispatcher.py``).

The environment defects these exports close were all live-verified on the
root-run flow (region unset: SSM 06ec7c91; gdk off PATH: SSM c77c67ef /
job a235bef0; HOME unset under ``set -u``: job f13904b1; HOME unset for
git's global config: job 81bc94a3; and finally "ModuleNotFoundError: No
module named 'gdk'", job ff6d89fe — ubuntu's pip user-site is invisible
to root's python). The durable, user-approved fix is ONE environment
model in BOTH execution modes: the generated SSM/agent text and the
ephemeral user-data still run as root, but root does only a privileged
prologue, and the sync + agent (or sync + setup-build-server.sh) body
executes AS THE UBUNTU USER via a quoted-here-doc temp script and
``sudo -H -u ubuntu`` — matching the documented provisioning flow
(launch-arm64-build-server.sh: everything as ubuntu; gdk in
/home/ubuntu/.local/bin).

Asserted here:

* the ROOT PROLOGUE of the agent command contains the guarded ownership
  heal ``chown -R ubuntu:ubuntu <repo_dir>`` (repo_dir shlex-quoted,
  ``|| true``) before the build-user execution;
* the UBUNTU BODY contains, in order: the environment exports (HOME with
  the /home/ubuntu braced default, both region spellings, the PATH
  export putting ${HOME}/.local/bin first), then the Source_Sync
  preamble, then the agent invocation as its LAST line;
* the body is executed via ``sudo -H -u ubuntu bash`` and transported
  through a here-doc whose delimiter is QUOTED (no root-shell expansion
  of the body text);
* the ephemeral user-data runs the sync + setup body the same way, and
  ROOT writes the Bootstrap_Marker as the LAST statement, OUTSIDE the
  ubuntu body;
* region resolution (dispatcher env first, config_snapshot fallback),
  shlex quoting of the region value, and the region-less flavor keeping
  the HOME and PATH exports.

Safety: pure text generation — no AWS calls, no subprocesses, no network.

Run with ``--noconftest`` like the rest of the ``portal_builds`` suite::

    python3 -m pytest \\
        test/backend-test/portal_builds/test_region_export_unit.py \\
        --noconftest -q
"""
import os
import re
import shlex
import sys
import types
from unittest import mock

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
    """Stand-in for the Lambda layer module build_dispatcher imports."""
    module = types.ModuleType("shared_utils")
    module.log_audit_event = lambda **kwargs: None
    module.create_response = lambda status_code, body: {
        "statusCode": status_code, "body": body}
    module.get_user_from_event = lambda event: {
        "user_id": "region-export-test", "role": "PortalAdmin"}
    return module


sys.modules.setdefault("shared_utils", _fake_shared_utils())

import build_planner  # noqa: E402
import build_source  # noqa: E402
import build_dispatcher  # noqa: E402

#: The dispatcher Lambda's own region as this test process sees it.
DISPATCHER_REGION = os.environ["AWS_REGION"]

REPO_DIR = "/home/ubuntu/DefectDetectionApplication"
REPO_URL = "https://github.com/awslabs/DefectDetectionApplication"

#: The PATH export pinned as exact text: ${HOME}/.local/bin, which under
#: ``sudo -H -u ubuntu`` resolves to /home/ubuntu/.local/bin — the one
#: place the documented provisioning flow (setup-build-server.sh, pip3
#: --user, run as ubuntu) installs gdk, on dedicated servers and (now)
#: ephemeral runners alike. The braced default form is kept for `set -u`
#: safety; the old /root fallback belonged to the retired root-run flow.
PATH_EXPORT = 'export PATH="${HOME:-/home/ubuntu}/.local/bin:$PATH"'

#: The HOME export at the top of the UBUNTU BODY: a no-op under sudo -H
#: (which sets HOME=/home/ubuntu) and a `set -u`-safe default anywhere
#: else, so git's global config always has a HOME to write.
BODY_HOME_EXPORT = 'export HOME="${HOME:-/home/ubuntu}"'

#: The resilient HOME export at the top of the ROOT PROLOGUE (SSM
#: RunShellScript runs as root with HOME unset — srv-3f963f3b, job
#: 81bc94a3).
ROOT_HOME_EXPORT = 'export HOME="${HOME:-/root}"'

EXPECTED_EXPORTS = [
    BODY_HOME_EXPORT,
    f"export AWS_DEFAULT_REGION={shlex.quote(DISPATCHER_REGION)}",
    f"export AWS_REGION={shlex.quote(DISPATCHER_REGION)}",
    PATH_EXPORT,
]

SUDO_LINE = (f'  sudo -H -u {build_dispatcher.BUILD_USER} '
             f'bash "${build_dispatcher.RUN_SCRIPT_VAR}"')


def _job(source_ref=None, snapshot_extra=None, execution_mode="ephemeral"):
    snapshot = {"source_ref": source_ref}
    if snapshot_extra:
        snapshot.update(snapshot_extra)
    return {
        "build_job_id": "job-region",
        "build_target": "JP5",
        "execution_mode": execution_mode,
        "config_snapshot": snapshot,
    }


def _heredoc_open_index(lines):
    """The index of the here-doc opener that transports the ubuntu body."""
    for index, line in enumerate(lines):
        if line.startswith(f'cat > "${build_dispatcher.RUN_SCRIPT_VAR}" <<'):
            return index
    raise AssertionError("no here-doc opener in:\n" + "\n".join(lines))


def _split_generated(text):
    """(root_prologue_lines, body_lines, root_epilogue_lines) of a
    generated command/user-data text, split on the quoted here-doc."""
    lines = text.splitlines()
    open_at = _heredoc_open_index(lines)
    delimiter_token = lines[open_at].split("<<", 1)[1]
    # The delimiter is QUOTED: the root shell must not expand the body.
    assert delimiter_token.startswith("'") and \
        delimiter_token.endswith("'"), lines[open_at]
    delimiter = delimiter_token[1:-1]
    close_at = lines.index(delimiter, open_at + 1)
    return lines[:open_at], lines[open_at + 1:close_at], \
        lines[close_at + 1:]


def _first_index_containing(lines, needle):
    for index, line in enumerate(lines):
        if needle in line:
            return index
    raise AssertionError(f"{needle!r} not found in:\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Ephemeral user-data: root prologue + ubuntu body + root marker write
# ---------------------------------------------------------------------------

class TestEphemeralUserDataRunsTheBodyAsUbuntu:
    """The generated user-data keeps root for the privileged prologue
    (git install, parent-dir preparation, ownership heal) and the final
    Bootstrap_Marker write, while the sync + setup-build-server.sh body
    executes as ubuntu — so gdk installs to /home/ubuntu/.local/bin,
    matching the dedicated servers."""

    def _user_data(self, source_ref="main"):
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               REPO_URL):
            return build_dispatcher.runner_bootstrap_user_data(
                _job(source_ref=source_ref), repo_dir=REPO_DIR)

    def test_exports_precede_the_first_git_or_aws_use_in_the_body(self):
        _, body, _ = _split_generated(self._user_data())
        first_tool_use = min(
            index for index, line in enumerate(body)
            if "git " in line or "aws " in line)
        for export in EXPECTED_EXPORTS:
            assert body.index(export) < first_tool_use, body

    def test_body_is_executed_via_sudo_as_the_build_user(self):
        user_data = self._user_data()
        assert SUDO_LINE in user_data.splitlines(), user_data

    def test_marker_write_is_the_last_statement_and_outside_the_body(self):
        user_data = self._user_data()
        _, body, epilogue = _split_generated(user_data)
        statements = [line for line in user_data.splitlines()
                      if line.strip()]
        marker = build_planner.BOOTSTRAP_MARKER_PATH
        assert statements[-1] == \
            f"touch {shlex.quote(marker)} || true", user_data
        assert all(marker not in line for line in body), body
        assert epilogue[-1].startswith("touch "), epilogue
        assert user_data.endswith("\n"), user_data

    def test_sync_and_setup_run_inside_the_body(self):
        """The whole build-environment sequence — clone, checkout,
        setup-build-server.sh — is ubuntu's, not root's."""
        _, body, _ = _split_generated(self._user_data())
        text = "\n".join(body)
        assert "git clone" in text
        assert "git checkout --force" in text
        assert build_source.SETUP_SCRIPT_RELPATH in text

    def test_root_prologue_prepares_and_heals_the_clone_directory(self):
        user_data = self._user_data()
        prologue, _, _ = _split_generated(user_data)
        quoted = shlex.quote(REPO_DIR)
        assert f'mkdir -p "$(dirname {quoted})"' in prologue, prologue
        chown_parent = (
            f'chown {build_dispatcher.BUILD_USER}:'
            f'{build_dispatcher.BUILD_USER} "$(dirname {quoted})" '
            '2>/dev/null || true')
        assert chown_parent in prologue, prologue
        assert build_dispatcher.repo_ownership_heal_command(REPO_DIR) \
            in prologue, prologue

    def test_classified_sync_failures_propagate_before_the_marker(self):
        """A body that exited with the classified Source_Sync codes
        (65/66) aborts the bootstrap BEFORE the marker write — the
        pre-change semantics the readiness gate depends on (Req 6.3)."""
        user_data = self._user_data()
        _, _, epilogue = _split_generated(user_data)
        text = "\n".join(epilogue)
        gate = "\n".join(build_dispatcher.classified_sync_exit_commands())
        assert gate in text, epilogue
        assert text.index(gate) < text.index("touch "), epilogue

    def test_path_export_is_emitted_even_without_a_region(self):
        """The gdk defect is independent of region configuration: a
        user-data generated with NO resolvable region still extends
        PATH inside the body, before the first git/aws statement."""
        environ = {k: v for k, v in os.environ.items() if k != "AWS_REGION"}
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                                  REPO_URL):
            user_data = build_dispatcher.runner_bootstrap_user_data(
                _job(source_ref="main"), repo_dir=REPO_DIR)
        assert "export AWS_REGION" not in user_data
        _, body, _ = _split_generated(user_data)
        first_tool_use = min(
            index for index, line in enumerate(body)
            if "git " in line or "aws " in line)
        assert body.index(PATH_EXPORT) < first_tool_use, body

    def test_no_bare_home_reference_in_generated_user_data(self):
        """Regression guard for the `set -u` HOME abort (runner
        i-0298c6f74db03ec69, job f13904b1): cloud-init runs the
        user-data as root without HOME set and the script runs under
        `set -u`, so any bare $HOME expansion aborts the bootstrap
        before the marker is written. Every HOME expansion in the
        generated user-data must use the ${HOME:-...} default form."""
        user_data = self._user_data()
        # Unbraced $HOME (`$HOME`) is fatal under set -u without HOME.
        assert not re.search(r"\$HOME\b", user_data), user_data
        # Braced expansions must all carry a default (${HOME:-...}).
        for match in re.finditer(r"\$\{HOME([^}]*)\}", user_data):
            assert match.group(1).startswith(":-"), user_data

    def test_exported_value_is_the_dispatchers_region(self):
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               REPO_URL):
            user_data = build_dispatcher.runner_bootstrap_user_data(
                _job(), repo_dir=REPO_DIR)
        assert f"export AWS_REGION={shlex.quote(DISPATCHER_REGION)}" \
            in user_data.splitlines()

    def test_no_repo_url_still_generates_no_user_data(self):
        """The pre-baked-AMI mode (no BUILD_REPO_URL) is untouched."""
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL", ""):
            assert build_dispatcher.runner_bootstrap_user_data(_job()) == ""


# ---------------------------------------------------------------------------
# Agent command: root prologue (heals) + ubuntu body + propagating exit
# ---------------------------------------------------------------------------

class TestAgentCommandRunsTheBodyAsUbuntu:
    """The generated agent SSM command text — one command string, both
    execution modes — heals ownership at root context, then executes the
    exports + Source_Sync preamble + agent invocation as ubuntu."""

    def test_root_prologue_contains_the_quoted_tolerated_chown_heal(self):
        hostile_dir = "/tmp/x; rm -rf /"
        command = build_dispatcher.agent_command(_job(), hostile_dir)
        prologue, _, _ = _split_generated(command)
        heal = build_dispatcher.repo_ownership_heal_command(hostile_dir)
        assert heal in prologue, prologue
        assert f"chown -R ubuntu:ubuntu {shlex.quote(hostile_dir)}" in heal
        assert "|| true" in heal
        # The hostile text never appears unquoted.
        assert "; rm -rf /" not in heal.replace(shlex.quote(hostile_dir), "")

    def test_root_prologue_starts_with_the_resilient_home_export(self):
        command = build_dispatcher.agent_command(_job(), REPO_DIR)
        assert command.splitlines()[0] == ROOT_HOME_EXPORT, command

    def test_body_orders_exports_then_preamble_then_agent_last(self):
        """The ubuntu body is exactly: environment exports, then the
        Source_Sync preamble (whose failure surfacing itself calls `aws
        events put-events`, so the exports must come first), then the
        agent invocation as the LAST line (its exit status is the
        body's)."""
        job = _job(source_ref="feature/some-branch")
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               REPO_URL):
            command = build_dispatcher.agent_command(job, REPO_DIR)
            preamble = build_dispatcher.agent_preamble_commands(
                job, REPO_DIR)
        _, body, _ = _split_generated(command)

        assert preamble, "a selected ref must produce a sync preamble"
        # build-fleet-execution-failures task 7.1 reconciliation
        # (evidence rows 1/8): the spec-sanctioned dispatch preflight
        # guard runs AFTER the sync (which may legitimately create the
        # agent script) and BEFORE the invocation. Exports still come
        # first, the preamble still directly follows them, and the
        # invocation stays the last line.
        guard = build_dispatcher.preflight_guard_commands(REPO_DIR)
        assert body[:len(EXPECTED_EXPORTS)] == EXPECTED_EXPORTS, body
        assert body[len(EXPECTED_EXPORTS):-1] == \
            list(preamble) + guard, body
        assert body[-1].startswith(
            "bash " + shlex.quote(build_source.agent_script_path(REPO_DIR))
        ), body

    def test_no_ref_body_is_exports_then_the_bare_invocation(self):
        # build-fleet-execution-failures task 7.1 reconciliation
        # (evidence rows 1/8): a no-ref body additionally carries the
        # spec-sanctioned dispatch preflight guard between the exports
        # and the invocation. Everything else is unchanged: exports
        # first, the invocation last, no sync text anywhere.
        command = build_dispatcher.agent_command(_job(), REPO_DIR)
        _, body, _ = _split_generated(command)
        guard = build_dispatcher.preflight_guard_commands(REPO_DIR)
        assert body[:-1] == EXPECTED_EXPORTS + guard, body
        assert body[-1].startswith("bash "), body
        assert "git " not in command

    def test_body_is_executed_via_sudo_and_status_propagates(self):
        command = build_dispatcher.agent_command(_job(), REPO_DIR)
        lines = command.splitlines()
        assert SUDO_LINE in lines, command
        assert lines[-1] == f'exit "${build_dispatcher.RUN_STATUS_VAR}"', \
            command

    def test_both_execution_modes_generate_the_same_structure(self):
        """ONE environment model: agent_command no longer branches on the
        execution mode, so a dedicated job and an ephemeral job with the
        same inputs produce identical text."""
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               REPO_URL):
            ephemeral = build_dispatcher.agent_command(
                _job(source_ref="main"), REPO_DIR)
            dedicated = build_dispatcher.agent_command(
                dict(_job(source_ref="main"), execution_mode="dedicated",
                     server_id="srv-1"),
                REPO_DIR)
        assert ephemeral == dedicated

    def test_the_body_is_agent_run_body_verbatim(self):
        job = _job(source_ref="main")
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               REPO_URL):
            command = build_dispatcher.agent_command(job, REPO_DIR)
            run_body = build_dispatcher.agent_run_body(job, REPO_DIR)
        _, body, _ = _split_generated(command)
        assert "\n".join(body) == run_body


# ---------------------------------------------------------------------------
# Region resolution and quoting
# ---------------------------------------------------------------------------

class TestRegionResolutionAndQuoting:
    """dispatch_region / runner_env_export_commands: env first,
    config_snapshot fallback, shlex quoting, no-region drops the region
    exports only (the HOME and PATH exports are unconditional)."""

    def test_env_region_wins(self):
        with mock.patch.dict(os.environ, {"AWS_REGION": "eu-west-2"}):
            job = _job(snapshot_extra={"region": "ap-south-1"})
            assert build_dispatcher.dispatch_region(job) == "eu-west-2"

    def test_snapshot_fallback_when_env_absent(self):
        environ = {k: v for k, v in os.environ.items() if k != "AWS_REGION"}
        with mock.patch.dict(os.environ, environ, clear=True):
            job = _job(snapshot_extra={"region": "ap-south-1"})
            assert build_dispatcher.dispatch_region(job) == "ap-south-1"
            assert build_dispatcher.runner_env_export_commands(job) == [
                BODY_HOME_EXPORT,
                "export AWS_DEFAULT_REGION=ap-south-1",
                "export AWS_REGION=ap-south-1",
                PATH_EXPORT,
            ]

    def test_no_region_resolvable_still_emits_the_home_and_path_exports(self):
        """No region -> no region exports, but the HOME and PATH exports
        remain: the gdk and unset-HOME defects are independent of region
        configuration."""
        environ = {k: v for k, v in os.environ.items() if k != "AWS_REGION"}
        with mock.patch.dict(os.environ, environ, clear=True):
            job = _job()
            assert build_dispatcher.runner_env_export_commands(job) == [
                BODY_HOME_EXPORT, PATH_EXPORT]
            # The no-ref build-user body is exactly the HOME and PATH
            # exports, the additive dispatch preflight guard
            # (build-fleet-execution-failures task 7.1 reconciliation),
            # then the invocation, which stays its last line.
            body = build_dispatcher.agent_run_body(job, REPO_DIR)
            lines = body.splitlines()
            guard = build_dispatcher.preflight_guard_commands(REPO_DIR)
            assert lines[0] == BODY_HOME_EXPORT
            assert lines[1] == PATH_EXPORT
            assert lines[2:-1] == guard
            assert len(lines) == 3 + len(guard)
            assert lines[-1].startswith("bash ")
            assert body in build_dispatcher.agent_command(job, REPO_DIR)

    def test_hostile_fallback_value_is_shlex_quoted(self):
        hostile = "us-east-1; rm -rf /"
        environ = {k: v for k, v in os.environ.items() if k != "AWS_REGION"}
        with mock.patch.dict(os.environ, environ, clear=True):
            job = _job(snapshot_extra={"region": hostile})
            exports = build_dispatcher.runner_env_export_commands(job)
        assert exports == [
            BODY_HOME_EXPORT,
            f"export AWS_DEFAULT_REGION={shlex.quote(hostile)}",
            f"export AWS_REGION={shlex.quote(hostile)}",
            PATH_EXPORT,
        ]
        for statement in exports[1:3]:
            # The hostile text is contained in a single quoted token: the
            # statement splits into exactly `export` + one assignment.
            tokens = shlex.split(statement)
            assert tokens[0] == "export"
            assert len(tokens) == 2
            assert tokens[1].split("=", 1)[1] == hostile

    def test_non_string_snapshot_region_is_ignored(self):
        environ = {k: v for k, v in os.environ.items() if k != "AWS_REGION"}
        with mock.patch.dict(os.environ, environ, clear=True):
            job = _job(snapshot_extra={"region": 7})
            assert build_dispatcher.dispatch_region(job) == ""
            assert build_dispatcher.runner_env_export_commands(job) == [
                BODY_HOME_EXPORT, PATH_EXPORT]

    def test_export_texts_match_the_dispatchers_definitions(self):
        """The exact text asserted throughout this module IS the text the
        dispatcher emits (a drift here would make every assertion above
        vacuous)."""
        assert build_dispatcher.PATH_EXPORT_COMMAND == PATH_EXPORT
        assert build_dispatcher.BUILD_USER_HOME_EXPORT_COMMAND == \
            BODY_HOME_EXPORT
        assert build_dispatcher.HOME_EXPORT_COMMAND == ROOT_HOME_EXPORT
        assert build_dispatcher.BUILD_USER_HOME == "/home/ubuntu"

    def test_path_export_targets_the_build_users_local_bin(self):
        """gdk lives in /home/ubuntu/.local/bin (ubuntu-user pip3 --user
        installs — the documented provisioning flow, now used by both
        fleets). Under sudo -H the ${HOME:-/home/ubuntu} expansion
        resolves exactly there; the retired root-run /root fallback must
        be gone."""
        assert "${HOME:-/home/ubuntu}/.local/bin" in \
            build_dispatcher.PATH_EXPORT_COMMAND
        assert "/root" not in build_dispatcher.PATH_EXPORT_COMMAND


class TestHomeExportsUseTheBracedDefaultForm:
    """Every HOME export survives `set -u` with HOME unset (cloud-init,
    job f13904b1): braced ${HOME:-...} defaults only, and the body's HOME
    export is the FIRST generated body line so git's global config always
    has a HOME."""

    def test_body_home_export_is_the_first_export_line(self):
        for job in (_job(), _job(snapshot_extra={"region": "ap-south-1"})):
            exports = build_dispatcher.runner_env_export_commands(job)
            assert exports[0] == BODY_HOME_EXPORT

    def test_home_exports_use_the_braced_default_form(self):
        for export in (BODY_HOME_EXPORT, ROOT_HOME_EXPORT):
            assert not re.search(r"\$HOME\b", export)
            match = re.search(r"\$\{HOME([^}]*)\}", export)
            assert match and match.group(1).startswith(":-")
