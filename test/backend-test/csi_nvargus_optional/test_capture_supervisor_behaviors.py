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
"""Capture supervisor behavioral tests (Task 4.2) for csi-nvargus-optional.

Property 3: Fix Checking — Persistent Pipeline Honors the Staging and
Settings Contracts.

Behavioral legs run the REAL ``src/host_scripts/nvidia_csi_capture.sh``
backgrounded (``subprocess.Popen`` + kill in ``finally``) with a stub
``gst-launch-1.0`` on PATH that records its argv and fakes multifilesink
stage-file production; ``CSI_CAPTURE_DIR`` points at a temp dir,
``CONFIG_POLL_INTERVAL=1`` and ``RESTART_BACKOFF=1`` keep the loop fast:

(a) the initial launch argv carries the validated parameter set
    (``aeantibanding=0``, ``wbmode=0``, ``exposuretimerange``/``gainrange``
    with the config values) and NO ``num-buffers`` (2.5, 2.7)
(b) a config.json change produces exactly ONE additional gst-launch
    invocation with the new argv (2.7)
(c) a stub exiting nonzero produces a visible ERROR log line on the
    script's stderr and a backoff relaunch (2.8)
(d) ``latest.jpg`` only ever appears via atomic ``mv`` from a COMPLETE
    stage file — its content equals a promoted stage file's content, and
    the script text contains no direct redirection into ``$LATEST_IMAGE``
    (2.6)

Unit legs drive the script's REAL functions (extracted verbatim, run with
real jq — the task-2 identity-test pattern): config diff detection per key
(gain, exposure, each crop edge), ``build_crop_params`` construction for
zero/nonzero edges, and the default-config bootstrap (missing config.json →
created with the default payload + chmod 666).

Honesty guard: no gst-launch, Argus, or CUDA is executed — the stub IS the
pipeline; real jq only.

Validates: Requirements 2.5, 2.6, 2.7, 2.8
"""
import json
import os
import re
import stat
import subprocess
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

CAPTURE_SCRIPT = os.path.join(
    REPO_ROOT, "src", "host_scripts", "nvidia_csi_capture.sh")

_HAS_JQ = subprocess.run(["bash", "-c", "command -v jq"],
                         capture_output=True).returncode == 0

#: Stub gst-launch-1.0 for a HEALTHY pipeline: records argv (one line per
#: invocation), fakes multifilesink staging by writing two complete stage
#: files (stage_00001 written measurably later, so it is the newest — the
#: newest-but-one promotion rule must pick stage_00000), then blocks like a
#: live pipeline until TERM'd by the supervisor.
_GST_STUB_HEALTHY = """\
#!/usr/bin/env bash
echo "$@" >> "$GST_LOG"
n=$(wc -l < "$GST_LOG")
printf 'stage-frame-%s-0' "$n" > "$STUB_CAPTURE_DIR/stage_00000.jpg"
sleep 0.2
printf 'stage-frame-%s-1' "$n" > "$STUB_CAPTURE_DIR/stage_00001.jpg"
exec sleep 3600
"""

#: Stub gst-launch-1.0 whose FIRST invocation dies with a nonzero exit
#: (pipeline failure); later invocations behave like the healthy stub.
_GST_STUB_FAIL_FIRST = """\
#!/usr/bin/env bash
echo "$@" >> "$GST_LOG"
n=$(wc -l < "$GST_LOG")
if [ "$n" -le 1 ]; then
    exit 7
fi
printf 'stage-frame-%s-0' "$n" > "$STUB_CAPTURE_DIR/stage_00000.jpg"
sleep 0.2
printf 'stage-frame-%s-1' "$n" > "$STUB_CAPTURE_DIR/stage_00001.jpg"
exec sleep 3600
"""

_WAIT_DEADLINE = 20.0  # generous; the loop polls every 1s


