"""Scene_Analytics_Nodes in the cloud test sandbox (rtsp-rtmp-stream-cameras
task 12.1 — Requirements 13.5, 13.6, 13.9, 14.6, 15.2, 15.6, 18.1).

- The sandbox condition evaluator resolves dotted field paths the way the
  LocalServer evaluator does, so an event gate's
  ``association.ppe.violations > 0`` means the same in both places.
- ``execute_bindings`` runs counters and associations over the configured
  simulated detections (an empty Detection_List when none are
  configured), then event gates starting inactive, then filters and
  recorders, gating recorders on analytics error outcomes and closed
  gates without failing the run.
- ``parse_simulated_inference`` accepts the optional ``detections`` and
  ``frame`` fields and leaves the pre-feature shape unchanged without
  them.
- A document without a Scene_Analytics_Node runs exactly as before.
"""
import json
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_TEST_SANDBOX_DIR = os.path.dirname(_TESTS_DIR)
_WORKFLOW_CORE_PYTHON = os.path.join(
    os.path.dirname(_TEST_SANDBOX_DIR), "backend", "layers", "workflow_core", "python")
if _TEST_SANDBOX_DIR not in sys.path:
    sys.path.insert(0, _TEST_SANDBOX_DIR)
if _WORKFLOW_CORE_PYTHON not in sys.path:
    sys.path.append(_WORKFLOW_CORE_PYTHON)

from harness import harness  # noqa: E402
from harness.bindings import (  # noqa: E402
    evaluate_condition,
    execute_bindings,
    frame_metadata,
    topological_order,
)
from harness.results import ResultsStore  # noqa: E402

METADATA = {"is_anomalous": False, "confidence": 0.9}
FRAME = {"width": 1920, "height": 1080}


def person(identifier, x, confidence=0.9):
    return {"id": identifier, "label": "person", "confidence": confidence,
            "x_min": x, "y_min": 100, "x_max": x + 200, "y_max": 600}


def hardhat(identifier, x):
    return {"id": identifier, "label": "hardhat", "confidence": 0.8,
            "x_min": x + 50, "y_min": 90, "x_max": x + 150, "y_max": 160}


def ppe_bindings(activate_after=1):
    """association 'ppe' -> gate 'alarm' -> recorder 'mq', as compiled."""
    return [
        {"nodeId": "ppe", "binding": "object_association",
         "parameters": {"subject_class": "person", "required_classes": "hardhat",
                        "min_overlap": 0.5, "min_confidence": 0.0, "zone": ""},
         "upstreamNodeIds": ["det"], "downstreamNodeIds": ["alarm"]},
        {"nodeId": "alarm", "binding": "event_gate",
         "parameters": {"condition": "association.ppe.violations > 0",
                        "activate_after": activate_after, "clear_after": 3,
                        "emit": "on_activate", "repeat_interval_ms": 0},
         "upstreamNodeIds": ["ppe"], "downstreamNodeIds": ["mq"]},
        {"nodeId": "mq", "binding": "recording_mqtt_publish",
         "parameters": {"topic": "dda/alarms"},
         "upstreamNodeIds": ["alarm"], "downstreamNodeIds": []},
    ]


def store_for(bindings):
    return ResultsStore([binding["nodeId"] for binding in bindings])


# --------------------------------------------------------------------------
# Dotted field paths in the condition evaluator (device parity)
# --------------------------------------------------------------------------

class TestDottedConditions:
    NESTED = {"counter": {"people": {"total": 3, "counts": {"person": 3}}},
              "event": {"alarm": {"state": "active"}},
              "detections": [{"label": "person", "flag": "true"}]}

    @pytest.mark.parametrize("condition,expected", [
        ("counter.people.total > 1", True),
        ("counter.people.counts.person == 3", True),
        ('event.alarm.state == "active"', True),
        ("detections.0.label == 'person'", True),
        # Resolved values are coerced like flat tag values.
        ("detections.0.flag == true", True),
        ("counter.people.total > 1 && !(counter.people.total > 5)", True),
    ])
    def test_dotted_paths_resolve_against_nested_metadata(self, condition, expected):
        assert evaluate_condition(condition, self.NESTED) is expected

    @pytest.mark.parametrize("condition", [
        "counter.nobody.total > 0",
        "counter.people.total.more > 0",
        "detections.5.label == 'person'",
        "detections.x.label == 'person'",
    ])
    def test_an_unresolved_path_is_an_unknown_field(self, condition):
        with pytest.raises(ValueError, match="Unknown metadata field"):
            evaluate_condition(condition, self.NESTED)

    def test_a_flat_key_wins_over_traversal(self):
        assert evaluate_condition("a.b == 5", {"a.b": 5, "a": {"b": 1}}) is True

    def test_flat_conditions_are_unchanged(self):
        assert evaluate_condition("confidence >= 0.8", METADATA) is True
        assert evaluate_condition("is_anomalous", METADATA) is False


