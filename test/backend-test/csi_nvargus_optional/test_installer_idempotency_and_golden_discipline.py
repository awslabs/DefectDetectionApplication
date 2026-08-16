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
"""Installer idempotency, golden discipline, and remaining unit assertions
(Task 4.3) for csi-nvargus-optional.

Property 5: Fix Checking — Golden Rebaseline Discipline (plus Property 1's
installer-idempotency leg).

Three sections:

1. **Installer idempotency (behavioral)** — run the REAL
   ``install_nvidia_csi_service.sh`` with stub binaries on PATH (the
   ``deploy_reliability`` stub pattern shared with the exploration suite):
   ``systemctl`` records its argv (the transcript under test), ``cp`` records
   its argv (the copy leg), ``jq``/``apt-get``/``mkdir``/``chmod`` are
   recording/no-op stubs so the real script runs to completion without
   touching /aws_dda, /etc/systemd, or the package manager. The opt-in
   marker is driven through the installer's ``CSI_OPTIN_MARKER`` env
   override (a test hook added in task 4.3 — production callers never set
   it, so the default is the hardcoded provisioning path). Checks: two
   no-marker runs each converge to capture-service-disabled +
   watchdog-timer-enabled with equivalent transcripts (the 2.3 repeated-
   deployment invariant); a marker-present run executes the capture install
   path exactly as the unfixed script did (jq check satisfied without
   apt-get, copy, daemon-reload, enable, restart); the marker-unreadable
   and watchdog-artifacts-missing edge cases are tolerated (exit 0, loud
   WARNING for the missing artifacts, capture-service path still handled).

2. **Golden discipline (2.4 / Property 5)** —
   ``dependency_baseline_setup_station.txt`` matches the fixed
   ``setup_station.sh`` under the security gate's own normalization (every
   line byte-identical; the unique ``$PYTHON311 ... --force-reinstall
   requests==`` pin line may differ only in its version token — the one
   allowance ``test_preservation_dependency_setup_station.py`` grants);
   the ``dependency_baseline_unpinned_py36.json`` entries still resolve
   verbatim at their recorded line numbers (656, 680); and the security
   preservation suite FILES are unmodified (git diff vs HEAD — no
   weakened, edited, or deleted gate tests).

3. **setup_station block units (text-level ONLY)** — the full provisioning
   script cannot be safely executed host-side, so the CSI opt-in block is
   asserted textually: both branches present, exact marker path,
   ``run_cmd``/``add_warning`` tolerant style, ``list-unit-files`` guard,
   and the block strictly APPENDED (its first line comes after the unfixed
   file's last line, 1625 — the placement that keeps the unpinned-py36
   golden's recorded line numbers valid).

Honesty guard: no gst/Argus/CUDA/real-systemd execution — stub-binary
behavioral runs of the installer plus text/hash/git-level assertions only.

Validates: Requirements 2.3, 2.4, 3.6
"""
import os
import re
import stat
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

HOST_SCRIPTS_DIR = os.path.join(REPO_ROOT, "src", "host_scripts")
INSTALLER = os.path.join(HOST_SCRIPTS_DIR, "install_nvidia_csi_service.sh")
SETUP_STATION = os.path.join(REPO_ROOT, "station_install", "setup_station.sh")

BASELINES_DIR = os.path.join(REPO_ROOT, "test", "backend-test", "security",
                             "baselines")
SETUP_STATION_GOLDEN = os.path.join(BASELINES_DIR,
                                    "dependency_baseline_setup_station.txt")
UNPINNED_PY36_BASELINE = os.path.join(BASELINES_DIR,
                                      "dependency_baseline_unpinned_py36.json")
PRESERVATION_SUITE_DIR = os.path.join("test", "backend-test", "security",
                                      "preservation")

#: The unfixed setup_station.sh line count (task 2's recorded baseline) —
#: the CSI opt-in block must start strictly AFTER this line.
UNFIXED_SETUP_STATION_LINES = 1625

