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
Unit tests for the host-settled gate decisions in
``edge-cv-portal/backend/functions/build_planner.py``:
``decide_host_settled`` (ephemeral runners, P0-A) and
``decide_dedicated_settle`` (dedicated servers, P1).

Spec: .kiro/specs/ephemeral-runner-patch-reboot

The expected semantics are restated here independently of the
implementation:

- an SSM association that is Pending or InProgress (any letter case)
  blocks; Success, Failed and Skipped never do; an empty list is settled;
- ephemeral: an unreadable status (None) WAITs (fail-safe); the budget is
  the bootstrap budget (``bootstrap_timeout_minutes``, default 20, from
  ``dispatched_at``); at ``now == deadline`` still WAIT, strictly past it
  TIMEOUT with the still-running associations named in the error;
- dedicated: an unreadable status never defers (fail open); a running
  association defers for at most 30 minutes from the deferral's start,
  then the job starts with an advisory. It never fails a job.
"""
import copy
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend",
                              "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_planner  # noqa: E402

# Expected constants, restated independently of the implementation.
_MS_PER_MINUTE = 60 * 1000
_DEFAULT_BUDGET_MINUTES = 20
_CAP_MINUTES = 30
_LOG = "/var/log/dda-build-server-bootstrap.log"
_DISPATCHED_AT = 1_762_000_000_000
_PATCH = "AWS-RunPatchBaseline"
_AGENT_UPDATE = "AWS-UpdateSSMAgent"


def _job(dispatched_at=_DISPATCHED_AT, snapshot=None, **extra):
    """A provisioning ephemeral Build_Job whose marker was observed."""
    job = {
        "build_job_id": "job-settle",
        "execution_mode": "ephemeral",
        "status": "provisioning",
        "dispatched_at": dispatched_at,
        "config_snapshot": {} if snapshot is None else snapshot,
    }
    job.update(extra)
    return job


def _deadline(minutes=_DEFAULT_BUDGET_MINUTES):
    return _DISPATCHED_AT + minutes * _MS_PER_MINUTE


_INSIDE = _DISPATCHED_AT + 5 * _MS_PER_MINUTE

class TestWhichStatusesBlock:
    """Pending/InProgress block (case-insensitive); nothing else does."""

    @pytest.mark.parametrize("status", [
        "Pending", "InProgress", "inprogress", "PENDING", "inProgress",
        " InProgress "])
    def test_pending_or_in_progress_waits(self, status):
        decision = build_planner.decide_host_settled(
            _job(), [(_PATCH, status)], _INSIDE)
        assert decision.readiness == build_planner.READINESS_WAIT
        assert decision.statuses_read is True
        assert decision.running == (f"{_PATCH} ({status.strip()})",)
        assert decision.error is None

    @pytest.mark.parametrize("status", [
        "Success", "Failed", "Skipped", "success", "FAILED", ""])
    def test_other_statuses_never_block(self, status):
        decision = build_planner.decide_host_settled(
            _job(), [(_PATCH, status)], _INSIDE)
        assert decision.readiness == build_planner.READINESS_READY
        assert decision.running == ()
        assert decision.error is None

    def test_an_empty_list_is_settled(self):
        decision = build_planner.decide_host_settled(_job(), [], _INSIDE)
        assert decision.readiness == build_planner.READINESS_READY
        assert decision.statuses_read is True

    def test_one_running_association_among_settled_ones_blocks(self):
        statuses = [(_AGENT_UPDATE, "Success"), (_PATCH, "InProgress"),
                    ("AWS-GatherSoftwareInventory", "Failed")]
        decision = build_planner.decide_host_settled(
            _job(), statuses, _INSIDE)
        assert decision.readiness == build_planner.READINESS_WAIT
        assert decision.running == (f"{_PATCH} (InProgress)",)

    def test_an_unreadable_status_never_opens_the_gate(self):
        """None is the dispatcher's failed read: WAIT (fail-safe)."""
        decision = build_planner.decide_host_settled(_job(), None, _INSIDE)
        assert decision.readiness == build_planner.READINESS_WAIT
        assert decision.statuses_read is False
        assert decision.running == ()


