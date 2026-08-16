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
"""Bug-condition exploration tests (Task 1) for csi-nvargus-optional.

Property 1: Bug Condition — CSI/nvargus Exposure Is Opt-In and Churn-Free.

**Cases 1-5 assert the FIXED expectation, so they are EXPECTED TO FAIL on the
unfixed tree.** The failures are the counterexamples confirming defects
1.1-1.5: provisioning has no CSI opt-in concept (nvargus-daemon stays in its
JetPack default everywhere), every arm64 deployment unconditionally enables
and restarts nvidia-csi-capture.service, the shipped capture script is a
per-frame Argus session churn loop (`nvarguscamerasrc num-buffers=1` at
~0.5 s cadence with stderr discarded — the jetson-thor1 incident onset
trigger pattern 1:1), no Error(89) degraded-state watchdog exists, and the
three consumer-less legacy capture scripts still ship.

Case 6 documents F(X) and PASSES on the unfixed tree (and must NOT be
inverted by the fix): all five arm64 recipes invoke
`install_nvidia_csi_service.sh` unconditionally in their Install lifecycle
and the amd64 recipes never do — the Decision 1 premise that the
unconditional Install hook becomes the fix's distribution channel.

The SAME suite is re-run in task 3.6 against the fixed tree, where cases 1-5
must PASS.

Honesty guard: every check here is GPU-free and host-runnable — text-level
assertions against the shipped scripts/recipes plus ONE behavioral test that
executes the REAL installer with stub binaries on PATH (the
`deploy_reliability` stub pattern). No gst-launch, Argus, CUDA, or real
systemd is executed.

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5
"""
import os
import re
import stat
import subprocess

import pytest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

HOST_SCRIPTS_DIR = os.path.join(REPO_ROOT, "src", "host_scripts")
SETUP_STATION = os.path.join(
    REPO_ROOT, "station_install", "setup_station.sh")
INSTALLER = os.path.join(HOST_SCRIPTS_DIR, "install_nvidia_csi_service.sh")
CAPTURE_SCRIPT = os.path.join(HOST_SCRIPTS_DIR, "nvidia_csi_capture.sh")

WATCHDOG_SCRIPT = os.path.join(
    HOST_SCRIPTS_DIR, "nvargus_error89_watchdog.sh")
WATCHDOG_SERVICE = os.path.join(
    HOST_SCRIPTS_DIR, "nvargus-error89-watchdog.service")
WATCHDOG_TIMER = os.path.join(
    HOST_SCRIPTS_DIR, "nvargus-error89-watchdog.timer")

LEGACY_SCRIPTS = (
    os.path.join(HOST_SCRIPTS_DIR, "start_csi_bridge.sh"),
    os.path.join(HOST_SCRIPTS_DIR, "stop_csi_bridge.sh"),
    os.path.join(HOST_SCRIPTS_DIR, "nvidia_csi_server.sh"),
)

#: All five arm64 recipe variants run the installer in Install (defect 1.2's
#: distribution surface — and, per design Decision 1, the fix's channel).
ARM64_RECIPES = (
    "recipe.yaml",
    "recipe-arm64.yaml",
    "recipe-arm64-jp5.yaml",
    "recipe-arm64-jp6.yaml",
    "recipe-arm64-jp7.yaml",
)

#: The amd64 recipes never touch the CSI installer.
AMD64_RECIPES = (
    "recipe-amd64.yaml",
    "recipe-amd64-nvidia.yaml",
)

#: Opt-in marker written by provisioning (design Decision 1).
CSI_OPTIN_MARKER = "/aws_dda/system/csi_camera_optin"


def _read(path):
    assert os.path.isfile(path), (
        "expected file does not exist: {}".format(path))
    with open(path, encoding="utf-8") as f:
        return f.read()


def _logical_lines(text):
    """Shell source lines with backslash continuations joined, so a
    multi-line pipeline invocation is inspected as one logical command."""
    logical = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        buf += line
        logical.append(buf)
        buf = ""
    if buf:
        logical.append(buf)
    return logical


def _while_loop_bodies(text):
    """Bodies of every `while ...; do ... done` loop, found by tracking
    do/done nesting over logical lines. Good enough for the shipped shell
    scripts (no here-doc trickery around loops)."""
    lines = _logical_lines(text)
    bodies = []
    i = 0
    while i < len(lines):
        if re.search(r"^\s*while\b", lines[i]):
            depth = 0
            body = []
            j = i
            while j < len(lines):
                depth += len(re.findall(r"\bdo\b", lines[j]))
                depth -= len(re.findall(r"\bdone\b", lines[j]))
                if j > i:
                    body.append(lines[j])
                if depth <= 0 and j > i:
                    break
                j += 1
            bodies.append("\n".join(body))
            i = j + 1
        else:
            i += 1
    return bodies


