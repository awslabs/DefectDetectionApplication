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
Unit tests for the bootstrap completion gate decision
``decide_runner_readiness`` in
``edge-cv-portal/backend/functions/build_planner.py``.

Spec: .kiro/specs/build-source-selection (design A2, task 4.1)

**Validates: Requirements 6.2, 6.3, 6.4**

The expected readiness semantics are restated here independently of the
implementation. For a provisioning Build_Job, its marker-probe output, and
the probe time:

- the marker ``/var/log/dda-build-server-bootstrap.done`` observed means
  READY, regardless of recorded inner-step failures — the live runner
  ``i-0b8221f5ed2ebc2a9`` logged ``Failed: sudo chmod 666
  /var/run/docker.sock`` and ``Failed to set Python 3.11 as default`` while
  cloud-init still finished successfully, so the readiness signal is
  authoritative and the bootstrap log location is surfaced for diagnosis
  (Req 6.4);
- the marker absent at or below the bootstrap budget means WAIT, so no
  agent command is sent while the marker has not been observed, and the
  decision is a function of the probe plus time alone — never a sleep
  (Req 6.1, 6.2);
- the marker absent STRICTLY past the budget deadline means TIMEOUT with an
  error naming bootstrap's budget and log, so the job fails instead of
  waiting indefinitely; at ``now == deadline`` the decision is still WAIT,
  matching the module's existing strict watchdog boundary (Req 6.3);
- the budget is ``bootstrap_timeout_minutes`` from the job's own
  ``config_snapshot`` (default 20 minutes; the observed bootstrap took
  ~140 s), measured from ``dispatched_at``;
- unexpected, empty, or absent probe output is never READY and never
  raises.
"""
import os
import sys

# Import the pure planner module from the portal Lambda bundle.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_planner  # noqa: E402

# Expected constants, restated independently of the implementation.
_MS_PER_MINUTE = 60 * 1000
_DEFAULT_BUDGET_MINUTES = 20                      # design A2 default
_MARKER = "/var/log/dda-build-server-bootstrap.done"
_LOG = "/var/log/dda-build-server-bootstrap.log"

_DISPATCHED_AT = 1_762_000_000_000

# The live inner-step failures the bootstrap log carried while cloud-init
# still finished successfully (Req 6.4).
_INNER_FAILURES = (
    "Failed: sudo chmod 666 /var/run/docker.sock\n"
    "Failed to set Python 3.11 as default\n"
)

# Exactly the probe output the dispatcher's BOOTSTRAP_PROBE_COMMANDS
# produce (task 4.2 wires them).
_PROBE_DONE = f"BOOTSTRAP_DONE=1\nBOOTSTRAP_LOG={_LOG}\n"
_PROBE_NOT_DONE = f"BOOTSTRAP_DONE=0\nBOOTSTRAP_LOG={_LOG}\n"


def _job(build_job_id="job-readiness", dispatched_at=_DISPATCHED_AT, snapshot=None):
    """A provisioning ephemeral Build_Job awaiting its runner's bootstrap."""
    return {
        "build_job_id": build_job_id,
        "execution_mode": "ephemeral",
        "build_target": "JP6",
        "status": "provisioning",
        "dispatched_at": dispatched_at,
        "config_snapshot": {} if snapshot is None else snapshot,
    }


def _deadline(minutes=_DEFAULT_BUDGET_MINUTES, dispatched_at=_DISPATCHED_AT):
    return dispatched_at + minutes * _MS_PER_MINUTE


