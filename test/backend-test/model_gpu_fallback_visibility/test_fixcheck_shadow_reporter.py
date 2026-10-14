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
"""Fix-check tests (Task 4.3) for the ``dda-model-status`` shadow reporter
(``src/backend/utils/model_status_shadow.py`` — design File 5, fix-check
case 7, Decisions 4-5).

Behaviors covered against a fake accessor (no real IPC, no real sleeps —
the debounce is driven by patching the module's ``_clock`` seam):

- write on the FIRST snapshot: exact accessor call asserted — thing name,
  ``dda-model-status`` shadow name, payload ``{"reported": snapshot}``
  (the camera-sync convention: the accessor wraps in ``{"state": ...}``
  itself), snapshot matching the documented shadow-document shape;
- NO write on an identical snapshot (canonical-JSON change gate);
- debounced write on change: within ``DEBOUNCE_SECONDS`` no write, after
  the window the changed snapshot is written;
- accessor exception swallowed and logged, ``report`` never raises, the
  in-flight flag is cleared, and — because the failed write un-pins the
  last-written canonical — a post-debounce retry of the SAME snapshot
  writes again;
- single in-flight write: a slow (event-blocking) accessor holds the first
  write in flight; a second report during the flight is dropped, and the
  reporter recovers once the flight completes;
- missing ``AWS_IOT_THING_NAME`` → complete no-op (host/dev context);
- debounce arithmetic unit legs: first-ever write bypasses the window,
  the boundary (``elapsed == DEBOUNCE_SECONDS``) admits the write,
  just-under does not (design Unit Tests).

Honesty guard: the reporter is exercised entirely through its module-level
test seams (``_accessor_override``, ``_clock``, ``_write_thread``,
``_reset_state``); no Greengrass IPC or awsiot stack is touched — the
module imports host-side by design (lazy accessor resolution).

Validates: Requirements 2.5, 3.4
"""
import logging
import threading

import pytest

from model_gpu_fallback_visibility.fakes import FakeShadowAccessor

from utils import model_status_shadow as mss

THING_NAME = "jetson-thor1"

#: Documented shadow document (design Decision 4) — the degraded example.
DEGRADED_SNAPSHOT = {
    "models": {
        "yolo_test": {
            "status": "READY",
            "runtime": "onnx",
            "gpuRequested": True,
            "gpuActive": False,
        }
    },
    "gpuDegraded": True,
    "gpuChainModels": 3,
    "gpuActiveModels": 0,
    "updatedAt": "2026-08-15T20:25:01Z",
}

HEALTHY_SNAPSHOT = {
    "models": {
        "yolo_test": {
            "status": "READY",
            "runtime": "onnx",
            "gpuRequested": True,
            "gpuActive": True,
        }
    },
    "gpuDegraded": False,
    "gpuChainModels": 3,
    "gpuActiveModels": 3,
    "updatedAt": "2026-08-15T21:00:00Z",
}


class FakeClock:
    """Controllable monotonic clock for the ``_clock`` seam."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class BlockingShadowAccessor:
    """Accessor whose write blocks until released — simulates a slow IPC
    write so the single-in-flight exclusion is observable."""

    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def update_thing_shadow_state_request(self, thing_name, shadow_name,
                                          payload):
        self.entered.set()
        assert self.release.wait(timeout=5), "blocking accessor never released"
        self.calls.append((thing_name, shadow_name, payload))


def _join_write():
    """Await the reporter's async write (tests join ``_write_thread``)."""
    thread = mss._write_thread
    if thread is not None:
        thread.join(timeout=5)
        assert not thread.is_alive(), "shadow write thread did not finish"


@pytest.fixture()
def reporter(monkeypatch):
    """Reporter wired to a recording fake accessor and a fake clock, with
    a real thing name; state reset on both sides of the test."""
    mss._reset_state()
    monkeypatch.setenv("AWS_IOT_THING_NAME", THING_NAME)
    clock = FakeClock()
    accessor = FakeShadowAccessor()
    monkeypatch.setattr(mss, "_clock", clock)
    monkeypatch.setattr(mss, "_accessor_override", accessor)
    monkeypatch.setattr(mss, "DEBOUNCE_SECONDS", 30.0)
    yield clock, accessor
    _join_write()
    mss._reset_state()


