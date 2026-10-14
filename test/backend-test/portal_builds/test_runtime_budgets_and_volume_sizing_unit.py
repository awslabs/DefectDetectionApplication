# Copyright 2026 Amazon Web Services, Inc.
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
Unit tests for build-fleet-execution-failures tasks 8.1-8.3/8.5:

* ``build_planner.decide_runtime_timeout`` delegates jobs carrying the
  new ``timing`` shape (and every queued/provisioning job) to the pure
  phase-clock model (``build_reconciliation.decide_timing``) — separate
  queue/provisioning/execution clocks, heartbeat/progress leases in the
  approved precedence, strict boundaries, non-extendable hard ceiling —
  while LEGACY-shaped jobs keep the frozen wall-clock decision exactly
  (Req 2.13-2.16, 2.18, 3.4, 3.12).
* ``build_domain`` validates the OPTIONAL target/mode ``runtime_budgets``
  map and the OPTIONAL ``volume_size_gb_by_target`` map (JP6 >= 200 GB)
  and resolves/snapshots the volume size once at submission
  (Req 2.17, 2.19, 2.20, 3.6, 3.7, 3.12, 3.13).

Everything here is pure: no AWS, no I/O, no moto.

Run from the repository root:

    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \\
        test/backend-test/portal_builds/test_runtime_budgets_and_volume_sizing_unit.py \\
        --noconftest -q
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_reconciliation as br  # noqa: E402

_MS_PER_MINUTE = 60 * 1000
_MS_PER_HOUR = 60 * _MS_PER_MINUTE
T0 = 1_754_000_000_000

BUDGETS = {
    "AMD64": {
        "dedicated": {
            "heartbeat_lease_minutes": 30,
            "progress_stall_minutes": 60,
            "hard_runtime_hours": 8,
        },
    },
}


def _job(status, *, timing=None, budgets=None, started_at=None,
         created_at=T0, dispatched_at=None, max_runtime_hours=4):
    job = {
        "build_job_id": "unit-job",
        "build_target": "AMD64",
        "execution_mode": "dedicated",
        "status": status,
        "created_at": created_at,
        "config_snapshot": {"max_runtime_hours": max_runtime_hours},
    }
    if budgets is not None:
        job["config_snapshot"]["runtime_budgets"] = budgets
    if started_at is not None:
        job["started_at"] = started_at
    if dispatched_at is not None:
        job["dispatched_at"] = dispatched_at
    if timing is not None:
        job["timing"] = timing
    return job


# ===========================================================================
# Task 8.1/8.2 — decide_runtime_timeout delegation (Req 2.13-2.16, 2.18)
# ===========================================================================

