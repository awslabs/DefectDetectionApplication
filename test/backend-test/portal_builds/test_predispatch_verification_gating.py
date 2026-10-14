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
"""
Property test for the pre-dispatch verification gating decision in
``edge-cv-portal/backend/functions/build_planner.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates

**Validates: Requirements 7.5, 7.6**

The expected gating semantics are restated here independently of the
implementation. For any pre-dispatch verification result (any ``pgrep``
output, including no output at all) and any Build_Job:

- the dispatch decision starts the build if and only if the output
  contains NO line reporting a running build process — a line reports a
  build process iff it contains one of the patterns
  ``gdk component build`` or ``build-custom.sh`` (Req 7.5);
- otherwise the job is deferred: it returns to its server's Build_Queue
  with the queued status, KEEPING its original submission time
  (``created_at``) so it stays at the head of the queue in submission
  order, with ``deferred_at`` recording the verification time and the
  detected build-process lines reported (Req 7.6);
- re-verification of a deferred job is due if and only if it has never
  been verified (``last_verified_at`` is None) or at least the 5-minute
  retry interval (300000 ms) has elapsed since the last attempt
  (Req 7.6).
"""
import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Import the pure planner module from the portal Lambda bundle.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_planner  # noqa: E402

# Expected semantics, restated independently of the implementation.
_PATTERNS = ("gdk component build", "build-custom.sh")
_RETRY_INTERVAL_MS = 5 * 60 * 1000  # 5 minutes (Req 7.6)

# Process command-line text guaranteed NOT to contain a build pattern
# (alphabet excludes '-' and '.' so 'build-custom.sh' cannot appear, and
# 'gdk component build' cannot appear without spaces).
_SAFE_TEXT = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_/"),
    min_size=0,
    max_size=30,
)

# A non-matching pgrep line: pid + benign command text (may still contain
# words like 'build' alone, which must NOT trigger the gate).
_NONMATCHING_LINES = st.builds(
    lambda pid, text: f"{pid} {text}",
    st.integers(min_value=1, max_value=99999),
    st.one_of(_SAFE_TEXT, st.sampled_from(["python3 server.py", "sshd", "build", "gdk component list"])),
)

# A matching pgrep line: pid + command line containing a build pattern.
_MATCHING_LINES = st.builds(
    lambda pid, prefix, pattern, suffix: f"{pid} {prefix}{pattern}{suffix}",
    st.integers(min_value=1, max_value=99999),
    _SAFE_TEXT,
    st.sampled_from(_PATTERNS),
    _SAFE_TEXT,
)

# Blank / whitespace-only lines must be ignored.
_BLANK_LINES = st.sampled_from(["", " ", "\t", "   "])


@st.composite
def _pgrep_outputs(draw):
    """Generate pgrep output mixing matching lines, non-matching process
    lines, and blank lines — or None (no output at all). Returns the
    output plus the independently expected 'build process found' flag."""
    if draw(st.booleans()) and draw(st.booleans()):
        return None, False
    lines = draw(
        st.lists(
            st.one_of(_NONMATCHING_LINES, _MATCHING_LINES, _BLANK_LINES),
            min_size=0,
            max_size=8,
        )
    )
    output = "\n".join(lines)
    found = any(
        line.strip() and any(p in line for p in _PATTERNS) for line in lines
    )
    return output, found


_JOBS = st.fixed_dictionaries(
    {
        "build_job_id": st.uuids().map(str),
        "status": st.just("queued"),
        "execution_mode": st.just("dedicated"),
        "server_id": st.sampled_from(["server-0", "server-1"]),
        "created_at": st.integers(min_value=0, max_value=10 ** 13),
    }
)


# Feature: portal-build-fleet-and-workflow-gates, Property 6: Pre-dispatch verification gates the start
# Validates: Requirements 7.5, 7.6
@settings(max_examples=200)
@given(
    output_and_found=_pgrep_outputs(),
    job=_JOBS,
    now=st.integers(min_value=0, max_value=10 ** 13),
    last_verified_at=st.one_of(
        st.none(),
        st.integers(min_value=0, max_value=10 ** 13),
    ),
)
def test_predispatch_verification_gates_the_start(
    output_and_found, job, now, last_verified_at
):
    """The dispatch decision starts the build iff the pgrep output reports
    no build process (Req 7.5); otherwise the job is deferred back to its
    queue — queued status, ORIGINAL created_at retained, deferred_at set,
    detected process lines reported — and re-verification is due iff never
    verified or the 5-minute retry interval has elapsed (Req 7.6)."""
    pgrep_output, expect_found = output_and_found

    decision = build_planner.decide_predispatch(job, pgrep_output, now)

    # --- Start iff no build process is reported in the output (Req 7.5) ---
    if expect_found:
        assert decision.action == build_planner.PREDISPATCH_DEFER, (
            f"build process present in {pgrep_output!r} but decision was "
            f"{decision.action!r}, expected defer"
        )
    else:
        assert decision.action == build_planner.PREDISPATCH_START, (
            f"no build process in {pgrep_output!r} but decision was "
            f"{decision.action!r}, expected start"
        )

    # --- The decision is always about this job, and the ORIGINAL
    # submission time is always retained so a deferred job stays at the
    # head of its server's queue in submission order (Req 7.6) ---
    assert decision.build_job_id == job["build_job_id"]
    assert decision.created_at == job["created_at"], (
        f"created_at changed from {job['created_at']!r} to "
        f"{decision.created_at!r}; a deferral must keep the original "
        f"submission time (Req 7.6)"
    )

    if decision.action == build_planner.PREDISPATCH_DEFER:
        # Deferred job returns to the queue with the queued status,
        # deferred_at records this verification attempt, and the detected
        # build-process lines are reported (Req 7.6).
        assert decision.status == "queued", (
            f"deferred job has status {decision.status!r}, expected 'queued'"
        )
        assert decision.deferred_at == now, (
            f"deferral recorded deferred_at={decision.deferred_at!r}, "
            f"expected the verification time {now!r}"
        )
        assert len(decision.build_processes) > 0
        for line in decision.build_processes:
            assert any(p in line for p in _PATTERNS), (
                f"reported process line {line!r} contains no build pattern"
            )
        # Every matching line of the output is reported.
        expected_lines = [
            line.strip()
            for line in (pgrep_output or "").splitlines()
            if line.strip() and any(p in line for p in _PATTERNS)
        ]
        assert list(decision.build_processes) == expected_lines
    else:
        # A start reports no deferral and no processes.
        assert decision.deferred_at is None
        assert decision.build_processes == ()

    # --- Re-verification cadence: due iff never verified or at least the
    # 5-minute retry interval has elapsed since the last attempt (Req 7.6) ---
    due = build_planner.is_reverification_due(last_verified_at, now)
    if last_verified_at is None:
        assert due, "a never-verified job must be verified immediately"
    else:
        expected_due = (now - last_verified_at) >= _RETRY_INTERVAL_MS
        assert due == expected_due, (
            f"last_verified_at={last_verified_at}, now={now}: "
            f"is_reverification_due returned {due}, expected {expected_due} "
            f"(5-minute interval = {_RETRY_INTERVAL_MS} ms)"
        )