# ---------------------------------------------------------------------------
# Case 1 — no provisioning opt-in (defect 1.1): setup_station.sh must carry
# the ENABLE_CSI_CAMERA block — default branch disables nvargus-daemon and
# clears the marker; opt-in branch enables the daemon and writes the marker;
# a list-unit-files guard makes the block a no-op off-Jetson. All absent on
# the unfixed tree: provisioning leaves nvargus-daemon in its JetPack
# default (enabled) on every device, camera or not.
# ---------------------------------------------------------------------------

def test_setup_station_default_branch_disables_nvargus_and_clears_marker():
    """Requirement 2.1: provisioning WITHOUT ENABLE_CSI_CAMERA=1 disables
    and stops nvargus-daemon and clears any stale opt-in marker.

    Validates: Requirements 1.1
    """
    content = _read(SETUP_STATION)
    assert "ENABLE_CSI_CAMERA" in content, (
        "COUNTEREXAMPLE (defect 1.1): station_install/setup_station.sh has "
        "no ENABLE_CSI_CAMERA handling at all — provisioning leaves "
        "nvargus-daemon in its JetPack default (enabled) on every device, "
        "with no opt-in or opt-out; the poisoned-state holder stays "
        "resident fleet-wide")
    assert re.search(r"systemctl\s+disable\s+--now\s+nvargus-daemon",
                     content), (
        "COUNTEREXAMPLE (defect 1.1): setup_station.sh never runs "
        "`systemctl disable --now nvargus-daemon` — the default (no opt-in) "
        "branch does not exist")
    assert "csi_camera_optin" in content, (
        "COUNTEREXAMPLE (defect 1.1): setup_station.sh never references the "
        "opt-in marker {} — there is no provisioning-time record of CSI "
        "opt-in for the installer to gate on".format(CSI_OPTIN_MARKER))
    assert re.search(
        r"rm\s+-f\s+(\"?\$\{?CSI_OPTIN_MARKER\}?\"?"
        r"|\"?/aws_dda/system/csi_camera_optin\"?)", content), (
        "COUNTEREXAMPLE (defect 1.1): the default branch does not clear a "
        "stale opt-in marker (`rm -f` of {}) — re-provisioning without the "
        "flag cannot consciously opt a device OUT".format(CSI_OPTIN_MARKER))


def test_setup_station_optin_branch_enables_daemon_and_records_marker():
    """Requirement 2.2: provisioning WITH ENABLE_CSI_CAMERA=1 leaves
    nvargus-daemon enabled and records the opt-in marker.

    Validates: Requirements 1.1
    """
    content = _read(SETUP_STATION)
    assert "ENABLE_CSI_CAMERA" in content, (
        "COUNTEREXAMPLE (defect 1.1): no ENABLE_CSI_CAMERA opt-in branch "
        "exists in setup_station.sh")
    assert re.search(r"systemctl\s+enable\s+--now\s+nvargus-daemon",
                     content), (
        "COUNTEREXAMPLE (defect 1.1): no opt-in branch enables "
        "nvargus-daemon (`systemctl enable --now nvargus-daemon` absent)")
    assert re.search(
        r">\s*(\"?\$\{?CSI_OPTIN_MARKER\}?\"?"
        r"|\"?/aws_dda/system/csi_camera_optin\"?)", content), (
        "COUNTEREXAMPLE (defect 1.1): the opt-in branch never WRITES the "
        "marker {} — an opted-in device would not be identifiable as "
        "CSI-enabled".format(CSI_OPTIN_MARKER))


def test_setup_station_guards_csi_block_on_unit_presence():
    """The CSI block must no-op on devices without nvargus-daemon (amd64
    stations): a `systemctl list-unit-files nvargus-daemon` guard.

    Validates: Requirements 1.1
    """
    content = _read(SETUP_STATION)
    assert re.search(r"systemctl\s+list-unit-files\s+nvargus-daemon",
                     content), (
        "COUNTEREXAMPLE (defect 1.1): setup_station.sh has no "
        "`systemctl list-unit-files nvargus-daemon` guard — the CSI opt-in "
        "block (and with it the whole opt-in concept) is absent")


# ---------------------------------------------------------------------------
# Case 2 — installer is unconditional (defect 1.2). Text leg: the installer
# must contain the marker-gated disable path. Behavioral leg: running the
# REAL installer with a stub systemctl and NO marker must disable the
# capture service and never enable/restart it. On the unfixed tree the
# installer enables+restarts unconditionally on every deployment.
# ---------------------------------------------------------------------------

