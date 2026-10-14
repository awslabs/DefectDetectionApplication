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
Property test for Build_Job creation fidelity in
``edge-cv-portal/backend/functions/build_domain.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates

**Validates: Requirements 1.2, 1.3, 1.5, 1.9, 2.7**

The property, transcribed from the acceptance criteria:

  - one Build_Job is created per selected Build_Target, in the order the
    Build_Targets appear in the request (Req 1.2, 1.3)
  - every job of the request shares the request id and carries its 0-based
    ``request_order``; ``predecessor_job_id`` chains job *n* to job *n-1*,
    None for the first, so each job after the first starts only after its
    predecessor is terminal (Req 1.3)
  - the request's execution mode — and the selected Dedicated_Build_Server
    for dedicated mode — applies identically to every job (Req 2.7)
  - each job records the requesting user and the submission time (Req 1.5)
  - each job starts in the queued status (Req 1.9)
  - each job snapshots the effective configuration with its own deep copy:
    mutating one job's snapshot affects neither another job's snapshot nor
    the caller's input configuration
"""
import copy
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


# ---------------------------------------------------------------------------
# Generators: valid, already-validated build requests (creation happens only
# after validate_build_request accepted the request).
# ---------------------------------------------------------------------------

_VALID_TARGETS = ["JP5", "JP6", "AMD64", "AMD64_NVIDIA"]

_config_value_st = st.one_of(
    st.integers(min_value=0, max_value=1000),
    st.sampled_from(["m6g.4xlarge", "m6i.4xlarge", "us-east-1", ""]),
    st.lists(st.integers(min_value=0, max_value=9), max_size=3),
)

# Nested mutable configuration so the deep-copy independence check is
# meaningful (shared inner dicts/lists would be visible through aliasing).
_config_st = st.one_of(
    st.none(),
    st.dictionaries(
        st.sampled_from([
            "arm64_instance_type", "x86_64_instance_type",
            "volume_size_gb", "region", "max_runtime_hours", "extras",
        ]),
        st.one_of(
            _config_value_st,
            st.dictionaries(
                st.sampled_from(["cpu", "memory_gb", "storage_gb", "tag"]),
                _config_value_st,
                max_size=3,
            ),
        ),
        max_size=6,
    ),
)


@st.composite
def _creation_case_st(draw):
    """Arguments for create_build_jobs spanning single- and multi-target
    requests, both execution modes, and arbitrary nested config snapshots."""
    # Duplicated targets are allowed: the request order, not target
    # uniqueness, drives job creation.
    targets = draw(st.lists(
        st.sampled_from(_VALID_TARGETS), min_size=1, max_size=6
    ))
    execution_mode = draw(st.sampled_from(["ephemeral", "dedicated"]))
    server_id = (
        draw(st.sampled_from(["srv-1", "srv-2", "srv-3"]))
        if execution_mode == "dedicated" else None
    )
    request_id = f"req-{draw(st.integers(min_value=0, max_value=10**9))}"
    job_ids = [f"job-{request_id}-{i}" for i in range(len(targets))]
    requested_by = draw(st.sampled_from(
        ["alice", "bob@example.com", "user-42"]
    ))
    created_at = draw(st.integers(min_value=0, max_value=2**33))
    config_snapshot = draw(_config_st)
    return (
        targets, execution_mode, server_id, request_id, job_ids,
        requested_by, created_at, config_snapshot,
    )


# Feature: portal-build-fleet-and-workflow-gates, Property 2: Job creation records the request faithfully
@settings(max_examples=200)
@given(_creation_case_st())
def test_job_creation_records_the_request_faithfully(case):
    """For any valid build request, create_build_jobs produces exactly one
    queued Build_Job per target in request order, chained by predecessor,
    with the request's mode/server/user/time applied to every job and an
    independent deep-copied config snapshot per job."""
    (targets, execution_mode, server_id, request_id, job_ids,
     requested_by, created_at, config_snapshot) = case

    config_before = copy.deepcopy(config_snapshot)

    jobs = build_domain.create_build_jobs(
        targets=targets,
        execution_mode=execution_mode,
        server_id=server_id,
        request_id=request_id,
        job_ids=job_ids,
        requested_by=requested_by,
        created_at=created_at,
        config_snapshot=config_snapshot,
    )

    # One Build_Job per selected Build_Target, in request order (Req 1.2, 1.3).
    assert len(jobs) == len(targets)
    assert [job["build_target"] for job in jobs] == targets
    assert [job["build_job_id"] for job in jobs] == job_ids

    for index, job in enumerate(jobs):
        # Shared request id and positional order (Req 1.3).
        assert job["request_id"] == request_id
        assert job["request_order"] == index

        # Predecessor chain: job n references job n-1, None for the first,
        # so each job after the first waits on its predecessor (Req 1.3).
        expected_predecessor = job_ids[index - 1] if index > 0 else None
        assert job["predecessor_job_id"] == expected_predecessor

        # Execution mode (and dedicated server) identical on every job
        # (Req 2.7).
        assert job["execution_mode"] == execution_mode
        if execution_mode == "dedicated":
            assert job["server_id"] == server_id
        else:
            assert job["server_id"] is None

        # Requesting user and submission time recorded (Req 1.5).
        assert job["requested_by"] == requested_by
        assert job["created_at"] == created_at

        # Initial status is queued (Req 1.9).
        assert job["status"] == "queued"

        # Snapshot equals the effective configuration at creation.
        assert job["config_snapshot"] == config_before

    # Deep-copy independence: mutating one job's snapshot must not affect
    # another job's snapshot or the caller's input configuration.
    victim = jobs[0]["config_snapshot"]
    if victim is not None:
        victim["__mutated__"] = "tampered"
        for inner in victim.values():
            if isinstance(inner, dict):
                inner["__mutated__"] = "tampered"
            elif isinstance(inner, list):
                inner.append("tampered")
        assert config_snapshot == config_before
        for other in jobs[1:]:
            assert other["config_snapshot"] == config_before