# --------------------------------------------------------------------------
# Execution through the harness entry point
# --------------------------------------------------------------------------

class TestExecuteBindings:
    def test_a_violation_passes_the_gate_and_triggers_the_recorder(self):
        bindings = ppe_bindings()
        store = store_for(bindings)
        execute_bindings(bindings, METADATA, store,
                         detections=[person("p1", 100), hardhat("h1", 100),
                                     person("p2", 900)],
                         frame_size=FRAME, now_ms=1000)

        (association,) = store.record("ppe")["outputs"]
        assert association["outcome"] == "ok"
        assert association["metadata"]["violations"] == 1
        assert association["metadata"]["violating_ids"] == ["p2"]
        (gate,) = store.record("alarm")["outputs"]
        assert gate["result"] is True and gate["passed"] is True
        assert gate["metadata"]["transition"] == "activated"
        assert gate["metadata"]["active_since"] == 1000

        (activity,) = store.record("mq")["stubActivity"]
        assert activity["triggered"] is True
        triggering = activity["triggeringMetadata"]
        assert triggering["association"]["ppe"]["violations"] == 1
        assert triggering["event"]["alarm"]["state"] == "active"
        assert triggering["frame"] == FRAME
        assert not store.has_failure()

    def test_a_gate_starts_inactive_on_every_test_run(self):
        """With activate_after 2, one violating test run never activates the
        gate, however many test runs came before (Requirement 15.6)."""
        for _test_run in range(3):
            bindings = ppe_bindings(activate_after=2)
            store = store_for(bindings)
            execute_bindings(bindings, METADATA, store,
                             detections=[person("p1", 100)], frame_size=FRAME)
            (gate,) = store.record("alarm")["outputs"]
            assert gate["metadata"]["consecutive_true"] == 1
            assert gate["passed"] is False
            assert store.record("mq")["stubActivity"][0]["triggered"] is False

    def test_without_configured_detections_the_list_is_empty(self):
        bindings = ppe_bindings()
        store = store_for(bindings)
        execute_bindings(bindings, METADATA, store)
        (association,) = store.record("ppe")["outputs"]
        # An empty Detection_List, not a missing one: no warning.
        assert association["outcome"] == "ok"
        assert association["metadata"]["subjects"] == 0
        assert store.record("mq")["stubActivity"][0]["triggered"] is False
        assert "frame" not in store.record("mq")["stubActivity"][0]["triggeringMetadata"]

    def test_a_zone_with_an_unknown_frame_gates_without_failing_the_run(self):
        bindings = [
            {"nodeId": "people", "binding": "detection_counter",
             "parameters": {"classes": "person", "min_confidence": 0.0,
                            "zone": "[[0, 0], [0.5, 0], [0.5, 1], [0, 1]]",
                            "zone_rule": "center"},
             "upstreamNodeIds": ["det"], "downstreamNodeIds": ["mq"]},
            {"nodeId": "mq", "binding": "recording_mqtt_publish",
             "parameters": {"topic": "dda/counts"},
             "upstreamNodeIds": ["people"], "downstreamNodeIds": []},
        ]
        store = store_for(bindings)
        execute_bindings(bindings, METADATA, store, detections=[person("p1", 100)])
        record = store.record("people")
        assert record["status"] == "completed"
        assert record["outputs"][0]["outcome"] == "error"
        assert "frame dimensions are unknown" in record["outputs"][0]["problems"][0]
        assert store.record("mq")["stubActivity"][0]["triggered"] is False
        assert not store.has_failure()

    def test_an_unevaluable_gate_condition_counts_as_false_and_is_recorded(self):
        bindings = ppe_bindings()
        bindings[1]["parameters"]["condition"] = "association.nobody.violations > 0"
        store = store_for(bindings)
        execute_bindings(bindings, METADATA, store, detections=[person("p1", 100)])
        (gate,) = store.record("alarm")["outputs"]
        assert gate["result"] is None and gate["passed"] is False
        assert gate["metadata"]["consecutive_false"] == 1
        assert "could not be evaluated" in gate["problems"][0]
        assert store.record("alarm")["status"] == "completed"
        assert not store.has_failure()

    def test_a_filter_can_reference_counter_metadata(self):
        bindings = [
            {"nodeId": "people", "binding": "detection_counter",
             "parameters": {"classes": "person", "min_confidence": 0.0,
                            "zone": "", "zone_rule": "center"},
             "upstreamNodeIds": ["det"], "downstreamNodeIds": ["busy"]},
            {"nodeId": "busy", "binding": "inference_filter",
             "parameters": {"condition": "counter.people.total >= 2"},
             "upstreamNodeIds": ["people"], "downstreamNodeIds": ["mq"]},
            {"nodeId": "mq", "binding": "recording_mqtt_publish",
             "parameters": {"topic": "dda/busy"},
             "upstreamNodeIds": ["busy"], "downstreamNodeIds": []},
        ]
        for detections, triggered in (([person("a", 0)], False),
                                      ([person("a", 0), person("b", 600)], True)):
            store = store_for(bindings)
            execute_bindings(bindings, METADATA, store, detections=detections)
            assert store.record("busy")["outputs"][0]["result"] is triggered
            assert store.record("mq")["stubActivity"][0]["triggered"] is triggered

    def test_a_document_without_analytics_runs_as_before(self):
        bindings = [
            {"nodeId": "mq", "binding": "recording_mqtt_publish",
             "parameters": {"topic": "t"},
             "upstreamNodeIds": ["inf"], "downstreamNodeIds": []},
        ]
        store = store_for(bindings)
        execute_bindings(bindings, dict(METADATA), store,
                         detections=[person("p1", 100)], frame_size=FRAME)
        (activity,) = store.record("mq")["stubActivity"]
        # No Detection_List, frame, or analytics section is added.
        assert activity["triggeringMetadata"] == METADATA


