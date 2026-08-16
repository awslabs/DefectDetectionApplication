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
"""Preservation tests (Task 2) for csi-nvargus-optional.

Property 2: Preservation — Everything Outside the CSI Exposure Surface Is
Unchanged.

Observation-first methodology: every golden under
``test/backend-test/csi_nvargus_optional/goldens/`` was captured from the
UNFIXED tree (2026-08-16, branch spec/jetpack7-support, before task 3). These
tests PASS on the unfixed tree and must KEEP passing on the fixed tree
(re-run in task 3.7):

- **Recipes byte-identical (3.5)**: sha256 of all five arm64 recipes + both
  amd64 recipes pinned in ``goldens/recipe_sha256.json``. Design Decision 1's
  keystone — the fix gates the installer SCRIPT, never the recipes.
- **setup_station prefix property (3.4, 3.6)**: every one of the 1625 unfixed
  lines of ``station_install/setup_station.sh`` (pinned byte-for-byte in
  ``goldens/setup_station_unfixed_prefix.txt``) is byte-identical AND in the
  same position in the fixed file — the task 3.1 CSI opt-in block is strictly
  APPENDED. The only allowed divergence is the requests-pin version token on
  the ``$PYTHON311 ... --force-reinstall requests==`` line (the same single
  allowance the security gate's
  ``test_preservation_dependency_setup_station.py`` grants). Corollary
  asserted directly: the ``dependency_baseline_unpinned_py36.json`` entries
  still resolve at their recorded line numbers (656, 680).
- **Staged-frame contract fingerprint (3.1, 3.2)**: the contract constants
  observed verbatim in the UNFIXED ``nvidia_csi_capture.sh`` — capture dir,
  ``latest.jpg``, ``config.json`` keys + jq defaults (gain 4, exposure
  5000000, crop 0s), 3264x2464@21/1 caps, ``jpegenc idct-method=2
  quality=100``, ``aeantibanding=0``/``wbmode=0``/``exposuretimerange``/
  ``gainrange``, atomic ``mv`` staging + ``chmod 666``, videocrop
  construction — all of which the task 3.3 persistent-pipeline rewrite must
  preserve verbatim (design File 3 "What is preserved verbatim").
- **Backend untouched (3.1, 3.3, 3.7)**: sha256 of the CSI consumer files
  (``workflow_engine/csi_capture.py``, ``workflow_engine/pipeline_executor.py``,
  ``gstreamer/pipeline_builder.py``,
  ``workflow_engine/vendor/workflow_core/catalog/nodes.py``) pinned in
  ``goldens/backend_csi_consumers_sha256.json`` — no file under
  ``src/backend/`` changes in this spec.

Honesty guard: text/hash-level assertions only; nothing here executes
gst-launch, Argus, CUDA, or systemd.

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7
"""
import hashlib
import json
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

GOLDENS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "goldens")
SETUP_STATION = os.path.join(REPO_ROOT, "station_install", "setup_station.sh")
CAPTURE_SCRIPT = os.path.join(REPO_ROOT, "src", "host_scripts",
                              "nvidia_csi_capture.sh")
UNPINNED_PY36_BASELINE = os.path.join(
    REPO_ROOT, "test", "backend-test", "security", "baselines",
    "dependency_baseline_unpinned_py36.json")

#: The one line allowed to differ from the unfixed prefix, and only in its
#: version token — the same allowance the security gate grants (the
#: dependency-CVE spec's F1 pin site).
_F1_PIN_SUBSTRING = "$PYTHON311 -m pip install --force-reinstall requests=="
_F1_VERSION_TOKEN = re.compile(r"requests==[0-9][0-9a-zA-Z.\-]*")


def _read(path):
    assert os.path.isfile(path), (
        "expected file does not exist: {}".format(path))
    with open(path, encoding="utf-8") as f:
        return f.read()


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _golden(name):
    path = os.path.join(GOLDENS_DIR, name)
    assert os.path.isfile(path), (
        "missing golden {} — the task 2 unfixed-tree baseline was not "
        "captured".format(path))
    return path


def _logical_lines(text):
    """Shell source lines with backslash continuations joined (same helper
    as the exploration suite), so multi-line pipeline invocations are
    inspected as one logical command."""
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


