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
"""The ``run`` record assembler (workflow-debug-mode design §4.2, §5.1
``recorder.py``; tasks.md 2.3) against the fixture artifact directories under
``fixtures/workflow_debug/``.

The ten cases of tasks.md 2.3: a completed detection run; a failed run with a
traceback; a Bedrock run and an LLM run with the named keys; a run with
missing files; a run whose ``.jsonl`` carries the detections and one whose
sidecar does; a run refused before RUNNING; a continuous run with
``frameSource``; a manual run with ``frameSource: null``; an unrecognised
Trigger_Context; an invalid confidence dropped and counted.

Validates: debug R4.1, R4.6, R12.3.
"""
import json
import time
from pathlib import Path

import pytest

from workflow_engine.telemetry.debug import recorder
from workflow_engine.telemetry.debug.recorder import assemble_run_record
from workflow_engine.telemetry.hooks import RunRecord

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "workflow_debug"


def _local_ms(date_text, ms):
    return int(time.mktime(time.strptime(date_text, "%Y-%m-%d %H:%M:%S")) * 1000) + ms


def make_record(**overrides):
    values = dict(
        registration_id="wf-1:3", workflow_id="wf-1", workflow_version="3", execution_id="exec-1",
        status="completed", error=None, failing_node_id=None, traceback=None,
        capture_id="wf-1-exec-1", output_dir=str(FIXTURES / "completed"),
        log_path=str(FIXTURES / "completed" / "run.log"),
        enqueued_at_s=1_792_075_062, accepted_ms=1_792_075_062_100, run_started_ms=1_792_075_062_400,
        finished_ms=1_792_075_063_100, queued_ms=100, setup_ms=300, readiness_wait_ms=0,
        trigger_context={"topic": "ppe/line1", "payload": '{"go": 1}', "qos": 1,
                         "timestamp": 1792075062.5},
        node_status={"n1": {"status": "success", "durationMs": 12},
                     "model_1": {"status": "success", "durationMs": 240, "detail": "2 detections"},
                     "pub": {"status": "success", "durationMs": 3,
                             "detail": "sent to topic 'ppe/out' (qos 1, greengrass): {}"}},
        tag_values=None, detections=None, detection_count=0, continuous=False,
    )
    values.update(overrides)
    return RunRecord(**values)


