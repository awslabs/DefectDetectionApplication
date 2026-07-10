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
"""Negative-fixture tests for the dependency / supply-chain CVE audit gate
(Task 5 / finding F4 / Req 2.4).

Task 5 finalizes ``dependency_audit`` so that ``disallowed_hits()`` returns
``[]`` on the fixed tree (both in-scope pins are now ``requests==2.32.4``). The
subtle part of the gate is that it must classify each ``requests`` reference
PER-PIN and SCOPE-CORRECT -- NOT file-global and NOT across arbitrary files:

* A ``requests==<version>`` pin is flagged ONLY when its parsed version is
  ``< 2.32.4`` (CVE-2024-47081) and the line carries no ``# nosec`` marker.
* A BARE unpinned ``requests`` (no ``==``) -- as in the Python-3.6 system
  ``python3 -m pip install requests`` / ``--upgrade requests`` host installs --
  NEVER matches ``_REQUESTS_PIN_RE``, so it is never flagged. This is the
  CRITICAL Ubuntu-18.04 / Python-3.6 preservation guarantee: the unpinned host
  installs stay clear (no pin added, Python floor never raised).
* A ``urllib3==<v>`` pin is never matched by the requests regex.
* The gate parses pins from the TWO in-scope files ONLY
  (``station_install/setup_station.sh`` and ``src/backend/requirements.txt``),
  so an out-of-scope ``requests==2.32.3`` in the portal layer or ``cdk.out``
  is never parsed by ``run_audit()`` / ``disallowed_hits()``.

These tests prove the per-pin, scope-correct classification by driving the
module's own primitives (``_REQUESTS_PIN_RE``, ``_parse_version``,
``_pin_is_disallowed``, ``_has_nosem``) against SYNTHETIC in-memory pin-line
fixtures. They deliberately DO NOT mutate the real pin files (those are
exercised by the live ``disallowed_hits()`` gate and the exploration test), so a
reintroduced sub-2.32.4 in-scope pin is caught structurally regardless of the
current on-disk state.

Because the audit module classifies a bare pin *line* (skip ``# nosec`` lines,
then apply ``_pin_is_disallowed`` to each ``requests==`` match), this test file
adds a tiny module-level helper (``_line_is_flagged``) that mirrors exactly how
``disallowed_hits()`` classifies a single line -- so a synthetic pin line can be
classified in memory WITHOUT weakening the audit module.

They also confirm the two-layer contract on the current fixed tree: the raw
``run_audit()`` still enumerates the two in-scope pins (non-empty) while the
precise ``disallowed_hits()`` gate is empty, and the B324 allowlist still matches
(``verify_accepted_exceptions_still_match() == []``).

Validates: Requirements 2.4
"""

import dependency_audit as audit


# --- Synthetic in-scope pin lines (mirroring the real pin-file line shapes) - #

# The reverted / bug-condition sub-2.32.4 pins (F1/F2 shape).
_REVERTED_REQS_LINE = "requests==2.32.3"
_REVERTED_SETUP_LINE = (
    'run_cmd "$PYTHON311 -m pip install --force-reinstall requests==2.32.3"'
    ' || add_warning "Failed to install requests"'
)
# The fixed / compliant pin (>= 2.32.4).
_FIXED_REQS_LINE = "requests==2.32.4"
_FIXED_SETUP_LINE = (
    'run_cmd "$PYTHON311 -m pip install --force-reinstall requests==2.32.4"'
    ' || add_warning "Failed to install requests"'
)

# The three REAL unpinned host-install forms (Python-3.6 surface). None carry
# an ``==`` specifier, so none may ever match ``_REQUESTS_PIN_RE``.
_BARE_INSTALL = "run_cmd \"python3 -m pip install requests protobuf\""
_BARE_UPGRADE = "run_cmd \"python3 -m pip install --upgrade requests\""
_BARE_UPGRADE_PY = '"$py" -m pip install --upgrade requests'

# A documented ``# nosec`` suppression on an otherwise-disallowed pin.
_NOSEC_REVERTED_LINE = "requests==2.32.3  # nosec"

# An out-of-scope pin (portal layer / cdk.out) -- correct version, but the gate
# must never parse it because it lives outside the two in-scope files.
_OUT_OF_SCOPE_PORTAL_LINE = "requests==2.31.0"

# A urllib3 pin -- never a requests pin.
_URLLIB3_LINE = "urllib3==2.2.3"


# --------------------------------------------------------------------------- #
# Tiny in-memory line classifier mirroring how disallowed_hits() classifies a
# single line. Lives in the TEST file (NOT the audit module) so the gate logic
# is exercised through the module's own primitives without weakening it.
# --------------------------------------------------------------------------- #
def _line_is_flagged(line):
    """True iff ``disallowed_hits()`` would flag this line (assuming it lives in
    an in-scope pin file): skip ``# nosec`` lines, then flag if any
    ``requests==`` match parses to a disallowed (< 2.32.4) version."""
    if audit._has_nosem(line):
        return False
    for m in audit._REQUESTS_PIN_RE.finditer(line):
        if audit._pin_is_disallowed(m.group(1)):
            return True
    return False


# --------------------------------------------------------------------------- #
# _pin_is_disallowed predicate (per-version boundary at 2.32.4).
# --------------------------------------------------------------------------- #
def test_pin_is_disallowed_flags_sub_2_32_4_versions():
    """Every ``requests`` version below the CVE-2024-47081 floor (2.32.4) is
    disallowed -- including the two real reverted pins (2.32.3) and the portal
    pin value (2.31.0)."""
    for v in ("2.32.3", "2.31.0", "2.0.0", "1.99.99", "2.32.0"):
        assert audit._pin_is_disallowed(v) is True, v