# ---------------------------------------------------------------------------
# Fix-check case 7 — reporter behaviors
# ---------------------------------------------------------------------------

class TestFirstSnapshotWrite:
    def test_first_snapshot_writes_exact_payload(self, reporter):
        """First-ever snapshot is written: one accessor call with the thing
        name, the dda-model-status shadow name, and {"reported": snapshot}
        (the accessor adds the {"state": ...} envelope itself)."""
        _clock, accessor = reporter
        mss.report(DEGRADED_SNAPSHOT)
        _join_write()
        assert accessor.calls == [
            (THING_NAME, "dda-model-status", {"reported": DEGRADED_SNAPSHOT})
        ]
        assert mss.MODEL_STATUS_SHADOW_NAME == "dda-model-status"

    def test_payload_matches_documented_shadow_document_shape(self, reporter):
        """The reported document carries exactly the documented top-level
        keys (design Decision 4) and per-model entry keys."""
        _clock, accessor = reporter
        mss.report(DEGRADED_SNAPSHOT)
        _join_write()
        (_thing, _shadow, payload), = accessor.calls
        assert set(payload.keys()) == {"reported"}
        document = payload["reported"]
        assert set(document.keys()) == {
            "models", "gpuDegraded", "gpuChainModels", "gpuActiveModels",
            "updatedAt",
        }
        assert set(document["models"]["yolo_test"].keys()) == {
            "status", "runtime", "gpuRequested", "gpuActive",
        }
        assert document["gpuDegraded"] is True


class TestChangeGate:
    def test_identical_snapshot_not_rewritten(self, reporter):
        """An identical snapshot after the first write produces NO second
        call — even far outside the debounce window (change gate, not
        merely debounce)."""
        clock, accessor = reporter
        mss.report(DEGRADED_SNAPSHOT)
        _join_write()
        clock.advance(1000.0)  # well past any debounce window
        # Equal-by-value copy: the gate compares canonical JSON, not identity.
        import copy
        mss.report(copy.deepcopy(DEGRADED_SNAPSHOT))
        _join_write()
        assert len(accessor.calls) == 1


class TestDebounce:
    def test_changed_snapshot_within_window_not_written(self, reporter):
        """A CHANGED snapshot arriving within DEBOUNCE_SECONDS of the last
        write is dropped (at most one write per window)."""
        clock, accessor = reporter
        mss.report(DEGRADED_SNAPSHOT)
        _join_write()
        clock.advance(10.0)  # < 30 s
        mss.report(HEALTHY_SNAPSHOT)
        _join_write()
        assert len(accessor.calls) == 1

    def test_changed_snapshot_after_window_written(self, reporter):
        """The same changed snapshot IS written once the debounce window
        has elapsed."""
        clock, accessor = reporter
        mss.report(DEGRADED_SNAPSHOT)
        _join_write()
        clock.advance(10.0)
        mss.report(HEALTHY_SNAPSHOT)  # dropped (within window)
        _join_write()
        clock.advance(25.0)  # total 35 s > 30 s since the write
        mss.report(HEALTHY_SNAPSHOT)
        _join_write()
        assert len(accessor.calls) == 2
        assert accessor.calls[1] == (
            THING_NAME, "dda-model-status", {"reported": HEALTHY_SNAPSHOT}
        )


class TestFailureIsolation:
    def test_accessor_exception_swallowed_logged_and_retry_works(
            self, reporter, monkeypatch, caplog):
        """An accessor exception never escapes report(); it is logged at
        WARNING, the in-flight flag is cleared, and — because the failed
        write un-pins the last-written canonical — the SAME snapshot is
        retried and written after the debounce window."""
        clock, _recording = reporter
        failing = FakeShadowAccessor(raise_exc=RuntimeError("IPC down"))
        monkeypatch.setattr(mss, "_accessor_override", failing)
        with caplog.at_level(logging.WARNING,
                             logger="utils.model_status_shadow"):
            mss.report(DEGRADED_SNAPSHOT)  # must not raise
            _join_write()
        assert len(failing.calls) == 1  # the attempt happened and raised
        assert any(
            "dda-model-status" in rec.message and "failed" in rec.message
            for rec in caplog.records
        ), "the swallowed accessor exception must be logged at WARNING"
        assert mss._write_in_flight is False
        # Failed write un-pins the canonical: a post-debounce report of the
        # SAME snapshot retries (were the canonical still pinned, the change
        # gate would drop it forever).
        failing.raise_exc = None
        clock.advance(5.0)
        mss.report(DEGRADED_SNAPSHOT)  # within debounce → still dropped
        _join_write()
        assert len(failing.calls) == 1
        clock.advance(30.0)
        mss.report(DEGRADED_SNAPSHOT)
        _join_write()
        assert failing.calls[-1] == (
            THING_NAME, "dda-model-status", {"reported": DEGRADED_SNAPSHOT}
        )
        assert len(failing.calls) == 2