#: The one line of setup_station.sh allowed to differ from its golden, and
#: only in its version token — the same allowance the security gate's
#: test_preservation_dependency_setup_station.py grants (its F1 pin site).
_F1_PIN_SUBSTRING = "$PYTHON311 -m pip install --force-reinstall requests=="
_F1_VERSION_TOKEN = re.compile(r"requests==[0-9][0-9a-zA-Z.\-]*")

WATCHDOG_ARTIFACTS = ("nvargus_error89_watchdog.sh",
                      "nvargus-error89-watchdog.service",
                      "nvargus-error89-watchdog.timer")


def _read(path):
    assert os.path.isfile(path), (
        "expected file does not exist: {}".format(path))
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Stub harness: the REAL installer, stub binaries on PATH, transcripts on
# disk. Same pattern as the exploration suite's behavioral leg, extended
# with a cp/apt-get transcript and the CSI_OPTIN_MARKER env override.
# ---------------------------------------------------------------------------

_STUB_RECORDING_TEMPLATE = """\
#!/usr/bin/env bash
echo "$@" >> "{log}"
exit 0
"""

_STUB_SILENT = """\
#!/usr/bin/env bash
exit 0
"""


class _InstallerHarness:
    """One stub-bin environment; each ``run`` records fresh transcripts."""

    def __init__(self, tmp_path, name):
        self.dir = tmp_path / name
        self.bin_dir = self.dir / "bin"
        self.bin_dir.mkdir(parents=True)
        self._install("jq", _STUB_SILENT)
        for stub in ("mkdir", "chmod"):
            self._install(stub, _STUB_SILENT)

    def _install(self, name, content):
        stub = self.bin_dir / name
        stub.write_text(content)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
                   | stat.S_IXOTH)

    def run(self, run_name, marker_path, installer=INSTALLER):
        """Run the REAL installer once; return (result, systemctl transcript,
        cp transcript, apt-get transcript) with per-run log files."""
        logs = {}
        for cmd in ("systemctl", "cp", "apt-get"):
            log = self.dir / "{}_{}.log".format(run_name, cmd)
            logs[cmd] = log
            self._install(cmd, _STUB_RECORDING_TEMPLATE.format(log=str(log)))
        env = dict(os.environ)
        env["PATH"] = str(self.bin_dir) + os.pathsep + env.get("PATH", "")
        env["CSI_OPTIN_MARKER"] = str(marker_path)
        result = subprocess.run(
            ["bash", installer], env=env, capture_output=True, text=True,
            timeout=60)

        def _lines(log):
            return log.read_text().splitlines() if log.exists() else []

        return (result, _lines(logs["systemctl"]), _lines(logs["cp"]),
                _lines(logs["apt-get"]))


def _assert_watchdog_timer_enabled(transcript, context):
    enables = [line for line in transcript
               if re.search(r"^\s*enable\s+--now\s+"
                            r"nvargus-error89-watchdog\.timer\s*$", line)]
    assert enables, (
        "{}: the watchdog timer was not enabled (`systemctl enable --now "
        "nvargus-error89-watchdog.timer` absent). systemctl transcript: "
        "{!r}".format(context, transcript))


def _assert_capture_service_disabled_not_enabled(transcript, context):
    offending = [line for line in transcript
                 if re.search(r"^\s*(enable|restart)\b", line)
                 and "nvidia-csi-capture" in line]
    assert not offending, (
        "{}: the capture service was enabled/restarted without the opt-in "
        "marker — the unconditional exposure defect 1.2 is back. systemctl "
        "transcript: {!r}".format(context, transcript))
    disabled = [line for line in transcript
                if re.search(r"^\s*disable\s+--now\b", line)
                and "nvidia-csi-capture" in line]
    assert disabled, (
        "{}: `systemctl disable --now nvidia-csi-capture.service` was not "
        "run — the device is not converged to capture-service-off. "
        "systemctl transcript: {!r}".format(context, transcript))


# ---------------------------------------------------------------------------
# Installer idempotency: repeated no-marker deployments converge to the
# same state with equivalent transcripts (requirement 2.3's invariant the
# old Install/Shutdown cycle violated).
# ---------------------------------------------------------------------------