def test_pin_is_disallowed_clears_2_32_4_and_above():
    """The fixed floor and every later version is cleared."""
    for v in ("2.32.4", "2.32.5", "2.33.0", "3.0.0"):
        assert audit._pin_is_disallowed(v) is False, v


def test_parse_version_normalizes_to_three_tuple():
    """``_parse_version`` normalizes to a 3-int tuple so the boundary compare is
    well-defined across the fixed floor."""
    assert audit._parse_version("2.32.4") == (2, 32, 4)
    assert audit._parse_version("2.32.3") == (2, 32, 3)
    assert audit._parse_version("2.31.0") == (2, 31, 0)
    # The boundary the whole gate hinges on.
    assert audit._parse_version("2.32.3") < audit.MIN_SAFE_REQUESTS
    assert not (audit._parse_version("2.32.4") < audit.MIN_SAFE_REQUESTS)


# --------------------------------------------------------------------------- #
# Per-pin line classification (regex match + predicate + nosec).
# --------------------------------------------------------------------------- #
def test_reverted_requests_pin_line_is_flagged():
    """A synthetic in-scope ``requests==2.32.3`` line classifies as disallowed
    (regex matches + predicate true + no nosec) -- both the bare requirements
    form and the setup_station force-reinstall form."""
    assert _line_is_flagged(_REVERTED_REQS_LINE) is True
    assert _line_is_flagged(_REVERTED_SETUP_LINE) is True


def test_fixed_requests_pin_line_is_not_flagged():
    """A ``requests==2.32.4`` line is cleared in both real line shapes."""
    assert _line_is_flagged(_FIXED_REQS_LINE) is False
    assert _line_is_flagged(_FIXED_SETUP_LINE) is False


def test_bare_unpinned_requests_never_matches_regex():
    """The three REAL unpinned host-install forms carry no ``==`` specifier, so
    ``_REQUESTS_PIN_RE`` never matches and they are never flagged -- proving the
    Ubuntu-18.04 / Python-3.6 host installs stay clear (no pin added)."""
    for line in (_BARE_INSTALL, _BARE_UPGRADE, _BARE_UPGRADE_PY):
        assert audit._REQUESTS_PIN_RE.search(line) is None, line
        assert _line_is_flagged(line) is False, line


def test_nosec_marker_clears_disallowed_pin():
    """A ``requests==2.32.3  # nosec`` line is cleared by ``_has_nosem`` even
    though the version itself is disallowed."""
    assert audit._has_nosem(_NOSEC_REVERTED_LINE) is True
    assert _line_is_flagged(_NOSEC_REVERTED_LINE) is False


def test_urllib3_pin_never_matches_requests_regex():
    """A ``urllib3==2.2.3`` line is never matched by the requests regex, so it
    is never flagged (requirements.txt:8 stays untouched)."""
    assert audit._REQUESTS_PIN_RE.search(_URLLIB3_LINE) is None
    assert _line_is_flagged(_URLLIB3_LINE) is False


# --------------------------------------------------------------------------- #
# Scope -- the gate parses ONLY the two in-scope files.
# --------------------------------------------------------------------------- #
def test_in_scope_pin_files_are_exactly_the_two_maintained_files():
    """``IN_SCOPE_PIN_FILES`` contains ONLY the two maintained Python-3.11 pin
    files, so an out-of-scope ``requests==2.32.3`` (portal layer / cdk.out) is
    never parsed by ``run_audit()`` / ``disallowed_hits()``."""
    import os

    expected = {
        os.path.normpath(os.path.join("station_install", "setup_station.sh")),
        os.path.normpath(os.path.join("src", "backend", "requirements.txt")),
    }
    assert set(audit.IN_SCOPE_PIN_FILES) == expected

    # The out-of-scope portal pin value is itself disallowed by the predicate
    # (2.31.0 < 2.32.4) -- confirming it is ONLY the file-scope that keeps it
    # from being flagged, not the version.
    assert audit._pin_is_disallowed("2.31.0") is True
    assert _line_is_flagged(_OUT_OF_SCOPE_PORTAL_LINE) is True


def test_cdk_out_asset_path_is_excluded():
    """``_excluded_path`` treats a generated ``cdk.out/asset.X/requirements.txt``
    path as excluded, so a portal-copy pin under cdk.out is never parsed."""
    import os

    cdk_path = os.path.join("cdk.out", "asset.abc123", "requirements.txt")
    assert audit._excluded_path(cdk_path) is True
    # An in-scope pin file is NOT excluded.
    assert audit._excluded_path(audit.BACKEND_REQS_REL) is False


# --------------------------------------------------------------------------- #
# Two-layer contract on the current fixed tree (Req 2.4 / Preservation 3.6-3.7).
# --------------------------------------------------------------------------- #
def test_run_audit_non_empty_but_disallowed_hits_empty_on_fixed_tree():
    """``run_audit()`` still enumerates the two raw in-scope pins (non-empty)
    while the precise ``disallowed_hits()`` gate is empty after the F1/F2
    bumps to 2.32.4."""
    raw = audit.run_audit()
    assert len(raw) == 2, (
        "run_audit() must enumerate the two in-scope requests pins; got "
        + "; ".join(f"{audit._rel(h.path)}:{h.lineno}" for h in raw)
    )

    disallowed = audit.disallowed_hits()
    assert disallowed == [], (
        "disallowed_hits() must be empty on the fixed tree; got: "
        + "; ".join(f"{h.category} {h.path}:{h.lineno}" for h in disallowed)
    )


def test_accepted_exceptions_still_match_on_fixed_tree():
    """The B324 documented allowlist still describes exactly the known vendored
    digest-auth usages, so the specificity guard reports no drift."""
    assert audit.verify_accepted_exceptions_still_match() == []