class TestMarkerObservedIsAuthoritative:
    """Req 6.2, 6.4 — the marker means READY."""

    def test_marker_present_is_ready_with_the_log_path_surfaced(self):
        decision = build_planner.decide_runner_readiness(
            _job(), _PROBE_DONE, _DISPATCHED_AT + 140_000  # the observed ~140 s
        )
        assert decision.readiness == build_planner.READINESS_READY
        assert decision.marker_observed is True
        assert decision.log_path == _LOG
        assert decision.error is None
        assert decision.build_job_id == "job-readiness"

    def test_marker_present_with_inner_step_failures_is_still_ready(self):
        """The readiness signal is authoritative: the live runner logged
        `Failed: sudo chmod 666 /var/run/docker.sock` and `Failed to set
        Python 3.11 as default` while cloud-init still finished (Req 6.4)."""
        probe = _INNER_FAILURES + _PROBE_DONE
        decision = build_planner.decide_runner_readiness(
            _job(), probe, _DISPATCHED_AT + 140_000
        )
        assert decision.readiness == build_planner.READINESS_READY
        assert decision.marker_observed is True
        # The log location is recorded so the partial failures are
        # diagnosable (Req 6.4).
        assert decision.log_path == _LOG

    def test_marker_present_past_the_deadline_is_ready_not_timeout(self):
        """The signal is authoritative even on a late probe: a runner that
        finished bootstrapping is ready, not timed out."""
        decision = build_planner.decide_runner_readiness(
            _job(), _PROBE_DONE, _deadline() + 60_000
        )
        assert decision.readiness == build_planner.READINESS_READY
        assert decision.error is None

    def test_lowercase_true_value_is_accepted(self):
        decision = build_planner.decide_runner_readiness(
            _job(), "BOOTSTRAP_DONE=true", _DISPATCHED_AT + 1
        )
        assert decision.readiness == build_planner.READINESS_READY


class TestBoundaryConvention:
    """Req 6.3 — WAIT at the deadline, TIMEOUT strictly past it."""

    def test_marker_absent_at_exactly_the_deadline_is_wait(self):
        decision = build_planner.decide_runner_readiness(
            _job(), _PROBE_NOT_DONE, _deadline()
        )
        assert decision.readiness == build_planner.READINESS_WAIT
        assert decision.marker_observed is False
        assert decision.deadline == _deadline()
        assert decision.error is None

    def test_marker_absent_one_ms_before_the_deadline_is_wait(self):
        decision = build_planner.decide_runner_readiness(
            _job(), _PROBE_NOT_DONE, _deadline() - 1
        )
        assert decision.readiness == build_planner.READINESS_WAIT

    def test_marker_absent_one_ms_past_the_deadline_is_timeout(self):
        decision = build_planner.decide_runner_readiness(
            _job(), _PROBE_NOT_DONE, _deadline() + 1
        )
        assert decision.readiness == build_planner.READINESS_TIMEOUT
        assert decision.marker_observed is False
        # The failure names the bootstrap budget and where to look (Req 6.3).
        assert decision.error is not None
        assert "20 minutes" in decision.error
        assert _LOG in decision.error

    def test_early_probe_inside_the_budget_is_wait(self):
        """The observed live case: the agent command was requested 140 s in,
        while cloud-init had not finished (Req 6.1)."""
        decision = build_planner.decide_runner_readiness(
            _job(), _PROBE_NOT_DONE, _DISPATCHED_AT + 140_000
        )
        assert decision.readiness == build_planner.READINESS_WAIT

    def test_unknown_dispatch_time_never_times_out(self):
        """No dispatch time means elapsed bootstrap time cannot be
        established, so the gate stays closed rather than failing the job."""
        decision = build_planner.decide_runner_readiness(
            _job(dispatched_at=None), _PROBE_NOT_DONE, _DISPATCHED_AT + 10 ** 9
        )
        assert decision.readiness == build_planner.READINESS_WAIT
        assert decision.deadline is None
        assert decision.error is None