def test_installer_text_contains_marker_gated_disable_path():
    """Requirement 2.3 (text leg): the installer self-gates on the
    provisioning marker and disables the capture service when it is absent.

    Validates: Requirements 1.2
    """
    content = _read(INSTALLER)
    assert "csi_camera_optin" in content, (
        "COUNTEREXAMPLE (defect 1.2): install_nvidia_csi_service.sh never "
        "references the opt-in marker {} — the install path is "
        "unconditional on every arm64 deployment".format(CSI_OPTIN_MARKER))
    assert re.search(r"systemctl\s+disable\s+--now\s+nvidia-csi-capture",
                     content), (
        "COUNTEREXAMPLE (defect 1.2): install_nvidia_csi_service.sh has no "
        "`systemctl disable --now nvidia-csi-capture` path — a "
        "non-opted-in device can never end a deployment with the capture "
        "service off")


_STUB_RECORDING_TEMPLATE = """\
#!/usr/bin/env bash
echo "$@" >> "{log}"
exit 0
"""

_STUB_SILENT = """\
#!/usr/bin/env bash
exit 0
"""


def _make_stub_env(tmp_path):
    """Stub bin dir resolved first on PATH for the installer subprocess:
    `systemctl` records its argv (the transcript under test); `jq` exists so
    the installer's `command -v jq` check passes without apt-get;
    `mkdir`/`cp`/`chmod`/`apt-get` are no-ops so the REAL installer runs to
    completion without touching /aws_dda, /etc/systemd, or the package
    manager on this host."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    systemctl_log = tmp_path / "systemctl_transcript.log"

    def _install(name, content):
        stub = bin_dir / name
        stub.write_text(content)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
                   | stat.S_IXOTH)

    _install("systemctl",
             _STUB_RECORDING_TEMPLATE.format(log=str(systemctl_log)))
    for name in ("jq", "apt-get", "mkdir", "cp", "chmod"):
        _install(name, _STUB_SILENT)

    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    return env, systemctl_log


def test_installer_without_marker_disables_capture_service_not_enables(
        tmp_path):
    """Requirement 2.3 (behavioral leg): run the REAL installer with a stub
    systemctl recording argv and NO opt-in marker present (this host has no
    /aws_dda/system/csi_camera_optin). The transcript must show
    `disable --now nvidia-csi-capture.service` and must NOT show any
    enable/restart of the capture service.

    On the unfixed tree this surfaces THE incident-class counterexample:
    the installer unconditionally runs `systemctl enable
    nvidia-csi-capture.service` + `systemctl restart
    nvidia-csi-capture.service` on every deployment to every arm64 device,
    camera or not (Shutdown only stops it; the next deployment re-enables
    it).

    Validates: Requirements 1.2
    """
    assert not os.path.exists(CSI_OPTIN_MARKER), (
        "test precondition: the opt-in marker {} exists on this host — "
        "cannot exercise the no-marker deployment path"
        .format(CSI_OPTIN_MARKER))
    env, systemctl_log = _make_stub_env(tmp_path)
    result = subprocess.run(
        ["bash", INSTALLER], env=env, capture_output=True, text=True,
        timeout=60)
    assert result.returncode == 0, (
        "installer exited {} under stubbed binaries (stderr: {!r})"
        .format(result.returncode, result.stderr))

    transcript = (systemctl_log.read_text().splitlines()
                  if systemctl_log.exists() else [])
    offending = [line for line in transcript
                 if re.search(r"^\s*(enable|restart)\b(?!.*watchdog)", line)
                 and "nvidia-csi-capture" in line]
    assert not offending, (
        "COUNTEREXAMPLE (defect 1.2): with NO opt-in marker present, the "
        "installer still enabled/restarted the capture service — the "
        "unconditional per-deployment exposure. systemctl transcript: "
        "{!r}".format(transcript))
    disabled = [line for line in transcript
                if re.search(r"^\s*disable\b.*--now\b", line)
                and "nvidia-csi-capture" in line]
    assert disabled, (
        "COUNTEREXAMPLE (defect 1.2): with NO opt-in marker present, the "
        "installer never ran `systemctl disable --now "
        "nvidia-csi-capture.service` — a non-opted-in device is not "
        "converged to capture-service-off. systemctl transcript: {!r}"
        .format(transcript))


# ---------------------------------------------------------------------------
# Case 3 — capture script is per-frame churn (defect 1.3): the shipped
# nvidia_csi_capture.sh must contain NO num-buffers=1, must launch ONE
# persistent pipeline (no synchronous gst-launch inside a per-frame loop;
# multifilesink staging supervisor present), and must not discard the
# capture command's stderr. The unfixed script fails every clause: a
# while-true loop of single-frame `nvarguscamerasrc num-buffers=1` captures
# with `sleep 0.1` cadence and `2>/dev/null` — one full Argus session
# create/teardown per frame, the incident onset trigger pattern 1:1.
# ---------------------------------------------------------------------------

def test_capture_script_has_no_per_frame_session_churn():
    """Requirement 2.5: no single-frame `num-buffers=1` captures — one
    persistent Argus session, not one per frame.

    Validates: Requirements 1.3
    """
    content = _read(CAPTURE_SCRIPT)
    churn_lines = [line for line in _logical_lines(content)
                   if "num-buffers" in line]
    assert not churn_lines, (
        "COUNTEREXAMPLE (defect 1.3): nvidia_csi_capture.sh still creates "
        "a full Argus session per frame — `num-buffers` capture found: "
        "{!r}".format(churn_lines))
    assert not re.search(r"\bsleep\s+0\.\d", content), (
        "COUNTEREXAMPLE (defect 1.3): the sub-second per-frame cadence "
        "sleep is still present — the ~0.5 s churn loop pacing that "
        "matched the jetson-thor1 incident onset 1:1")


def test_capture_script_does_not_discard_capture_stderr():
    """Requirement 2.8's visibility premise: pipeline failures must be
    loggable — the capture command must not send stderr to /dev/null (on a
    camera-less device the unfixed loop fails silently, forever).

    Validates: Requirements 1.3
    """
    content = _read(CAPTURE_SCRIPT)
    gst_lines = [line for line in _logical_lines(content)
                 if "gst-launch" in line]
    assert gst_lines, (
        "nvidia_csi_capture.sh contains no gst-launch invocation at all — "
        "cannot be the CSI capture path")
    discarded = [line for line in gst_lines if "2>/dev/null" in line]
    assert not discarded, (
        "COUNTEREXAMPLE (defect 1.3): the capture command discards stderr "
        "(`2>/dev/null`) — Argus failures are invisible: {!r}"
        .format(discarded))


def test_capture_script_launches_one_persistent_supervised_pipeline():
    """Requirement 2.5/2.6: ONE long-lived pipeline with a staging
    supervisor (multifilesink stage pattern + atomic mv), not a synchronous
    gst-launch inside a per-frame while-loop.

    Validates: Requirements 1.3
    """
    content = _read(CAPTURE_SCRIPT)
    assert "multifilesink" in content, (
        "COUNTEREXAMPLE (defect 1.3): no multifilesink staging supervisor "
        "— the script writes one file per gst-launch invocation "
        "(per-frame single-shot capture), not a persistent pipeline "
        "staging frames continuously")
    for body in _while_loop_bodies(content):
        synchronous_gst = [
            line for line in _logical_lines(body)
            if "gst-launch" in line and not line.rstrip().endswith("&")]
        assert not synchronous_gst, (
            "COUNTEREXAMPLE (defect 1.3): a while-loop body invokes "
            "gst-launch synchronously — a new Argus session is created and "
            "torn down on every loop iteration: {!r}"
            .format(synchronous_gst))


# ---------------------------------------------------------------------------
# Case 4 — no watchdog exists (defect 1.4): host_scripts/ must ship the
# Error(89) watchdog script + systemd oneshot service + timer, the script
# must match BOTH kernel signature patterns, and the installer must enable
# the timer. All absent on the unfixed tree: the degraded state persists
# indefinitely (200,273+ signature lines on jetson-thor1) until a human
# restarts nvargus-daemon, while ONNX models silently serve from CPU.
# ---------------------------------------------------------------------------

def test_watchdog_artifacts_exist_and_match_both_signatures():
    """Requirement 2.9: the watchdog script and its systemd units exist and
    the script detects BOTH halves of the degraded-state signature.

    Validates: Requirements 1.4
    """
    missing = [p for p in (WATCHDOG_SCRIPT, WATCHDOG_SERVICE, WATCHDOG_TIMER)
               if not os.path.isfile(p)]
    assert not missing, (
        "COUNTEREXAMPLE (defect 1.4): no Error(89) watchdog exists — "
        "missing: {!r}. Nothing detects the degraded-state signature or "
        "restarts nvargus-daemon; device-wide CUDA context creation stays "
        "broken until a human intervenes"
        .format([os.path.basename(p) for p in missing]))
    content = _read(WATCHDOG_SCRIPT)
    assert ("osCreateOsDescriptorFromFileHandle" in content
            and "Error (89)" in content), (
        "COUNTEREXAMPLE (defect 1.4): the watchdog script does not match "
        "the NVRM `osCreateOsDescriptorFromFileHandle ... Error (89)` "
        "kernel signature")
    assert "Can't map dma attachment" in content, (
        "COUNTEREXAMPLE (defect 1.4): the watchdog script does not match "
        "the `Can't map dma attachment` kernel signature")


def test_installer_enables_watchdog_timer():
    """Design Decision 2: the existing installer distributes the watchdog
    to ALL Jetson targets and enables its timer.

    Validates: Requirements 1.4
    """
    content = _read(INSTALLER)
    assert "nvargus-error89-watchdog.timer" in content, (
        "COUNTEREXAMPLE (defect 1.4): install_nvidia_csi_service.sh never "
        "references nvargus-error89-watchdog.timer — the watchdog is not "
        "distributed to any Jetson target")
    assert re.search(r"systemctl\s+enable\s+--now\s+"
                     r"nvargus-error89-watchdog\.timer", content), (
        "COUNTEREXAMPLE (defect 1.4): the installer does not enable the "
        "watchdog timer (`systemctl enable --now "
        "nvargus-error89-watchdog.timer` absent)")


# ---------------------------------------------------------------------------
# Case 5 — legacy scripts still ship (defect 1.5 / requirement 2.13): the
# three consumer-less experiment scripts carrying the same churn pattern
# must no longer exist. All three are present on the unfixed tree.
# ---------------------------------------------------------------------------

def test_legacy_capture_scripts_no_longer_ship():
    """Requirement 2.13: the component no longer ships start_csi_bridge.sh,
    stop_csi_bridge.sh, nvidia_csi_server.sh.

    Validates: Requirements 1.5
    """
    shipping = [os.path.basename(p) for p in LEGACY_SCRIPTS
                if os.path.exists(p)]
    assert not shipping, (
        "COUNTEREXAMPLE (defect 1.5): consumer-less legacy capture scripts "
        "still ship in src/host_scripts/ (packaged into every component "
        "artifact, carrying the per-frame Argus churn pattern and inviting "
        "the manual-debugging reuse that poisoned jetson-thor1): {!r}"
        .format(shipping))


# ---------------------------------------------------------------------------
# Case 6 — documents F(X); PASSES on the unfixed tree and must NOT be
# inverted by the fix: all five arm64 recipes invoke the installer
# unconditionally in Install (Decision 1 changes the SCRIPT, not the
# recipes — the unconditional hook becomes the fix's distribution channel),
# and the amd64 recipes never invoke it.
# ---------------------------------------------------------------------------

def _install_scripts(recipe_name):
    path = os.path.join(REPO_ROOT, recipe_name)
    with open(path, encoding="utf-8") as f:
        recipe = yaml.safe_load(f)
    manifests = recipe.get("Manifests") or []
    assert manifests, "{}: recipe declares no Manifests".format(recipe_name)
    scripts = []
    for i, manifest in enumerate(manifests):
        lifecycle = manifest.get("Lifecycle") or {}
        install = lifecycle.get("Install") or {}
        script = install.get("Script") if isinstance(install, dict) else ""
        scripts.append((i, script or ""))
    return scripts


@pytest.mark.parametrize("recipe_name", ARM64_RECIPES)
def test_arm64_recipe_install_invokes_csi_installer(recipe_name):
    """F(X) pin (Decision 1 premise): every arm64 recipe's Install
    lifecycle invokes install_nvidia_csi_service.sh unconditionally — true
    before AND after the fix (the fix gates the script, not the recipes).

    Validates: Requirements 1.2
    """
    scripts = _install_scripts(recipe_name)
    invoking = [i for i, script in scripts
                if "install_nvidia_csi_service.sh" in script]
    assert invoking, (
        "{}: no Install lifecycle invokes install_nvidia_csi_service.sh — "
        "the Decision 1 distribution channel (and the defect 1.2 exposure "
        "surface) is not where the design says it is".format(recipe_name))


@pytest.mark.parametrize("recipe_name", AMD64_RECIPES)
def test_amd64_recipe_never_invokes_csi_installer(recipe_name):
    """F(X) pin: the amd64 recipes contain no CSI installer invocation
    anywhere — amd64 devices are untouched by construction.

    Validates: Requirements 1.2
    """
    content = _read(os.path.join(REPO_ROOT, recipe_name))
    assert "install_nvidia_csi_service" not in content, (
        "{}: unexpectedly references install_nvidia_csi_service — the "
        "amd64 recipes must never install the CSI service"
        .format(recipe_name))