def test_installer_twice_without_marker_is_idempotent(tmp_path):
    """Requirement 2.3: run the REAL installer TWICE with no opt-in marker
    — both runs exit 0, disable the capture service, enable the watchdog
    timer, and produce equivalent systemctl transcripts (every deployment
    converges the device to the marker's state, stable across repeats).

    Validates: Requirements 2.3
    """
    harness = _InstallerHarness(tmp_path, "no_marker")
    marker = tmp_path / "csi_camera_optin"  # never created
    transcripts = []
    for run_name in ("first", "second"):
        result, systemctl, _cp, _apt = harness.run(run_name, marker)
        assert result.returncode == 0, (
            "{} no-marker run exited {} (stderr: {!r})"
            .format(run_name, result.returncode, result.stderr))
        _assert_capture_service_disabled_not_enabled(
            systemctl, "{} no-marker run".format(run_name))
        _assert_watchdog_timer_enabled(
            systemctl, "{} no-marker run".format(run_name))
        transcripts.append(systemctl)
    assert transcripts[0] == transcripts[1], (
        "repeated no-marker deployments produced DIFFERENT systemctl "
        "transcripts — the installer is not idempotent.\n  first:  {!r}\n"
        "  second: {!r}".format(transcripts[0], transcripts[1]))


def test_installer_with_marker_runs_capture_install_path(tmp_path):
    """Requirement 2.3 (opted-in leg): with the marker present the capture
    install path runs with unchanged semantics — jq check satisfied
    without apt-get, capture script + unit file copied, then exactly the
    unfixed installer's transcript tail: daemon-reload, enable, restart of
    nvidia-csi-capture.service (task 1 recorded that tail as the unfixed
    F(X); opted-in devices must keep getting it).

    Validates: Requirements 2.3
    """
    harness = _InstallerHarness(tmp_path, "marker")
    marker = tmp_path / "csi_camera_optin"
    marker.write_text("enabled_by=test\n")
    result, systemctl, cp_calls, apt_calls = harness.run("opted_in", marker)
    assert result.returncode == 0, (
        "opted-in run exited {} (stderr: {!r})"
        .format(result.returncode, result.stderr))

    # Watchdog installed on ALL Jetson targets, marker or not (Decision 2).
    _assert_watchdog_timer_enabled(systemctl, "opted-in run")

    # jq check: the stub jq is on PATH, so apt-get must never be invoked.
    assert not apt_calls, (
        "opted-in run invoked apt-get despite jq being available: {!r}"
        .format(apt_calls))

    # Copy leg: capture script and unit file are copied into place.
    assert any("nvidia_csi_capture.sh" in line for line in cp_calls), (
        "opted-in run never copied nvidia_csi_capture.sh. cp transcript: "
        "{!r}".format(cp_calls))
    assert any("nvidia-csi-capture.service" in line for line in cp_calls), (
        "opted-in run never copied nvidia-csi-capture.service. cp "
        "transcript: {!r}".format(cp_calls))

    # The capture-path transcript tail is EXACTLY the unfixed installer's
    # behavior (task 1's recorded transcript) — unchanged semantics.
    assert systemctl[-3:] == ["daemon-reload",
                              "enable nvidia-csi-capture.service",
                              "restart nvidia-csi-capture.service"], (
        "opted-in run did not end with the unfixed capture install "
        "sequence [daemon-reload, enable, restart]. systemctl transcript: "
        "{!r}".format(systemctl))
    assert not any("disable" in line and "nvidia-csi-capture" in line
                   for line in systemctl), (
        "opted-in run disabled the capture service: {!r}".format(systemctl))