# ---------------------------------------------------------------------------
# Recipes byte-identical (3.5): all five arm64 + both amd64 recipes hash to
# their unfixed-tree sha256 goldens. The output_bindings_fixes and
# deploy_reliability golden suites double-cover the parsed structure; this is
# the byte-level pin.
# ---------------------------------------------------------------------------

with open(os.path.join(GOLDENS_DIR, "recipe_sha256.json"),
          encoding="utf-8") as _f:
    _RECIPE_GOLDEN = json.load(_f)


@pytest.mark.parametrize("recipe_name", sorted(_RECIPE_GOLDEN))
def test_recipe_byte_identical_to_unfixed_golden(recipe_name):
    """Requirement 3.5: no recipe YAML changes in this spec — every arm64
    and amd64 recipe stays byte-identical to its unfixed-tree hash
    (design Decision 1: the fix gates the installer script, not the
    recipes).

    Validates: Requirements 3.5
    """
    path = os.path.join(REPO_ROOT, recipe_name)
    assert _sha256(path) == _RECIPE_GOLDEN[recipe_name], (
        "{} changed since the unfixed-tree baseline — this spec must not "
        "touch any recipe YAML (Decision 1 keeps all five arm64 recipes "
        "and both amd64 recipes byte-identical; a change here also "
        "invalidates the output_bindings_fixes/deploy_reliability recipe "
        "goldens)".format(recipe_name))


# ---------------------------------------------------------------------------
# setup_station prefix property (3.4, 3.6): the unfixed file is pinned
# byte-for-byte as goldens/setup_station_unfixed_prefix.txt (1625 lines).
# Every unfixed line must remain byte-identical and IN THE SAME POSITION in
# the (future) fixed file — the CSI opt-in block is strictly appended.
# ---------------------------------------------------------------------------

def _prefix_golden_lines():
    return _read(_golden("setup_station_unfixed_prefix.txt")).splitlines()


def test_setup_station_unfixed_lines_form_a_byte_identical_prefix():
    """Requirement 3.4: every existing provisioning step unchanged — each of
    the 1625 unfixed lines is byte-identical at the same line number in the
    current file; anything the fix adds comes strictly AFTER them. Only the
    F1 requests-pin line may differ, and only in its version token (the
    security gate's own allowance).

    Validates: Requirements 3.4, 3.6
    """
    golden_lines = _prefix_golden_lines()
    assert len(golden_lines) == 1625, (
        "the unfixed-tree golden should have exactly 1625 lines (the "
        "recorded unfixed line count), got {}".format(len(golden_lines)))
    current_lines = _read(SETUP_STATION).splitlines()
    assert len(current_lines) >= len(golden_lines), (
        "setup_station.sh shrank below the unfixed line count ({} < {}) — "
        "existing provisioning lines were deleted, not preserved"
        .format(len(current_lines), len(golden_lines)))
    for idx, (golden, current) in enumerate(
            zip(golden_lines, current_lines), start=1):
        if golden == current:
            continue
        # The single allowed divergence: the F1 requests-pin version token.
        assert (_F1_PIN_SUBSTRING in golden
                and _F1_PIN_SUBSTRING in current
                and _F1_VERSION_TOKEN.sub("requests==X", golden)
                == _F1_VERSION_TOKEN.sub("requests==X", current)), (
            "setup_station.sh line {} diverged from the unfixed baseline — "
            "the CSI opt-in block must be strictly APPENDED, never "
            "inserted/edited mid-file (shifting lines breaks the "
            "unpinned-py36 golden's recorded line numbers).\n"
            "  unfixed: {!r}\n  current: {!r}"
            .format(idx, golden, current))


