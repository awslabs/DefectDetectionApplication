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
Property test for interruption handling and the retry-clone function in
``edge-cv-portal/backend/functions/build_domain.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates

**Validates: Requirements 3.5, 3.6**
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
_TARGETS = st.sampled_from(sorted(build_domain.SUPPORTED_BUILD_TARGETS))
_IDS = st.text(
    alphabet="abcdef0123456789-", min_size=1, max_size=16
)

_EXECUTION_MODES = st.sampled_from(["ephemeral", "dedicated"])


@st.composite
def _jobs(draw):
    """A Build_Job record with an arbitrary status, supported Build_Target,
    and execution mode (dedicated jobs carry a server id)."""
    mode = draw(_EXECUTION_MODES)
    return {
        "build_job_id": draw(_IDS),
        "build_target": draw(_TARGETS),
        "execution_mode": mode,
        "server_id": draw(_IDS) if mode == "dedicated" else None,
        "status": draw(_STATUSES),
        "requested_by": draw(_IDS),
        "created_at": draw(st.integers(min_value=0, max_value=2**40)),
        "config_snapshot": draw(
            st.none()
            | st.dictionaries(
                st.sampled_from(["instance_type", "volume_gb", "max_runtime_hours"]),
                st.integers(min_value=1, max_value=1000),
                max_size=3,
            )
        ),
    }


# Feature: portal-build-fleet-and-workflow-gates, Property 23: Interruption and retry preserve job identity
@settings(max_examples=200)
@given(
    job=_jobs(),
    new_job_id=_IDS,
    retried_by=_IDS,
    retried_at=st.integers(min_value=0, max_value=2**40),
)
def test_interruption_and_retry_preserve_job_identity(
    job, new_job_id, retried_by, retried_at
):
    """For any Build_Job whose runner emits an interruption event, a
    non-terminal status becomes interrupted while terminal statuses are
    unchanged (Req 3.5); and for any interrupted Build_Job, the retry action
    creates a new Build_Job with the same Build_Target and execution mode
    carrying a reference to the interrupted job (Req 3.6)."""
    # --- Interruption (Req 3.5) ---
    interrupted_status = build_domain.apply_interruption(job["status"])
    if build_domain.is_terminal(job["status"]):
        assert interrupted_status == job["status"]
    else:
        assert interrupted_status == build_domain.STATUS_INTERRUPTED

    # --- Retry clone (Req 3.6) ---
    source = dict(job, status=interrupted_status)
    if interrupted_status == build_domain.STATUS_INTERRUPTED:
        clone = build_domain.retry_clone(
            source, new_job_id, retried_by, retried_at
        )

        # New job identity: same Build_Target and execution mode, including
        # the selected Dedicated_Build_Server for dedicated mode.
        assert clone["build_target"] == source["build_target"]
        assert clone["execution_mode"] == source["execution_mode"]
        assert clone["server_id"] == source["server_id"]

        # Reference from the new Build_Job to the interrupted one.
        assert clone["retry_of"] == source["build_job_id"]

        # The clone is a genuinely new job: new id, queued status, its own
        # requester and submission time.
        assert clone["build_job_id"] == new_job_id
        assert clone["status"] == build_domain.STATUS_QUEUED

        # The clone's derived target attributes match the target definition.
        definition = build_domain.target_definition(source["build_target"])
        assert clone["component_name"] == definition["component_name"]
        assert clone["required_arch"] == definition["required_arch"]

        # The source job record itself is not mutated by the retry.
        assert source["status"] == build_domain.STATUS_INTERRUPTED
        assert source["build_job_id"] == job["build_job_id"]
    else:
        # Retry exists only for interrupted Build_Jobs: any other terminal
        # status is rejected without creating a job.
        try:
            build_domain.retry_clone(source, new_job_id, retried_by, retried_at)
            assert False, "retry_clone must reject non-interrupted jobs"
        except ValueError:
            pass