def test_installer_tolerates_unreadable_marker(tmp_path):
    """Edge case: a marker that exists but is unreadable (mode 000) must
    not crash the installer. `[ -f ]` only stats the path, so the marker
    counts as PRESENT or ABSENT depending on shell semantics — either
    outcome is acceptable; the run must exit 0, enable the watchdog timer,
    and take exactly one of the two valid capture-service actions.

    Validates: Requirements 2.3
    """
    harness = _InstallerHarness(tmp_path, "unreadable")
    marker = tmp_path / "csi_camera_optin"
    marker.write_text("enabled_by=test\n")
    os.chmod(marker, 0)
    try:
        result, systemctl, _cp, _apt = harness.run("unreadable", marker)
    finally:
        os.chmod(marker, 0o644)  # let pytest clean tmp_path up
    assert result.returncode == 0, (
        "installer crashed on an unreadable marker: exit {} (stderr: {!r})"
        .format(result.returncode, result.stderr))
    _assert_watchdog_timer_enabled(systemctl, "unreadable-marker run")
    enabled = [line for line in systemctl
               if re.search(r"^\s*(enable|restart)\b", line)
               and "nvidia-csi-capture" in line]
    disabled = [line for line in systemctl
                if re.search(r"^\s*disable\s+--now\b", line)
                and "nvidia-csi-capture" in line]
    assert bool(enabled) != bool(disabled), (
        "unreadable-marker run neither cleanly installed nor cleanly "
        "disabled the capture service (or did both). systemctl "
        "transcript: {!r}".format(systemctl))


def test_installer_tolerates_missing_watchdog_artifacts(tmp_path):
    """Edge case: watchdog artifacts missing next to the installer (copied
    alone into an empty dir — same effect as moving them aside) → a
    visible WARNING, exit 0, watchdog skipped, and the capture-service
    path still handled in BOTH marker states.

    Validates: Requirements 2.3
    """
    bare_dir = tmp_path / "bare_script_dir"
    bare_dir.mkdir()
    lone_installer = bare_dir / "install_nvidia_csi_service.sh"
    lone_installer.write_text(_read(INSTALLER))
    for artifact in WATCHDOG_ARTIFACTS:
        assert not (bare_dir / artifact).exists()

    harness = _InstallerHarness(tmp_path, "no_watchdog")
    marker = tmp_path / "csi_camera_optin"

    # Marker absent: WARNING + skip, then the disable path.
    result, systemctl, _cp, _apt = harness.run(
        "absent", marker, installer=str(lone_installer))
    assert result.returncode == 0, (
        "no-artifacts no-marker run exited {} (stderr: {!r})"
        .format(result.returncode, result.stderr))
    assert "WARNING" in result.stdout, (
        "missing watchdog artifacts produced no visible WARNING. stdout: "
        "{!r}".format(result.stdout))
    assert not any("nvargus-error89-watchdog" in line
                   for line in systemctl), (
        "watchdog units were touched despite the artifacts being absent: "
        "{!r}".format(systemctl))
    _assert_capture_service_disabled_not_enabled(
        systemctl, "no-artifacts no-marker run")

    # Marker present: WARNING + skip, then the capture install path.
    marker.write_text("enabled_by=test\n")
    result, systemctl, _cp, _apt = harness.run(
        "present", marker, installer=str(lone_installer))
    assert result.returncode == 0, (
        "no-artifacts opted-in run exited {} (stderr: {!r})"
        .format(result.returncode, result.stderr))
    assert "WARNING" in result.stdout
    assert systemctl[-3:] == ["daemon-reload",
                              "enable nvidia-csi-capture.service",
                              "restart nvidia-csi-capture.service"], (
        "no-artifacts opted-in run did not still run the capture install "
        "path. systemctl transcript: {!r}".format(systemctl))


# ---------------------------------------------------------------------------
# Golden discipline (requirement 2.4 / Property 5): the conscious
# rebaseline landed correctly and the gate itself was not weakened.
# ---------------------------------------------------------------------------