class TestCompletedDetectionRun:
    def test_timing_trigger_nodes_outputs_artifacts_and_logs(self):
        record = assemble_run_record(make_record(), None, [])

        assert record["type"] == "run"
        assert record["ts"] == 1_792_075_063_100
        assert record["executionId"] == "exec-1"
        assert record["status"] == "completed"
        assert record["enqueuedAtS"] == 1_792_075_062
        assert record["acceptedMs"] == 1_792_075_062_100
        assert record["startedMs"] == 1_792_075_062_400
        assert record["finishedMs"] == 1_792_075_063_100
        assert record["queuedMs"] == 100 and record["setupMs"] == 300
        assert record["readinessWaitMs"] == 0
        assert record["durationMs"] == 700

        assert record["trigger"] == {
            "source": "mqtt", "type": "mqtt", "topic": "ppe/line1", "payload": '{"go": 1}',
            "timestamp": 1_792_075_062_500, "receivedMs": 1_792_075_062_500,
        }
        assert [n["nodeId"] for n in record["nodes"]] == ["n1", "model_1", "pub"]
        assert record["nodes"][1] == {"nodeId": "model_1", "status": "success", "durationMs": 240,
                                      "detail": "2 detections"}
        assert record["bindings"] == [{
            "nodeId": "pub", "kind": "mqtt_publish", "result": "success",
            "detail": "sent to topic 'ppe/out' (qos 1, greengrass): {}",
        }]
        assert record["error"] is None and record["failingNodeId"] is None
        assert "traceback" not in record

        # tag_values None -> read from {captureId}.json
        outputs = record["outputs"]
        assert outputs["isAnomalous"] is False and outputs["confidence"] == 0.91
        assert outputs["llm"] == {} and outputs["bedrock"] == {}
        assert outputs["detections"] == [
            {"label": "person", "confidence": 0.93, "box": [10.0, 20.0, 110.0, 220.0], "id": "a1b2c3d4"},
            {"label": "vest", "confidence": 0.71, "box": [30.0, 60.0, 90.0, 160.0], "id": "e5f6a7b8"},
        ]
        assert record["frameSource"] == {"kind": "mqtt", "cameraId": "cfg-7", "width": 1920,
                                         "height": 1080, "frameSeq": 77, "frameTs": 1_792_075_062_400}
        assert record["artifacts"] == {"outputDir": str(FIXTURES / "completed"),
                                       "captureId": "wf-1-exec-1", "hasOverlay": True, "missing": []}
        assert record["image"] is None
        assert record["model"] == {"readiness": [], "wait": None}

        logs = record["logs"]
        assert [e["src"] for e in logs] == ["runlog", "runlog", "runlog"]
        assert logs[0]["ts"] == _local_ms("2026-10-17 10:15:02", 123)
        assert logs[1]["message"] == "node n1 resolved\nline two of the same message"
        assert "hunter2" not in json.dumps(record)  # redacted
        assert "rtsp://***@camera.local/main" in logs[2]["message"]
        assert record["truncated"] == []

    def test_hook_detections_and_tag_values_take_precedence_over_disk(self):
        record = assemble_run_record(make_record(
            tag_values={"is_anomalous": True, "confidence": 0.5,
                        "detections": [{"id": "x", "label": "p", "confidence": 0.9, "x_min": 1, "y_min": 2,
                                        "x_max": 3, "y_max": 4}]},
            detections=[{"id": "x", "label": "p", "confidence": 0.9, "x_min": 1, "y_min": 2, "x_max": 3,
                         "y_max": 4}],
            detection_count=1,
        ), None, [])
        assert record["outputs"]["detections"] == [{"label": "p", "confidence": 0.9,
                                                    "box": [1, 2, 3, 4], "id": "x"}]
        assert record["outputs"]["isAnomalous"] is True

    def test_tap_lines_are_merged_by_time_then_source_order(self):
        tap = [
            {"ts": _local_ms("2026-10-17 10:15:02", 300), "level": "DEBUG", "logger": "workflow_engine.x",
             "message": "between the first two runlog lines"},
            {"ts": _local_ms("2026-10-17 10:15:02", 456), "level": "INFO",
             "logger": "dda_triton.model_readiness", "message": "same ts as the second runlog line"},
            {"ts": _local_ms("2026-10-17 10:15:09", 0), "level": "DEBUG", "logger": "gstreamer",
             "message": "x" * 5000},
        ]
        record = assemble_run_record(make_record(), None, tap)
        logs = record["logs"]
        assert [e["src"] for e in logs] == ["runlog", "tap", "runlog", "tap", "runlog", "tap"]
        assert logs[1]["message"] == "between the first two runlog lines"
        assert logs[2]["src"] == "runlog" and logs[3]["src"] == "tap"  # runlog first at equal ts
        assert len(logs[-1]["message"]) == recorder.MESSAGE_CAP

    def test_tap_buffer_object_contributes_its_overflow_marker(self):
        class Buffer:
            def snapshot(self):
                return ([{"ts": 1, "level": "DEBUG", "logger": "l", "message": "m"}], 3, 900)

        record = assemble_run_record(make_record(), None, Buffer())
        assert {"field": "logs.tap", "originalBytes": 900, "count": 3} in record["truncated"]
        assert record["logs"][0] == {"ts": 1, "level": "DEBUG", "logger": "l", "message": "m", "src": "tap"}


