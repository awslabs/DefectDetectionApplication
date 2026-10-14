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
Property test for the watchdog deadline arithmetic decisions in
``edge-cv-portal/backend/functions/build_planner.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates

**Validates: Requirements 3.8, 3.9, 6.11, 7.7**

The expected deadline semantics are restated here independently of the
implementation. For any generated timestamps and configured limits:

- a running Build_Job (building or publishing, with a known start time)
  is timed out if and only if its elapsed runtime STRICTLY exceeds the
  ``max_runtime_hours`` of its own ``config_snapshot`` (default 4 hours
  when absent); a timeout marks the job failed with a timeout error,
  anything else leaves the status unchanged with no error (Req 3.8);
- a failed Ephemeral_Build_Runner termination is retried if and only if
  less than 1 hour has passed since the FIRST failure AND at least the
  10-minute retry interval has elapsed since the last attempt (a retry
  is immediately due when none was attempted yet); the orphaned-runner
  notification fires exactly when the 1-hour window is exhausted and it
  has not fired before, and no retry is due once the window is
  exhausted (Req 3.9);
- a pending fleet action is reported failed if and only if its
  10-minute deadline has STRICTLY passed (at or before the deadline it
  is still pending); a failure's error identifies the action, the
  target server, and the server's current lifecycle state (Req 6.11);
- a running server's serialization check is due if and only if the
  server was never checked or at least the 5-minute check interval has
  elapsed since its last check (Req 7.7).
