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
"""Bug-condition exploration test (FLIPPED in Task 6) for
security-dependency-cve-fixes.

Property 1: Expected Behavior -- every in-scope ``requests`` pin is now
``>= 2.32.4`` (CVE-2024-47081 removed) on its Python-3.8+ target
(``setup_station.sh:513``, ``requirements.txt:9`` -- both bumped to ``2.32.4``),
the B324 vendored HTTP Digest-auth ``md5`` / ``sha1`` usage
(``requests/auth.py`` 148/156/205) carries a documented accepted-exception, and
the audit returns zero disallowed hits.

This file was WRITTEN IN TASK 1 to OBSERVE the counterexample shape (the two
``requests==2.32.3`` pins) on the UNFIXED tree, then FLIPPED IN TASK 6 to assert
the FIXED / secure invariants: the F1/F2 assertions now require the ``==2.32.4``
pins (with the surrounding structure preserved byte-for-byte), and the gate
assertion ``test_dependency_audit_returns_no_disallowed_hits`` now PASSES because
``disallowed_hits()`` returns ``[]`` once both pins are bumped.

Validates: Requirements 2.1, 2.2, 2.3, 2.4
"""
import os

import pytest

import dependency_audit as audit

REPO_ROOT = audit.REPO_ROOT


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #
def _lines(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as f:
        return f.read().splitlines()


# --------------------------------------------------------------------------- #
# F1 -- station_install/setup_station.sh:513 (Req 1.1)
# --------------------------------------------------------------------------- #
def test_f1_setup_station_line_513_pins_vulnerable_requests():
    """F1 (Req 2.1): station_install/setup_station.sh line 513 now force-reinstalls
    the FIXED ``requests==2.32.4`` (CVE-2024-47081 removed) into the
    built-from-source Python 3.11 (``$PYTHON311``).

    FIXED-TREE INVARIANT (flipped in Task 6): this PASSES -- it asserts the secure
    ``==2.32.4`` pin and that the vulnerable ``==2.32.3`` shape is gone. The
    ``run_cmd`` / ``--force-reinstall`` / ``$PYTHON311`` / ``|| add_warning`` tail
    are asserted present (they were preserved byte-for-byte by the fix)."""
    line = _lines(audit.SETUP_STATION_REL)[513 - 1]
    print(f"\n[F1 fixed] setup_station.sh:513 == {line!r}")

    assert "requests==2.32.4" in line, (
        f"expected the fixed requests==2.32.4 pin on line 513, got {line!r}"
    )
    assert "requests==2.32.3" not in line, (
        f"the vulnerable requests==2.32.3 pin must be gone from line 513, got {line!r}"
    )
    # The fixed version is no longer classified as a disallowed (< 2.32.4) pin.
    assert not audit._pin_is_disallowed("2.32.4"), (
        "requests==2.32.4 must NOT classify as a disallowed (< 2.32.4) pin"
    )
    # Surrounding structure preserved byte-for-byte by the fix.
    assert "run_cmd" in line, f"expected run_cmd wrapper on line 513: {line!r}"
    assert "--force-reinstall" in line, (
        f"expected --force-reinstall on line 513: {line!r}"
    )
    assert "$PYTHON311" in line, f"expected $PYTHON311 usage on line 513: {line!r}"
    assert '|| add_warning "Failed to install requests"' in line, (
        f"expected the '|| add_warning \"Failed to install requests\"' tail: {line!r}"
    )


# --------------------------------------------------------------------------- #
# F2 -- src/backend/requirements.txt:9 (Req 1.2)
# --------------------------------------------------------------------------- #
def test_f2_requirements_line_9_pins_vulnerable_requests():
    """F2 (Req 2.2): src/backend/requirements.txt line 9 now pins the FIXED
    ``requests==2.32.4`` (CVE-2024-47081 removed), immediately after line 8
    ``urllib3==2.2.3``.

    FIXED-TREE INVARIANT (flipped in Task 6): this PASSES -- it asserts the secure
    ``==2.32.4`` pin; line 8 ``urllib3==2.2.3`` is asserted unchanged (NOT flagged,
    NOT bumped)."""
    lines = _lines(audit.BACKEND_REQS_REL)
    line9 = lines[9 - 1]
    line8 = lines[8 - 1]
    print(f"\n[F2 fixed] requirements.txt:8 == {line8!r}")
    print(f"[F2 fixed] requirements.txt:9 == {line9!r}")

    assert line9.strip() == "requests==2.32.4", (
        f"expected line 9 to be 'requests==2.32.4', got {line9!r}"
    )
    assert not audit._pin_is_disallowed("2.32.4"), (
        "requests==2.32.4 must NOT classify as a disallowed (< 2.32.4) pin"
    )
    # Line 8 urllib3==2.2.3 is out of scope (NOT flagged, NOT bumped).
    assert line8.strip() == "urllib3==2.2.3", (
        f"expected line 8 to be 'urllib3==2.2.3', got {line8!r}"
    )


# --------------------------------------------------------------------------- #
# Repo audit (F4 / Req 1.4) -- the gate re-run in task 6.
# --------------------------------------------------------------------------- #
def test_dependency_audit_returns_no_disallowed_hits():
    """The dependency audit must return ZERO disallowed bug-condition hits across
    the two in-scope pin files.

    UNFIXED-TREE EXPECTATION: this MUST FAIL -- both in-scope pins are
    ``requests==2.32.3`` (``< 2.32.4``), so ``disallowed_hits()`` returns the two
    counterexamples that confirm the bug exists. TASK 6 sees it PASS once both
    pins are bumped to ``2.32.4``. Validates Req 1.4 (audit gate)."""
    disallowed = audit.disallowed_hits()
    assert disallowed == [], (
        f"dependency audit found {len(disallowed)} disallowed requests pin(s) "
        f"(< 2.32.4, CVE-2024-47081) -- these ARE the counterexamples that "
        f"confirm the bug on the unfixed tree:\n"
        + "\n".join(
            f"  [{h.category}] {os.path.relpath(h.path, REPO_ROOT)}:{h.lineno}: "
            f"{h.text.strip()}"
            for h in disallowed
        )
    )


def test_run_audit_non_empty():
    """The RAW ``run_audit()`` enumeration is NON-EMPTY and enumerates the two
    in-scope ``requests==`` pins (F1, F2) while excluding cdk.out and the portal
    ``2.31.0`` pins. This is the enumeration anchor and stays green before and
    after the fix (only the version token changes)."""
    all_hits = audit.run_audit()
    assert all_hits, "expected a non-empty raw enumeration of in-scope requests pins"

    # Exactly two in-scope pins (F1 setup_station.sh, F2 requirements.txt).
    assert len(all_hits) == 2, (
        f"expected 2 in-scope requests pins (F1, F2), got {len(all_hits)}:\n"
        + "\n".join(
            f"  {os.path.relpath(h.path, REPO_ROOT)}:{h.lineno}: {h.text.strip()}"
            for h in all_hits
        )
    )

    # Each in-scope file carries exactly one pin.
    assert len(audit.hits_for(audit.SETUP_STATION_REL, all_hits)) == 1
    assert len(audit.hits_for(audit.BACKEND_REQS_REL, all_hits)) == 1

    # No cdk.out artifact and no portal 2.31.0 pin may leak in.
    leaked = [h for h in all_hits if audit._excluded_path(h.path)]
    assert leaked == [], f"cdk.out artifacts must be excluded, got {leaked}"
    portal = [h for h in all_hits if os.path.join("edge-cv-portal", "backend") in h.path]
    assert portal == [], f"portal requests pins must be out of scope, got {portal}"
    for h in all_hits:
        assert "2.31.0" not in h.text, (
            f"the portal requests==2.31.0 pin must not be enumerated, got {h.text!r}"
        )


# --------------------------------------------------------------------------- #
# B324 documented allowlist (F3 / Req 1.3)
# --------------------------------------------------------------------------- #
def test_b324_accepted_exceptions_present_and_justified():
    """F3 (Req 1.3): the B324 vendored HTTP Digest-auth ``md5`` / ``sha1`` usage
    carries a DOCUMENTED accepted-false-positive allowlist entry for
    ``requests/auth.py`` lines 148 (``hashlib.md5``), 156 (``hashlib.sha1``), and
    205 (``hashlib.sha1``), each with a non-empty RFC-2617 justification, and the
    allowlist still matches the vendored file (specificity guard)."""
    by_lineno = {exc.lineno: exc for exc in audit.ACCEPTED_EXCEPTIONS}

    for lineno, expected_token in ((148, "hashlib.md5"),
                                   (156, "hashlib.sha1"),
                                   (205, "hashlib.sha1")):
        assert lineno in by_lineno, (
            f"expected an ACCEPTED_EXCEPTIONS entry for auth.py line {lineno}"
        )
        exc = by_lineno[lineno]
        assert exc.token == expected_token, (
            f"line {lineno} expected token {expected_token!r}, got {exc.token!r}"
        )
        assert exc.path == audit.VENDORED_DIGEST_AUTH_REL, (
            f"line {lineno} exception must target the vendored auth.py, got "
            f"{exc.path!r}"
        )
        assert exc.justification and exc.justification.strip(), (
            f"line {lineno} exception must carry a non-empty justification"
        )
        assert "RFC" in exc.justification.upper(), (
            f"line {lineno} justification must cite the RFC-2617/7616 protocol: "
            f"{exc.justification!r}"
        )

    # SPECIFICITY guard: the recorded (path, lineno, token) still match the
    # vendored file, so the allowlist is not a blanket clear.
    drifted = audit.verify_accepted_exceptions_still_match()
    assert drifted == [], (
        "the B324 allowlist no longer matches the vendored auth.py "
        f"(drifted entries: {drifted})"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
