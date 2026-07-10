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
"""``setup_station.sh`` full-file golden (Req 3.2) -- dependency CVE spec.

Spec: security-dependency-cve-fixes -- Property 2: Preservation.

Capture every line of ``station_install/setup_station.sh`` on the UNFIXED tree,
recording line 513's baseline ``requests==2.32.3`` pin content (the F1 site,
located by content: the ``$PYTHON311 ... --force-reinstall requests==`` line).
Task 7 re-runs this against the FIXED tree and asserts the file differs ONLY at
that pin line's ``2.32.3`` -> ``2.32.4`` token; every other line -- the
``PYTHON311`` detection block, the ``--upgrade pip`` step, the ``protobuf``
install, and CRITICALLY the UNPINNED ``python3`` (3.6) installs -- is
byte-for-byte identical.

Golden: ``baselines/dependency_baseline_setup_station.txt``.

**Validates: Requirements 3.2**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_dependency_setup_station.py \
        -p no:cacheprovider --noconftest -v
"""
from _dependency_preservation_support import (
    BASELINE_REQUESTS_VERSION,
    SETUP_STATION_REL,
    assert_pin_file_matches_baseline,
)

_GOLDEN = "dependency_baseline_setup_station.txt"

# The F1 pin line, located by content (not line number): the Python-3.11
# force-reinstall of requests. This is the ONLY line allowed to change (version
# token only).
_F1_PIN_SUBSTRING = "$PYTHON311 -m pip install --force-reinstall requests=="


# Validates: Requirements 3.2
def test_setup_station_full_file_golden():
    """Every line of ``setup_station.sh`` is byte-for-byte identical to the
    baseline except the F1 pin line, which may differ ONLY in its version token
    (``2.32.3`` unfixed -> ``2.32.4`` fixed)."""
    assert_pin_file_matches_baseline(_GOLDEN, SETUP_STATION_REL, _F1_PIN_SUBSTRING)


# Validates: Requirements 3.2
def test_setup_station_baseline_records_2_32_3_pin():
    """The captured baseline records line 513's ``requests==2.32.3`` content, so
    task 7 can assert the fix flips exactly that token to ``2.32.4``."""
    golden_lines, _current_lines, pin_idx = assert_pin_file_matches_baseline(
        _GOLDEN, SETUP_STATION_REL, _F1_PIN_SUBSTRING
    )
    baseline_pin_line = golden_lines[pin_idx]
    assert f"requests=={BASELINE_REQUESTS_VERSION}" in baseline_pin_line, (
        "baseline F1 pin line should record requests==2.32.3, got: "
        f"{baseline_pin_line!r}"
    )
    # Sanity: the located pin line is the Python-3.11 force-reinstall, and the
    # surrounding structure (run_cmd / --force-reinstall / $PYTHON311 / the
    # add_warning tail) is recorded in the golden for the task-7 flip.
    assert "run_cmd" in baseline_pin_line
    assert "--force-reinstall" in baseline_pin_line
    assert "$PYTHON311" in baseline_pin_line
    assert 'add_warning "Failed to install requests"' in baseline_pin_line
