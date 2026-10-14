"""Unit tests for harnesslib.restoration (Reqs 4.3, 6.4, 8.3).

Covers reverse-order teardown, found-running entries left untouched, and
restoration failures collected as warnings without ever raising into test
outcomes.
"""

import pytest
from harnesslib.restoration import RestorationEntry, StateRegistry


def recorder(log, label, error=None):
    """A stop callable appending ``label`` to ``log`` (or raising ``error``)."""

    def stop():
        if error is not None:
            raise error
        log.append(label)

    return stop


class TestRecording:
    def test_entries_kept_in_record_order(self):
        registry = StateRegistry()
        registry.record("model", "m1", "UNAVAILABLE", lambda: None)
        registry.record("workflow", "wf1", None, lambda: None)
        assert registry.entries == (
            RestorationEntry("model", "m1", "UNAVAILABLE"),
            RestorationEntry("workflow", "wf1", None),
        )

    def test_harness_started_when_pre_state_inactive(self):
        assert RestorationEntry("model", "m", "UNAVAILABLE").harness_started
        assert RestorationEntry("model", "m", "STOPPED").harness_started
        assert RestorationEntry("workflow", "w", None).harness_started

    def test_not_harness_started_when_found_active(self):
        assert not RestorationEntry("model", "m", "READY").harness_started
        assert not RestorationEntry("model", "m", "RUNNING").harness_started
        assert not RestorationEntry("model", "m", "LOADING").harness_started
        assert not RestorationEntry("model", "m", "STARTING").harness_started


class TestRestoreAll:
    def test_teardown_runs_in_reverse_order(self):
        log = []
        registry = StateRegistry()
        registry.record("model", "m1", "UNAVAILABLE", recorder(log, "stop-m1"))
        registry.record("model", "m2", "UNAVAILABLE", recorder(log, "stop-m2"))
        registry.record("workflow", "wf", None, recorder(log, "stop-wf"))
        registry.restore_all()
        assert log == ["stop-wf", "stop-m2", "stop-m1"]

    def test_found_running_entries_untouched(self):
        log = []
        registry = StateRegistry()
        registry.record("model", "found-ready", "READY", recorder(log, "stop-found"))
        registry.record("model", "started", "UNAVAILABLE", recorder(log, "stop-started"))
        warnings = registry.restore_all()
        assert log == ["stop-started"]  # the READY entry was never stopped
        assert warnings == []

    def test_restore_all_is_consumed_and_idempotent(self):
        log = []
        registry = StateRegistry()
        registry.record("model", "m", "UNAVAILABLE", recorder(log, "stop"))
        registry.restore_all()
        registry.restore_all()
        assert log == ["stop"]  # second call is a no-op


class TestWarningCapture:
    def test_failing_stop_collected_as_warning_never_raises(self):
        registry = StateRegistry()
        registry.record("model", "m", "UNAVAILABLE", recorder([], "x", error=RuntimeError("boom")))
        warnings = registry.restore_all()  # must not raise (Req 8.3)
        assert len(warnings) == 1
        assert "model" in warnings[0]
        assert "'m'" in warnings[0]
        assert "boom" in warnings[0]
        assert registry.warnings == warnings

    def test_one_failure_does_not_stop_remaining_restorations(self):
        log = []
        registry = StateRegistry()
        registry.record("model", "m1", "UNAVAILABLE", recorder(log, "stop-m1"))
        registry.record("model", "m2", "UNAVAILABLE", recorder(log, "x", error=OSError("net down")))
        registry.record("model", "m3", "UNAVAILABLE", recorder(log, "stop-m3"))
        warnings = registry.restore_all()
        # m2 (reverse order: after m3, before m1) failed; m3 and m1 still restored.
        assert log == ["stop-m3", "stop-m1"]
        assert len(warnings) == 1
        assert "'m2'" in warnings[0]

    def test_warnings_accumulate_and_survive_for_results_bundle(self):
        registry = StateRegistry()
        registry.record("workflow", "wf", None, recorder([], "x", error=RuntimeError("first")))
        registry.restore_all()
        assert len(registry.warnings) == 1  # readable after teardown (results.json)

    def test_warning_names_pre_state(self):
        registry = StateRegistry()
        registry.record("model", "m", "STOPPED", recorder([], "x", error=RuntimeError("nope")))
        (warning,) = registry.restore_all()
        assert "STOPPED" in warning
