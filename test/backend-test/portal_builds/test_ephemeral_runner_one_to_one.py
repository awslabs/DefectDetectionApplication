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
Property test for ephemeral runner one-to-one planning in
``edge-cv-portal/backend/functions/build_planner.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates

**Validates: Requirements 2.3, 7.4, 3.1, 3.3**

The expected planning semantics are restated here independently of the
implementation. For any dispatcher planning run over any set of
Build_Jobs:

- the plan provisions exactly one Ephemeral_Build_Runner per
  dispatch-eligible queued ephemeral Build_Job — one runner per job,
  never more, never shared between jobs (Req 2.3, 7.4);
- when no ephemeral Build_Job is dispatch-eligible and queued, the plan
  provisions zero runners (Req 3.3);
- each planned runner's CPU architecture derives from its job's
  Build_Target (JP5/JP6 -> arm64, AMD64/AMD64_NVIDIA -> x86_64) and the
  planned Build_Job status is provisioning (Req 3.1).

A queued job is dispatch-eligible iff its ``predecessor_job_id`` is null
or references a job (within the collection) whose status is terminal
(succeeded, failed, interrupted, or cancelled).
"""
import os
import sys
from collections import Counter

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
_TERMINAL = frozenset({"succeeded", "failed", "interrupted", "cancelled"})
_ALL_STATUSES = sorted(
    _TERMINAL | {"queued", "provisioning", "building", "publishing"}
)
_TARGETS = ["JP5", "JP6", "AMD64", "AMD64_NVIDIA"]
_EXPECTED_ARCH = {
    "JP5": "arm64",
    "JP6": "arm64",
    "AMD64": "x86_64",
    "AMD64_NVIDIA": "x86_64",
}

# Raw per-job draw: (status, execution_mode, target, has predecessor,
# predecessor index seed, created_at, config_snapshot or None).
_JOB_SPECS = st.tuples(
    st.sampled_from(_ALL_STATUSES),
    st.sampled_from(["ephemeral", "dedicated"]),
    st.sampled_from(_TARGETS),
    st.booleans(),
    st.integers(min_value=0, max_value=10 ** 6),
    st.integers(min_value=0, max_value=10 ** 12),
    st.one_of(
        st.none(),
        st.fixed_dictionaries(
            {},
            optional={
                "arm64_instance_type": st.sampled_from(["m6g.4xlarge", "c7g.8xlarge"]),
                "x86_64_instance_type": st.sampled_from(["m6i.4xlarge", "c6i.8xlarge"]),
                "volume_size_gb": st.integers(min_value=1, max_value=500),
                "use_spot_for_ephemeral": st.booleans(),
            },
        ),
    ),
)


@st.composite
def _job_sets(draw):
    """Generate a set of Build_Jobs with mixed statuses, execution modes,
    Build_Targets, and predecessor chains referencing earlier jobs."""
    specs = draw(st.lists(_JOB_SPECS, min_size=0, max_size=12))
    jobs = []
    for i, (status, mode, target, has_pred, pred_seed, created_at, snap) in enumerate(specs):
        predecessor = None
        if has_pred and i > 0:
            predecessor = f"job-{pred_seed % i}"  # some earlier job
        job = {
            "build_job_id": f"job-{i}",
            "status": status,
            "execution_mode": mode,
            "build_target": target,
            "predecessor_job_id": predecessor,
            "created_at": created_at,
        }
        if mode == "dedicated":
            job["server_id"] = f"server-{i % 3}"
        if snap is not None:
            job["config_snapshot"] = snap
        jobs.append(job)
    return jobs


def _expected_dispatchable_ephemeral(jobs):
    """Independently computed set of Build_Job ids that must get a runner:
    queued ephemeral jobs whose predecessor is null or terminal."""
    status_by_id = {job["build_job_id"]: job["status"] for job in jobs}
    expected = set()
    for job in jobs:
        if job["status"] != "queued" or job["execution_mode"] != "ephemeral":
            continue
        pred = job["predecessor_job_id"]
        if pred is None or status_by_id.get(pred) in _TERMINAL:
            expected.add(job["build_job_id"])
    return expected


# Feature: portal-build-fleet-and-workflow-gates, Property 5: Ephemeral runner/job one-to-one
# Validates: Requirements 2.3, 7.4, 3.1, 3.3
@settings(max_examples=200)
@given(jobs=_job_sets())
def test_ephemeral_runner_one_to_one(jobs):
    """For any set of Build_Jobs, the provisioning plan contains exactly
    one RunnerPlan per dispatch-eligible queued ephemeral job — never
    shared, never duplicated (Req 2.3, 7.4); zero runners when no
    ephemeral job is dispatch-eligible (Req 3.3); each plan's arch derives
    from the job's Build_Target and its status is provisioning (Req 3.1)."""
    plans = build_planner.plan_ephemeral_provisioning(jobs)

    expected_ids = _expected_dispatchable_ephemeral(jobs)

    # --- Exactly one runner per dispatch-eligible queued ephemeral job,
    # never shared between jobs, never duplicated (Req 2.3, 7.4) ---
    planned_counts = Counter(plan.build_job_id for plan in plans)
    assert set(planned_counts) == expected_ids, (
        f"planned runners for {sorted(planned_counts)} but expected exactly "
        f"one per dispatchable ephemeral job {sorted(expected_ids)}"
    )
    duplicated = {jid: n for jid, n in planned_counts.items() if n != 1}
    assert not duplicated, (
        f"more than one Ephemeral_Build_Runner planned per Build_Job: "
        f"{duplicated!r}"
    )

    # --- Zero runners when no ephemeral job is queued and
    # dispatch-eligible (Req 3.3) ---
    if not expected_ids:
        assert plans == [], (
            f"runners provisioned with no dispatchable ephemeral job: {plans!r}"
        )

    # --- Arch derives from the job's Build_Target; the dispatch implies
    # the provisioning status (Req 3.1) ---
    job_by_id = {job["build_job_id"]: job for job in jobs}
    for plan in plans:
        target = job_by_id[plan.build_job_id]["build_target"]
        assert plan.arch == _EXPECTED_ARCH[target], (
            f"runner for Build_Target {target!r} planned with arch "
            f"{plan.arch!r}, expected {_EXPECTED_ARCH[target]!r}"
        )
        assert plan.status == "provisioning", (
            f"dispatched ephemeral job {plan.build_job_id!r} planned with "
            f"status {plan.status!r}, expected 'provisioning'"
        )
