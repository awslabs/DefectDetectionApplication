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
"""Capture supervisor config-sequence property (Task 4.2) for
csi-nvargus-optional.

Property 3: Fix Checking — Persistent Pipeline Honors the Staging and
Settings Contracts (the settings-change arithmetic leg): _for any_ sequence
of ``config.json`` writes, the supervisor performs exactly ONE relaunch per
EFFECTIVE gain/exposure/crop change and ZERO relaunches for no-op rewrites
of identical values.

To keep the property fast and deterministic it drives the SHIPPED
change-detection logic rather than the full 1s-sleep supervisor loop
(sanctioned by task 4.2): the script's real ``read_config`` function AND
its real settings-change comparison condition are both extracted VERBATIM
from ``src/host_scripts/nvidia_csi_capture.sh`` (the comparison is pulled
out of the loop text, not re-implemented) and replayed with real jq over
the generated config sequence, exactly as the loop replays them once per
poll. The end-to-end relaunch behavior through the real backgrounded loop
(with a stub gst-launch-1.0 as the pipeline) is covered by
``test_capture_supervisor_behaviors.py``.

Honesty guard: no gst-launch, Argus, or CUDA is executed — the stub is the
pipeline in the behavioral suite, and this file executes only bash + jq.

Validates: Requirements 2.6, 2.7
"""
import json
import os
import re
import subprocess
import tempfile

import pytest
from hypothesis import given, settings, strategies as st

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

CAPTURE_SCRIPT = os.path.join(
    REPO_ROOT, "src", "host_scripts", "nvidia_csi_capture.sh")

_HAS_JQ = subprocess.run(["bash", "-c", "command -v jq"],
                         capture_output=True).returncode == 0


def _read_script():
    with open(CAPTURE_SCRIPT, encoding="utf-8") as f:
        return f.read()


def _extract_read_config():
    """The read_config() { ... } block, verbatim, from the shipped
    script (same extraction the task-2 identity PBT uses)."""
    lines = _read_script().splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*read_config\s*\(\)\s*\{?\s*$", line):
            start = i
            break
    assert start is not None, (
        "nvidia_csi_capture.sh no longer defines read_config()")
    for j in range(start + 1, len(lines)):
        if re.match(r"^\}\s*$", lines[j]):
            return "\n".join(lines[start:j + 1])
    raise AssertionError("could not find the closing brace of read_config()")


def _extract_change_condition():
    """The supervisor loop's ACTUAL settings-change condition, extracted
    from the shipped script text — the property exercises the code that
    ships, not a re-implementation of it."""
    match = re.search(
        r'if (\[ "\$GAIN" != "\$LAST_GAIN" \][^\n]*); then', _read_script())
    assert match, (
        "the supervisor's settings-change comparison "
        '(`[ "$GAIN" != "$LAST_GAIN" ] || ...`) is missing from '
        "nvidia_csi_capture.sh")
    return match.group(1)


_DRIVER_TEMPLATE = """\
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


def _effective_settings(config):
    """Python mirror of the shipped jq extraction (`.gain // 4` etc.): the
    effective (GAIN, EXPOSURE, CROP_T, CROP_B, CROP_L, CROP_R) tuple a
    config.json resolves to. jq's // yields the default when the value is
    absent or null; present values pass through."""
    def get(key, default):
        value = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return str(default)
            value = value[part]
        if value is None or value is False:
            return str(default)
        return str(value)
    return (get("gain", 4), get("exposure", 5000000),
            get("crop.top", 0), get("crop.bottom", 0),
            get("crop.left", 0), get("crop.right", 0))


#: Config payloads drawn from small pools so identical and jq-equivalent
#: rewrites (e.g. {"gain": 4} vs {} — both resolve to gain 4) occur
#: naturally alongside effective changes.
_GAIN = st.sampled_from([1, 4, 9])
_EXPOSURE = st.sampled_from([100000, 5000000])
_EDGE = st.sampled_from([0, 8])

_CONFIGS = st.fixed_dictionaries(
    {},
    optional={
        "gain": _GAIN,
        "exposure": _EXPOSURE,
        "crop": st.fixed_dictionaries(
            {},
            optional={"top": _EDGE, "bottom": _EDGE,
                      "left": _EDGE, "right": _EDGE}),
    })

#: A write sequence: each drawn config is written 1..3 times in a row —
#: the repeats are guaranteed byte-identical no-op rewrites, which must
#: NEVER count as a change.
_WRITE_SEQUENCES = st.lists(
    st.tuples(_CONFIGS, st.integers(min_value=1, max_value=3)),
    min_size=1, max_size=5)


@pytest.mark.skipif(not _HAS_JQ, reason="jq is not installed on this host")
@settings(deadline=None)
@given(sequence=_WRITE_SEQUENCES)
def test_exactly_one_relaunch_per_effective_change_zero_for_noop_rewrites(
        sequence):
    """Requirements 2.6 + 2.7: for ANY sequence of config.json writes, the
    shipped change-detection logic (real read_config + the loop's real
    comparison, replayed once per write exactly as the poll loop does)
    decides exactly ONE relaunch per EFFECTIVE settings change and ZERO for
    rewrites that resolve to identical effective values — every relaunch is
    a full Argus session, so the session arithmetic of Property 3 rides on
    this count being exact.

    # Validates: Requirements 2.6, 2.7
    """
    configs = [config for config, repeats in sequence
               for _ in range(repeats)]

    # Expected: one relaunch per change of the EFFECTIVE settings tuple
    # relative to the previous poll (the first read is the initial launch,
    # not a relaunch).
    tuples = [_effective_settings(c) for c in configs]
    expected = sum(1 for prev, cur in zip(tuples, tuples[1:])
                   if cur != prev)

    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, config in enumerate(configs):
            path = os.path.join(tmp, "config_{:03d}.json".format(i))
            with open(path, "w", encoding="utf-8") as f:
                json.dump(config, f)
            paths.append(path)
        driver = _DRIVER_TEMPLATE.format(
            read_config=_extract_read_config(),
            condition=_extract_change_condition())
        result = subprocess.run(
            ["bash", "-c", driver, "relaunch_driver"] + paths,
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, (
            "relaunch driver failed (rc={}, stderr={!r}) for sequence {!r}"
            .format(result.returncode, result.stderr, configs))
        got = int(result.stdout.strip())

    assert got == expected, (
        "the supervisor's change detection decided {} relaunches for the "
        "write sequence {!r} (effective tuples {!r}) — exactly {} "
        "effective changes occurred; no-op rewrites of identical values "
        "must never relaunch, effective changes must always relaunch "
        "exactly once".format(got, configs, tuples, expected))