class TestPhaseClockDelegation:
    """decide_runtime_timeout delegates timing-shaped jobs to
    decide_timing and exposes classification + evidence."""

    def test_fresh_progress_below_hard_ceiling_continues(self):
        now = T0 + 5 * _MS_PER_HOUR
        job = _job("building", budgets=BUDGETS, started_at=T0,
                   timing={"execution_started_at": T0,
                           "last_heartbeat_at": now - _MS_PER_MINUTE,
                           "last_progress_at": now - 2 * _MS_PER_MINUTE})
        decision = build_planner.decide_runtime_timeout(job, now)
        assert decision.timed_out is False
        assert decision.error is None
        assert decision.classification == br.TIMEOUT_CONTINUE
        assert decision.evidence["phase"] == "execution"

    def test_stale_heartbeat_classified_before_progress(self):
        now = T0 + 3 * _MS_PER_HOUR
        job = _job("building", budgets=BUDGETS, started_at=T0,
                   timing={"execution_started_at": T0,
                           "last_heartbeat_at": now - 2 * _MS_PER_HOUR,
                           "last_progress_at": now - 2 * _MS_PER_HOUR})
        decision = build_planner.decide_runtime_timeout(job, now)
        assert decision.timed_out is True
        assert decision.classification == br.CODE_AGENT_HEARTBEAT_EXPIRED
        assert decision.status == "failed"
        assert "AGENT_HEARTBEAT_EXPIRED" in decision.error

    def test_progress_stall_with_fresh_heartbeat_classified(self):
        now = T0 + 3 * _MS_PER_HOUR
        job = _job("building", budgets=BUDGETS, started_at=T0,
                   timing={"execution_started_at": T0,
                           "last_heartbeat_at": now - _MS_PER_MINUTE,
                           "last_progress_at": now - 2 * _MS_PER_HOUR})
        decision = build_planner.decide_runtime_timeout(job, now)
        assert decision.timed_out is True
        assert decision.classification == br.CODE_BUILD_PROGRESS_STALLED
        assert "BUILD_PROGRESS_STALLED" in decision.error

    def test_hard_ceiling_is_not_extendable_by_activity(self):
        """Fresh heartbeat/progress cannot extend the hard ceiling
        (Req 2.16): one ms past execution start + hard budget expires."""
        now = T0 + 8 * _MS_PER_HOUR + 1
        job = _job("building", budgets=BUDGETS, started_at=T0,
                   timing={"execution_started_at": T0,
                           "last_heartbeat_at": now - 1,
                           "last_progress_at": now - 1})
        decision = build_planner.decide_runtime_timeout(job, now)
        assert decision.timed_out is True
        assert decision.classification == br.CODE_MAX_RUNTIME_EXCEEDED
        # The hard-ceiling message keeps the legacy wording.
        assert "maximum runtime of 8 hours (timeout)" in decision.error

    def test_strict_boundary_now_equals_hard_deadline_continues(self):
        now = T0 + 8 * _MS_PER_HOUR
        job = _job("building", budgets=BUDGETS, started_at=T0,
                   timing={"execution_started_at": T0})
        assert build_planner.decide_runtime_timeout(
            job, now).timed_out is False

    def test_no_execution_evidence_never_charges_active_runtime(self):
        """A timing-shaped building job WITHOUT positive execution-start
        evidence waits instead of timing out (Req 2.14)."""
        now = T0 + 100 * _MS_PER_HOUR
        job = _job("building", budgets=BUDGETS, started_at=T0, timing={})
        decision = build_planner.decide_runtime_timeout(job, now)
        assert decision.timed_out is False
        assert decision.classification == \
            br.TIMEOUT_WAIT_FOR_EXECUTION_EVIDENCE
        assert decision.evidence["execution_runtime_ms"] == 0

    def test_queued_job_exposes_queue_wait_evidence(self):
        now = T0 + 6 * _MS_PER_HOUR
        job = _job("queued", budgets=BUDGETS)
        decision = build_planner.decide_runtime_timeout(job, now)
        assert decision.timed_out is False
        assert decision.status == "queued"
        assert decision.error is None
        assert decision.evidence["phase"] == "queue_wait"
        assert decision.evidence["queue_wait_ms"] == 6 * _MS_PER_HOUR
        assert decision.evidence["execution_runtime_ms"] == 0

    def test_explicit_queue_budget_expires_to_failed(self):
        budgets = {"AMD64": {"dedicated": {"hard_runtime_hours": 8,
                                           "queue_wait_hours": 2}}}
        job = _job("queued", budgets=budgets)
        boundary = T0 + 2 * _MS_PER_HOUR
        assert build_planner.decide_runtime_timeout(
            job, boundary).timed_out is False
        decision = build_planner.decide_runtime_timeout(job, boundary + 1)
        assert decision.timed_out is True
        assert decision.classification == br.CODE_QUEUE_WAIT_TIMEOUT
        assert decision.status == "failed"
        assert "QUEUE_WAIT_TIMEOUT" in decision.error

    def test_provisioning_without_budget_never_times_out(self):
        job = _job("provisioning", budgets=BUDGETS, dispatched_at=T0)
        decision = build_planner.decide_runtime_timeout(
            job, T0 + 45 * _MS_PER_MINUTE)
        assert decision.timed_out is False
        assert decision.evidence["phase"] == "provisioning"
        assert decision.evidence["provisioning_ms"] == 45 * _MS_PER_MINUTE

    def test_execution_anchor_is_execution_start_not_started_at(self):
        """Active runtime anchors on positive execution-start evidence,
        not the started_at transition (Req 2.14)."""
        execution_started = T0 + 30 * _MS_PER_MINUTE
        now = T0 + 4 * _MS_PER_HOUR + 1
        job = _job("building", started_at=T0,
                   timing={"execution_started_at": execution_started,
                           "last_heartbeat_at": now - _MS_PER_MINUTE,
                           "last_progress_at": now - _MS_PER_MINUTE})
        assert build_planner.decide_runtime_timeout(
            job, now).timed_out is False


class TestLegacyPathPreserved:
    """LEGACY-shaped jobs (no timing map) keep the frozen wall-clock
    decision exactly (preservation contract, Req 3.12)."""

    def test_legacy_past_deadline_expires_with_legacy_message(self):
        job = _job("building", started_at=T0)
        decision = build_planner.decide_runtime_timeout(
            job, T0 + 4 * _MS_PER_HOUR + 1)
        assert decision.timed_out is True
        assert decision.status == "failed"
        assert decision.error == ("Build_Job exceeded its maximum runtime "
                                  "of 4 hours (timeout).")

    def test_legacy_exact_deadline_not_expired(self):
        job = _job("building", started_at=T0)
        assert build_planner.decide_runtime_timeout(
            job, T0 + 4 * _MS_PER_HOUR).timed_out is False

    def test_legacy_unknown_start_never_times_out(self):
        job = _job("building")
        decision = build_planner.decide_runtime_timeout(
            job, T0 + 100 * _MS_PER_HOUR)
        assert decision.timed_out is False
        assert decision.error is None


# ===========================================================================
# Task 8.3 — runtime_budgets validation and snapshot (Req 2.17, 3.12)
# ===========================================================================

