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
"""The shared telemetry hooks (workflow-telemetry-dashboards design §5.2
``hooks.py``; workflow-debug-mode tasks.md 1.1).

Observers receive the records the ``notify_*`` functions carry; a raising
observer is contained and its failure logged at most once per minute; and
``RunRecord.from_execution`` computes the timing arithmetic and the capped
detection list.

Validates: dashboards R5.2, R2.2; debug R4.1, R7.3.
"""
import json
import logging
from types import SimpleNamespace

import pytest

from workflow_engine.telemetry import hooks


@pytest.fixture(autouse=True)
def clean_registries():
    hooks._clear_observers()
    yield
    hooks._clear_observers()


def _execution(**overrides):
    values = dict(
        id="exec-1", status="completed", error=None, failing_node_id=None,
        capture_id="wf-1-exec-1", output_dir="/tmp/out", log_path="/tmp/out/run.log",
        node_status_json=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


_REGISTRATION = SimpleNamespace(id="wf-1:3", workflow_id="wf-1", version="3")


class _Collector:
    def __init__(self, document):
        self.document = document

    def to_map(self):
        return dict(self.document)


def _record(**overrides):
    kwargs = dict(
        execution=_execution(), registration=_REGISTRATION, collector=None,
        tag_values={}, trigger_context={}, enqueued_at_s=1_700_000_000,
        accepted_ms=1_700_000_003_000, run_started_ms=1_700_000_003_250,
        finished_ms=1_700_000_003_900, readiness_wait_ms=0, failure_traceback=None,
    )
    kwargs.update(overrides)
    return hooks.RunRecord.from_execution(**kwargs)


# ---------------------------------------------------------------------------
# RunRecord.from_execution
# ---------------------------------------------------------------------------


class TestFromExecution:
    def test_identity_and_terminal_fields_come_from_the_row_and_registration(self):
        record = _record(
            execution=_execution(status="failed", error="boom", failing_node_id="n2"),
            failure_traceback="Traceback...",
        )
        assert record.registration_id == "wf-1:3"
        assert record.workflow_id == "wf-1"
        assert record.workflow_version == "3"
        assert record.execution_id == "exec-1"
        assert record.status == "failed"
        assert record.error == "boom"
        assert record.failing_node_id == "n2"
        assert record.traceback == "Traceback..."
        assert record.capture_id == "wf-1-exec-1"
        assert record.output_dir == "/tmp/out"
        assert record.log_path == "/tmp/out/run.log"

    def test_timing_arithmetic(self):
        record = _record()
        assert record.queued_ms == 3000
        assert record.setup_ms == 250
        assert record.finished_ms - record.run_started_ms == 650
        assert record.readiness_wait_ms == 0

    def test_run_started_none_gives_setup_none(self):
        record = _record(run_started_ms=None)
        assert record.run_started_ms is None
        assert record.setup_ms is None
        assert record.queued_ms == 3000

    def test_queued_ms_is_never_negative_and_zero_without_stamps(self):
        assert _record(enqueued_at_s=1_700_000_010).queued_ms == 0
        assert _record(enqueued_at_s=None).queued_ms == 0
        assert _record(accepted_ms=None).queued_ms == 0
        assert _record(accepted_ms=None).setup_ms is None

    def test_detections_are_capped_at_1000_and_counted_in_full(self):
        items = [{"id": str(i), "label": "person", "confidence": 0.5} for i in range(1200)]
        record = _record(tag_values={"detections": items})
        assert len(record.detections) == 1000
        assert record.detections[0]["id"] == "0"
        assert record.detections[-1]["id"] == "999"
        assert record.detection_count == 1200

    def test_no_detection_list_gives_none_and_zero(self):
        assert _record(tag_values={}).detections is None
        assert _record(tag_values={}).detection_count == 0
        assert _record(tag_values={"detections": "nope"}).detections is None
        assert _record(tag_values=None).detections is None
        assert _record(tag_values=None).tag_values is None

    def test_node_status_comes_from_the_collector(self):
        collector = _Collector({"n1": {"status": "success", "durationMs": 12}})
        record = _record(collector=collector)
        assert record.node_status == {"n1": {"status": "success", "durationMs": 12}}

    def test_node_status_falls_back_to_the_persisted_json(self):
        row = _execution(node_status_json=json.dumps({"n1": {"status": "failed"}}))
        record = _record(execution=row)
        assert record.node_status == {"n1": {"status": "failed"}}
        assert _record(execution=_execution(node_status_json="not json")).node_status is None
        assert _record().node_status is None

    def test_continuous_flag_follows_the_trigger_context(self):
        assert _record(trigger_context={"source": "continuous", "frameSeq": 1}).continuous
        assert not _record(trigger_context={"topic": "a/b"}).continuous
        assert not _record(trigger_context={}).continuous
        assert _record(trigger_context=None).trigger_context == {}

    def test_record_is_frozen(self):
        record = _record()
        with pytest.raises(Exception):
            record.status = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Observers
# ---------------------------------------------------------------------------


class TestObservers:
    def test_run_observers_receive_the_record(self):
        seen = []
        hooks.register_run_observer(seen.append)
        record = _record()
        hooks.notify_run_finished(record)
        assert seen == [record]

    def test_drop_continuous_and_model_observers_receive_their_arguments(self):
        drops, ticks, model = [], [], []
        hooks.register_drop_observer(lambda *a: drops.append(a))
        hooks.register_continuous_observer(lambda *a: ticks.append(a))
        hooks.register_model_observer(lambda *a: model.append(a))

        hooks.notify_trigger_dropped("wf-1:3", "trig", "queue_full")
        hooks.notify_continuous_tick("wf-1:3", "skippedBusy", 2)
        hooks.notify_model_readiness("wf-1:3", "exec-1", "m", "READY", 1500, True)
        hooks.notify_model_wait("wf-1:3", {"model": "m", "state": "LOADING"})
        hooks.notify_model_wait("wf-1:3", None)

        assert drops == [("wf-1:3", "trig", "queue_full")]
        assert ticks == [("wf-1:3", "skippedBusy", 2)]
        assert model == [
            ("readiness", "wf-1:3", "exec-1", "m", "READY", 1500, True),
            ("wait", "wf-1:3", {"model": "m", "state": "LOADING"}),
            ("wait", "wf-1:3", None),
        ]

    def test_registering_twice_notifies_once(self):
        seen = []
        hooks.register_run_observer(seen.append)
        hooks.register_run_observer(seen.append)
        hooks.notify_run_finished(_record())
        assert len(seen) == 1

    def test_raising_observer_is_contained_and_later_observers_still_run(self, caplog):
        seen = []

        def boom(record):
            raise RuntimeError("observer boom")

        hooks.register_run_observer(boom)
        hooks.register_run_observer(seen.append)
        with caplog.at_level(logging.WARNING, logger="workflow_engine.telemetry.hooks"):
            hooks.notify_run_finished(_record())  # must not raise
        assert len(seen) == 1
        assert any("observer boom" in (r.exc_text or "") or "raised" in r.getMessage()
                   for r in caplog.records)

    def test_observer_failures_are_logged_once_per_minute(self, caplog, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(hooks, "_monotonic", lambda: clock[0])

        def boom(*args):
            raise RuntimeError("boom")

        hooks.register_drop_observer(boom)
        with caplog.at_level(logging.WARNING, logger="workflow_engine.telemetry.hooks"):
            for _ in range(5):
                hooks.notify_trigger_dropped("wf-1:3", "t", "drop_policy")
            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert len(warnings) == 1
            clock[0] += 59.0
            hooks.notify_trigger_dropped("wf-1:3", "t", "drop_policy")
            assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
            clock[0] += 1.5
            hooks.notify_trigger_dropped("wf-1:3", "t", "drop_policy")
            assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2

    def test_rate_limit_is_per_observer(self, caplog, monkeypatch):
        monkeypatch.setattr(hooks, "_monotonic", lambda: 5.0)

        def boom_a(*args):
            raise RuntimeError("a")

        def boom_b(*args):
            raise ValueError("b")

        hooks.register_continuous_observer(boom_a)
        hooks.register_continuous_observer(boom_b)
        with caplog.at_level(logging.WARNING, logger="workflow_engine.telemetry.hooks"):
            hooks.notify_continuous_tick("wf-1:3", "k", 1)
            hooks.notify_continuous_tick("wf-1:3", "k", 1)
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2

    def test_notify_without_observers_is_a_no_op(self):
        hooks.notify_run_finished(_record())
        hooks.notify_trigger_dropped(None, None, "queue_full")
        hooks.notify_continuous_tick("r", "k", 1)
        hooks.notify_model_wait("r", None)

    def test_non_callable_observer_is_refused(self):
        with pytest.raises(TypeError):
            hooks.register_run_observer("nope")  # type: ignore[arg-type]

    def test_current_execution_defaults_to_none(self):
        assert hooks.CURRENT_EXECUTION.get() is None
        token = hooks.CURRENT_EXECUTION.set(("wf-1:3", "wf-1", "exec-1"))
        try:
            assert hooks.CURRENT_EXECUTION.get() == ("wf-1:3", "wf-1", "exec-1")
        finally:
            hooks.CURRENT_EXECUTION.reset(token)
        assert hooks.CURRENT_EXECUTION.get() is None

    def test_public_surface_is_the_documented_one(self):
        assert set(hooks.__all__) == {
            "RunRecord", "CURRENT_EXECUTION",
            "register_run_observer", "notify_run_finished",
            "register_drop_observer", "notify_trigger_dropped",
            "register_continuous_observer", "notify_continuous_tick",
            "register_model_observer", "notify_model_readiness", "notify_model_wait",
        }
