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
"""Unpinned Python-3.6 host installs golden (Req 3.1 -- CRITICAL) -- dependency
CVE spec.

Spec: security-dependency-cve-fixes -- Property 2: Preservation.

This is the **primary preservation guarantee** for this batch: the CRITICAL
JP4 / Ubuntu-18.04 / Python-3.6 constraint. ``requests==2.32.4`` requires Python
3.8+, so the fix MUST NOT add any ``==`` version pin to the UNPINNED *system*
``python3`` (3.6) host installs, and MUST NOT raise the Python floor on anything
Python 3.6 touches. We locate the three unpinned install lines by CONTENT
(never hardcoded line numbers) and record them byte-for-byte:

* ``station_install/setup_station.sh`` ~518 -- ``python3 -m pip install requests protobuf``
* ``station_install/setup_station.sh`` ~542 -- ``python3 -m pip install --upgrade requests``
* ``station_install/patch_docker_host_prereqs.sh`` ~242 -- ``"$py" -m pip install --upgrade requests``

Each carries NO ``==`` specifier, and the audit's ``_REQUESTS_PIN_RE`` does NOT
match them (proving they stay clear of the pin gate). On the UNFIXED tree the
golden is captured; task 7 re-runs this against the FIXED tree and asserts these
lines are byte-for-byte identical (no pin added, Python floor NOT raised).

Golden: ``baselines/dependency_baseline_unpinned_py36.json``.

**Validates: Requirements 3.1**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_dependency_unpinned_py36.py \
        -p no:cacheprovider --noconftest -v
"""
from _dependency_preservation_support import (
    PATCH_DOCKER_HOST_REL,
    SETUP_STATION_REL,
    capture_or_assert_json,
    import_audit,
    located_line,
)

_GOLDEN = "dependency_baseline_unpinned_py36.json"

# The three UNPINNED Python-3.6 host installs, located by unique content
# substrings (robust to line-number drift). None may carry a ``==`` pin.
_UNPINNED_SITES = (
    (SETUP_STATION_REL, 'run_cmd "python3 -m pip install requests protobuf"'),
    (SETUP_STATION_REL, 'run_cmd "python3 -m pip install --upgrade requests"'),
    (PATCH_DOCKER_HOST_REL, '"$py" -m pip install --upgrade requests'),
)


def _current_entries():
    entries = []
    for rel_path, substring in _UNPINNED_SITES:
        lineno, text = located_line(rel_path, substring)
        entries.append(
            {
                "file": rel_path.replace("\\", "/"),
                "lineno": lineno,
                "text": text,
                "has_version_pin": "==" in text,
            }
        )
    return entries


# Validates: Requirements 3.1
def test_unpinned_py36_installs_golden():
    """The three unpinned Python-3.6 host installs match the captured golden
    byte-for-byte (no ``==`` pin added, no Python floor raised)."""
    current = {
        "note": (
            "CRITICAL Python-3.6 constraint: these system-python3 installs MUST "
            "stay UNPINNED (no ==) so pip resolves a 3.6-compatible requests on "
            "Ubuntu 18.04. requests==2.32.4 requires Python 3.8+, so the bump "
            "applies ONLY to the two Python-3.11 pin sites, never here."
        ),
        "entries": _current_entries(),
    }
    recorded = capture_or_assert_json(_GOLDEN, current)
    assert current == recorded


# Validates: Requirements 3.1
def test_unpinned_installs_carry_no_version_specifier():
    """Each located host install line carries NO ``==`` version specifier."""
    for entry in _current_entries():
        assert entry["has_version_pin"] is False, (
            f"{entry['file']}:{entry['lineno']} unexpectedly carries a version "
            f"pin: {entry['text']!r}"
        )


# Validates: Requirements 3.1
def test_pin_regex_does_not_match_unpinned_installs():
    """The audit's ``_REQUESTS_PIN_RE`` does NOT match any unpinned install line,
    proving the Python-3.6 host installs stay clear of the pin gate."""
    audit = import_audit()
    for entry in _current_entries():
        assert audit._REQUESTS_PIN_RE.search(entry["text"]) is None, (
            f"{entry['file']}:{entry['lineno']} was matched by the requests pin "
            f"regex but must stay unpinned: {entry['text']!r}"
        )