def test_setup_station_golden_matches_fixed_file_under_gate_normalization():
    """Property 5: dependency_baseline_setup_station.txt was regenerated
    from the FIXED setup_station.sh — same line count, every line
    byte-identical, with the security gate's single allowance replicated:
    the unique $PYTHON311 requests-pin line may differ only in its
    ``requests==`` version token.

    Validates: Requirements 2.4, 3.6
    """
    golden_lines = _read(SETUP_STATION_GOLDEN).splitlines()
    current_lines = _read(SETUP_STATION).splitlines()
    assert len(current_lines) == len(golden_lines), (
        "setup_station.sh has {} lines but its golden records {} — the "
        "task 3.1 rebaseline drifted from the fixed file"
        .format(len(current_lines), len(golden_lines)))

    pin_indices = [i for i, line in enumerate(current_lines)
                   if _F1_PIN_SUBSTRING in line]
    assert len(pin_indices) == 1, (
        "expected exactly one requests-pin line in setup_station.sh, "
        "found {}".format(len(pin_indices)))
    pin_idx = pin_indices[0]

    for i, (cur, gold) in enumerate(zip(current_lines, golden_lines)):
        if i == pin_idx:
            assert (_F1_VERSION_TOKEN.sub("requests==X", cur)
                    == _F1_VERSION_TOKEN.sub("requests==X", gold)), (
                "setup_station.sh:{} differs from its golden outside the "
                "requests version token.\n  golden:  {!r}\n  current: {!r}"
                .format(i + 1, gold, cur))
        else:
            assert cur == gold, (
                "setup_station.sh:{} differs from "
                "dependency_baseline_setup_station.txt — the golden was "
                "not regenerated byte-for-byte from the fixed file.\n"
                "  golden:  {!r}\n  current: {!r}".format(i + 1, gold, cur))


def test_unpinned_py36_baseline_entries_resolve_at_656_and_680():
    """Property 5 / requirement 3.6: dependency_baseline_unpinned_py36.json
    was NOT rebaselined — its setup_station.sh entries still resolve
    verbatim at their recorded line numbers 656 and 680, proving the CSI
    block was strictly appended and shifted nothing.

    Validates: Requirements 2.4, 3.6
    """
    import json
    with open(UNPINNED_PY36_BASELINE, encoding="utf-8") as f:
        baseline = json.load(f)
    entries = [e for e in baseline["entries"]
               if e["file"] == "station_install/setup_station.sh"]
    assert sorted(e["lineno"] for e in entries) == [656, 680], (
        "the unpinned-py36 baseline no longer records setup_station.sh "
        "entries at lines 656 and 680: {!r}".format(entries))
    current_lines = _read(SETUP_STATION).splitlines()
    for entry in entries:
        assert current_lines[entry["lineno"] - 1] == entry["text"], (
            "setup_station.sh line {} no longer matches the baseline's "
            "recorded text.\n  recorded: {!r}\n  current:  {!r}"
            .format(entry["lineno"], entry["text"],
                    current_lines[entry["lineno"] - 1]))