class TestFailedRun:
    def test_traceback_error_and_failing_node(self):
        record = assemble_run_record(make_record(
            execution_id="exec-4", status="failed", error="Pipeline failed to change state to PLAYING",
            failing_node_id="n2",
            traceback="Traceback (most recent call last):\n  ...\nRuntimeError: Pipeline failed with key=abcd",
            capture_id="wf-1-exec-4", output_dir=str(FIXTURES / "failed"),
            log_path=str(FIXTURES / "failed" / "run.log"),
            node_status={"n1": {"status": "success"}, "n2": {"status": "failed", "detail": "state change"}},
        ), None, [])
        assert record["status"] == "failed"
        assert record["error"] == "Pipeline failed to change state to PLAYING"
        assert record["failingNodeId"] == "n2"
        assert record["traceback"].startswith("Traceback (most recent call last)")
        assert "key=***" in record["traceback"]
        assert record["outputs"]["detections"] is None
        assert set(record["artifacts"]["missing"]) == {"metadata", "detections"}
        assert record["artifacts"]["hasOverlay"] is False
        levels = [e["level"] for e in record["logs"]]
        assert levels == ["INFO", "ERROR", "ERROR"]


class TestVerdictKeys:
    def test_bedrock_run_uses_text_and_the_sibling_keys(self):
        record = assemble_run_record(make_record(
            tag_values={"bedrock": {"br1": {"text": "no helmet visible", "is_anomalous": True,
                                            "confidence": 0.8, "detection_id": "a1b2c3d4"},
                                    "br2": {"error": "ThrottlingException"}},
                        "is_anomalous": True, "confidence": 0.8},
            node_status={"br1": {"status": "success", "durationMs": 900}, "br2": {"status": "failed"}},
        ), None, [])
        assert record["outputs"]["bedrock"] == {
            "br1": {"text": "no helmet visible", "isAnomalous": True, "confidence": 0.8,
                    "detectionId": "a1b2c3d4"},
            "br2": {"text": None, "error": "ThrottlingException"},
        }
        assert record["outputs"]["isAnomalous"] is True and record["outputs"]["confidence"] == 0.8
        assert record["bindings"] == [
            {"nodeId": "br1", "kind": "bedrock_inference", "result": "success", "detail": None},
            {"nodeId": "br2", "kind": "bedrock_inference", "result": "failed",
             "detail": "ThrottlingException"},
        ]

    def test_llm_run_uses_generated_text_and_the_sibling_keys(self):
        record = assemble_run_record(make_record(
            tag_values={"llm": {"llm1": {"generated_text": "all workers wear vests",
                                         "is_anomalous": False, "confidence": 0.95},
                                "llm2": {"generated_text": None, "error": "timeout after 30 s"}},
                        "is_anomalous": False, "confidence": 0.95},
            node_status={"llm1": {"status": "success"}, "llm2": {"status": "failed",
                                                                 "detail": "timeout after 30 s"}},
        ), None, [])
        assert record["outputs"]["llm"] == {
            "llm1": {"text": "all workers wear vests", "isAnomalous": False, "confidence": 0.95},
            "llm2": {"text": None, "error": "timeout after 30 s"},
        }
        assert [b["kind"] for b in record["bindings"]] == ["llm_inference", "llm_inference"]
        assert record["bindings"][1]["result"] == "failed"


class TestArtifacts:
    def test_missing_files_leave_nulls_and_list_them(self, tmp_path):
        record = assemble_run_record(make_record(
            capture_id="wf-1-exec-9", output_dir=str(tmp_path / "nowhere"),
            log_path=str(tmp_path / "nowhere" / "run.log"), node_status=None,
        ), None, [])
        assert record["outputs"]["detections"] is None
        assert record["outputs"]["isAnomalous"] is None
        assert record["logs"] == []
        assert record["nodes"] == [] and record["bindings"] == []
        assert record["artifacts"]["hasOverlay"] is False
        assert record["artifacts"]["missing"] == ["metadata", "detections", "run.log"]

    def test_detections_from_the_jsonl_capture_record(self):
        record = assemble_run_record(make_record(
            execution_id="exec-2", capture_id="wf-1-exec-2", output_dir=str(FIXTURES / "jsonl"),
            log_path=None, tag_values={},
        ), None, [])
        assert record["outputs"]["detections"] == [
            {"label": "helmet", "confidence": 0.88, "box": [1, 2, 3, 4]},
            {"label": "person", "confidence": 0.5, "box": [9, 10, 11, 12]},
        ]
        # the 1.7 confidence item is dropped and counted, not the whole field
        markers = [m for m in record["truncated"] if m["field"] == "outputs.detections.invalid"]
        assert len(markers) == 1 and markers[0]["count"] == 1 and markers[0]["originalBytes"] > 0
        assert "detections" not in record["artifacts"]["missing"]

    def test_detections_from_the_sidecar(self):
        record = assemble_run_record(make_record(
            execution_id="exec-3", capture_id="wf-1-exec-3", output_dir=str(FIXTURES / "sidecar"),
            log_path=None, tag_values={},
        ), None, [])
        assert record["outputs"]["detections"] == [{"label": "cone", "confidence": 0.42, "box": [0, 0, 5, 5]}]
        assert record["truncated"] == []


