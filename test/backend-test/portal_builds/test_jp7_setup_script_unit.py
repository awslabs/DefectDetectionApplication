# Copyright 2026 Amazon Web Services, Inc.
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
Unit tests for `setup-build-server.sh` structure and syntax (task 5.8 of
jp7-ephemeral-runner-provisioning).

**Validates: Requirements 2.4, 2.6, 2.8, 3.8**

The shared bootstrap script is the single seam through which both
execution modes (ephemeral runner bootstrap and dedicated fleet launch)
receive the noble (Ubuntu 24.04) host deltas. These tests statically
verify the fixed script's structure:

  - release detection near the top: the `/etc/os-release` read with the
    `lsb_release -rs` fallback, performed before any delta is keyed on it;
  - the docker-compose shim block (`exec docker compose "$@"` plus
    `chmod +x`) appears under the detected-24.04 branch ONLY, with the
    snap install preserved on the other branch;
  - `--break-system-packages` reaches exactly the three PEP 668-affected
    pip installs (awscli, botocore[crt], the GDK CLI) via the
    release-keyed `$PIP_BREAK_FLAG`, and no other command;
  - the script's `run_cmd` / error-summary conventions are kept for the
    new statements;
  - `bash -n setup-build-server.sh` exits zero.

The script is only ever READ as text — it is never executed (except the
no-execute syntax check `bash -n`). No AWS call, no network, no compute.

Run only this file, from the repository root:

    python3 -m pytest \\
        test/backend-test/portal_builds/test_jp7_setup_script_unit.py \\
        --noconftest -q