def _wait_for(predicate, deadline=_WAIT_DEADLINE, interval=0.05):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _Supervisor:
    """Runs the REAL capture script backgrounded against a temp capture dir
    with a stub gst-launch-1.0 on PATH. Kill-in-finally discipline via
    context manager."""

    def __init__(self, tmp, gst_stub=_GST_STUB_HEALTHY, config=None):
        self.capture_dir = os.path.join(tmp, "capture")
        self.bin_dir = os.path.join(tmp, "bin")
        os.makedirs(self.capture_dir)
        os.makedirs(self.bin_dir)
        self.gst_log = os.path.join(tmp, "gst_invocations.log")
        stub_path = os.path.join(self.bin_dir, "gst-launch-1.0")
        with open(stub_path, "w", encoding="utf-8") as f:
            f.write(gst_stub)
        os.chmod(stub_path, os.stat(stub_path).st_mode | stat.S_IXUSR
                 | stat.S_IXGRP | stat.S_IXOTH)
        if config is not None:
            self.write_config(config)
        self.proc = None
        self.stdout = ""
        self.stderr = ""

    def write_config(self, config):
        with open(os.path.join(self.capture_dir, "config.json"), "w",
                  encoding="utf-8") as f:
            json.dump(config, f)

    def __enter__(self):
        env = dict(os.environ)
        env["PATH"] = self.bin_dir + os.pathsep + env.get("PATH", "")
        env["CSI_CAPTURE_DIR"] = self.capture_dir
        env["CONFIG_POLL_INTERVAL"] = "1"
        env["RESTART_BACKOFF"] = "1"
        env["GST_LOG"] = self.gst_log
        env["STUB_CAPTURE_DIR"] = self.capture_dir
        self.proc = subprocess.Popen(
            ["bash", CAPTURE_SCRIPT], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        return self

    def __exit__(self, *exc):
        try:
            self.proc.terminate()
            self.stdout, self.stderr = self.proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.stdout, self.stderr = self.proc.communicate(timeout=15)
        return False

    def gst_invocations(self):
        if not os.path.exists(self.gst_log):
            return []
        with open(self.gst_log, encoding="utf-8") as f:
            return f.read().splitlines()

    def latest_jpg(self):
        return os.path.join(self.capture_dir, "latest.jpg")


# ---------------------------------------------------------------------------
# (a) initial launch argv: the validated parameter set, config values
#     applied, NO num-buffers.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_JQ, reason="jq is not installed on this host")
def test_initial_launch_argv_uses_validated_params_from_config(tmp_path):
    """Requirements 2.5 + 2.7: the initial pipeline launch reads
    config.json and builds ONE persistent nvarguscamerasrc invocation with
    the validated manual-exposure set (aeantibanding=0, wbmode=0,
    exposuretimerange/gainrange pinned to the config values), the videocrop
    stage for nonzero edges, multifilesink staging — and NO num-buffers
    (the churn fingerprint must be gone from the live argv, not just the
    script text)."""
    config = {"gain": 7, "exposure": 123456,
              "crop": {"top": 10, "bottom": 0, "left": 4, "right": 0}}
    with _Supervisor(str(tmp_path), config=config) as sup:
        assert _wait_for(lambda: len(sup.gst_invocations()) >= 1), (
            "gst-launch-1.0 was never invoked; script stderr so far is "
            "unavailable until exit")
        argv = sup.gst_invocations()[0]

    assert "nvarguscamerasrc" in argv
    assert "aeantibanding=0" in argv, argv
    assert "wbmode=0" in argv, argv
    assert "exposuretimerange=123456 123456" in argv, (
        "exposuretimerange not pinned to the config exposure: {!r}"
        .format(argv))
    assert "gainrange=7 7" in argv, (
        "gainrange not pinned to the config gain: {!r}".format(argv))
    assert "videocrop top=10 bottom=0 left=4 right=0" in argv, (
        "videocrop stage missing for nonzero crop edges: {!r}".format(argv))
    assert "multifilesink" in argv, argv
    assert "num-buffers" not in argv, (
        "the per-frame churn fingerprint num-buffers appears in the LIVE "
        "pipeline argv: {!r}".format(argv))


# ---------------------------------------------------------------------------
# (b) config change → exactly ONE additional invocation with the new argv.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_JQ, reason="jq is not installed on this host")
def test_config_change_produces_exactly_one_relaunch_with_new_argv(tmp_path):
    """Requirement 2.7: an effective config.json change (gain 4 → 9)
    produces exactly ONE additional gst-launch invocation carrying the new
    settings; the steady state after the change produces no further
    relaunches (no-op polls never restart the pipeline)."""
    with _Supervisor(str(tmp_path),
                     config={"gain": 4, "exposure": 5000000}) as sup:
        assert _wait_for(lambda: len(sup.gst_invocations()) >= 1)
        assert "gainrange=4 4" in sup.gst_invocations()[0]

        sup.write_config({"gain": 9, "exposure": 5000000})
        assert _wait_for(lambda: len(sup.gst_invocations()) >= 2), (
            "config change never produced a relaunch; invocations: {!r}"
            .format(sup.gst_invocations()))
        assert "gainrange=9 9" in sup.gst_invocations()[1], (
            "relaunch does not carry the new gain: {!r}"
            .format(sup.gst_invocations()[1]))
        # Steady state: several more poll cycles must add nothing.
        time.sleep(3.5)
        invocations = sup.gst_invocations()

    assert len(invocations) == 2, (
        "expected exactly one relaunch for one effective change, got {} "
        "invocations: {!r}".format(len(invocations), invocations))


# ---------------------------------------------------------------------------
# (c) pipeline death → visible ERROR log + backoff relaunch.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_JQ, reason="jq is not installed on this host")
def test_pipeline_death_logs_visibly_and_relaunches_after_backoff(tmp_path):
    """Requirement 2.8: when the pipeline dies with a nonzero exit, the
    supervisor logs the failure LOUDLY (stderr, not discarded) and
    relaunches after RESTART_BACKOFF — without the script itself exiting."""
    with _Supervisor(str(tmp_path), gst_stub=_GST_STUB_FAIL_FIRST,
                     config={"gain": 4, "exposure": 5000000}) as sup:
        assert _wait_for(lambda: len(sup.gst_invocations()) >= 2), (
            "no relaunch after the pipeline died; invocations: {!r}"
            .format(sup.gst_invocations()))
        assert sup.proc.poll() is None, (
            "the supervisor itself exited after a pipeline death")

    assert "ERROR: capture pipeline died" in sup.stderr, (
        "no visible failure log on stderr after a nonzero pipeline exit; "
        "stderr: {!r}".format(sup.stderr))
    assert "exit status 7" in sup.stderr, (
        "the failure log does not carry the pipeline exit status; "
        "stderr: {!r}".format(sup.stderr))


# ---------------------------------------------------------------------------
# (d) latest.jpg atomicity: only ever produced by mv from a COMPLETE stage
#     file; never opened for writing directly.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_JQ, reason="jq is not installed on this host")
def test_latest_jpg_is_promoted_complete_stage_file_content(tmp_path):
    """Requirement 2.6: latest.jpg appears via atomic mv promotion of a
    complete stage file — its content is byte-identical to a stage file the
    stub wrote in full, the promoted stage file is GONE (mv, not cp), and
    the consumer-facing chmod 666 is applied."""
    with _Supervisor(str(tmp_path),
                     config={"gain": 4, "exposure": 5000000}) as sup:
        assert _wait_for(lambda: os.path.exists(sup.latest_jpg())), (
            "latest.jpg never appeared; invocations: {!r}"
            .format(sup.gst_invocations()))
        with open(sup.latest_jpg(), encoding="utf-8") as f:
            content = f.read()
        # The stub wrote exactly these two COMPLETE stage payloads for
        # invocation 1; whichever was promoted, the bytes must match one
        # in full (a partial read would truncate).
        assert content in ("stage-frame-1-0", "stage-frame-1-1"), (
            "latest.jpg does not equal a complete stage file's content: "
            "{!r}".format(content))
        promoted = "stage_00000.jpg" if content.endswith("-0") \
            else "stage_00001.jpg"
        assert not os.path.exists(
            os.path.join(sup.capture_dir, promoted)), (
            "the promoted stage file still exists — promotion must be an "
            "atomic mv (rename), not a copy")
        assert (os.stat(sup.latest_jpg()).st_mode & 0o777) == 0o666, (
            "latest.jpg is not chmod 666 for the container consumer")


def test_script_never_redirects_directly_into_latest_jpg():
    """Requirement 2.6 (text leg): the script never opens $LATEST_IMAGE for
    writing directly — the ONLY producer is the atomic mv from a stage
    file. A direct redirection would let consumers observe partial
    frames."""
    with open(CAPTURE_SCRIPT, encoding="utf-8") as f:
        text = f.read()
    direct_writes = re.findall(
        r'[^12&]>+\s*"?\$\{?LATEST_IMAGE\}?"?', text)
    assert not direct_writes, (
        "the capture script redirects output directly into $LATEST_IMAGE "
        "— latest.jpg must only ever be produced by mv from a complete "
        "stage file: {!r}".format(direct_writes))
    assert re.search(r'\bmv\b[^\n]*"\$LATEST_IMAGE"', text), (
        "the atomic mv promotion into $LATEST_IMAGE is missing")


# ---------------------------------------------------------------------------
# Unit legs: config diff detection per key, crop-params construction,
# default-config bootstrap. These drive the script's REAL functions
# (extracted verbatim, real jq) — the task-2 identity-test pattern.
# ---------------------------------------------------------------------------

def _read_script():
    with open(CAPTURE_SCRIPT, encoding="utf-8") as f:
        return f.read()


def _extract_function(name):
    """Extract a top-level `name() { ... }` block verbatim from the shipped
    capture script."""
    lines = _read_script().splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*" + re.escape(name) + r"\s*\(\)\s*\{?\s*$", line):
            start = i
            break
    assert start is not None, (
        "nvidia_csi_capture.sh no longer defines {}()".format(name))
    for j in range(start + 1, len(lines)):
        if re.match(r"^\}\s*$", lines[j]):
            return "\n".join(lines[start:j + 1])
    raise AssertionError(
        "could not find the closing brace of {}()".format(name))


def _extract_change_condition():
    """The supervisor loop's ACTUAL settings-change condition, extracted
    from the script text so the unit driver exercises the shipped
    comparison, not a re-implementation."""
    match = re.search(
        r'if (\[ "\$GAIN" != "\$LAST_GAIN" \][^\n]*); then', _read_script())
    assert match, (
        "the supervisor's settings-change comparison "
        '(`[ "$GAIN" != "$LAST_GAIN" ] || ...`) is missing from '
        "nvidia_csi_capture.sh")
    return match.group(1)


_DIFF_DRIVER_TEMPLATE = """\
set -u
USE_JQ=true
{read_config}
RELAUNCHES=0
FIRST=1
for CONFIG_FILE in "$@"; do
    read_config
    CURRENT_CROP="$CROP_TOP,$CROP_BOTTOM,$CROP_LEFT,$CROP_RIGHT"
    if [ "$FIRST" = "1" ]; then
        FIRST=0
    elif {condition}; then
        RELAUNCHES=$((RELAUNCHES + 1))
    fi
    LAST_GAIN=$GAIN
    LAST_EXPOSURE=$EXPOSURE
    LAST_CROP=$CURRENT_CROP
done
echo "$RELAUNCHES"
"""


def _count_relaunch_decisions(tmp_path, configs):
    """Run the extracted read_config + the extracted change condition over
    a sequence of config.json payloads; return the relaunch count."""
    paths = []
    for i, config in enumerate(configs):
        path = os.path.join(str(tmp_path), "config_{:03d}.json".format(i))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f)
        paths.append(path)
    driver = _DIFF_DRIVER_TEMPLATE.format(
        read_config=_extract_function("read_config"),
        condition=_extract_change_condition())
    result = subprocess.run(
        ["bash", "-c", driver, "diff_driver"] + paths,
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (
        "diff driver failed (rc={}, stderr={!r})".format(
            result.returncode, result.stderr))
    return int(result.stdout.strip())


_BASE_CONFIG = {"gain": 4, "exposure": 5000000,
                "crop": {"top": 0, "bottom": 0, "left": 0, "right": 0}}


@pytest.mark.skipif(not _HAS_JQ, reason="jq is not installed on this host")
@pytest.mark.parametrize("key,changed", [
    ("gain", {"gain": 9}),
    ("exposure", {"exposure": 100000}),
    ("crop.top", {"crop": {"top": 8, "bottom": 0, "left": 0, "right": 0}}),
    ("crop.bottom",
     {"crop": {"top": 0, "bottom": 8, "left": 0, "right": 0}}),
    ("crop.left", {"crop": {"top": 0, "bottom": 0, "left": 8, "right": 0}}),
    ("crop.right",
     {"crop": {"top": 0, "bottom": 0, "left": 0, "right": 8}}),
])
def test_config_diff_detected_per_key(tmp_path, key, changed):
    """Requirement 2.7 (unit leg): the shipped change-detection logic sees
    a change in EACH individual key — gain, exposure, and every crop edge —
    as exactly one relaunch decision, while an identical rewrite adds
    zero."""
    changed_config = dict(_BASE_CONFIG)
    changed_config.update(changed)
    # base → base (no-op) → changed → changed (no-op): exactly 1 relaunch.
    count = _count_relaunch_decisions(
        tmp_path, [_BASE_CONFIG, _BASE_CONFIG, changed_config,
                   changed_config])
    assert count == 1, (
        "a change in {} alone must be exactly one relaunch decision "
        "(no-op rewrites zero), got {}".format(key, count))


@pytest.mark.parametrize("edges,expected", [
    ((0, 0, 0, 0), ""),
    ((10, 0, 0, 0), "videocrop top=10 bottom=0 left=0 right=0 !"),
    ((0, 3, 0, 0), "videocrop top=0 bottom=3 left=0 right=0 !"),
    ((0, 0, 7, 0), "videocrop top=0 bottom=0 left=7 right=0 !"),
    ((0, 0, 0, 5), "videocrop top=0 bottom=0 left=0 right=5 !"),
    ((10, 20, 30, 40), "videocrop top=10 bottom=20 left=30 right=40 !"),
])
def test_build_crop_params_construction(edges, expected):
    """Requirement 2.7 (unit leg): the REAL build_crop_params emits an
    empty CROP_PARAMS for all-zero edges (no videocrop stage) and the
    `videocrop top=.. bottom=.. left=.. right=.. !` stage whenever any edge
    is nonzero."""
    top, bottom, left, right = edges
    driver = (
        "set -u\n"
        "CROP_TOP={}\nCROP_BOTTOM={}\nCROP_LEFT={}\nCROP_RIGHT={}\n"
        "{}\n"
        "build_crop_params\n"
        'printf \'%s\' "$CROP_PARAMS"\n'
    ).format(top, bottom, left, right, _extract_function(
        "build_crop_params"))
    result = subprocess.run(["bash", "-c", driver], capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected, (
        "build_crop_params for edges {} produced {!r}, expected {!r}"
        .format(edges, result.stdout.strip(), expected))


@pytest.mark.skipif(not _HAS_JQ, reason="jq is not installed on this host")
def test_default_config_bootstrap_creates_payload_and_chmod_666(tmp_path):
    """Preservation-shaped unit leg (2.7's read path): with NO config.json
    present, the REAL script bootstraps the default payload
    {"gain":4,"exposure":5000000} with chmod 666 (backend-writable), and
    the first launch uses those defaults."""
    with _Supervisor(str(tmp_path)) as sup:  # no config written
        config_path = os.path.join(sup.capture_dir, "config.json")
        assert _wait_for(lambda: os.path.exists(config_path)), (
            "the script did not bootstrap a default config.json")
        assert _wait_for(lambda: len(sup.gst_invocations()) >= 1)
        argv = sup.gst_invocations()[0]
        with open(config_path, encoding="utf-8") as f:
            payload = json.load(f)
        mode = os.stat(config_path).st_mode & 0o777

    assert payload == {"gain": 4, "exposure": 5000000}, (
        "default config payload changed: {!r}".format(payload))
    assert mode == 0o666, (
        "bootstrapped config.json is not chmod 666 (got {:o}) — the "
        "backend container could not write settings".format(mode))
    assert "gainrange=4 4" in argv and \
        "exposuretimerange=5000000 5000000" in argv, (
        "first launch does not use the bootstrapped defaults: {!r}"
        .format(argv))
