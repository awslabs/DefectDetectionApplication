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
"""Unit tests for local_auth.lockout.

Feature: portal-user-manager (Requirement 8.10).
"""
from local_auth.lockout import (
    LOCKOUT_DURATION_SECONDS,
    MAX_CONSECUTIVE_FAILURES,
    LockoutTracker,
    get_default_tracker,
)

NOW = 1_700_000_000.0
USER = "operator1"


def make_tracker():
    return LockoutTracker(clock=lambda: NOW)


def fail_times(tracker, count, now=NOW, username=USER):
    for _ in range(count):
        tracker.record_failure(username, now=now)


class TestLockThreshold:
    def test_constants_match_requirement(self):
        assert MAX_CONSECUTIVE_FAILURES == 5
        assert LOCKOUT_DURATION_SECONDS == 15 * 60

    def test_not_locked_with_no_attempts(self):
        assert not make_tracker().is_locked(USER, now=NOW)

    def test_four_failures_not_locked(self):
        tracker = make_tracker()
        fail_times(tracker, 4)
        assert not tracker.is_locked(USER, now=NOW)

    def test_fifth_consecutive_failure_locks(self):
        tracker = make_tracker()
        fail_times(tracker, 5)
        assert tracker.is_locked(USER, now=NOW)

    def test_injected_clock_used_when_now_omitted(self):
        tracker = make_tracker()
        for _ in range(5):
            tracker.record_failure(USER)
        assert tracker.is_locked(USER)


class TestLockWindow:
    def test_locked_just_before_window_ends(self):
        tracker = make_tracker()
        fail_times(tracker, 5)
        assert tracker.is_locked(USER, now=NOW + LOCKOUT_DURATION_SECONDS - 1)

    def test_unlocked_at_window_end(self):
        tracker = make_tracker()
        fail_times(tracker, 5)
        assert not tracker.is_locked(USER, now=NOW + LOCKOUT_DURATION_SECONDS)

    def test_failures_while_locked_do_not_extend_window(self):
        tracker = make_tracker()
        fail_times(tracker, 5)
        # Hammer the account halfway through the window; the lock must
        # still end exactly 15 minutes after the 5th original failure.
        fail_times(tracker, 10, now=NOW + LOCKOUT_DURATION_SECONDS / 2)
        assert not tracker.is_locked(USER, now=NOW + LOCKOUT_DURATION_SECONDS)

    def test_success_while_locked_does_not_clear_lock(self):
        tracker = make_tracker()
        fail_times(tracker, 5)
        tracker.record_success(USER, now=NOW + 60)
        assert tracker.is_locked(USER, now=NOW + 61)

    def test_counter_resets_after_window_elapses(self):
        tracker = make_tracker()
        fail_times(tracker, 5)
        after = NOW + LOCKOUT_DURATION_SECONDS
        # A fresh streak of 5 failures is required to lock again.
        fail_times(tracker, 4, now=after)
        assert not tracker.is_locked(USER, now=after)
        tracker.record_failure(USER, now=after)
        assert tracker.is_locked(USER, now=after)


class TestSuccessReset:
    def test_success_resets_consecutive_failure_count(self):
        tracker = make_tracker()
        fail_times(tracker, 4)
        tracker.record_success(USER, now=NOW)
        fail_times(tracker, 4)
        assert not tracker.is_locked(USER, now=NOW)
        tracker.record_failure(USER, now=NOW)
        assert tracker.is_locked(USER, now=NOW)


class TestPerAccountIsolation:
    def test_lock_applies_only_to_the_failing_account(self):
        tracker = make_tracker()
        fail_times(tracker, 5, username="alice")
        assert tracker.is_locked("alice", now=NOW)
        assert not tracker.is_locked("bob", now=NOW)


class TestDefaultTracker:
    def test_default_tracker_is_shared_singleton(self):
        assert get_default_tracker() is get_default_tracker()
        assert isinstance(get_default_tracker(), LockoutTracker)