"""
import os
import re
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))

#: The one shared bootstrap seam both execution modes execute (Req 2.8).
_SETUP_SCRIPT_PATH = os.path.join(_REPO_ROOT, "setup-build-server.sh")


def _script_text():
    with open(_SETUP_SCRIPT_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _script_lines():
    return _script_text().splitlines()


def _code_lines():
    """(line_number, text) pairs with comment-only lines removed, so
    structural assertions are not satisfied by prose in comments."""
    pairs = []
    for number, line in enumerate(_script_lines()):
        if line.strip().startswith("#"):
            continue
        pairs.append((number, line))
    return pairs


def _first_index(lines, predicate, description):
    for number, line in lines:
        if predicate(line):
            return number
    raise AssertionError(f"not found in setup-build-server.sh: {description}")


def _docker_compose_section():
    """The docker-compose install section: from its banner echo to the
    banner of the next section, as (line_number, text) pairs."""
    lines = _script_lines()
    start = None
    end = None
    for number, line in enumerate(lines):
        if "Installing docker-compose" in line:
            start = number
        elif start is not None and line.startswith('echo "▶') \
                and number > start:
            end = number
            break
    assert start is not None, "docker-compose section banner not found"
    assert end is not None, "docker-compose section is not followed by " \
                            "another section banner"
    return [(number, lines[number]) for number in range(start, end)]


def _noble_branch_of_docker_compose_section():
    """Split the docker-compose section into the detected-24.04 branch
    and everything else in the section, as ((pairs), (pairs)).

    Tracks if/fi nesting so an inner `if run_cmd ...; then ... else
    add_error ... fi` inside the branch does not end it early: the
    branch runs from the release-keyed `if` to the `else`/`fi` at the
    SAME nesting depth."""
    section = _docker_compose_section()
    branch_start = None
    branch_end = None
    depth = 0
    for number, line in section:
        stripped = line.strip()
        if branch_start is None:
            if re.search(r'if\s+\[\s+"\$OS_RELEASE"\s+=\s+"24\.04"\s+\]',
                         line):
                branch_start = number
            continue
        # Inside the release-keyed branch: track nested if/fi pairs.
        if re.match(r"if\b", stripped) or re.search(r";\s*then\b.*\bif\b",
                                                    stripped):
            depth += 1
        elif re.match(r"fi\b", stripped):
            if depth == 0:
                branch_end = number
                break
            depth -= 1
        elif depth == 0 and re.match(r"else\b", stripped):
            branch_end = number
            break
    assert branch_start is not None, (
        "the docker-compose section has no branch keyed on "
        '[ "$OS_RELEASE" = "24.04" ]')
    assert branch_end is not None, (
        "the detected-24.04 docker-compose branch has no else branch "
        "(the snap install must be preserved for 22.04)")
    noble = [(number, line) for number, line in section
             if branch_start < number < branch_end]
    rest = [(number, line) for number, line in section
            if not (branch_start < number < branch_end)]
    return noble, rest


# ===========================================================================
# Release detection (Req 2.4, 2.6: the deltas are keyed on the detected
# release; Req 3.8: 22.04 stays on today's path)
# ===========================================================================

class TestReleaseDetection:

    def test_os_release_read_from_etc_os_release(self):
        """OS_RELEASE is derived from /etc/os-release's VERSION_ID."""
        pattern = re.compile(
            r'OS_RELEASE=\$\(\s*\.\s+/etc/os-release\b.*\$VERSION_ID')
        assert any(pattern.search(line) for _, line in _code_lines()), (
            "setup-build-server.sh must detect the host release by "
            "sourcing /etc/os-release and echoing $VERSION_ID into "
            "OS_RELEASE")

    def test_lsb_release_fallback_present(self):
        """When /etc/os-release yields nothing, `lsb_release -rs` is the
        fallback (the mechanism the Python 3.11 block already uses)."""
        assert any(re.search(r"OS_RELEASE=\$\(lsb_release -rs", line)
                   for _, line in _code_lines()), (
            "setup-build-server.sh must fall back to `lsb_release -rs` "
            "when the /etc/os-release read yields no VERSION_ID")

    def test_detection_precedes_every_use(self):
        """Detection happens near the top: before the flag derivation,
        and the flag derivation before any $PIP_BREAK_FLAG use or the
        docker-compose branch."""
        lines = _code_lines()
        detect_at = _first_index(
            lines, lambda l: re.search(r"OS_RELEASE=\$\(", l),
            "OS_RELEASE detection")
        flag_derived_at = _first_index(
            lines,
            lambda l: re.search(
                r"PIP_BREAK_FLAG='--break-system-packages'", l),
            "PIP_BREAK_FLAG derivation")
        first_flag_use = _first_index(
            lines,
            lambda l: "$PIP_BREAK_FLAG" in l,
            "$PIP_BREAK_FLAG use")
        compose_branch_at = _first_index(
            lines,
            lambda l: re.search(
                r'if\s+\[\s+"\$OS_RELEASE"\s+=\s+"24\.04"\s+\]', l),
            "release-keyed docker-compose branch")
        assert detect_at < flag_derived_at, (
            "OS_RELEASE must be detected before PIP_BREAK_FLAG is "
            "derived from it")
        assert flag_derived_at < first_flag_use, (
            "PIP_BREAK_FLAG must be derived before its first use")
        assert detect_at < compose_branch_at, (
            "OS_RELEASE must be detected before the docker-compose "
            "section branches on it")

    def test_flag_keyed_on_2404_and_empty_otherwise(self):
        """PIP_BREAK_FLAG defaults to empty and gains the PEP 668 flag
        only under the detected-24.04 condition (jammy's pip would
        reject the unknown option, Req 3.8)."""
        script = _script_text()
        assert re.search(r"^PIP_BREAK_FLAG=''$", script, re.MULTILINE), (
            "PIP_BREAK_FLAG must default to the empty string so 22.04 "
            "command lines are today's exactly")
        keyed = re.search(
            r'if\s+\[\s+"\$OS_RELEASE"\s+=\s+"24\.04"\s+\];\s+then\s*\n'
            r"\s*PIP_BREAK_FLAG='--break-system-packages'",
            script)
        assert keyed, (
            "PIP_BREAK_FLAG='--break-system-packages' must be assigned "
            'only under [ "$OS_RELEASE" = "24.04" ]')


