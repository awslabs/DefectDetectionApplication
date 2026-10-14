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
"""Device scene analytics units (rtsp-rtmp-stream-cameras Requirements
13.2-13.6, 14.2-14.6, 15.2-15.5, 18.1).

- LLM prompts can reference counter keys (the counters run before the
  LLM processor);
- a zone with an unknown frame size gates its downstream nodes without
  failing the run;
- workflows without analytics nodes produce identical metadata and
  outputs;
- event gates keep their state per registration across runs, in
  topological order, and gate their outputs;
- node statuses carry each analytics outcome.
"""
import json
import time
from unittest.mock import patch

import pytest

from test_workflow_stream_executor import FakePipelineManager, seed_run
from workflow_engine_test_utils import DEVICE_ARCH, make_session_factory, write_artifact_set

from workflow_engine import gst_plugins, pipeline_executor
from workflow_engine.models import WorkflowExecution
from workflow_engine.node_status import NodeStatusCollector
from workflow_engine.output_bindings import LlmInferenceProcessor, OutputBindingProcessor
from workflow_engine.pipeline_executor import WorkflowExecutor
from workflow_engine.scene_analytics import (
    OUTCOMES_KEY,
    EventGateStateStore,
    apply_scene_analytics,
    evaluate_event_gates,
    has_scene_analytics,
    needs_frame_size,
    registration_key,
    topological_order,
)
from workflow_engine.output_bindings import evaluate_condition

DETECTIONS = [
    {"id": "d1", "label": "person", "confidence": 0.9, "x_min": 10, "y_min": 10, "x_max": 110, "y_max": 310},
    {"id": "d2", "label": "person", "confidence": 0.8, "x_min": 700, "y_min": 10, "x_max": 800, "y_max": 310},
    {"id": "d3", "label": "Hard Hat", "confidence": 0.7, "x_min": 30, "y_min": 10, "x_max": 90, "y_max": 50},
]


def counter(node_id="count_1", upstream=("det",), **parameters):
    values = {"classes": "person, hard hat", "min_confidence": 0.0, "zone": "", "zone_rule": "center"}
    values.update(parameters)
    return {"nodeId": node_id, "binding": "detection_counter", "parameters": values,
            "upstreamNodeIds": list(upstream), "downstreamNodeIds": []}


def association(node_id="ppe", upstream=("det",), **parameters):
    values = {"subject_class": "person", "required_classes": "hard hat", "min_overlap": 0.5,
              "min_confidence": 0.0, "zone": ""}
    values.update(parameters)
    return {"nodeId": node_id, "binding": "object_association", "parameters": values,
            "upstreamNodeIds": list(upstream), "downstreamNodeIds": []}


def gate(node_id, condition, upstream, **parameters):
    values = {"condition": condition, "activate_after": 2, "clear_after": 2, "emit": "on_activate",
              "repeat_interval_ms": 0}
    values.update(parameters)
    return {"nodeId": node_id, "binding": "event_gate", "parameters": values,
            "upstreamNodeIds": list(upstream), "downstreamNodeIds": []}


def output(node_id, upstream):
    return {"nodeId": node_id, "binding": "mqtt_publish",
            "parameters": {"topic": "line/{0}".format(node_id), "greengrass": True,
                           "payload_template": "{inference_json}"},
            "upstreamNodeIds": list(upstream), "downstreamNodeIds": []}


def document(*bindings, version="3"):
    return {"schemaVersion": 1, "workflowId": "wf-1", "workflowVersion": version, "targetArch": DEVICE_ARCH,
            "segments": [{"name": "s0", "elements": [
                {"nodeId": "det", "factory": "videotestsrc", "args": {"num-buffers": 1}},
                {"nodeId": None, "factory": "fakesink", "args": {}}]}],
            "executorBindings": list(bindings), "pluginDependencies": []}


class Published:
    def __init__(self):
        self.messages = []

    def __call__(self, topic, payload, qos, **kwargs):
        self.messages.append((topic, payload))

    @property
    def topics(self):
        return [topic for topic, _payload in self.messages]


