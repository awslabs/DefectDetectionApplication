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
Property test for config snapshot immutability in
``edge-cv-portal/backend/functions/build_planner.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates

**Validates: Requirements 9.3**

The expected semantics are restated here independently of the
implementation. A Build_Job's provisioning plan (instance type per CPU
architecture, volume size, spot flag) derives ONLY from the job's own
``config_snapshot`` — the configuration in effect at the job's creation:

- for any job and any sequence/variation of "current" configuration
  values, the plan is identical regardless of the current configuration
  (which is not even an input to the planner);
- re-planning the same job always yields the same RunnerPlan
  (determinism);
- planning never mutates the job or its snapshot: the configuration
  values in effect at the job's creation continue to apply, byte for
  byte, no matter how often the dispatcher re-plans (Req 9.3).
"""
import copy
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
_TARGETS = ["JP5", "JP6", "AMD64", "AMD64_NVIDIA"]
_EXPECTED_ARCH = {
    "JP5": "arm64",
    "JP6": "arm64",
    "AMD64": "x86_64",
    "AMD64_NVIDIA": "x86_64",
}
# Documented defaults (Req 9.2) applied when a snapshot lacks a field.
_DEFAULT_INSTANCE_TYPE = {"arm64": "m6g.4xlarge", "x86_64": "m6i.4xlarge"}
_SNAPSHOT_INSTANCE_KEY = {"arm64": "arm64_instance_type", "x86_64": "x86_64_instance_type"}
_DEFAULT_VOLUME_SIZE_GB = 100

_INSTANCE_TYPES = [
    "m6g.4xlarge", "c7g.8xlarge", "r6g.2xlarge",
    "m6i.4xlarge", "c6i.8xlarge", "r6i.2xlarge",
]

# A configuration value set: the sizing fields a Build_Infrastructure_Config
# carries. Used both for a job's config_snapshot (possibly partial or absent)
# and for the "current" configurations the admin changes to over time.
_CONFIG_VALUES = st.fixed_dictionaries(
    {},
    optional={
        "arm64_instance_type": st.sampled_from(_INSTANCE_TYPES),
        "x86_64_instance_type": st.sampled_from(_INSTANCE_TYPES),
        "volume_size_gb": st.integers(min_value=1, max_value=2000),
        "use_spot_for_ephemeral": st.booleans(),
        "max_runtime_hours": st.integers(min_value=1, max_value=24),
    },
)

# An ephemeral queued Build_Job with no predecessor (dispatch-eligible), so
# both plan_runner and plan_ephemeral_provisioning must plan it.
_JOBS = st.builds(
    lambda target, created_at, snap: {
        "build_job_id": "job-0",
        "status": "queued",
        "execution_mode": "ephemeral",
        "build_target": target,
        "predecessor_job_id": None,
        "created_at": created_at,
        **({"config_snapshot": snap} if snap is not None else {}),
    },
    target=st.sampled_from(_TARGETS),
    created_at=st.integers(min_value=0, max_value=10 ** 12),
    snap=st.one_of(st.none(), _CONFIG_VALUES),
)


def _expected_plan_fields(job):
    """Independently computed provisioning sizing, derived ONLY from the
    job's own config_snapshot with the documented defaults (Req 9.2, 9.3)."""
    arch = _EXPECTED_ARCH[job["build_target"]]
    snapshot = job.get("config_snapshot") or {}
    instance_type = (
        snapshot.get(_SNAPSHOT_INSTANCE_KEY[arch]) or _DEFAULT_INSTANCE_TYPE[arch]
    )
    volume = snapshot.get("volume_size_gb")
    if volume is None:
        volume = _DEFAULT_VOLUME_SIZE_GB
    spot = bool(snapshot.get("use_spot_for_ephemeral", False))
    return arch, instance_type, volume, spot


# Feature: portal-build-fleet-and-workflow-gates, Property 15: Config snapshots are immutable under config changes
# Validates: Requirements 9.3
@settings(max_examples=200)
@given(
    job=_JOBS,
    config_changes=st.lists(_CONFIG_VALUES, min_size=1, max_size=6),
)
def test_config_snapshot_immutability(job, config_changes):
    """For any created Build_Job and any subsequent sequence of
    configuration changes, the job's config_snapshot is unchanged and every
    planning decision for that job derives from the snapshot, never the
    current configuration (Req 9.3)."""
    pristine_job = copy.deepcopy(job)
    expected_arch, expected_type, expected_volume, expected_spot = \
        _expected_plan_fields(job)

    # Plan once at creation-time configuration.
    first_plan = build_planner.plan_runner(job)

    # The plan derives ONLY from the job's own config_snapshot (Req 9.3).
    assert first_plan.arch == expected_arch
    assert first_plan.instance_type == expected_type, (
        f"instance type {first_plan.instance_type!r} does not derive from the "
        f"job's config_snapshot (expected {expected_type!r})"
    )
    assert first_plan.volume_size_gb == expected_volume, (
        f"volume size {first_plan.volume_size_gb!r} does not derive from the "
        f"job's config_snapshot (expected {expected_volume!r})"
    )
    assert first_plan.spot == expected_spot

    # Simulate an arbitrary sequence of admin configuration changes. The
    # planner takes no current-configuration input, so after every change
    # re-planning the SAME job must yield the IDENTICAL plan: the values in
    # effect at the job's creation keep applying (Req 9.3).
    for _current_config in config_changes:
        replanned = build_planner.plan_runner(job)
        assert replanned == first_plan, (
            f"re-planning after a configuration change altered the plan: "
            f"{replanned!r} != {first_plan!r}"
        )
        # The dispatch-tick planner agrees with the per-job plan.
        tick_plans = build_planner.plan_ephemeral_provisioning([job])
        assert tick_plans == [first_plan], (
            f"dispatch-tick plan {tick_plans!r} differs from the job's "
            f"creation-time plan {first_plan!r}"
        )

    # Planning never mutated the job or its config_snapshot (Req 9.3).
    assert job == pristine_job, (
        f"planner mutated the Build_Job/config_snapshot: {job!r} != "
        f"{pristine_job!r}"
    )