class TestBudgetBoundary:
    """The bootstrap budget and strict boundary of decide_runner_readiness."""

    def test_at_the_deadline_it_still_waits(self):
        decision = build_planner.decide_host_settled(
            _job(), [(_PATCH, "InProgress")], _deadline())
        assert decision.readiness == build_planner.READINESS_WAIT
        assert decision.deadline == _deadline()
        assert decision.error is None

    def test_strictly_past_the_deadline_times_out_naming_them(self):
        statuses = [(_PATCH, "InProgress"), (_AGENT_UPDATE, "Pending")]
        decision = build_planner.decide_host_settled(
            _job(), statuses, _deadline() + 1)
        assert decision.readiness == build_planner.READINESS_TIMEOUT
        assert f"{_PATCH} (InProgress)" in decision.error
        assert f"{_AGENT_UPDATE} (Pending)" in decision.error
        assert f"{_DEFAULT_BUDGET_MINUTES} minutes" in decision.error
        assert _LOG in decision.error
        # The shared bootstrap-timeout failure path records the log.
        assert decision.log_path == _LOG

    def test_an_unreadable_status_past_the_deadline_times_out(self):
        decision = build_planner.decide_host_settled(
            _job(), None, _deadline() + 1)
        assert decision.readiness == build_planner.READINESS_TIMEOUT
        assert "could not be read" in decision.error

    def test_settled_is_ready_even_past_the_deadline(self):
        decision = build_planner.decide_host_settled(
            _job(), [(_PATCH, "Success")], _deadline() + 60_000)
        assert decision.readiness == build_planner.READINESS_READY

    def test_the_budget_comes_from_the_jobs_own_snapshot(self):
        job = _job(snapshot={"bootstrap_timeout_minutes": 5})
        running = [(_PATCH, "InProgress")]
        assert build_planner.decide_host_settled(
            job, running, _deadline(5)).readiness == \
            build_planner.READINESS_WAIT
        late = build_planner.decide_host_settled(
            job, running, _deadline(5) + 1)
        assert late.readiness == build_planner.READINESS_TIMEOUT
        assert "5 minutes" in late.error

    def test_a_missing_dispatched_at_waits(self):
        decision = build_planner.decide_host_settled(
            _job(dispatched_at=None), [(_PATCH, "InProgress")],
            _deadline() * 2)
        assert decision.readiness == build_planner.READINESS_WAIT
        assert decision.deadline is None

    def test_inputs_are_not_mutated(self):
        job = _job()
        statuses = [(_PATCH, "InProgress")]
        before = (copy.deepcopy(job), list(statuses))
        build_planner.decide_host_settled(job, statuses, _deadline() + 1)
        assert (job, statuses) == before

def _dedicated(since=None):
    """A queued dedicated Build_Job, optionally mid settle-deferral."""
    job = {"build_job_id": "job-dedicated", "execution_mode": "dedicated",
           "status": "queued", "server_id": "srv-1"}
    if since is not None:
        job["host_settle"] = {"deferral_started_at": since}
    return job


_T0 = 1_762_100_000_000
_CAP_MS = _CAP_MINUTES * _MS_PER_MINUTE


class TestDedicatedSettle:
    """P1: advisory deferral — capped, fail open, never a failure."""

    def test_the_cap_constant(self):
        assert build_planner.DEDICATED_SETTLE_DEFERRAL_CAP_MINUTES == \
            _CAP_MINUTES

    def test_an_unreadable_status_never_defers(self):
        decision = build_planner.decide_dedicated_settle(
            _dedicated(), None, _T0)
        assert decision.action == build_planner.PREDISPATCH_START
        assert decision.statuses_read is False
        assert "could not be read" in decision.advisory
        assert "fail open" in decision.advisory

    def test_a_settled_server_starts_with_no_advisory(self):
        decision = build_planner.decide_dedicated_settle(
            _dedicated(), [(_PATCH, "Success")], _T0)
        assert decision.action == build_planner.PREDISPATCH_START
        assert decision.advisory is None
        assert decision.deferral_started_at is None

    def test_a_running_association_defers_and_starts_the_clock(self):
        decision = build_planner.decide_dedicated_settle(
            _dedicated(), [(_PATCH, "InProgress")], _T0)
        assert decision.action == build_planner.PREDISPATCH_DEFER
        assert decision.deferral_started_at == _T0
        assert decision.running == (f"{_PATCH} (InProgress)",)
        assert f"{_PATCH} (InProgress)" in decision.advisory

    def test_a_later_deferral_keeps_its_start(self):
        decision = build_planner.decide_dedicated_settle(
            _dedicated(since=_T0), [(_PATCH, "pending")],
            _T0 + 10 * _MS_PER_MINUTE)
        assert decision.action == build_planner.PREDISPATCH_DEFER
        assert decision.deferral_started_at == _T0

    def test_at_the_cap_it_still_defers(self):
        decision = build_planner.decide_dedicated_settle(
            _dedicated(since=_T0), [(_PATCH, "InProgress")], _T0 + _CAP_MS)
        assert decision.action == build_planner.PREDISPATCH_DEFER

    def test_past_the_cap_it_starts_with_an_advisory(self):
        decision = build_planner.decide_dedicated_settle(
            _dedicated(since=_T0), [(_PATCH, "InProgress")],
            _T0 + _CAP_MS + 1)
        assert decision.action == build_planner.PREDISPATCH_START
        assert f"{_CAP_MINUTES}-minute" in decision.advisory
        assert f"{_PATCH} (InProgress)" in decision.advisory

    def test_a_deferral_ending_in_success_starts_with_an_advisory(self):
        decision = build_planner.decide_dedicated_settle(
            _dedicated(since=_T0), [(_PATCH, "Success")],
            _T0 + 12 * _MS_PER_MINUTE)
        assert decision.action == build_planner.PREDISPATCH_START
        assert decision.deferral_started_at == _T0
        assert "12-minute deferral" in decision.advisory

    def test_it_never_returns_anything_but_start_or_defer(self):
        """No input makes the dedicated decision fail a job."""
        actions = set()
        for statuses in (None, [], [(_PATCH, "InProgress")],
                         [(_PATCH, "Failed")]):
            for since in (None, _T0):
                for now in (_T0, _T0 + _CAP_MS, _T0 + 10 * _CAP_MS):
                    actions.add(build_planner.decide_dedicated_settle(
                        _dedicated(since), statuses, now).action)
        assert actions == {build_planner.PREDISPATCH_START,
                           build_planner.PREDISPATCH_DEFER}
