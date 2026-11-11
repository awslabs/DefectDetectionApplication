# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""``OutputBindingProcessor.process_subset`` dependency order and result
visibility (bedrock-inspection-chain, design 6.1), plus the offline chain
harness of design 6.2: ``trigger -> camera -> check (inspection, on_error
record) -> upload_raw / upload_annotated -> publish_t2 (json)`` with fakes
(JPEG fixture, scripted invoker, recording uploader, recording Greengrass
publisher, a local presign client). Neutral fixture names only.
"""
import json
import logging
import os
import re
from unittest.mock import patch

import cv2
import numpy as np
import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine import output_bindings
from workflow_engine.node_status import STATUS_FAILURE, NodeStatusCollector
from workflow_engine.output_bindings import (
    BedrockInferenceProcessor,
    OutputBindingProcessor,
    RunContext,
    merge_binding_outputs,
    order_output_bindings,
)

FRAME_NAME = "bedrock_frame_camera.jpg"
CAPTURE_ID = "cap-1"
NOVA = "us.amazon.nova-pro-v1:0"
ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
PAYLOAD = {
    "order_id": "o-1",
    "body": {
        "qc_stage": "qc2",
        "image_key": "images/o-1/u.jpg",
        "order": {"items": [
            {"shape": "roof", "color": "red"},
            {"shape": "fender", "color": "blue"},
            {"shape": "fender", "color": "blue"},
        ]},
    },
}
TRIGGER = {
    "topic": "plant/line1/evaluate",
    "payload": json.dumps(PAYLOAD),
    "payload_json": PAYLOAD,
    "qos": 1,
    "timestamp": 1700000000.0,
}
#: Design 5.2's demo template, verbatim (the topic/stage are node parameters).
T2_TEMPLATE = (
    '{"msg_id": "{ulid}", "ts": "{now_iso}", "source": "edge.inspection", '
    '"schema_version": "0.1-draft", "order_id": "{trigger.payload_json.order_id}", '
    '"body": {"qc_stage": "{trigger.payload_json.body.qc_stage}", "decided_by": "dda", '
    '"verdict": "{bedrock.check.verdict}", "confidence": "{bedrock.check.confidence}", '
    '"reason": "{bedrock.check.reason}", "error": "{bedrock.check.error}", '
    '"image_s3_key": "{s3_upload.upload_raw.key}", '
    '"annotated_image_s3_key": "{s3_upload.upload_annotated.key}", '
    '"annotated_image_url": "{s3_upload.upload_annotated.presigned_url}", '
    '"latency_ms": "{latency_ms}"}}'
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class RecordingUploader:
    def __init__(self, error_for=None):
        self.calls = []
        self.error_for = error_for or {}

    def __call__(self, destination, body, content_type, timeout_s, retries,
                 region, **kwargs):
        self.calls.append((destination, body, kwargs))
        error = self.error_for.get(destination)
        if error is not None:
            raise error

    def destinations(self):
        return [call[0] for call in self.calls]


class RecordingPublisher:
    def __init__(self):
        self.log = []

    def __call__(self, topic, payload, qos, **kwargs):
        self.log.append((topic, payload, qos, kwargs))

    def payloads(self):
        return [json.loads(payload) for _, payload, _, _ in self.log]


class ScriptedInvoker:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, *args, **options):
        self.calls.append((args, options))
        item = self.script.pop(0) if self.script else good_answer()
        if isinstance(item, BaseException):
            raise item
        return item


def assert_signed_get_url(url, key):
    """A presigned GET for ``key`` on the neutral test endpoint (botocore
    signs a custom endpoint path-style): host suffix, key and signature."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc.endswith("s3.example.invalid")
    assert parsed.path.endswith("/" + key)
    assert "X-Amz-Signature=" in parsed.query


def presign_client():
    """A real boto3 client with fake credentials and a neutral endpoint: the
    presign is a local computation, no request is ever made."""
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3", region_name="us-east-1", endpoint_url="https://s3.example.invalid",
        aws_access_key_id="testing", aws_secret_access_key="testing",
        config=Config(signature_version="s3v4"))


def throttled():
    from botocore.exceptions import ClientError
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "Converse")


def write_frame(directory, width=64, height=48, name=FRAME_NAME):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    path = os.path.join(str(directory), name)
    assert cv2.imwrite(path, frame)
    return path


def obj(name="roof", qc="OK", box=(100, 200, 500, 900)):
    x0, y0, x1, y1 = box
    return {"name": name, "qc": qc, "reason": "r",
            "bounding_box": {"x_min": x0, "y_min": y0, "x_max": x1, "y_max": y1}}


