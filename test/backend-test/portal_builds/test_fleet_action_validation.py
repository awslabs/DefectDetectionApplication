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
Property test for fleet action validation in
``edge-cv-portal/backend/functions/build_domain.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates

**Validates: Requirements 6.4, 6.10**

The expected fleet action validation table is restated here independently
of the implementation. For any (action, lifecycle state, running-job
presence) combination, ``validate_fleet_action`` permits:

- start if and only if the lifecycle state is stopped (Req 6.10);
- stop if and only if the lifecycle state is running and no Build_Job is
  running on the server (Req 6.10, 6.4);
- terminate if and only if the lifecycle state is not terminated and no
  Build_Job is running on the server (Req 6.10, 6.4).

Every rejection identifies the server's current lifecycle state
(Req 6.10), and rejections caused by a running Build_Job identify that
Build_Job (Req 6.4).
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

_ACTIONS = st.sampled_from(sorted(build_domain.FLEET_ACTIONS))

# The Dedicated_Build_Server lifecycle states (the EC2 instance state set,
# Req 6.1 / 6.10).
_LIFECYCLE_STATES = st.sampled_from([
    build_domain.SERVER_STATE_PENDING,
    build_domain.SERVER_STATE_RUNNING,
    build_domain.SERVER_STATE_STOPPING,
    build_domain.SERVER_STATE_STOPPED,
    build_domain.SERVER_STATE_SHUTTING_DOWN,
    build_domain.SERVER_STATE_TERMINATED,
])

# The Build_Job currently running on the server: absent, a job record with
# an id, or a bare job id.
_RUNNING_JOBS = st.sampled_from([
    None,
    {"build_job_id": "job-123"},
    {"build_job_id": "bj-0aa11bb22cc33dd44"},
    "job-as-plain-id",
])


def _expected_job_id(running_job):
    """Identifier a rejection must use for the running Build_Job."""
    if isinstance(running_job, dict):
        return str(running_job["build_job_id"])
    return str(running_job)


# Feature: portal-build-fleet-and-workflow-gates, Property 13: Fleet action validation table
# Validates: Requirements 6.4, 6.10
@settings(max_examples=200)
@given(
    action=_ACTIONS,
    lifecycle_state=_LIFECYCLE_STATES,
    running_job=_RUNNING_JOBS,
)
def test_fleet_action_validation_table(action, lifecycle_state, running_job):
    """For any (action, lifecycle state, running-job presence) combination:
    start is permitted iff the state is stopped; stop is permitted iff the
    state is running and no Build_Job runs on the server; terminate is
    permitted iff the state is not terminated and no Build_Job runs on the
    server (Req 6.10, 6.4). Every rejection identifies the current
    lifecycle state, and running-job rejections identify the Build_Job."""
    server = {"server_id": "server-a", "lifecycle_state": lifecycle_state}

    result = build_domain.validate_fleet_action(action, server, running_job)

    # --- Expected permission per the validation table ---
    if action == build_domain.FLEET_ACTION_START:
        expected_valid = lifecycle_state == build_domain.SERVER_STATE_STOPPED
    elif action == build_domain.FLEET_ACTION_STOP:
        expected_valid = (
            lifecycle_state == build_domain.SERVER_STATE_RUNNING
            and running_job is None
        )
    else:  # terminate
        expected_valid = (
            lifecycle_state != build_domain.SERVER_STATE_TERMINATED
            and running_job is None
        )

    assert result.valid == expected_valid, (
        f"validate_fleet_action({action!r}, state={lifecycle_state!r}, "
        f"running_job={running_job!r}) returned valid={result.valid}, "
        f"expected {expected_valid}"
    )

    if expected_valid:
        assert result.errors == ()
        return

    # --- Every rejection identifies the current lifecycle state (Req 6.10) ---
    assert len(result.errors) > 0, "rejection carries no error"
    assert any(
        lifecycle_state in error["message"] for error in result.errors
    ), (
        f"no error identifies the current lifecycle state "
        f"{lifecycle_state!r}: {result.errors!r}"
    )

    # --- Rejections caused by a running Build_Job identify it (Req 6.4) ---
    job_blocks = (
        action in (build_domain.FLEET_ACTION_STOP, build_domain.FLEET_ACTION_TERMINATE)
        and running_job is not None
    )
    if job_blocks:
        job_id = _expected_job_id(running_job)
        assert any(job_id in error["message"] for error in result.errors), (
            f"no error identifies the running Build_Job {job_id!r}: "
            f"{result.errors!r}"
        )
