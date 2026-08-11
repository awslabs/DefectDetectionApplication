# Copyright 2026 Amazon Web Services, Inc.
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
"""
**Property 17: ENOSPC Classification** (build-fleet-execution-failures
task 4.4, storage amendment; fix side of the task 14 exploration).

**Validates: Requirements 2.21, 3.15**

_For any_ agent error message, stderr, or invocation output containing
disk-exhaustion patterns (``no space left on device``, ENOSPC) or an
agent-reported ``error_kind=disk``, classification SHALL yield the
stable ``RUNNER_DISK_FULL`` error code and never generic
``BUILD_FAILED``, and _for any_ output containing no disk-exhaustion
evidence, classification SHALL never yield ``RUNNER_DISK_FULL``
(existing classification rows keep their codes — preservation, Req
3.15's "failures unrelated to disk exhaustion keep existing behavior").

Everything here is pure (``build_reconciliation.py``): no AWS, no I/O,
no moto. Generators embed ENOSPC patterns at arbitrary positions inside
otherwise disk-free text, exercise the ``error_kind=disk`` shortcut,
and generate disk-free outputs for the never-otherwise direction.

Run ONLY this file, from the repository root:

    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \\
        test/backend-test/portal_builds/test_enospc_classification_properties.py \\
        --noconftest -q

(This run contains property-based tests and may generate/shrink
counterexamples.)
"""
import os
import sys

from hypothesis import given, settings, strategies as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_domain  # noqa: E402
import build_reconciliation as br  # noqa: E402

# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

#: The design's disk-exhaustion patterns, in several casings (detection
#: is case-insensitive).
_DISK_PATTERNS = (
    "no space left on device",
    "No space left on device",
    "NO SPACE LEFT ON DEVICE",
    "ENOSPC",
    "enospc",
    "Enospc",
)


def _is_disk_free(text: str) -> bool:
    """Independent (non-regex) oracle: True iff the text carries no
    disk-exhaustion evidence. Deliberately conservative — it excludes
    even embedded 'enospc' substrings so the negative direction never
    depends on the implementation's own word-boundary subtleties.

    Also excludes the dispatch preflight failure marker: filler text
    carrying ``DDA_PREFLIGHT_FAILED`` would legitimately classify as
    COMMAND_PREFLIGHT_FAILED (that row precedes the disk row at the
    same authority), which is a different classification question than
    this property tests."""
    lower = text.lower()
    return ("enospc" not in lower
            and "no space left on device" not in lower
            and br.PREFLIGHT_FAILURE_MARKER.lower() not in lower)


#: Arbitrary build-output-like text guaranteed to be disk-free.
disk_free_text = st.text(max_size=300).filter(_is_disk_free)


@st.composite
def enospc_text(draw) -> str:
    """Disk-free text with one ENOSPC pattern embedded at an arbitrary
    position (whitespace-delimited, as real agent/buildkit output is)."""
    prefix = draw(disk_free_text)
    suffix = draw(disk_free_text)
    pattern = draw(st.sampled_from(_DISK_PATTERNS))
    return "{0} {1} {2}".format(prefix, pattern, suffix)


#: The three invocation text fields classification inspects.
_INVOCATION_FIELDS = ("StandardErrorContent", "StandardOutputContent",
                      "StatusDetails")

#: Agent error kinds that must NOT shortcut disk classification.
non_disk_error_kind = st.sampled_from(
    [None, "build", "publish", "network", "preflight", "unknown"])


def _failed_invocation(fields):
    invocation = {"Status": "Failed", "ResponseCode": 1}
    invocation.update(fields)
    return invocation


# ---------------------------------------------------------------------------
# Property 17 — RUNNER_DISK_FULL exactly when disk evidence exists
# ---------------------------------------------------------------------------

