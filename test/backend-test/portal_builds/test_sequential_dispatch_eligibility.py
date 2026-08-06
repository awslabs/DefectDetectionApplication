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
Property test for sequential dispatch eligibility in
``edge-cv-portal/backend/functions/build_planner.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates

**Validates: Requirements 1.3**

The expected eligibility rule is restated here independently of the
implementation: a job is dispatch-eligible iff it is ``queued`` AND
(its ``predecessor_job_id`` is null OR the predecessor is present in the
job set with a terminal status — succeeded, failed, interrupted, or
cancelled, regardless of which one). Non-queued jobs are never eligible,
and a predecessor reference that cannot be resolved leaves the job
ineligible (eligibility must be positively established).
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

# A predecessor id that is referenced by a job but absent from the job set.
_MISSING_PREDECESSOR_ID = "missing-predecessor"


@st.composite
def _request_chains(draw):
    """Generate a flat list of Build_Jobs made of several request chains.

    Each chain links jobs via ``predecessor_job_id`` in request order, and
    every job gets an arbitrary status. Some jobs are additionally rewired
    to reference a predecessor id that does not exist in the set, to cover
    the unresolvable-predecessor case.
    """
    jobs = []
    n_chains = draw(st.integers(min_value=0, max_value=4))
    counter = 0
    for chain_idx in range(n_chains):
        chain_len = draw(st.integers(min_value=1, max_value=5))
        prev_id = None
        for order in range(chain_len):
            job_id = f"job-{chain_idx}-{order}"
            predecessor = prev_id
            # Occasionally point at a predecessor absent from the set.
            if predecessor is not None and draw(st.booleans()) and draw(st.booleans()):
                predecessor = _MISSING_PREDECESSOR_ID
            jobs.append({
                "build_job_id": job_id,
                "request_id": f"request-{chain_idx}",
                "request_order": order,
                "predecessor_job_id": predecessor,
                "status": draw(_STATUSES),
                "created_at": counter,
            })
            prev_id = job_id
            counter += 1
    return jobs


# Feature: portal-build-fleet-and-workflow-gates, Property 3: Sequential dispatch eligibility within a request
@settings(max_examples=200)
@given(jobs=_request_chains())
def test_sequential_dispatch_eligibility_within_a_request(jobs):
    """For any set of request chains (jobs with predecessor links and
    arbitrary statuses), a job is dispatch-eligible if and only if it is
    queued and its predecessor is null or resolvable with a terminal
    status — any terminal status unblocks; non-queued jobs and jobs with
    an unresolvable predecessor are never eligible (Req 1.3)."""
    status_by_id = {job["build_job_id"]: job["status"] for job in jobs}

    expected_eligible_ids = set()
    for job in jobs:
        pred_id = job["predecessor_job_id"]
        expected = job["status"] == build_domain.STATUS_QUEUED and (
            pred_id is None
            or (
                pred_id in status_by_id
                and status_by_id[pred_id] in build_domain.TERMINAL_STATUSES
            )
        )
        if expected:
            expected_eligible_ids.add(job["build_job_id"])

        # Point check: is_dispatch_eligible matches the rule exactly for
        # this job given its (possibly unresolvable) predecessor status.
        actual = build_planner.is_dispatch_eligible(
            job, status_by_id.get(pred_id)
        )
        assert actual == expected, (
            f"is_dispatch_eligible({job!r}, "
            f"{status_by_id.get(pred_id)!r}) = {actual}, expected {expected}"
        )

    # Set check: eligible_queued_jobs selects exactly the eligible jobs,
    # resolving predecessors within the supplied collection.
    result = build_planner.eligible_queued_jobs(jobs)
    result_ids = [job["build_job_id"] for job in result]
    assert len(result_ids) == len(set(result_ids)), "duplicate jobs in result"
    assert set(result_ids) == expected_eligible_ids

    # The result preserves submission order (ascending created_at).
    created = [job["created_at"] for job in result]
    assert created == sorted(created)