def test_security_preservation_suite_files_unmodified():
    """Property 5: no gate test was weakened, edited, or deleted — every
    file under test/backend-test/security/preservation/ is byte-identical
    to HEAD (staged or unstaged changes both count as violations).

    Validates: Requirements 2.4
    """
    result = subprocess.run(
        ["git", "diff", "--exit-code", "--stat", "HEAD", "--",
         PRESERVATION_SUITE_DIR],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (
        "files under {} were modified — the security preservation gate "
        "must never be weakened to absorb this spec's changes (rebaseline "
        "goldens, never edit gate tests):\n{}"
        .format(PRESERVATION_SUITE_DIR, result.stdout))


# ---------------------------------------------------------------------------
# setup_station CSI block units (text-level ONLY — the full provisioning
# script cannot be safely executed host-side).
# ---------------------------------------------------------------------------

def _csi_block():
    """The appended CSI block: everything from its banner line to EOF,
    with the banner's 1-based line number."""
    lines = _read(SETUP_STATION).splitlines()
    starts = [i for i, line in enumerate(lines)
              if "Configuring CSI camera exposure (opt-in)" in line]
    assert len(starts) == 1, (
        "expected exactly one CSI opt-in block banner in setup_station.sh, "
        "found {}".format(len(starts)))
    return starts[0] + 1, "\n".join(lines[starts[0]:])


def test_setup_station_block_is_strictly_appended():
    """The block's first line comes AFTER the unfixed file's last line
    (1625) and every CSI artifact lives inside the block — nothing was
    inserted mid-file (which would shift the unpinned-py36 golden's
    recorded line numbers).

    Validates: Requirements 2.4, 3.6
    """
    banner_lineno, _block = _csi_block()
    assert banner_lineno > UNFIXED_SETUP_STATION_LINES, (
        "the CSI opt-in block starts at line {} — inside the unfixed "
        "file's {} lines, i.e. NOT strictly appended"
        .format(banner_lineno, UNFIXED_SETUP_STATION_LINES))
    lines = _read(SETUP_STATION).splitlines()
    strays = [i + 1 for i, line in enumerate(
                  lines[:UNFIXED_SETUP_STATION_LINES])
              if "ENABLE_CSI_CAMERA" in line or "csi_camera_optin" in line
              or "nvargus" in line]
    assert not strays, (
        "CSI opt-in artifacts found INSIDE the unfixed prefix (lines "
        "{!r}) — the block must be strictly appended".format(strays))


def test_setup_station_block_has_both_branches():
    """Requirements 2.1/2.2: the opt-in branch enables nvargus-daemon and
    WRITES the marker; the default branch disables the daemon and CLEARS
    the marker.

    Validates: Requirements 2.3 (provisioning precondition), 2.4
    """
    _lineno, block = _csi_block()
    assert 'if [ "${ENABLE_CSI_CAMERA:-0}" = "1" ]' in block, (
        "the ENABLE_CSI_CAMERA=1 opt-in branch condition is missing")
    assert re.search(r"systemctl\s+enable\s+--now\s+nvargus-daemon", block), (
        "the opt-in branch does not enable nvargus-daemon")
    assert re.search(r'>\s*"\$CSI_OPTIN_MARKER"', block), (
        "the opt-in branch does not write the marker")
    assert re.search(r"systemctl\s+disable\s+--now\s+nvargus-daemon",
                     block), (
        "the default branch does not disable nvargus-daemon")
    assert re.search(r'rm\s+-f\s+"\$CSI_OPTIN_MARKER"', block), (
        "the default branch does not clear a stale marker")


def test_setup_station_block_marker_path_is_exact():
    """The marker path the installer gates on, verbatim — a mismatch would
    silently break the whole opt-in mechanism.

    Validates: Requirements 2.3, 2.4
    """
    _lineno, block = _csi_block()
    assert ('CSI_OPTIN_MARKER="/aws_dda/system/csi_camera_optin"'
            in block), (
        "the block does not declare "
        'CSI_OPTIN_MARKER="/aws_dda/system/csi_camera_optin" verbatim — '
        "provisioning and the installer would gate on different paths")


def test_setup_station_block_uses_tolerant_run_cmd_style():
    """The file's existing tolerant style: every systemctl action in the
    block goes through run_cmd with an add_warning fallback — the block
    must never hard-fail provisioning.

    Validates: Requirements 2.4
    """
    _lineno, block = _csi_block()
    systemctl_actions = [line for line in block.splitlines()
                         if re.search(r"systemctl\s+(enable|disable)", line)]
    assert systemctl_actions, "no systemctl actions found in the CSI block"
    for line in systemctl_actions:
        assert "run_cmd" in line and "add_warning" in line, (
            "CSI-block systemctl action not in the tolerant run_cmd/"
            "add_warning style: {!r}".format(line))


def test_setup_station_block_guards_on_unit_presence():
    """The list-unit-files guard: the block is a no-op on devices without
    nvargus-daemon (amd64 stations).

    Validates: Requirements 2.4
    """
    _lineno, block = _csi_block()
    assert re.search(
        r"if\s+systemctl\s+list-unit-files\s+nvargus-daemon\.service",
        block), (
        "the block is not guarded by `systemctl list-unit-files "
        "nvargus-daemon.service` — it would act on non-Jetson stations")