"""
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

# Expected constants, restated independently of the implementation.
_MS_PER_MINUTE = 60 * 1000
_MS_PER_HOUR = 60 * _MS_PER_MINUTE
_DEFAULT_MAX_RUNTIME_HOURS = 4                      # Req 9.2 default
_TERMINATION_RETRY_INTERVAL_MS = 10 * _MS_PER_MINUTE  # Req 3.9
_TERMINATION_RETRY_WINDOW_MS = 1 * _MS_PER_HOUR       # Req 3.9
_FLEET_ACTION_DEADLINE_MS = 10 * _MS_PER_MINUTE       # Req 6.11
_SERIALIZATION_CHECK_INTERVAL_MS = 5 * _MS_PER_MINUTE  # Req 7.7

_RUNNING = ("building", "publishing")
_ALL_STATUSES = (
    "queued", "provisioning", "building", "publishing",
    "succeeded", "failed", "interrupted", "cancelled",
)

_T0 = st.integers(min_value=0, max_value=10 ** 13)  # base ms-epoch times


def _around(boundary_ms):
    """Millisecond offsets concentrated around a deadline boundary so the
    strict-vs-inclusive comparisons are actually exercised, plus offsets
    across the whole surrounding range."""
    return st.one_of(
        st.sampled_from([boundary_ms - 1, boundary_ms, boundary_ms + 1]),
        st.integers(min_value=0, max_value=2 * boundary_ms),
    )


# ---- Runtime timeout scenario (Req 3.8) -----------------------------------

@st.composite
def _timeout_scenarios(draw):
    """A Build_Job in any status, with/without a known start time and
    with/without a snapshot max_runtime_hours, observed at a time
    concentrated around its runtime limit."""
    status = draw(st.sampled_from(_ALL_STATUSES))
    hours = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=8)))
    limit_ms = (hours if hours is not None else _DEFAULT_MAX_RUNTIME_HOURS) * _MS_PER_HOUR
    started_at = draw(st.one_of(st.none(), _T0))
    snapshot = draw(st.one_of(
        st.none(),
        st.just({}),
        st.just({"max_runtime_hours": hours} if hours is not None else {}),
    ))
    # Keep the job/snapshot consistent: when the snapshot carries no
    # max_runtime_hours the expected limit is the 4-hour default.
    if not snapshot or "max_runtime_hours" not in snapshot:
        limit_ms = _DEFAULT_MAX_RUNTIME_HOURS * _MS_PER_HOUR
    base = started_at if started_at is not None else draw(_T0)
    now = base + draw(_around(limit_ms))
    job = {
        "build_job_id": draw(st.uuids().map(str)),
        "status": status,
        "started_at": started_at,
        "config_snapshot": snapshot,
    }
    return job, now, limit_ms


# ---- Termination retry scenario (Req 3.9) ---------------------------------

@st.composite
def _termination_scenarios(draw):
    first_failed_at = draw(_T0)
    now = first_failed_at + draw(_around(_TERMINATION_RETRY_WINDOW_MS))
    last_attempt_at = draw(st.one_of(
        st.none(),
        st.builds(
            lambda d: first_failed_at + d,
            _around(_TERMINATION_RETRY_INTERVAL_MS),
        ).filter(lambda t: t <= now),
        st.integers(min_value=first_failed_at, max_value=max(now, first_failed_at)),
    ))
    already_notified = draw(st.booleans())
    return first_failed_at, last_attempt_at, now, already_notified


# ---- Pending fleet action scenario (Req 6.11) ------------------------------

@st.composite
def _pending_action_scenarios(draw):
    initiated_at = draw(_T0)
    explicit_deadline = draw(st.booleans())
    if explicit_deadline:
        deadline = draw(_T0)
        pending = {
            "action": draw(st.sampled_from(["start", "stop", "terminate", "launch"])),
            "deadline": deadline,
        }
    else:
        deadline = initiated_at + _FLEET_ACTION_DEADLINE_MS
        pending = {
            "action": draw(st.sampled_from(["start", "stop", "terminate", "launch"])),
            "initiated_at": initiated_at,
        }
    now = deadline + draw(st.one_of(
        st.sampled_from([-1, 0, 1]),
        st.integers(min_value=-deadline if deadline < 10 ** 13 else 0,
                    max_value=2 * _FLEET_ACTION_DEADLINE_MS),
    ))
    server = {
        "server_id": draw(st.sampled_from(["srv-a", "srv-b", "srv-c"])),
        "lifecycle_state": draw(st.sampled_from(
            ["pending", "running", "stopping", "stopped",
             "shutting-down", "terminated"]
        )),
    }
    return pending, server, deadline, now


# Feature: portal-build-fleet-and-workflow-gates, Property 8: Watchdog deadline arithmetic
# Validates: Requirements 3.8, 3.9, 6.11, 7.7
@settings(max_examples=200)
@given(
    timeout_scenario=_timeout_scenarios(),
    termination_scenario=_termination_scenarios(),
    pending_scenario=_pending_action_scenarios(),
    last_checked_at=st.one_of(st.none(), _T0),
    check_now=_T0,
    check_delta=_around(_SERIALIZATION_CHECK_INTERVAL_MS),
)
def test_watchdog_deadline_arithmetic(
    timeout_scenario,
    termination_scenario,
    pending_scenario,
    last_checked_at,
    check_now,
    check_delta,
):
    """For any generated timestamps and configured limits: runtime timeout
    iff elapsed > snapshot max (Req 3.8); termination retry cadence and
    orphaned-runner notification exactly at window exhaustion (Req 3.9);
    pending fleet action failed iff its 10-minute deadline passed
    (Req 6.11); serialization check due iff the check interval elapsed
    since the last check (Req 7.7)."""

    # --- Runtime timeout: timed out iff running, start known, and
    # elapsed STRICTLY exceeds the job's own snapshot limit (Req 3.8) ---
    job, now, limit_ms = timeout_scenario
    decision = build_planner.decide_runtime_timeout(job, now)
    expect_timeout = (
        job["status"] in _RUNNING
        and job["started_at"] is not None
        and (now - job["started_at"]) > limit_ms
    )
    assert decision.timed_out == expect_timeout, (
        f"status={job['status']!r} started_at={job['started_at']!r} "
        f"now={now} limit_ms={limit_ms}: decide_runtime_timeout returned "
        f"timed_out={decision.timed_out}, expected {expect_timeout}"
    )
    assert decision.build_job_id == job["build_job_id"]
    if expect_timeout:
        # Timed out -> failed with a timeout error (Req 3.8).
        assert decision.status == "failed", (
            f"timed-out job status is {decision.status!r}, expected 'failed'"
        )
        assert decision.error and "timeout" in decision.error.lower(), (
            f"timed-out job error {decision.error!r} does not identify a "
            f"timeout"
        )
    else:
        # Not timed out -> status unchanged, no error.
        assert decision.status == job["status"]
        assert decision.error is None

    # --- Termination retry cadence and orphaned-runner notification
    # (Req 3.9) ---
    first_failed_at, last_attempt_at, t_now, already_notified = termination_scenario
    tdecision = build_planner.decide_termination_retry(
        first_failed_at, last_attempt_at, t_now, already_notified
    )
    within_window = (t_now - first_failed_at) < _TERMINATION_RETRY_WINDOW_MS
    expect_retry = within_window and (
        last_attempt_at is None
        or (t_now - last_attempt_at) >= _TERMINATION_RETRY_INTERVAL_MS
    )
    expect_notify = (not within_window) and not already_notified
    assert tdecision.retry == expect_retry, (
        f"first_failed_at={first_failed_at} last_attempt_at={last_attempt_at} "
        f"now={t_now}: retry={tdecision.retry}, expected {expect_retry} "
        f"(<=10-minute cadence within the 1-hour window, Req 3.9)"
    )
    assert tdecision.notify_orphaned == expect_notify, (
        f"first_failed_at={first_failed_at} now={t_now} "
        f"already_notified={already_notified}: "
        f"notify_orphaned={tdecision.notify_orphaned}, expected "
        f"{expect_notify} (notification exactly at window exhaustion, Req 3.9)"
    )
    # A retry is never due once the window is exhausted.
    assert not (tdecision.retry and not within_window)

    # --- Pending fleet action failed iff its 10-minute deadline has
    # strictly passed (Req 6.11) ---
    pending, server, deadline, p_now = pending_scenario
    pdecision = build_planner.decide_pending_action(pending, server, p_now)
    expect_failed = p_now > deadline
    assert pdecision.failed == expect_failed, (
        f"deadline={deadline} now={p_now}: pending action "
        f"failed={pdecision.failed}, expected {expect_failed} (Req 6.11)"
    )
    assert pdecision.action == pending["action"]
    assert pdecision.server_id == server["server_id"]
    assert pdecision.lifecycle_state == server["lifecycle_state"]
    if expect_failed:
        # The error identifies the action, the target server, and the
        # server's current lifecycle state (Req 6.11).
        assert pdecision.error
        assert pending["action"] in pdecision.error
        assert str(server["server_id"]) in pdecision.error
        assert str(server["lifecycle_state"]) in pdecision.error
    else:
        assert pdecision.error is None

    # --- Serialization check due iff the 5-minute interval elapsed since
    # the last check, immediately when never checked (Req 7.7) ---
    if last_checked_at is None:
        assert build_planner.is_serialization_check_due(None, check_now), (
            "a never-checked server must be checked immediately (Req 7.7)"
        )
    else:
        s_now = last_checked_at + check_delta
        due = build_planner.is_serialization_check_due(last_checked_at, s_now)
        expect_due = (s_now - last_checked_at) >= _SERIALIZATION_CHECK_INTERVAL_MS
        assert due == expect_due, (
            f"last_checked_at={last_checked_at} now={s_now}: "
            f"is_serialization_check_due returned {due}, expected "
            f"{expect_due} (5-minute interval, Req 7.7)"
        )
