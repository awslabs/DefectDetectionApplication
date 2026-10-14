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
Property test for queue promotion order in
``edge-cv-portal/backend/functions/build_planner.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates

**Validates: Requirements 7.3**

The expected promotion rule is restated here independently of the
implementation: promotion happens exactly when a server's current
Build_Job reaches a terminal status (succeeded, failed, interrupted, or
cancelled), and then the promotion function selects — from the queued
jobs belonging to that server only — the job with the earliest
submission time (``created_at``). Jobs on other servers and jobs not in
the ``queued`` status are never candidates, and an empty queue promotes
nothing.
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

_STATUSES = st.sampled_from(sorted(build_domain.ALL_STATUSES))

_SERVER_IDS = ["server-a", "server-b", "server-c"]


@st.composite
def _promotion_cases(draw):
    """Generate a promotion scenario: a fleet-wide set of Build_Jobs with
    distinct submission times spread across several servers with arbitrary
    statuses, a server whose current job just changed, and that current
    job's (arbitrary) new status."""
    n_jobs = draw(st.integers(min_value=0, max_value=12))
    # Distinct submission times, per the property statement.
    times = draw(
        st.lists(
            st.integers(min_value=0, max_value=10**9),
            min_size=n_jobs,
            max_size=n_jobs,
            unique=True,
        )
    )
    jobs = [
        {
            "build_job_id": f"job-{i}",
            "server_id": draw(st.sampled_from(_SERVER_IDS)),
            "status": draw(_STATUSES),
            "created_at": times[i],
        }
        for i in range(n_jobs)
    ]
    server_id = draw(st.sampled_from(_SERVER_IDS))
    current_job_status = draw(_STATUSES)
    return server_id, current_job_status, jobs


# Feature: portal-build-fleet-and-workflow-gates, Property 11: Queue promotion picks the oldest queued job
@settings(max_examples=200)
@given(case=_promotion_cases())
def test_queue_promotion_picks_the_oldest_queued_job(case):
    """For any set of Build_Jobs with distinct submission times and any
    status of a server's current job: promotion is triggered exactly by
    the terminal statuses, and it selects — from that server's queued
    jobs only — the one with the earliest submission time, or nothing
    when the server's queue is empty (Req 7.3)."""
    server_id, current_job_status, jobs = case

    # Promotion happens iff the current job reached a terminal status.
    expected_promote = current_job_status in build_domain.TERMINAL_STATUSES
    assert build_planner.should_promote(current_job_status) == expected_promote

    if not expected_promote:
        return

    # The server's Build_Queue: queued jobs for this server only.
    queue = [
        job
        for job in jobs
        if job["server_id"] == server_id
        and job["status"] == build_domain.STATUS_QUEUED
    ]

    selected = build_planner.promote_next(server_id, jobs)

    if not queue:
        assert selected is None, (
            f"promoted {selected!r} from an empty queue on {server_id}"
        )
        return

    expected = min(queue, key=lambda j: j["created_at"])
    assert selected is not None, "nothing promoted from a non-empty queue"
    # Only eligible (queued, same-server) jobs are considered.
    assert selected["server_id"] == server_id
    assert selected["status"] == build_domain.STATUS_QUEUED
    # Exactly the oldest queued job is selected.
    assert selected["build_job_id"] == expected["build_job_id"], (
        f"promoted {selected['build_job_id']} (created_at="
        f"{selected['created_at']}), expected {expected['build_job_id']} "
        f"(created_at={expected['created_at']})"
    )