class TestApply:
    def test_counters_and_associations_merge_their_metadata_and_record_status(self):
        doc = document(counter(), association())
        collector = NodeStatusCollector({}, extra_node_ids=["count_1", "ppe"])
        tags = {"detections": [dict(entry) for entry in DETECTIONS]}

        outcomes = apply_scene_analytics(doc, tags, collector=collector)

        assert outcomes == {"count_1": "ok", "ppe": "ok"}
        assert doc[OUTCOMES_KEY] == outcomes
        assert tags["counter"]["count_1"] == {"counts": {"person": 2, "hard_hat": 1}, "total": 3,
                                              "labels": {"person": "person", "hard_hat": "Hard Hat"}}
        assert tags["association"]["ppe"] == {"subjects": 2, "compliant": 1, "violations": 1,
                                              "missing": {"hard_hat": 1}, "violating_ids": ["d2"]}
        status = collector.to_map()
        assert status["count_1"]["detail"] == "person 2, hard_hat 1"
        assert status["ppe"]["detail"] == "1 of 2 compliant"

    def test_no_detection_list_reports_zeros_with_a_warning(self):
        doc = document(counter())
        collector = NodeStatusCollector({}, extra_node_ids=["count_1"])
        tags = {}

        assert apply_scene_analytics(doc, tags, collector=collector) == {"count_1": "warning"}
        assert tags["counter"]["count_1"]["total"] == 0
        assert tags["counter"]["count_1"]["counts"] == {"person": 0, "hard_hat": 0}
        entry = collector.to_map()["count_1"]
        assert entry["status"] == "warning" and "no detection list" in entry["detail"]

    def test_a_zone_without_a_frame_size_is_an_error_outcome(self):
        doc = document(counter(zone="[[0,0],[0.5,0],[0.5,1],[0,1]]"))
        collector = NodeStatusCollector({}, extra_node_ids=["count_1"])

        assert apply_scene_analytics(doc, {"detections": DETECTIONS}, collector=collector) == {"count_1": "error"}
        entry = collector.to_map()["count_1"]
        assert entry["status"] == "failure" and "frame dimensions are unknown" in entry["detail"]

    def test_a_zone_scales_to_the_frame_size(self):
        doc = document(counter(zone="[[0,0],[0.5,0],[0.5,1],[0,1]]"))
        tags = {"detections": DETECTIONS}

        apply_scene_analytics(doc, tags, frame_size=(1280, 720))

        # The second person (center x 750) is outside the left half.
        assert tags["counter"]["count_1"]["counts"]["person"] == 1

    def test_documents_without_analytics_are_untouched(self):
        doc = document(output("out", ["det"]))
        tags = {"is_anomalous": True, "detections": DETECTIONS}
        before = json.dumps(tags, sort_keys=True)

        assert apply_scene_analytics(doc, tags) == {}
        assert json.dumps(tags, sort_keys=True) == before
        assert OUTCOMES_KEY not in doc
        assert not has_scene_analytics(doc) and not needs_frame_size(doc)

    def test_only_a_zone_needs_the_frame_size(self):
        assert not needs_frame_size(document(counter(), association(), gate("g", "x", ["count_1"])))
        assert needs_frame_size(document(association(zone="[[0,0],[1,0],[1,1]]")))