def good_answer(verdict="pass", count=3, confidence=0.9):
    qc = "OK" if verdict == "pass" else "NOK"
    return json.dumps({
        "verdict": verdict, "confidence": confidence, "reason": "checked",
        "objects": [obj("part-{0}".format(i), qc) for i in range(count)],
    })


# ---------------------------------------------------------------------------
# Compiled-document builders (what the compiler emits for the six-node graph)
# ---------------------------------------------------------------------------

def check_binding(**parameters):
    params = {"prompt": "Inspect.", "answer_format": "inspection", "model": NOVA,
              "on_error": "record",
              "expected_count_path": "trigger.payload_json.body.order.items"}
    params.update(parameters)
    return {"nodeId": "check", "binding": "bedrock_inference", "parameters": params,
            "upstreamNodeIds": ["camera"], "downstreamNodeIds": [],
            "capturePaths": {"in": "{work_dir}/" + FRAME_NAME, "reference": None}}


def upload_binding(node_id, source, upstream=("check",), **extra):
    params = {"destination": "s3://example-bucket/{trigger.payload_json.body.image_key}",
              "content_type": "image/jpeg", "timeout_s": 2, "retries": 0,
              "region": "us-east-1", "source": source}
    params.update(extra)
    return {"nodeId": node_id, "binding": "s3_upload", "parameters": params,
            "upstreamNodeIds": list(upstream), "downstreamNodeIds": [],
            "capturePaths": {"in": "{work_dir}/" + FRAME_NAME}}


def publish_binding(node_id="publish_t2", upstream=("upload_raw", "upload_annotated"),
                    template=T2_TEMPLATE, **extra):
    params = {"topic": "plant/line1/evt/inspection/result", "qos": 1,
              "greengrass": True, "payload_format": "json",
              "payload_template": template, "retain": False}
    params.update(extra)
    return {"nodeId": node_id, "binding": "mqtt_publish", "parameters": params,
            "upstreamNodeIds": list(upstream), "downstreamNodeIds": []}


def output_binding(kind, node_id, upstream=()):
    return {"nodeId": node_id, "binding": kind, "parameters": {},
            "upstreamNodeIds": list(upstream), "downstreamNodeIds": []}


def document(*bindings):
    return {"schemaVersion": 1, "workflowId": "wf-1", "workflowVersion": "3",
            "targetArch": "aarch64-jp5", "segments": [],
            "executorBindings": list(bindings), "pluginDependencies": []}


def ids(bindings):
    return [binding["nodeId"] for binding in bindings]


# ---------------------------------------------------------------------------
# order_output_bindings
# ---------------------------------------------------------------------------

class TestOrderOutputBindings:
    def test_reverse_emission_order_runs_the_uploads_before_the_publish(self):
        bindings = [
            output_binding("mqtt_publish", "publish_t2", ["upload_raw", "upload_annotated"]),
            output_binding("s3_upload", "upload_annotated", ["check"]),
            output_binding("s3_upload", "upload_raw", ["check"]),
        ]
        assert ids(order_output_bindings(bindings)) == \
            ["upload_annotated", "upload_raw", "publish_t2"]

    def test_legacy_edges_to_non_output_nodes_keep_emission_order(self):
        bindings = [
            {"nodeId": "f", "binding": "inference_filter",
             "parameters": {"condition": "is_anomalous == true"},
             "upstreamNodeIds": [], "downstreamNodeIds": ["m"]},
            output_binding("mqtt_publish", "m", ["f"]),
            output_binding("digital_output", "d", ["f"]),
            output_binding("opcua_write", "o", ["bedrock_1"]),
            output_binding("modbus_write", "w", []),
        ]
        ordered = order_output_bindings(bindings)
        assert ordered == bindings
        assert all(a is b for a, b in zip(ordered, bindings))

    def test_ties_break_by_emission_order_and_chains_are_honoured(self):
        bindings = [
            output_binding("mqtt_publish", "p2", ["u2"]),
            output_binding("mqtt_publish", "p1", ["u1"]),
            output_binding("s3_upload", "u2", ["u1"]),
            output_binding("s3_upload", "u1", []),
            output_binding("digital_output", "d", []),
        ]
        # At every step the earliest-emitted READY binding runs: p1 (emitted
        # second) becomes ready after u1 and precedes d (emitted last).
        assert ids(order_output_bindings(bindings)) == ["u1", "p1", "u2", "p2", "d"]

    def test_non_output_entries_keep_their_positions(self):
        bindings = [
            output_binding("mqtt_publish", "p", ["u"]),
            {"nodeId": "c", "binding": "conditional", "parameters": {},
             "upstreamNodeIds": ["p"], "downstreamNodeIds": []},
            output_binding("s3_upload", "u", []),
        ]
        assert ids(order_output_bindings(bindings)) == ["u", "c", "p"]

    def test_cycle_falls_back_to_emission_order_with_a_warning(self, caplog):
        bindings = [output_binding("mqtt_publish", "a", ["b"]),
                    output_binding("s3_upload", "b", ["a"])]
        with caplog.at_level(logging.WARNING, logger="workflow_engine.output_bindings"):
            assert ids(order_output_bindings(bindings)) == ["a", "b"]
        assert any("dependency cycle" in record.getMessage() for record in caplog.records)

    def test_merge_binding_outputs_replaces_the_kind_map(self):
        target = {"s3_upload": {"a": {"key": "1"}}, "x": 1}
        before = target["s3_upload"]
        merge_binding_outputs(target, {"s3_upload": {"b": {"key": "2"}}, "junk": 3})
        assert target == {"s3_upload": {"a": {"key": "1"}, "b": {"key": "2"}}, "x": 1}
        assert before == {"a": {"key": "1"}}
        assert target["s3_upload"] is not before