class TestRefusedAndTriggerShapes:
    def test_refused_before_running_has_null_start_duration_and_setup(self):
        record = assemble_run_record(make_record(
            status="failed", error="Workflow registration 'wf-1:3' is 'invalid' and cannot be run",
            run_started_ms=None, setup_ms=None, capture_id=None, output_dir=None, log_path=None,
            node_status=None, tag_values=None, trigger_context={},
        ), None, [])
        assert record["startedMs"] is None
        assert record["durationMs"] is None
        assert record["setupMs"] is None
        assert record["status"] == "failed"
        assert record["trigger"]["type"] == "manual"
        assert record["artifacts"] == {"outputDir": None, "captureId": None, "hasOverlay": False,
                                       "missing": []}
        assert record["logs"] == []

    def test_continuous_run_frame_source_from_context_and_frame(self):
        context = {"source": "continuous", "frameSeq": 412, "frameAcquiredAtMs": 1_792_075_062_380,
                   "tickAtMs": 1_792_075_062_390}
        record = assemble_run_record(make_record(
            trigger_context=context, continuous=True,
            tag_values={"frame": {"width": 1280, "height": 720},
                        "stream": {"cam": {"seq": 412, "acquiredAtMs": 1_792_075_062_380, "width": 1280,
                                           "height": 720, "cameraSourceId": "cfg-9"}}},
        ), None, [])
        assert record["trigger"] == {"source": "continuous", "type": "continuous", "topic": None,
                                     "payload": None, "timestamp": 1_792_075_062_380,
                                     "receivedMs": 1_792_075_062_390}
        assert record["frameSource"] == {"kind": "continuous", "cameraId": "cfg-9", "width": 1280,
                                         "height": 720, "frameSeq": 412, "frameTs": 1_792_075_062_380}

    def test_continuous_without_stream_metadata_still_has_a_frame_source(self):
        context = {"source": "continuous", "frameSeq": 1, "frameAcquiredAtMs": 5, "tickAtMs": 6}
        record = assemble_run_record(make_record(trigger_context=context, tag_values={"frame": {"width": 8,
                                                                                          "height": 4}}),
                                     None, [])
        assert record["frameSource"] == {"kind": "continuous", "cameraId": None, "width": 8, "height": 4,
                                         "frameSeq": 1, "frameTs": 5}

    def test_manual_run_has_null_frame_source(self):
        record = assemble_run_record(make_record(trigger_context={}, tag_values={"is_anomalous": False}),
                                     None, [])
        assert record["trigger"]["type"] == "manual"
        assert record["trigger"]["source"] is None
        assert record["frameSource"] is None

    def test_opcua_shape(self):
        context = {"endpoint": "opc.tcp://plc.local:4840", "node_id": "ns=2;s=Start", "value": True,
                   "source_timestamp": "2026-10-17T10:15:02.500000+00:00"}
        record = assemble_run_record(make_record(trigger_context=context), None, [])
        assert record["trigger"] == {"source": "opcua", "type": "opcua",
                                     "topic": "opc.tcp://plc.local:4840 ns=2;s=Start", "payload": "True",
                                     "timestamp": 1_792_232_102_500, "receivedMs": None}

    def test_unknown_shape_serializes_the_whole_context(self):
        context = {"source": "gpio", "pin": 17, "edge": "rising", "nested": {"b": 1, "a": [2]}}
        record = assemble_run_record(make_record(trigger_context=context), None, [])
        trigger = record["trigger"]
        assert trigger["type"] == "unknown"
        assert trigger["source"] == "gpio"
        assert trigger["topic"] is None and trigger["timestamp"] is None and trigger["receivedMs"] is None
        assert json.loads(trigger["payload"]) == context
        assert trigger["payload"] == json.dumps(context, sort_keys=True, default=str)
        record = assemble_run_record(make_record(trigger_context={"pin": 1}), None, [])
        assert record["trigger"]["type"] == "unknown" and record["trigger"]["source"] is None

    def test_shape_order_is_topic_then_endpoint_then_continuous(self):
        both = {"topic": "t", "endpoint": "e", "source": "continuous"}
        assert assemble_run_record(make_record(trigger_context=both), None, [])["trigger"]["type"] == "mqtt"
        del both["topic"]
        assert assemble_run_record(make_record(trigger_context=both), None, [])["trigger"]["type"] == "opcua"