# ===========================================================================
# docker-compose shim under the 24.04 branch only (Req 2.4, 2.6, 3.8)
# ===========================================================================

class TestDockerComposeShimPlacement:

    def test_shim_write_and_chmod_inside_noble_branch(self):
        """`exec docker compose "$@"` and its chmod +x live under the
        detected-24.04 branch of the docker-compose section."""
        noble, _ = _noble_branch_of_docker_compose_section()
        noble_text = "\n".join(line for _, line in noble)
        assert "exec docker compose" in noble_text, (
            "the docker-compose shim body (exec docker compose \"$@\") "
            "must be written under the detected-24.04 branch")
        assert "/usr/local/bin/docker-compose" in noble_text, (
            "the shim must be written to /usr/local/bin/docker-compose")
        assert re.search(r"chmod \+x /usr/local/bin/docker-compose",
                         noble_text), (
            "the shim must be made executable with chmod +x under the "
            "detected-24.04 branch")

    def test_shim_appears_nowhere_outside_noble_branch(self):
        """The shim exists exactly once, on the 24.04 branch (Req 2.8:
        one implementation, no divergent copy)."""
        noble, _ = _noble_branch_of_docker_compose_section()
        noble_numbers = {number for number, _ in noble}
        shim_lines = [number for number, line in _code_lines()
                      if "exec docker compose" in line]
        assert shim_lines, "shim body not found anywhere in the script"
        outside = [number for number in shim_lines
                   if number not in noble_numbers]
        assert not outside, (
            f"shim body found outside the detected-24.04 docker-compose "
            f"branch at line(s) {[n + 1 for n in outside]}")

    def test_snap_install_preserved_outside_noble_branch(self):
        """The 22.04 path still installs docker-compose via snap, and
        the snap install is unreachable from the 24.04 branch
        (Req 3.8)."""
        noble, rest = _noble_branch_of_docker_compose_section()
        noble_text = "\n".join(line for _, line in noble)
        rest_text = "\n".join(line for _, line in rest)
        assert "snap install docker-compose" in rest_text, (
            "the snap docker-compose install must remain on the non-"
            "24.04 branch of the docker-compose section")
        assert "snap install docker-compose" not in noble_text, (
            "the snap docker-compose install must be unreachable on the "
            "detected-24.04 branch")


# ===========================================================================
# PEP 668 flag reaches exactly the three affected installs (Req 2.4, 2.6)
# ===========================================================================

class TestPep668FlagScope:

    #: The three PEP 668-affected installs (bugfix clause 1.6 / Req 2.6).
    _AFFECTED_MARKERS = ("awscli", "botocore[crt]", "aws-greengrass-gdk-cli")

    def _pip_install_lines(self):
        return [(number, line) for number, line in _code_lines()
                if "pip3 install" in line]

    def test_exactly_three_pip_installs_carry_the_flag_variable(self):
        """Each of the three affected installs carries $PIP_BREAK_FLAG;
        no other pip install does."""
        pip_lines = self._pip_install_lines()
        flagged = [(number, line) for number, line in pip_lines
                   if "$PIP_BREAK_FLAG" in line]
        assert len(flagged) == 3, (
            f"expected exactly 3 pip install lines carrying "
            f"$PIP_BREAK_FLAG, found {len(flagged)}: "
            f"{[line.strip() for _, line in flagged]}")
        for marker in self._AFFECTED_MARKERS:
            assert any(marker in line for _, line in flagged), (
                f"the {marker} install must carry $PIP_BREAK_FLAG")
        unflagged_affected = [
            line.strip() for _, line in pip_lines
            if any(marker in line for marker in self._AFFECTED_MARKERS)
            and "$PIP_BREAK_FLAG" not in line]
        assert not unflagged_affected, (
            f"PEP 668-affected installs without the release-keyed flag: "
            f"{unflagged_affected}")

    def test_flag_variable_reaches_no_other_command(self):
        """$PIP_BREAK_FLAG appears on the three pip installs and
        nowhere else."""
        uses = [(number, line) for number, line in _code_lines()
                if "$PIP_BREAK_FLAG" in line]
        stray = [line.strip() for _, line in uses
                 if "pip3 install" not in line]
        assert not stray, (
            f"$PIP_BREAK_FLAG must reach only the three pip installs; "
            f"also found on: {stray}")

    def test_literal_flag_appears_only_in_the_keyed_assignment(self):
        """The literal --break-system-packages text exists exactly once
        in code — the release-keyed PIP_BREAK_FLAG assignment — so no
        command line carries it unconditionally (Req 3.8: jammy's pip
        would reject it)."""
        literal_lines = [(number, line) for number, line in _code_lines()
                         if "--break-system-packages" in line]
        assert len(literal_lines) == 1, (
            f"expected the literal flag only in the PIP_BREAK_FLAG "
            f"assignment, found {len(literal_lines)} occurrence(s): "
            f"{[line.strip() for _, line in literal_lines]}")
        number, line = literal_lines[0]
        assert re.search(r"PIP_BREAK_FLAG='--break-system-packages'",
                         line), (
            f"the literal flag at line {number + 1} is not the "
            f"release-keyed assignment: {line.strip()}")