class TestEventGates:
    def test_a_gate_steps_across_runs_per_registration(self):
        store = EventGateStateStore()
        bindings = [gate("g", "counter.c.total >= 1", ["c"])]
        results = []
        for version, total in (("3", 1), ("3", 1), ("3", 1), ("4", 1)):
            doc = document(*bindings, version=version)
            tags = {"counter": {"c": {"total": total}}}
            results.append(evaluate_event_gates(doc, bindings, dict(tags), tags, evaluate=evaluate_condition,
                                                store=store, now_ms=1000)["g"])
        # Activates on the second consecutive true run; version 4 is a new
        # registration and starts inactive.
        assert results == [False, True, False, False]
        assert registration_key(document(version="4")) == "wf-1:4"
        store.forget("wf-1:3")
        assert len(store) == 1

    def test_gates_run_in_topological_order_whatever_the_emission_order(self):
        """A gate reading another gate sees it, even when listed first."""
        store = EventGateStateStore()
        second = gate("second", "event.first.state == \"active\"", ["first"], activate_after=1)
        first = gate("first", "counter.c.total >= 1", ["c"], activate_after=1)
        doc = document(second, first)
        tags = {"counter": {"c": {"total": 3}}}

        passed = evaluate_event_gates(doc, doc["executorBindings"], dict(tags), tags,
                                      evaluate=evaluate_condition, store=store, now_ms=5)

        assert passed == {"first": True, "second": True}
        assert list(tags["event"]) == ["first", "second"]
        assert [binding["nodeId"] for binding in topological_order(doc["executorBindings"])] == ["first", "second"]

    def test_an_unevaluable_condition_counts_as_false_and_is_recorded(self):
        store = EventGateStateStore()
        details = {}
        bindings = [gate("g", "nowhere.total > 1", ["c"], activate_after=1)]
        tags = {}

        passed = evaluate_event_gates(document(*bindings), bindings, {}, tags, evaluate=evaluate_condition,
                                      store=store, now_ms=1, detail_sink=details.__setitem__)

        assert passed == {"g": False}
        assert tags["event"]["g"]["consecutive_false"] == 1
        assert "could not be evaluated and counts as false" in details["g"]

    def test_the_processor_gates_outputs_and_persists_the_event_metadata(self):
        published = Published()
        store = EventGateStateStore()
        doc = document(counter("c"), gate("g", "counter.c.total >= 2", ["c"]), output("alarm", ["g"]),
                       output("always", ["c"]))
        processor = OutputBindingProcessor(greengrass_publisher=published, event_gate_store=store,
                                           clock_ms=lambda: 42)
        sent = []
        for _run in range(3):
            tags = {"detections": [dict(entry) for entry in DETECTIONS]}
            apply_scene_analytics(doc, tags)
            processor(None, doc, tags)
            sent.append(sorted(published.topics))
            published.messages.clear()
        # on_activate: the alarm fires once, on the second run; the output
        # downstream of the counter fires on every run.
        assert sent == [["line/always"], ["line/alarm", "line/always"], ["line/always"]]
        assert tags["event"]["g"]["state"] == "active"

    def test_an_error_outcome_gates_only_its_direct_downstream(self):
        published = Published()
        doc = document(counter("zoned", zone="[[0,0],[1,0],[1,1]]"), counter("plain"),
                       output("from_zoned", ["zoned"]), output("from_plain", ["plain"]))
        tags = {"detections": DETECTIONS}
        apply_scene_analytics(doc, tags)
        details = {}

        OutputBindingProcessor(greengrass_publisher=published, event_gate_store=EventGateStateStore())(
            None, doc, tags, detail_sink=details.__setitem__)

        assert published.topics == ["line/from_plain"]
        assert details["from_zoned"].startswith("not sent: gated out")

    def test_filters_can_reference_the_event_metadata(self):
        published = Published()
        doc = document(counter("c"), gate("g", "counter.c.total >= 1", ["c"], activate_after=1),
                       {"nodeId": "f", "binding": "inference_filter",
                        "parameters": {"condition": "event.g.transition == \"activated\""},
                        "upstreamNodeIds": ["g"], "downstreamNodeIds": ["out"]},
                       output("out", ["f"]))
        tags = {"detections": DETECTIONS}
        apply_scene_analytics(doc, tags)

        OutputBindingProcessor(greengrass_publisher=published, event_gate_store=EventGateStateStore())(
            None, doc, tags)

        assert published.topics == ["line/out"]


class TestWithoutAnalytics:
    def test_metadata_and_outputs_are_identical(self):
        """18.1: no gate, no section, no state; the payload is the plain
        metadata."""
        published = Published()
        store = EventGateStateStore()
        doc = document(output("out", ["det"]))
        tags = {"is_anomalous": False, "confidence": 0.93, "detections": DETECTIONS}
        before = json.loads(json.dumps(tags))

        OutputBindingProcessor(greengrass_publisher=published, event_gate_store=store)(None, doc, tags)

        assert tags == before
        assert len(store) == 0
        assert published.messages == [("line/out", json.dumps(before, sort_keys=True, default=str))]


# --- through the executor -------------------------------------------------------