class TestDefensiveProbeParsing:
    """Req 6.2 — unexpected output is never READY and never raises."""

    def test_empty_none_and_garbage_output_is_not_ready(self):
        for probe in (
            None,
            "",
            "   ",
            "\n\n",
            "BOOTSTRAP_DONE",                       # no '=' at all
            "BOOTSTRAP_DONE=",                       # empty value
            "BOOTSTRAP_DONE=maybe",                  # unrecognized value
            "=1",                                    # no key
            "cannot open /var/log/...: Permission denied",
            "An error occurred (InvalidInstanceId) when calling SendCommand",
            f"BOOTSTRAP_LOG={_MARKER}",              # marker path, not the signal
            _INNER_FAILURES,
            "\x00\x01 binary garbage \ufffd",
        ):
            decision = build_planner.decide_runner_readiness(
                _job(), probe, _DISPATCHED_AT + 1
            )
            assert decision.readiness != build_planner.READINESS_READY, probe
            assert decision.marker_observed is False, probe
            # Inside the budget the absent marker is a WAIT, not a failure.
            assert decision.readiness == build_planner.READINESS_WAIT, probe

    def test_absent_log_line_falls_back_to_the_documented_log_path(self):
        decision = build_planner.decide_runner_readiness(
            _job(), "BOOTSTRAP_DONE=1", _DISPATCHED_AT + 1
        )
        assert decision.readiness == build_planner.READINESS_READY
        assert decision.log_path == _LOG

    def test_contradictory_output_is_not_ready(self):
        """Readiness must be positively established: a probe reporting both
        states leaves the gate closed."""
        decision = build_planner.decide_runner_readiness(
            _job(), "BOOTSTRAP_DONE=1\nBOOTSTRAP_DONE=0", _DISPATCHED_AT + 1
        )
        assert decision.readiness == build_planner.READINESS_WAIT

    def test_garbage_output_past_the_deadline_still_times_out(self):
        decision = build_planner.decide_runner_readiness(
            _job(), "totally unexpected output", _deadline() + 1
        )
        assert decision.readiness == build_planner.READINESS_TIMEOUT


class TestSnapshottedBudget:
    """The budget comes from the job's OWN config_snapshot."""

    def test_custom_bootstrap_timeout_minutes_is_honored(self):
        job = _job(snapshot={"bootstrap_timeout_minutes": 3})
        deadline = _deadline(minutes=3)
        assert build_planner.decide_runner_readiness(
            job, _PROBE_NOT_DONE, deadline
        ).readiness == build_planner.READINESS_WAIT
        timed_out = build_planner.decide_runner_readiness(
            job, _PROBE_NOT_DONE, deadline + 1
        )
        assert timed_out.readiness == build_planner.READINESS_TIMEOUT
        assert timed_out.deadline == deadline
        assert timed_out.timeout_minutes == 3
        assert "3 minutes" in timed_out.error
        # The default budget would still be waiting at this time.
        assert build_planner.decide_runner_readiness(
            _job(), _PROBE_NOT_DONE, deadline + 1
        ).readiness == build_planner.READINESS_WAIT

    def test_absent_and_none_budget_use_the_documented_default(self):
        for snapshot in ({}, {"bootstrap_timeout_minutes": None}, None):
            job = _job(snapshot=snapshot)
            assert build_planner.decide_runner_readiness(
                job, _PROBE_NOT_DONE, _deadline()
            ).readiness == build_planner.READINESS_WAIT
            assert build_planner.decide_runner_readiness(
                job, _PROBE_NOT_DONE, _deadline() + 1
            ).readiness == build_planner.READINESS_TIMEOUT
            assert build_planner.bootstrap_timeout_ms(snapshot) == \
                _DEFAULT_BUDGET_MINUTES * _MS_PER_MINUTE


class TestPurityAndConstants:
    def test_decision_does_not_mutate_its_inputs(self):
        job = _job(snapshot={"bootstrap_timeout_minutes": 7})
        before = {"job": dict(job), "snapshot": dict(job["config_snapshot"])}
        build_planner.decide_runner_readiness(job, _PROBE_DONE, _DISPATCHED_AT + 5)
        build_planner.decide_runner_readiness(job, _PROBE_NOT_DONE, _deadline() + 5)
        assert job == before["job"]
        assert job["config_snapshot"] == before["snapshot"]

    def test_readiness_values_are_the_documented_strings(self):
        assert build_planner.READINESS_READY == "ready"
        assert build_planner.READINESS_WAIT == "wait"
        assert build_planner.READINESS_TIMEOUT == "timeout"
        assert build_planner.BOOTSTRAP_MARKER_PATH == _MARKER
        assert build_planner.BOOTSTRAP_LOG_PATH == _LOG
        assert build_planner.DEFAULT_BOOTSTRAP_TIMEOUT_MINUTES == \
            _DEFAULT_BUDGET_MINUTES