class TestCapsAndContainment:
    def test_invalid_confidence_items_are_dropped_and_counted(self):
        items = [{"label": "a", "confidence": 0.5, "x_min": 0, "y_min": 0, "x_max": 1, "y_max": 1},
                 {"label": "b", "confidence": 1.5, "x_min": 0, "y_min": 0, "x_max": 1, "y_max": 1},
                 {"label": "c", "confidence": "high", "x_min": 0, "y_min": 0, "x_max": 1, "y_max": 1},
                 {"label": "d", "confidence": -0.1, "x_min": 0, "y_min": 0, "x_max": 1, "y_max": 1},
                 "garbage"]
        record = assemble_run_record(make_record(detections=items, detection_count=5, tag_values={}), None, [])
        assert [d["label"] for d in record["outputs"]["detections"]] == ["a"]
        markers = [m for m in record["truncated"] if m["field"] == "outputs.detections.invalid"]
        assert markers[0]["count"] == 4

    def test_nodes_are_capped_at_64(self):
        status = {"n%03d" % i: {"status": "success", "durationMs": i} for i in range(70)}
        record = assemble_run_record(make_record(node_status=status, tag_values={}), None, [])
        assert len(record["nodes"]) == 64
        markers = [m for m in record["truncated"] if m["field"] == "nodes"]
        assert markers[0]["count"] == 6

    def test_model_readiness_and_wait_are_copied(self):
        readiness = [{"model": "plates", "state": "READY", "waitedMs": 1200, "ready": True}]
        wait = {"model": "plates", "tritonModel": "model-plates", "state": "LOADING", "reason": None,
                "sinceMs": 1, "stalled": False}
        record = assemble_run_record(make_record(tag_values={}), None, [], readiness=readiness, model_wait=wait)
        assert record["model"] == {"readiness": readiness, "wait": wait}

    def test_secrets_are_redacted_everywhere(self):
        record = assemble_run_record(make_record(
            tag_values={"llm": {"l": {"generated_text": "token is s3cretvalue"}}},
            error="failed with s3cretvalue", trigger_context={"topic": "t", "payload": "pwd=s3cretvalue"},
        ), None, [], secrets=frozenset({"s3cretvalue"}))
        text = json.dumps(record)
        assert "s3cretvalue" not in text

    def test_assembly_failure_yields_a_minimal_record(self, monkeypatch):
        monkeypatch.setattr(recorder, "_trigger", lambda context: 1 / 0)
        record = assemble_run_record(make_record(), None, [])
        assert record["type"] == "run" and record["executionId"] == "exec-1"
        assert record["error"] == "[record assembly failed]"
        assert record["truncated"] == [{"field": "record", "originalBytes": 0}]

    def test_output_is_json_serializable(self):
        record = assemble_run_record(make_record(), None, [])
        json.dumps(record, separators=(",", ":"))