def test_unpinned_py36_entries_resolve_at_recorded_line_numbers():
    """Requirement 3.4/3.6 corollary asserted directly: the security gate's
    dependency_baseline_unpinned_py36.json records the unpinned
    system-python3 install lines of setup_station.sh WITH line numbers
    (656, 680); those lines must still be found verbatim at exactly those
    numbers — the premise that an end-of-file append never shifts them.

    Validates: Requirements 3.4, 3.6
    """
    with open(UNPINNED_PY36_BASELINE, encoding="utf-8") as f:
        baseline = json.load(f)
    entries = [e for e in baseline["entries"]
               if e["file"] == "station_install/setup_station.sh"]
    assert sorted(e["lineno"] for e in entries) == [656, 680], (
        "the unpinned-py36 baseline no longer records setup_station.sh "
        "entries at lines 656 and 680 — it was rebaselined with shifted "
        "line numbers, meaning the CSI block was NOT strictly appended: "
        "{!r}".format(entries))
    current_lines = _read(SETUP_STATION).splitlines()
    for entry in entries:
        lineno = entry["lineno"]
        assert len(current_lines) >= lineno, (
            "setup_station.sh has only {} lines; baseline entry expects "
            "line {}".format(len(current_lines), lineno))
        assert current_lines[lineno - 1] == entry["text"], (
            "setup_station.sh line {} no longer matches the security "
            "baseline's recorded text — the unpinned system-python3 "
            "install line shifted or changed.\n  recorded: {!r}\n"
            "  current:  {!r}"
            .format(lineno, entry["text"], current_lines[lineno - 1]))


# ---------------------------------------------------------------------------
# Staged-frame contract fingerprint (3.1, 3.2): the contract constants as
# observed VERBATIM in the unfixed nvidia_csi_capture.sh. The task 3.3
# persistent-pipeline rewrite must carry every one of them forward (design
# File 3 "What is preserved verbatim"); only num-buffers=1, the per-frame
# loop, sleep 0.1 and the stderr discard may go.
# ---------------------------------------------------------------------------

def test_capture_script_contract_paths():
    """The capture directory, staged frame, and config file paths — the
    consumer contract the backend file-source pipeline depends on.

    Validates: Requirements 3.1, 3.2
    """
    content = _read(CAPTURE_SCRIPT)
    assert 'CAPTURE_DIR="/aws_dda/nvidia-csi-capture"' in content, (
        "capture dir /aws_dda/nvidia-csi-capture no longer declared — the "
        "backend consumer contract (csi_capture.CSI_CAPTURE_DIR) breaks")
    assert '"$CAPTURE_DIR/latest.jpg"' in content, (
        "latest.jpg staging target no longer derived from $CAPTURE_DIR — "
        "the staged-frame contract path changed")
    assert '"$CAPTURE_DIR/config.json"' in content, (
        "config.json path no longer derived from $CAPTURE_DIR — the "
        "backend's write_csi_config target changed")


def test_capture_script_default_config_bootstrap():
    """The default-config bootstrap: exact JSON content and world-writable
    mode, so a fresh device converges to gain=4 exposure=5000000 and the
    backend can overwrite the file.

    Validates: Requirements 3.2
    """
    content = _read(CAPTURE_SCRIPT)
    assert "'{\"gain\":4,\"exposure\":5000000}'" in content, (
        "the default config bootstrap payload "
        "'{\"gain\":4,\"exposure\":5000000}' is gone")
    assert re.search(r'chmod\s+666\s+"\$CONFIG_FILE"', content), (
        "config.json is no longer chmod 666 after bootstrap — the backend "
        "(non-root container) could not write acquisition settings")


def test_capture_script_jq_read_config_keys_and_defaults():
    """The jq read expressions with their defaults: gain 4, exposure
    5000000, crop.top/bottom/left/right 0 — the exact config.json schema
    the backend writes via csi_capture.write_csi_config.

    Validates: Requirements 3.2
    """
    content = _read(CAPTURE_SCRIPT)
    for expr in ("'.gain // 4'",
                 "'.exposure // 5000000'",
                 "'.crop.top // 0'",
                 "'.crop.bottom // 0'",
                 "'.crop.left // 0'",
                 "'.crop.right // 0'"):
        assert expr in content, (
            "jq read expression {} missing — the config.json key set or a "
            "default changed (contract: gain 4, exposure 5000000, crop "
            "edges 0)".format(expr))
    assert "read_config()" in content, (
        "the read_config function is gone — the sourceable config-read "
        "shape (which the property suite drives) was not preserved")


