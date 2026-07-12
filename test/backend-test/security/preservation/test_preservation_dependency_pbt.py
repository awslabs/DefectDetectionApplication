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
"""Property-based tests for the dependency-audit disallowed-pin predicate (PBT 1)
and pin-line classification (PBT 2) -- dependency CVE spec.

Spec: security-dependency-cve-fixes -- Property 2: Preservation (non-over-flagging)
and Property 1: Fix Checking (the disallowed-pin predicate). These exercise the
REAL audit primitives (``_pin_is_disallowed`` / ``_parse_version`` /
``_REQUESTS_PIN_RE`` / ``IN_SCOPE_PIN_FILES`` / ``_has_nosem``) across a generated
input domain, so the invariants hold for ALL inputs, not just the fixed sites.

* **PBT 1** -- ``_pin_is_disallowed(v)`` is True **iff**
  ``_parse_version(v) < (2, 32, 4)``. In particular ``2.32.3`` (and every
  ``< 2.32.4`` version) is flagged; ``2.32.4`` / ``2.33.0`` / ``3.0.0`` are cleared.
* **PBT 2** -- a candidate dependency line is flagged **iff** it is a
  ``requests==`` pin ``< 2.32.4`` in an in-scope pin file with no ``# nosec``. A
  BARE unpinned ``requests`` is NEVER flagged (proving the Python-3.6 host
  installs stay clear); an out-of-scope ``requests==2.32.3`` is NEVER flagged; a
  ``urllib3`` pin is NEVER flagged.

**Validates: Requirements 3.1, 3.3, 3.6, 3.7**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_dependency_pbt.py \
        -p no:cacheprovider --noconftest -v
"""
import os

from hypothesis import given, settings
from hypothesis import strategies as st

from _dependency_preservation_support import import_audit

audit = import_audit()

MIN_SAFE = (2, 32, 4)  # CVE-2024-47081 fixed in requests 2.32.4


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #
# Well-formed MAJOR.MINOR.PATCH(.EXTRA) version strings (1-4 numeric components).
_well_formed_version = st.lists(
    st.integers(min_value=0, max_value=60), min_size=1, max_size=4
).map(lambda parts: ".".join(str(p) for p in parts))

# Malformed / adversarial version strings (letters, empty components, extra dots).
_malformed_version = st.text(alphabet="0123456789.abcxyz", min_size=0, max_size=12)

_any_version = st.one_of(_well_formed_version, _malformed_version)


# --------------------------------------------------------------------------- #
# PBT 1 -- disallowed-pin predicate over generated version strings.
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.3
@settings(max_examples=400)
@given(v=_any_version)
def test_pbt1_disallowed_pin_predicate_matches_parsed_floor(v):
    """``_pin_is_disallowed(v)`` is True iff the parsed version is below the
    CVE-fixed floor ``(2, 32, 4)``."""
    expected = audit._parse_version(v) < MIN_SAFE
    assert audit._pin_is_disallowed(v) is expected, (
        f"_pin_is_disallowed({v!r})={audit._pin_is_disallowed(v)} but "
        f"_parse_version={audit._parse_version(v)} vs floor {MIN_SAFE}"
    )


# Validates: Requirements 3.3
def test_pbt1_concrete_anchors():
    """Concrete anchors: the CVE pin is flagged; the fixed / newer pins clear."""
    assert audit._pin_is_disallowed("2.32.3") is True
    assert audit._pin_is_disallowed("2.31.0") is True
    assert audit._pin_is_disallowed("2.32.4") is False
    assert audit._pin_is_disallowed("2.33.0") is False
    assert audit._pin_is_disallowed("3.0.0") is False