# ===========================================================================
# run_cmd / error-summary conventions kept for the new statements (Req 2.8)
# ===========================================================================

class TestErrorHandlingConventions:

    def test_shim_statements_use_run_cmd(self):
        """The shim write and its chmod go through run_cmd like every
        other mutating statement in the script."""
        noble, _ = _noble_branch_of_docker_compose_section()
        shim_write = [line for _, line in noble
                      if "exec docker compose" in line]
        chmod = [line for _, line in noble
                 if "chmod +x /usr/local/bin/docker-compose" in line]
        assert shim_write and all("run_cmd" in line for line in shim_write), (
            f"the shim write must go through run_cmd: {shim_write}")
        assert chmod and all("run_cmd" in line for line in chmod), (
            f"the shim chmod must go through run_cmd: {chmod}")

    def test_noble_branch_reports_through_error_summary(self):
        """A failed shim install lands in the script's error summary via
        add_error, matching the snap branch's convention."""
        noble, rest = _noble_branch_of_docker_compose_section()
        noble_text = "\n".join(line for _, line in noble)
        rest_text = "\n".join(line for _, line in rest)
        assert "add_error" in noble_text, (
            "the detected-24.04 shim branch must report failure through "
            "add_error (the script's error-summary convention)")
        snap_lines = [line for line in rest_text.splitlines()
                      if "snap install docker-compose" in line]
        assert snap_lines and all(
            "run_cmd" in line and "add_error" in line
            for line in snap_lines), (
            f"the preserved snap branch must keep its run_cmd/add_error "
            f"convention: {snap_lines}")

    def test_flagged_pip_installs_keep_run_cmd_and_warnings(self):
        """The three flagged pip installs keep their existing
        run_cmd + add_warning shape (the flag is the only delta)."""
        pip_lines = [(number, line) for number, line in _code_lines()
                     if "pip3 install" in line
                     and "$PIP_BREAK_FLAG" in line]
        assert len(pip_lines) == 3
        for number, line in pip_lines:
            assert "run_cmd" in line, (
                f"flagged pip install at line {number + 1} must go "
                f"through run_cmd: {line.strip()}")
            assert "add_warning" in line, (
                f"flagged pip install at line {number + 1} must keep "
                f"its add_warning fallback: {line.strip()}")


# ===========================================================================
# Syntax (Req 3.8: the edited script still parses)
# ===========================================================================

class TestScriptSyntax:

    def test_bash_no_exec_syntax_check_exits_zero(self):
        """`bash -n setup-build-server.sh` exits zero. -n parses without
        executing anything — the only way the script is ever 'run' in
        this suite."""
        completed = subprocess.run(
            ["bash", "-n", _SETUP_SCRIPT_PATH],
            capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, (
            f"bash -n reported a syntax error:\n{completed.stderr}")