def test_capture_script_caps_and_encoder_constants():
    """Resolution/caps and JPEG encoder constants: 3264x2464 @ 21/1 NVMM
    caps and jpegenc idct-method=2 quality=100.

    Validates: Requirements 3.1
    """
    content = " ".join(_logical_lines(_read(CAPTURE_SCRIPT)))
    assert "width=3264,height=2464,framerate=21/1" in content, (
        "the 3264x2464@21/1 caps changed — staged frames would no longer "
        "match the consumer-expected resolution")
    assert "jpegenc idct-method=2 quality=100" in content, (
        "the jpegenc idct-method=2 quality=100 encoder settings changed")


def test_capture_script_validated_manual_exposure_parameter_set():
    """The validated manual-exposure method (NVIDIA_CSI_SETTINGS_FIX.md):
    aeantibanding=0, wbmode=0, exposuretimerange \"$E $E\",
    gainrange \"$G $G\" on nvarguscamerasrc.

    Validates: Requirements 3.2
    """
    content = " ".join(_logical_lines(_read(CAPTURE_SCRIPT)))
    assert "aeantibanding=0" in content, (
        "aeantibanding=0 missing — auto-exposure antibanding would fight "
        "manual exposure")
    assert "wbmode=0" in content, (
        "wbmode=0 missing — auto white balance would fight manual settings")
    assert 'exposuretimerange="$EXPOSURE $EXPOSURE"' in content, (
        'exposuretimerange="$EXPOSURE $EXPOSURE" missing — the validated '
        "manual-exposure method changed")
    assert 'gainrange="$GAIN $GAIN"' in content, (
        'gainrange="$GAIN $GAIN" missing — the validated manual-gain '
        "method changed")


def test_capture_script_atomic_mv_staging_and_permissions():
    """Atomic staging: latest.jpg only ever appears via mv (never a partial
    read for consumers) and is chmod 666 so the container can read it.

    Validates: Requirements 3.1
    """
    lines = _logical_lines(_read(CAPTURE_SCRIPT))
    mv_lines = [line for line in lines
                if re.search(r'\bmv\b.*"\$LATEST_IMAGE"', line)]
    assert mv_lines, (
        "no `mv ... \"$LATEST_IMAGE\"` found — the atomic-replace staging "
        "of latest.jpg is gone (consumers could read partial frames)")
    chmod_lines = [line for line in lines
                   if re.search(r'chmod\s+666\s+"\$LATEST_IMAGE"', line)]
    assert chmod_lines, (
        "no `chmod 666 \"$LATEST_IMAGE\"` found — the staged frame would "
        "not be readable by the backend container")


def test_capture_script_videocrop_construction():
    """The videocrop element construction from the four crop edges,
    inserted only when a crop is configured.

    Validates: Requirements 3.2
    """
    content = _read(CAPTURE_SCRIPT)
    assert ("videocrop top=$CROP_TOP bottom=$CROP_BOTTOM "
            "left=$CROP_LEFT right=$CROP_RIGHT") in content, (
        "the videocrop construction from CROP_TOP/BOTTOM/LEFT/RIGHT is "
        "gone — configured crops would no longer be applied to staged "
        "frames")


# ---------------------------------------------------------------------------
# Backend untouched (3.1, 3.3, 3.7): the CSI consumer files hash to their
# unfixed-tree sha256 goldens — no file under src/backend/ changes in this
# spec.
# ---------------------------------------------------------------------------

with open(os.path.join(GOLDENS_DIR, "backend_csi_consumers_sha256.json"),
          encoding="utf-8") as _f:
    _BACKEND_GOLDEN = json.load(_f)


@pytest.mark.parametrize("rel_path", sorted(_BACKEND_GOLDEN))
def test_backend_csi_consumer_file_untouched(rel_path):
    """Requirements 3.1/3.3/3.7: the backend CSI consumers (file-source
    pipeline builder, deployed-workflow executor, csi_capture config
    writer, catalog node mappings) are byte-identical to the unfixed tree
    — this spec changes host scripts and provisioning only, never backend
    code.

    Validates: Requirements 3.1, 3.3, 3.7
    """
    path = os.path.join(REPO_ROOT, rel_path)
    assert _sha256(path) == _BACKEND_GOLDEN[rel_path], (
        "{} changed since the unfixed-tree baseline — no file under "
        "src/backend/ may change in this spec (the CSI consumer contract, "
        "non-CSI camera paths, and inference paths must be preserved "
        "untouched)".format(rel_path))