class TestProperty17EnospcClassification:
    """**Property 17: ENOSPC Classification**

    **Validates: Requirements 2.21, 3.15**
    """

    @settings(max_examples=200, deadline=None)
    @given(field=st.sampled_from(_INVOCATION_FIELDS),
           evidence=enospc_text(),
           other=disk_free_text)
    def test_enospc_in_any_invocation_field_yields_runner_disk_full(
            self, field, evidence, other):
        """ENOSPC evidence at any position of stderr, stdout, or status
        details classifies as the stable RUNNER_DISK_FULL code — never
        generic — with a decided failed status (Req 2.21)."""
        fields = {name: other for name in _INVOCATION_FIELDS}
        fields[field] = evidence
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_failed_invocation(fields))
        assert outcome.decided is True
        assert outcome.status == build_domain.STATUS_FAILED
        assert outcome.error_code == br.CODE_RUNNER_DISK_FULL
        assert outcome.error_code != "BUILD_FAILED"

    @settings(max_examples=200, deadline=None)
    @given(message=enospc_text())
    def test_enospc_in_agent_error_message_yields_runner_disk_full(
            self, message):
        """ENOSPC evidence in the agent's own terminal failure message
        keeps agent authority (precedence 1) but maps to the distinct
        stable disk-exhaustion code (Req 2.21)."""
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            agent_result={"phase": "failed", "message": message})
        assert outcome.decided is True
        assert outcome.status == build_domain.STATUS_FAILED
        assert outcome.error_code == br.CODE_RUNNER_DISK_FULL
        assert outcome.authority == 1

    @settings(max_examples=200, deadline=None)
    @given(message=disk_free_text)
    def test_agent_error_kind_disk_shortcuts_without_pattern(
            self, message):
        """An agent-reported ``error_kind=disk`` classifies as
        RUNNER_DISK_FULL even when the message itself carries no ENOSPC
        pattern (the shortcut needs no pattern matching, Req 2.21)."""
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            agent_result={"phase": "failed", "message": message,
                          "error_kind": br.AGENT_ERROR_KIND_DISK})
        assert outcome.decided is True
        assert outcome.status == build_domain.STATUS_FAILED
        assert outcome.error_code == br.CODE_RUNNER_DISK_FULL
        assert outcome.authority == 1

    @settings(max_examples=200, deadline=None)
    @given(stderr=disk_free_text, stdout=disk_free_text,
           details=disk_free_text)
    def test_disk_free_invocation_never_runner_disk_full(
            self, stderr, stdout, details):
        """Disk-free invocation output NEVER classifies as
        RUNNER_DISK_FULL (no false positives) and keeps the existing
        COMMAND_EXECUTION_FAILED row unchanged (Req 2.21 'never
        otherwise'; preservation, Req 3.15)."""
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_failed_invocation({
                "StandardErrorContent": stderr,
                "StandardOutputContent": stdout,
                "StatusDetails": details,
            }))
        assert outcome.error_code != br.CODE_RUNNER_DISK_FULL
        assert outcome.error_code == br.CODE_COMMAND_EXECUTION_FAILED

    @settings(max_examples=200, deadline=None)
    @given(message=disk_free_text, error_kind=non_disk_error_kind)
    def test_disk_free_agent_failure_never_runner_disk_full(
            self, message, error_kind):
        """A disk-free agent failure without ``error_kind=disk`` NEVER
        classifies as RUNNER_DISK_FULL; the agent-authoritative failure
        result keeps its existing (code-less) authority (Req 2.21,
        3.15)."""
        agent_result = {"phase": "failed", "message": message}
        if error_kind is not None:
            agent_result["error_kind"] = error_kind
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING, agent_result=agent_result)
        assert outcome.error_code != br.CODE_RUNNER_DISK_FULL
        assert outcome.error_code is None
        assert outcome.status == build_domain.STATUS_FAILED
        assert outcome.authority == 1

    @settings(max_examples=200, deadline=None)
    @given(evidence=enospc_text(), clean=disk_free_text)
    def test_detection_helper_is_exact(self, evidence, clean):
        """The pure detection helper fires exactly on disk evidence or
        the explicit disk error kind, never on disk-free text."""
        assert br.is_disk_exhaustion_evidence(evidence) is True
        assert br.is_disk_exhaustion_evidence(clean) is False
        assert br.is_disk_exhaustion_evidence(
            clean, agent_error_kind=br.AGENT_ERROR_KIND_DISK) is True
        assert br.is_disk_exhaustion_evidence(
            clean, agent_error_kind="build") is False