# --------------------------------------------------------------------------- #
# PBT 2 -- pin-line classification (scope x pin shape x nosec).
# --------------------------------------------------------------------------- #
_IN_SCOPE_PATHS = st.sampled_from(
    [audit.SETUP_STATION_REL, audit.BACKEND_REQS_REL]
)
_OUT_OF_SCOPE_PATHS = st.sampled_from(
    [
        os.path.join("edge-cv-portal", "backend", "layers", "jwt", "requirements.txt"),
        os.path.join("cdk.out", "asset.abc123", "requirements.txt"),
        os.path.join("some", "other", "file.txt"),
    ]
)


def _classify(rel_path, line):
    """The REAL gate's per-line classification, built from the audit primitives:
    flagged iff in an in-scope pin file, no ``# nosec`` marker, and the line
    carries a ``requests==<version>`` pin with a disallowed version."""
    if os.path.normpath(rel_path) not in audit.IN_SCOPE_PIN_FILES:
        return False
    if audit._has_nosem(line):
        return False
    for m in audit._REQUESTS_PIN_RE.finditer(line):
        if audit._pin_is_disallowed(m.group(1)):
            return True
    return False


# token kinds: (kind, builder(version) -> line-fragment)
@st.composite
def _candidate(draw):
    in_scope = draw(st.booleans())
    rel_path = draw(_IN_SCOPE_PATHS if in_scope else _OUT_OF_SCOPE_PATHS)
    version = draw(_well_formed_version)
    kind = draw(
        st.sampled_from(["requests_pin", "bare_requests", "urllib3_pin", "unrelated"])
    )
    if kind == "requests_pin":
        fragment = f"requests=={version}"
    elif kind == "bare_requests":
        # Mirrors the Python-3.6 host installs: a bare unpinned requests token.
        fragment = 'run_cmd "python3 -m pip install requests protobuf"'
    elif kind == "urllib3_pin":
        fragment = f"urllib3=={version}"
    else:
        fragment = "flask==1.0.0"
    has_nosec = draw(st.booleans())
    line = fragment + ("  # nosec" if has_nosec else "")
    return {
        "rel_path": rel_path,
        "in_scope": in_scope,
        "kind": kind,
        "version": version,
        "has_nosec": has_nosec,
        "line": line,
    }


# Validates: Requirements 3.1, 3.6, 3.7
@settings(max_examples=500)
@given(case=_candidate())
def test_pbt2_pin_line_classification(case):
    """A line is flagged iff it is a ``requests==`` pin ``< 2.32.4`` in an
    in-scope file with no ``# nosec``. Unpinned ``requests``, out-of-scope pins,
    and ``urllib3`` pins are NEVER flagged."""
    expected = (
        case["in_scope"]
        and not case["has_nosec"]
        and case["kind"] == "requests_pin"
        and audit._parse_version(case["version"]) < MIN_SAFE
    )
    actual = _classify(case["rel_path"], case["line"])
    assert actual is expected, (
        f"classification mismatch for {case!r}: expected {expected}, got {actual}"
    )


# Validates: Requirements 3.1
def test_pbt2_bare_unpinned_requests_never_flagged():
    """A bare unpinned ``requests`` install (the Python-3.6 host install shape)
    is NEVER flagged, even in an in-scope file."""
    for line in (
        'run_cmd "python3 -m pip install requests protobuf"',
        'run_cmd "python3 -m pip install --upgrade requests"',
        '"$py" -m pip install --upgrade requests',
    ):
        assert _classify(audit.SETUP_STATION_REL, line) is False


# Validates: Requirements 3.6, 3.7
def test_pbt2_out_of_scope_and_urllib3_never_flagged():
    """An out-of-scope ``requests==2.32.3`` and any ``urllib3`` pin are NEVER
    flagged."""
    portal = os.path.join(
        "edge-cv-portal", "backend", "layers", "jwt", "requirements.txt"
    )
    assert _classify(portal, "requests==2.32.3") is False
    assert _classify(audit.BACKEND_REQS_REL, "urllib3==2.2.3") is False
    # A # nosec marker clears an otherwise-disallowed in-scope pin.
    assert _classify(audit.BACKEND_REQS_REL, "requests==2.32.3  # nosec") is False