class TestRuntimeBudgetValidation:

    def test_valid_target_mode_and_default_entries_accepted(self):
        result = build_domain.validate_build_config({"runtime_budgets": {
            "JP6": {"default": {"hard_runtime_hours": 3,
                                "heartbeat_lease_minutes": 15}},
            "AMD64": {"dedicated": {"progress_stall_minutes": 45,
                                    "queue_wait_hours": 6,
                                    "provisioning_minutes": 30}},
        }})
        assert result.valid, [dict(e) for e in result.errors]

    @pytest.mark.parametrize("budgets", [
        "not-a-map",
        {"NOT_A_TARGET": {"default": {"hard_runtime_hours": 1}}},
        {"JP5": "not-a-map"},
        {"JP5": {"bogus_mode": {"hard_runtime_hours": 1}}},
        {"JP5": {"default": "not-a-map"}},
        {"JP5": {"default": {"bogus_key": 1}}},
        {"JP5": {"default": {"hard_runtime_hours": -1}}},
        {"JP5": {"default": {"hard_runtime_hours": "4"}}},
    ])
    def test_malformed_budget_maps_rejected(self, budgets):
        result = build_domain.validate_build_config(
            {"runtime_budgets": budgets})
        assert not result.valid
        assert all(e["rule"] == build_domain.RULE_CONFIG_RUNTIME_BUDGETS_INVALID
                   for e in result.errors)

    def test_snapshotted_budgets_drive_effective_budget(self):
        """The snapshot taken at creation is exactly what
        build_reconciliation.effective_budget resolves (Req 2.17), in
        design order, with legacy fallback (Req 3.12)."""
        config = build_domain.effective_build_config(
            {"runtime_budgets": {"AMD64": {
                "dedicated": {"hard_runtime_hours": 8},
                "default": {"hard_runtime_hours": 6},
            }}})
        jobs = build_domain.create_build_jobs(
            targets=["AMD64"], execution_mode="dedicated",
            server_id="srv-1", request_id="r", job_ids=["j1"],
            requested_by="u", created_at=T0, config_snapshot=config)
        budget = br.effective_budget(jobs[0])
        assert budget.hard_runtime_ms == 8 * _MS_PER_HOUR
        assert budget.source == "target_mode_override"

        # Ephemeral mode has no override: the target default applies.
        jobs = build_domain.create_build_jobs(
            targets=["AMD64"], execution_mode="ephemeral", server_id=None,
            request_id="r", job_ids=["j2"], requested_by="u",
            created_at=T0, config_snapshot=config)
        budget = br.effective_budget(jobs[0])
        assert budget.hard_runtime_ms == 6 * _MS_PER_HOUR
        assert budget.source == "target_default"

    def test_legacy_snapshot_without_budgets_keeps_max_runtime_hours(self):
        """Existing jobs lacking the new shape continue using their own
        snapshotted max_runtime_hours (Req 3.12)."""
        budget = br.effective_budget(
            {"build_target": "JP5", "execution_mode": "ephemeral",
             "config_snapshot": {"max_runtime_hours": 4}})
        assert budget.hard_runtime_ms == 4 * _MS_PER_HOUR
        assert budget.source == "snapshot_max_runtime_hours"
        assert budget.heartbeat_lease_ms is None
        assert budget.progress_stall_ms is None
        assert budget.queue_wait_ms is None
        assert budget.provisioning_ms is None

    def test_no_production_budget_value_is_encoded(self):
        """No default runtime budget beyond the existing snapshotted
        max_runtime_hours fallback is encoded (evidence gate row 6:
        observability/evidence-model fix only, no unevidenced timeout
        increase — Req 2.19)."""
        assert build_domain.DEFAULT_BUILD_CONFIG["runtime_budgets"] is None
        assert build_domain.DEFAULT_BUILD_CONFIG["max_runtime_hours"] == 4


# ===========================================================================
# Task 8.5 — volume-size resolution helpers (Req 2.20, 3.13)
# ===========================================================================

class TestVolumeSizeResolution:

    def test_resolution_order(self):
        config = {"volume_size_gb": 200,
                  "volume_size_gb_by_target": {"JP6": 400}}
        assert build_domain.resolve_volume_size_gb(150, "JP6", config) == 150
        assert build_domain.resolve_volume_size_gb(None, "JP6", config) == 400
        assert build_domain.resolve_volume_size_gb(None, "JP5", config) == 200
        assert build_domain.resolve_volume_size_gb(None, "JP5", {}) is None

    def test_snapshot_untouched_without_any_volume_source(self):
        jobs = build_domain.create_build_jobs(
            targets=["JP5"], execution_mode="ephemeral", server_id=None,
            request_id="r", job_ids=["j1"], requested_by="u",
            created_at=T0, config_snapshot={"max_runtime_hours": 4})
        assert jobs[0]["config_snapshot"] == {"max_runtime_hours": 4}

    def test_none_snapshot_stays_none(self):
        jobs = build_domain.create_build_jobs(
            targets=["JP5"], execution_mode="ephemeral", server_id=None,
            request_id="r", job_ids=["j1"], requested_by="u",
            created_at=T0, config_snapshot=None)
        assert jobs[0]["config_snapshot"] is None
