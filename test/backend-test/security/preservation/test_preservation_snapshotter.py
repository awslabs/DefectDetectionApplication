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
"""#1 Snapshotter preservation baseline (Req 3.1).

Spec: security-injection-deserialization-fixes — Property 2: Preservation.

For a VALID ``^[a-zA-Z0-9_-]+$`` ``stationName`` (timestamp pinned), the UNFIXED
``take_snapshot`` builds ``path = "/aws_dda/system/snapshot-<name>-<ts>.tar"``,
invokes ``subprocess.check_output(["sh", "/snapshot/snapshot.sh", path])`` and
returns ``"snapshotfile/snapshot-<name>-<ts>.tar.gz"``. The fix (task 3) only
adds allowlist validation + a pathlib constraint IN FRONT of this for invalid
names; for valid names the path and return must be byte-for-byte identical.

Baseline model (recorded, keyed by ``stationName`` + pinned timestamp):
    file = "snapshot-" + name + "-" + ts + ".tar"
    argv = ["sh", "/snapshot/snapshot.sh", "/aws_dda/system/" + file]
    return = "snapshotfile/" + file + ".gz"

These tests load the REAL Snapshotter.py in isolation, so task 13 re-runs them
unchanged against the fixed source.

**Validates: Requirements 3.1**

Run:
    python3 -m pytest test/backend-test/security/preservation/test_preservation_snapshotter.py \
        -p no:cacheprovider --noconftest -v
"""
import datetime

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from _preservation_support import load_module_from_path

# Pinned timestamp so the baseline is deterministic (F uses datetime.now()).
PINNED = datetime.datetime(2024, 1, 2, 3, 4, 5)
PINNED_TS = "2024-01-02-03-04-05"

SNAPSHOT_SH = "/snapshot/snapshot.sh"
SNAPSHOT_DIR = "/aws_dda/system/"


class _FixedDateTime:
    """Drop-in for the module's ``datetime`` so ``datetime.now()`` is pinned."""

    @staticmethod
    def now():
        return PINNED


def _expected(name, ts=PINNED_TS):
    file = "snapshot-" + name + "-" + ts + ".tar"
    path = SNAPSHOT_DIR + file
    return file, path, "snapshotfile/" + file + ".gz"


def _load_snapshotter_with_capture():
    """Load the real Snapshotter, pin the clock, and capture the argv passed to
    the shell script. Returns (module, captured-dict)."""
    snap = load_module_from_path(
        "snapshotter_preservation", "src/backend/snapshot/Snapshotter.py"
    )
    snap.datetime = _FixedDateTime
    captured = {}
    snap.subprocess.check_output = (
        lambda argv, *a, **k: captured.__setitem__("argv", list(argv)) or b""
    )
    return snap, captured


# --------------------------------------------------------------------------- #
# Example baseline
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.1
def test_valid_name_baseline_path_and_return():
    """Canonical valid name reproduces the recorded path + return."""
    snap, captured = _load_snapshotter_with_capture()
    name = "Station_01-A"
    file, path, expected_return = _expected(name)

    result = snap.take_snapshot(name)

    assert captured["argv"] == ["sh", SNAPSHOT_SH, path]
    assert result == expected_return
    assert result == "snapshotfile/snapshot-Station_01-A-2024-01-02-03-04-05.tar.gz"


# --------------------------------------------------------------------------- #
# Property: for every valid stationName, F builds the same path + return
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.1
@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(name=st.from_regex(r"\A[a-zA-Z0-9_-]{1,40}\Z"))
def test_valid_names_preserve_path_and_return_property(name):
    """Invariant: for any valid ``^[a-zA-Z0-9_-]+$`` name, the argv passed to the
    snapshot script is exactly ``["sh", "/snapshot/snapshot.sh",
    "/aws_dda/system/snapshot-<name>-<ts>.tar"]`` and the return is
    ``"snapshotfile/<file>.gz"`` — the recorded F baseline (task 13 must match)."""
    snap, captured = _load_snapshotter_with_capture()
    file, path, expected_return = _expected(name)

    result = snap.take_snapshot(name)

    assert captured["argv"] == ["sh", SNAPSHOT_SH, path], (
        f"path for {name!r} changed: {captured['argv']!r}"
    )
    # The path stays inside /aws_dda/system/ (defense-in-depth constraint the fix
    # adds must not move a valid snapshot elsewhere).
    assert captured["argv"][2].startswith(SNAPSHOT_DIR)
    assert result == expected_return, f"return for {name!r} changed: {result!r}"