# ---------------------------------------------------------------------------
# process_subset: order honoured, outputs visible to the later binding,
# returned dict
# ---------------------------------------------------------------------------

@pytest.fixture
def work_dir(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    write_frame(path)
    return str(path)


@pytest.fixture
def presign():
    with patch.object(output_bindings, "_s3_presign_client",
                      side_effect=lambda region, timeout_s: presign_client()):
        yield


class TestProcessSubsetResultVisibility:
    def test_publish_emitted_first_still_renders_the_upload_outputs(
            self, work_dir, presign):
        uploader, publisher = RecordingUploader(), RecordingPublisher()
        processor = OutputBindingProcessor(
            s3_uploader=uploader, greengrass_publisher=publisher)
        doc = document(
            publish_binding(upstream=["upload_annotated"], template=json.dumps({
                "url": "{s3_upload.upload_annotated.presigned_url}",
                "key": "{s3_upload.upload_annotated.key}",
                "https": "{s3_upload.upload_annotated.https_url}",
                "raw": "{s3_upload.upload_raw.key}"})),
            upload_binding("upload_annotated", "frame", upstream=["camera"],
                           presign_get_expiry_s=900, strip_suffix=".jpg",
                           append_suffix=".annotated.jpg"),
        )
        details = []
        produced = processor.process_subset(
            doc, {"trigger": TRIGGER}, ["publish_t2", "upload_annotated"],
            detail_sink=lambda node_id, detail: details.append((node_id, detail)),
            work_dir=work_dir)
        assert uploader.destinations() == ["s3://example-bucket/images/o-1/u.annotated.jpg"]
        assert [node_id for node_id, _ in details] == ["upload_annotated", "publish_t2"]
        payload, = publisher.payloads()
        assert payload["key"] == "images/o-1/u.annotated.jpg"
        assert payload["https"] == \
            "https://example-bucket.s3.example.invalid/images/o-1/u.annotated.jpg"
        assert_signed_get_url(payload["url"], ANNOTATED_KEY)
        assert "raw" not in payload
        outputs = produced["s3_upload"]["upload_annotated"]
        assert outputs["presigned_url"] == payload["url"]
        assert set(outputs) == {"bucket", "key", "uri", "bytes", "content_type",
                                "destination_kind", "https_url", "region",
                                "presigned_url", "presigned_url_expires_at"}
        # The detail of the publish masks the signed values; the upload
        # detail names the masked destination.
        publish_detail = dict(details)["publish_t2"]
        assert "X-Amz-Signature=***" in publish_detail
        assert not re.search(r"X-Amz-Signature=[0-9a-f]{8}", publish_detail)

    def test_returned_dict_is_empty_without_outputs_and_input_untouched(self):
        publisher = RecordingPublisher()
        processor = OutputBindingProcessor(greengrass_publisher=publisher)
        tag_values = {"is_anomalous": True, "trigger": TRIGGER}
        produced = processor.process_subset(
            document(publish_binding(upstream=[], template='{"a": "{is_anomalous}"}')),
            tag_values, ["publish_t2"])
        assert produced == {}
        assert processor.process_subset(document(), tag_values, []) == {}
        assert tag_values == {"is_anomalous": True, "trigger": TRIGGER}
        assert publisher.payloads() == [{"a": True}]

    def test_returned_dict_is_merged_by_process_concurrent(self, tmp_path):
        """A fake output processor returns outputs; the Bedrock processor
        folds them into the metadata it returns (design 6.1 rule 2)."""
        write_frame(tmp_path)

        class FakeOutputProcessor:
            def __init__(self):
                self.calls = []

            def process_subset(self, doc, snapshot, binding_ids, **kwargs):
                self.calls.append((list(binding_ids), dict(snapshot), kwargs))
                return {"s3_upload": {"upload_raw": {"key": "k", "bytes": 3}}}

        fake = FakeOutputProcessor()
        collector = NodeStatusCollector(extra_node_ids=["check", "upload_raw"])
        context = RunContext(tag_values={"trigger": TRIGGER}, output_dir=str(tmp_path),
                             capture_id=CAPTURE_ID, node_status=collector)
        processor = BedrockInferenceProcessor(
            invoker=ScriptedInvoker(good_answer()), output_processor=fake)
        metadata = processor.process(
            document(check_binding(), upload_binding("upload_raw", "frame")),
            {"trigger": TRIGGER}, str(tmp_path), run_context=context)
        assert metadata["bedrock"]["check"]["verdict"] == "pass"
        assert metadata["s3_upload"] == {"upload_raw": {"key": "k", "bytes": 3}}
        (binding_ids, snapshot, kwargs), = fake.calls
        assert binding_ids == ["upload_raw"]
        assert kwargs["work_dir"] == str(tmp_path)
        assert "s3_upload" not in snapshot


# ---------------------------------------------------------------------------
# Chain harness (design 6.2 rows): the six-node graph's post-pipeline path
# ---------------------------------------------------------------------------

RAW_KEY = "images/o-1/u.jpg"
ANNOTATED_KEY = "images/o-1/u.annotated.jpg"
RAW_URI = "s3://example-bucket/" + RAW_KEY
ANNOTATED_URI = "s3://example-bucket/" + ANNOTATED_KEY


def chain_document(**check_parameters):
    return document(
        check_binding(**check_parameters),
        upload_binding("upload_raw", "frame"),
        upload_binding("upload_annotated", "annotated_image",
                       strip_suffix=".jpg", append_suffix=".annotated.jpg",
                       presign_get_expiry_s=900),
        publish_binding(),
    )


def run_chain(tmp_path, invoker, upload_errors=None, **check_parameters):
    write_frame(tmp_path)
    collector = NodeStatusCollector(
        extra_node_ids=["check", "upload_raw", "upload_annotated", "publish_t2"])
    context = RunContext(tag_values={"trigger": TRIGGER}, output_dir=str(tmp_path),
                         capture_id=CAPTURE_ID, node_status=collector)
    uploader = RecordingUploader(upload_errors)
    publisher = RecordingPublisher()
    processor = BedrockInferenceProcessor(
        invoker=invoker,
        output_processor=OutputBindingProcessor(
            s3_uploader=uploader, greengrass_publisher=publisher))
    outcome = {}
    try:
        outcome["metadata"] = processor.process(
            chain_document(**check_parameters), {"trigger": TRIGGER}, str(tmp_path),
            run_context=context)
    except Exception as error:  # noqa: BLE001 - asserted by the tests
        outcome["error"] = error
    return outcome, uploader, publisher, collector


def body_of(publisher):
    payload, = publisher.payloads()
    assert ULID_RE.match(payload["msg_id"])
    assert payload["source"] == "edge.inspection"
    assert payload["order_id"] == "o-1"
    body = payload["body"]
    assert ("verdict" in body) != ("error" in body), body
    assert isinstance(body["latency_ms"], int)
    assert body["qc_stage"] == "qc2" and body["decided_by"] == "dda"
    return body


@pytest.mark.usefixtures("presign")
class TestChainHarness:
    def test_pass_row_runs_uploads_then_publishes_the_full_body(self, tmp_path):
        outcome, uploader, publisher, collector = run_chain(
            tmp_path, ScriptedInvoker(good_answer()))
        assert "error" not in outcome
        assert uploader.destinations() == [RAW_URI, ANNOTATED_URI]
        annotated_path = outcome["metadata"]["bedrock"]["check"]["annotated_image_path"]
        with open(annotated_path, "rb") as handle:
            assert uploader.calls[1][1] == handle.read()
        body = body_of(publisher)
        assert body["verdict"] == "pass" and body["confidence"] == 0.9
        assert body["reason"] == "checked"
        assert body["image_s3_key"] == RAW_KEY
        assert body["annotated_image_s3_key"] == ANNOTATED_KEY
        assert_signed_get_url(body["annotated_image_url"], ANNOTATED_KEY)
        assert set(body) == {"qc_stage", "decided_by", "verdict", "confidence", "reason",
                             "image_s3_key", "annotated_image_s3_key",
                             "annotated_image_url", "latency_ms"}
        uploads = outcome["metadata"]["s3_upload"]
        assert uploads["upload_raw"]["key"] == RAW_KEY
        assert uploads["upload_annotated"]["presigned_url"] == body["annotated_image_url"]

    def test_fail_row_publishes_verdict_fail(self, tmp_path):
        _, uploader, publisher, _ = run_chain(
            tmp_path, ScriptedInvoker(good_answer("fail", confidence=0.7)))
        body = body_of(publisher)
        assert body["verdict"] == "fail" and body["confidence"] == 0.7
        assert uploader.destinations() == [RAW_URI, ANNOTATED_URI]

    @pytest.mark.parametrize("script, error_prefix", [
        ([good_answer(count=1)], "malformed Bedrock answer"),
        (["this is not json"], "malformed Bedrock answer"),
        ([throttled()], "ClientError"),
        ([RuntimeError("boom")], "RuntimeError: boom"),
    ], ids=["short-answer", "malformed", "throttled", "transport"])
    def test_error_rows_publish_error_and_raw_key_only(
            self, tmp_path, script, error_prefix):
        outcome, uploader, publisher, collector = run_chain(
            tmp_path, ScriptedInvoker(*script))
        assert "error" not in outcome
        assert collector.status_of("check") == STATUS_FAILURE
        # The annotated upload is skipped (no image), the raw upload ran.
        assert uploader.destinations() == [RAW_URI]
        body = body_of(publisher)
        assert body["error"].startswith(error_prefix)
        assert set(body) == {"qc_stage", "decided_by", "error", "image_s3_key", "latency_ms"}
        assert body["image_s3_key"] == RAW_KEY
        assert "s3_upload" in outcome["metadata"]
        assert set(outcome["metadata"]["s3_upload"]) == {"upload_raw"}

    def test_throttled_then_good_passes_on_the_second_attempt(self, tmp_path):
        invoker = ScriptedInvoker(throttled(), good_answer())
        outcome, _, publisher, _ = run_chain(
            tmp_path, invoker, attempts=2, attempt_timeout_s=5)
        assert len(invoker.calls) == 2
        assert outcome["metadata"]["bedrock"]["check"]["attempts"] == 2
        assert body_of(publisher)["verdict"] == "pass"

    def test_error_verdict_fail_publishes_a_fail_verdict(self, tmp_path):
        _, _, publisher, _ = run_chain(
            tmp_path, ScriptedInvoker(RuntimeError("boom")), error_verdict="fail")
        body = body_of(publisher)
        assert body["verdict"] == "fail" and body["confidence"] == 0.0
        assert body["reason"] == "RuntimeError: boom"
        assert "error" not in body

    def test_raw_upload_failure_publishes_without_the_raw_key_and_fails_the_run(
            self, tmp_path):
        outcome, uploader, publisher, _ = run_chain(
            tmp_path, ScriptedInvoker(good_answer()),
            upload_errors={RAW_URI: RuntimeError("PutObject failed")})
        assert uploader.destinations() == [RAW_URI, ANNOTATED_URI]
        body = body_of(publisher)
        assert body["verdict"] == "pass"
        assert "image_s3_key" not in body
        assert body["annotated_image_s3_key"] == ANNOTATED_KEY
        assert "upload_raw" in outcome["error"].node_ids

    def test_disabled_check_publishes_the_unconditional_error_body(self, tmp_path):
        invoker = ScriptedInvoker(good_answer())
        _, uploader, publisher, _ = run_chain(
            tmp_path, invoker, enabled=False, error_verdict="fail")
        assert invoker.calls == []
        assert uploader.destinations() == [RAW_URI]
        body = body_of(publisher)
        assert body["error"] == "bedrock check disabled by configuration"

    def test_no_pass_without_an_accepted_answer(self, tmp_path):
        """Invariant (design 13): ``verdict: pass`` only from a parsed,
        complete answer -- a two-object answer for three items never passes."""
        for script in (["{}"], ['{"verdict": "pass"}'], [good_answer(count=2)]):
            _, _, publisher, _ = run_chain(tmp_path, ScriptedInvoker(*script))
            assert "verdict" not in body_of(publisher)