class TestSingleInFlightWrite:
    def test_report_during_flight_is_dropped_then_recovers(
            self, reporter, monkeypatch):
        """While a slow write is in flight, a further (changed, out-of-
        window) report is dropped — no overlapping writes. After the flight
        completes, the reporter accepts new changed snapshots again."""
        clock, _recording = reporter
        blocking = BlockingShadowAccessor()
        monkeypatch.setattr(mss, "_accessor_override", blocking)
        mss.report(DEGRADED_SNAPSHOT)
        assert blocking.entered.wait(timeout=5), "write never started"
        first_thread = mss._write_thread
        # Changed snapshot, far outside the debounce window — dropped purely
        # by the in-flight exclusion.
        clock.advance(100.0)
        mss.report(HEALTHY_SNAPSHOT)
        assert mss._write_thread is first_thread  # no second thread spawned
        blocking.release.set()
        _join_write()
        assert len(blocking.calls) == 1
        assert blocking.calls[0][2] == {"reported": DEGRADED_SNAPSHOT}
        assert mss._write_in_flight is False
        # Recovery: the flag cleared, a changed snapshot writes again.
        clock.advance(100.0)
        blocking.release.set()  # keep the accessor non-blocking now
        mss.report(HEALTHY_SNAPSHOT)
        _join_write()
        assert len(blocking.calls) == 2
        assert blocking.calls[1][2] == {"reported": HEALTHY_SNAPSHOT}


class TestMissingThingName:
    def test_no_thing_name_is_a_complete_noop(self, monkeypatch):
        """Without AWS_IOT_THING_NAME (host/dev context) report() does
        nothing: no thread, no accessor call, no state pinned."""
        mss._reset_state()
        monkeypatch.delenv("AWS_IOT_THING_NAME", raising=False)
        accessor = FakeShadowAccessor()
        monkeypatch.setattr(mss, "_accessor_override", accessor)
        try:
            mss.report(DEGRADED_SNAPSHOT)  # must not raise
            assert accessor.calls == []
            assert mss._write_thread is None
            assert mss._last_written_canonical is None
            assert mss._last_write_monotonic is None
            assert mss._write_in_flight is False
        finally:
            mss._reset_state()


# ---------------------------------------------------------------------------
# Unit legs — debounce arithmetic (design Unit Tests)
# ---------------------------------------------------------------------------

class TestDebounceArithmetic:
    def test_first_write_bypasses_window(self, reporter):
        """With no prior write (_last_write_monotonic is None) the clock
        value is irrelevant: the first snapshot always writes."""
        clock, accessor = reporter
        clock.now = 0.0  # arbitrary; nothing to be 'within 30 s' of
        mss.report(DEGRADED_SNAPSHOT)
        _join_write()
        assert len(accessor.calls) == 1

    def test_boundary_elapsed_equal_to_window_writes(self, reporter):
        """elapsed == DEBOUNCE_SECONDS admits the write (the gate is a
        strict '< DEBOUNCE_SECONDS')."""
        clock, accessor = reporter
        mss.report(DEGRADED_SNAPSHOT)
        _join_write()
        clock.advance(30.0)  # exactly the window
        mss.report(HEALTHY_SNAPSHOT)
        _join_write()
        assert len(accessor.calls) == 2

    def test_just_under_window_does_not_write(self, reporter):
        """elapsed just under DEBOUNCE_SECONDS is still inside the window."""
        clock, accessor = reporter
        mss.report(DEGRADED_SNAPSHOT)
        _join_write()
        clock.advance(29.999)
        mss.report(HEALTHY_SNAPSHOT)
        _join_write()
        assert len(accessor.calls) == 1
