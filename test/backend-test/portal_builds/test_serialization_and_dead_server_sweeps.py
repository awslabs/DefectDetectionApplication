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
Property test for the serialization-violation and dead-server sweep
decisions in ``edge-cv-portal/backend/functions/build_planner.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates

**Validates: Requirements 7.8, 7.9**

The expected sweep semantics are restated here independently of the
implementation (design Property 12):

- Serialization violation (Req 7.8): for any detected build-process
  count on a server and any set of associated Build_Jobs, the
  stop-all/fail-all action is taken if and only if the count is two or
  more, and then EVERY associated Build_Job is marked failed with the
  ``SERIALIZATION_VIOLATION`` error. A count of zero or one stops
  nothing and fails no job.

- Dead-server sweep (Req 7.9): for any server lifecycle state with
  queued jobs, every queued Build_Job for that server is marked failed
  with an error identifying the server state if and only if the state
  is ``stopped`` or ``terminated``. Jobs on other servers, and jobs of
  the server not in the queued status, are never swept; in any other
  lifecycle state no job is failed.
"""
import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Import the pure planner/domain modules from the portal Lambda bundle.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_domain  # noqa: E402
import build_planner  # noqa: E402

# Expected semantics, restated independently of the implementation.
_SERIALIZATION_ERROR = "SERIALIZATION_VIOLATION"
_DEAD_STATES = frozenset({"stopped", "terminated"})

# The full EC2-pinned lifecycle state set (Req 6.1) — the sweep must fire
# for exactly the dead subset and never for the transitional states.
_LIFECYCLE_STATES = [
    "pending",
    "running",
    "stopping",
    "stopped",
    "shutting-down",
    "terminated",
]

_SERVER_IDS = ["server-a", "server-b", "server-c"]

_STATUSES = st.sampled_from(sorted(build_domain.ALL_STATUSES))


@st.composite
def _sweep_cases(draw):
    """Generate one combined sweep scenario.

    Serialization side: a checked server, a detected build-process count
    (0..6), and the associated Build_Job ids.

    Dead-server side: a server record with an arbitrary lifecycle state
    and a fleet-wide set of Build_Jobs with arbitrary statuses spread
    across several servers (so the sweep's queued/same-server filtering
    is exercised).
    """
    # --- Serialization violation inputs (Req 7.8) ---
    checked_server_id = draw(st.sampled_from(_SERVER_IDS))
    process_count = draw(st.integers(min_value=0, max_value=6))
    associated_job_ids = draw(
        st.lists(
            st.uuids().map(str),
            min_size=0,
            max_size=5,
            unique=True,
        )
    )

    # --- Dead-server sweep inputs (Req 7.9) ---
    swept_server_id = draw(st.sampled_from(_SERVER_IDS))
    lifecycle_state = draw(st.sampled_from(_LIFECYCLE_STATES))
    n_jobs = draw(st.integers(min_value=0, max_value=10))
    jobs = [
        {
            "build_job_id": f"job-{i}",
            "server_id": draw(st.sampled_from(_SERVER_IDS)),
            "status": draw(_STATUSES),
            "created_at": draw(st.integers(min_value=0, max_value=10**9)),
        }
        for i in range(n_jobs)
    ]
    return (
        checked_server_id,
        process_count,
        associated_job_ids,
        swept_server_id,
        lifecycle_state,
        jobs,
    )


# Feature: portal-build-fleet-and-workflow-gates, Property 12: Serialization violation and dead-server sweeps
# Validates: Requirements 7.8, 7.9
@settings(max_examples=200)
@given(case=_sweep_cases())
def test_serialization_violation_and_dead_server_sweeps(case):
    """The stop-all/fail-all action is taken iff the detected
    build-process count is >= 2, and then every associated Build_Job is
    failed with the serialization-violation error (Req 7.8); and every
    queued Build_Job of a server is failed with a server-state error iff
    the server's lifecycle state is stopped or terminated (Req 7.9)."""
    (
        checked_server_id,
        process_count,
        associated_job_ids,
        swept_server_id,
        lifecycle_state,
        jobs,
    ) = case

    # ------------------------------------------------------------------
    # Serialization-violation decision (Req 7.8)
    # ------------------------------------------------------------------
    decision = build_planner.decide_serialization_violation(
        checked_server_id, process_count, associated_job_ids
    )

    expected_violation = process_count >= 2
    assert decision.violation == expected_violation, (
        f"process_count={process_count}: violation={decision.violation}, "
        f"expected {expected_violation} (threshold is two or more)"
    )
    # The stop-all action is taken iff the violation is detected.
    assert decision.stop_all == expected_violation
    assert decision.server_id == checked_server_id
    assert decision.process_count == process_count

    if expected_violation:
        # EVERY associated Build_Job is failed with the
        # serialization-violation error.
        assert list(decision.failed_job_ids) == list(associated_job_ids), (
            f"violation must fail every associated job: got "
            f"{decision.failed_job_ids!r}, expected {associated_job_ids!r}"
        )
        assert decision.error == _SERIALIZATION_ERROR
    else:
        # No violation: nothing is stopped, no job fails.
        assert decision.failed_job_ids == ()
        assert decision.error is None

    # ------------------------------------------------------------------
    # Dead-server sweep decision (Req 7.9)
    # ------------------------------------------------------------------
    server = {"server_id": swept_server_id, "lifecycle_state": lifecycle_state}
    sweep = build_planner.sweep_dead_server(server, jobs)

    expected_sweep = lifecycle_state in _DEAD_STATES
    assert sweep.sweep == expected_sweep, (
        f"lifecycle_state={lifecycle_state!r}: sweep={sweep.sweep}, "
        f"expected {expected_sweep} (sweep iff stopped or terminated)"
    )
    assert sweep.server_id == swept_server_id
    assert sweep.lifecycle_state == lifecycle_state

    # The server's Build_Queue: queued jobs for this server only.
    expected_queued_ids = {
        job["build_job_id"]
        for job in jobs
        if job["server_id"] == swept_server_id and job["status"] == "queued"
    }

    if expected_sweep:
        # Every queued Build_Job for the server — and only those — is
        # failed with an error identifying the server state.
        assert set(sweep.failed_job_ids) == expected_queued_ids, (
            f"swept jobs {set(sweep.failed_job_ids)!r} != expected queued "
            f"jobs {expected_queued_ids!r} for server {swept_server_id!r}"
        )
        assert len(sweep.failed_job_ids) == len(set(sweep.failed_job_ids))
        assert sweep.error, "a sweep must carry a server-state error"
        assert lifecycle_state in sweep.error, (
            f"sweep error {sweep.error!r} does not identify the server "
            f"state {lifecycle_state!r}"
        )
    else:
        # Any other lifecycle state (including transitional states):
        # no job is failed.
        assert sweep.failed_job_ids == ()
        assert sweep.error is None