@pytest.fixture(autouse=True)
def no_registry_scan():
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


@pytest.fixture
def session_factory():
    return make_session_factory()


def inject_detections(tag_values, *args, **kwargs):
    tag_values.setdefault("detections", [dict(entry) for entry in DETECTIONS])
    tag_values.setdefault("detection_count", len(DETECTIONS))
    return tag_values["detections"]


def run_executor(session_factory, tmp_path, doc, post_run_handler=None, llm_processor=None,
                 source_dimensions=None):
    artifact_path = write_artifact_set(tmp_path / "workflows", compiled=doc)
    execution_id = seed_run(session_factory, artifact_path)
    observed = []
    handler = post_run_handler or (lambda registration, document, tags: observed.append(tags))
    executor = WorkflowExecutor(session_factory=session_factory,
                                pipeline_manager_factory=lambda: FakePipelineManager(),
                                post_run_handler=handler, llm_processor=llm_processor)
    with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", str(tmp_path / "captures")), \
            patch.object(pipeline_executor.detections, "merge_detections", side_effect=inject_detections), \
            patch.object(pipeline_executor, "_capture_record_source_dimensions",
                         return_value=source_dimensions):
        executor.execute(execution_id)
    session = session_factory()
    row = session.get(WorkflowExecution, execution_id)
    session.expunge(row)
    session.close()
    return row, observed


class TestThroughTheExecutor:
    def test_llm_prompts_can_reference_counter_keys(self, session_factory, tmp_path):
        prompts = []

        def invoker(model_name, prompt, parameters, *args, **kwargs):
            prompts.append(prompt)
            return "ok"

        doc = document(counter(), {"nodeId": "llm1", "binding": "llm_inference",
                                   "parameters": {"modelName": "qwen", "prompt_template":
                                                  "Count {counter.count_1.total}, people "
                                                  "{counter.count_1.counts.person}"},
                                   "upstreamNodeIds": ["count_1"], "downstreamNodeIds": []})

        row, observed = run_executor(session_factory, tmp_path, doc, llm_processor=LlmInferenceProcessor(invoker))

        assert row.status == "completed"
        assert prompts == ["Count 3, people 2"]
        assert observed[0]["counter"]["count_1"]["total"] == 3

    def test_a_zone_with_an_unknown_frame_size_gates_without_failing_the_run(self, session_factory, tmp_path):
        published = Published()
        doc = document(counter(zone="[[0,0],[0.5,0],[0.5,1],[0,1]]"), output("out", ["count_1"]))

        row, _observed = run_executor(
            session_factory, tmp_path, doc,
            post_run_handler=OutputBindingProcessor(greengrass_publisher=published,
                                                    event_gate_store=EventGateStateStore()))

        assert row.status == "completed" and row.failing_node_id is None
        assert published.messages == []
        status = json.loads(row.node_status_json)
        assert status["count_1"]["status"] == "failure"
        assert "frame dimensions are unknown" in status["count_1"]["detail"]
        assert status["out"]["detail"].startswith("not sent: gated out")

    def test_the_capture_record_size_scales_the_zone(self, session_factory, tmp_path):
        doc = document(counter(zone="[[0,0],[0.5,0],[0.5,1],[0,1]]"))

        row, observed = run_executor(session_factory, tmp_path, doc, source_dimensions=(1280, 720))

        assert row.status == "completed"
        assert observed[0]["counter"]["count_1"]["counts"]["person"] == 1

    def test_gate_transitions_reach_the_persisted_run_metadata(self, session_factory, tmp_path):
        store = EventGateStateStore()
        doc = document(counter(), gate("g", "counter.count_1.total >= 1", ["count_1"], activate_after=1))

        row, _observed = run_executor(
            session_factory, tmp_path, doc,
            post_run_handler=OutputBindingProcessor(event_gate_store=store))

        with open("{0}/{1}.json".format(row.output_dir, row.capture_id), "r", encoding="utf-8") as handle:
            persisted = json.load(handle)
        assert persisted["event"]["g"]["transition"] == "activated"
        assert persisted["counter"]["count_1"]["total"] == 3
        assert "_sceneAnalyticsOutcomes" not in json.dumps(persisted)
