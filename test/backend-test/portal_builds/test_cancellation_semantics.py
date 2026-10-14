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
Property test for Build_Job cancellation semantics in
``edge-cv-portal/backend/functions/build_domain.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates

**Validates: Requirements 4.5, 4.6, 4.8, 4.9**

The expected cancellation semantics are restated here independently of
the implementation. For any Build_Job status and any cancellation
request:

- a queued job becomes cancelled and is removed from the Build_Queue
  (Req 4.5);
- a running job (building or publishing) becomes cancelled if and only
  if the stop of its build process is confirmed within the confirmation
  window; otherwise it keeps its current status and the error
  identifies the affected Build_Server (Req 4.6, 4.9);
- a terminal job (succeeded, failed, interrupted, cancelled) is
  rejected unchanged with an error identifying its current status
  (Req 4.8).
"""
import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Import the pure domain module from the portal Lambda bundle.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_domain  # noqa: E402

_STATUSES = st.sampled_from(sorted(build_domain.ALL_STATUSES))

# Result of the stop confirmation (pgrep verification): confirmed stopped,
# confirmed still running, or not (yet) known.
_STOP_CONFIRMED = st.sampled_from([True, False, None])

_SERVER_IDS = st.sampled_from(["server-a", "server-b", "i-0123456789abcdef0", None])


# Feature: portal-build-fleet-and-workflow-gates, Property 9: Cancellation semantics by status
# Validates: Requirements 4.5, 4.6, 4.8, 4.9
@settings(max_examples=200)
@given(
    current_status=_STATUSES,
    stop_confirmed=_STOP_CONFIRMED,
    server_id=_SERVER_IDS,
)
def test_cancellation_semantics_by_status(current_status, stop_confirmed, server_id):
    """For any Build_Job status and any cancellation request: queued jobs
    are cancelled and leave the queue (Req 4.5); running jobs are
    cancelled iff the stop is confirmed, otherwise they keep their status
    with an error naming the Build_Server (Req 4.6, 4.9); terminal jobs
    are rejected unchanged with an error identifying the current status
    (Req 4.8)."""
    decision = build_domain.decide_cancellation(
        current_status,
        stop_confirmed=stop_confirmed,
        server_id=server_id,
    )

    # --- Queued: cancelled and removed from the Build_Queue (Req 4.5) ---
    if current_status == build_domain.STATUS_QUEUED:
        assert decision.cancelled is True
        assert decision.next_status == build_domain.STATUS_CANCELLED
        assert decision.remove_from_queue is True
        assert decision.errors == ()
        return

    # --- Running (building/publishing): cancelled iff the stop is
    # confirmed (Req 4.6); otherwise status kept, error names the
    # Build_Server (Req 4.9) ---
    if current_status in build_domain.RUNNING_STATUSES:
        if stop_confirmed is True:
            assert decision.cancelled is True
            assert decision.next_status == build_domain.STATUS_CANCELLED
            assert decision.errors == ()
        else:
            assert decision.cancelled is False
            assert decision.next_status == current_status, (
                f"running job changed status to {decision.next_status!r} "
                f"without a confirmed stop"
            )
            assert len(decision.errors) > 0, "rejection carries no error"
            expected_server = server_id if server_id else "unknown"
            assert any(
                expected_server in error["message"] for error in decision.errors
            ), (
                f"no error identifies the affected Build_Server "
                f"{expected_server!r}: {decision.errors!r}"
            )
        # A running job is not in the Build_Queue either way.
        assert decision.remove_from_queue is False
        return

    # --- Terminal: rejected unchanged, error identifies the current
    # status (Req 4.8) ---
    if current_status in build_domain.TERMINAL_STATUSES:
        assert decision.cancelled is False
        assert decision.next_status == current_status, (
            f"terminal job changed status to {decision.next_status!r}"
        )
        assert decision.remove_from_queue is False
        assert len(decision.errors) > 0, "rejection carries no error"
        assert any(
            current_status in error["message"] for error in decision.errors
        ), (
            f"no error identifies the current status {current_status!r}: "
            f"{decision.errors!r}"
        )
        return

    # --- Remaining non-terminal status (provisioning): never silently
    # cancelled — the job is unchanged and the rejection carries an error
    # identifying the current status ---
    assert decision.cancelled is False
    assert decision.next_status == current_status
    assert decision.remove_from_queue is False
    assert len(decision.errors) > 0
    assert any(current_status in error["message"] for error in decision.errors)