# --------------------------------------------------------------------------
# Ordering and the simulated test configuration
# --------------------------------------------------------------------------

class TestOrderingAndConfiguration:
    def test_topological_order_over_listed_upstream_nodes(self):
        bindings = [
            {"nodeId": "gate", "upstreamNodeIds": ["count"]},
            {"nodeId": "count", "upstreamNodeIds": ["det"]},
            {"nodeId": "other", "upstreamNodeIds": []},
        ]
        assert [b["nodeId"] for b in topological_order(bindings)] == \
            ["count", "gate", "other"]
        # Already ordered: unchanged. A cycle falls back to emission order.
        ordered = [{"nodeId": "a", "upstreamNodeIds": []},
                   {"nodeId": "b", "upstreamNodeIds": ["a"]}]
        assert topological_order(ordered) == ordered
        cycle = [{"nodeId": "x", "upstreamNodeIds": ["y"]},
                 {"nodeId": "y", "upstreamNodeIds": ["x"]}]
        assert [b["nodeId"] for b in topological_order(cycle)] == ["x", "y"]

    @pytest.mark.parametrize("value,expected", [
        ({"width": 1920, "height": 1080}, {"width": 1920, "height": 1080}),
        ([640, 480], {"width": 640, "height": 480}),
        ({"width": 0, "height": 1080}, None),
        ({"width": "1920", "height": 1080}, None),
        ({"width": True, "height": 1080}, None),
        ({"width": float("nan"), "height": 1080}, None),
        ({"width": float("inf"), "height": 1080}, None),
        ([1920], None),
        (None, None),
    ])
    def test_frame_metadata(self, value, expected):
        assert frame_metadata(value) == expected

    def test_the_pre_feature_configuration_shape_is_unchanged(self):
        assert harness.parse_simulated_inference(
            json.dumps({"is_anomalous": True, "confidence": 0.4})) == \
            {"is_anomalous": True, "confidence": 0.4}

    def test_detections_and_frame_are_read_when_supplied(self):
        raw = json.dumps({"is_anomalous": False, "confidence": 0.9,
                          "detections": [person("p1", 100), "junk", 3],
                          "frame": {"width": 1280, "height": 720}})
        parsed = harness.parse_simulated_inference(raw)
        assert parsed["detections"] == [person("p1", 100)]
        assert parsed["frame"] == {"width": 1280, "height": 720}

    def test_malformed_detections_and_frame_are_ignored(self):
        parsed = harness.parse_simulated_inference(json.dumps(
            {"detections": {"label": "person"}, "frame": {"width": -1}}))
        assert "detections" not in parsed and "frame" not in parsed

    def test_the_detection_list_is_capped(self):
        many = [person(f"p{i}", 0) for i in range(harness.MAX_SIMULATED_DETECTIONS + 5)]
        parsed = harness.parse_simulated_inference(json.dumps({"detections": many}))
        assert len(parsed["detections"]) == harness.MAX_SIMULATED_DETECTIONS
