# Copyright 2026 Amazon Web Services, Inc.
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
"""The backfill records the verdict of inspection-format runs (AC-8) and
rebuilds list-form runs from their persisted frames (AC-34).

``backfill.build_sample`` / ``tunable_nodes`` on temporary run-artifact
trees (node frames plus the persisted run metadata), and the real
``SampleExporter`` on a dict-backed S3 stub where the uploaded objects
matter. No database, no AWS call.

At c299a6e1 the backfill reads ``is_anomalous`` (absent from inspection
outcomes, so the verdict is null), exports the error bodies that carry no
``error`` key, takes the ``in`` frame, does not select an inspection node
with ``anomaly_mode: false``, and ignores the recorded references.
"""
import io
import json
import os

import pytest
from PIL import Image

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine import run_artifacts
from workflow_engine.tuning import backfill as backfill_module
from workflow_engine.tuning.sample_export import (
    ExportConfig,
    SampleExporter,
    build_sidecar,
)

NODE_ID = "check"
CAPTURE_ID = "cap-1"
PREFIX = "workflow-tuning/samples/"
DEVICE = "device-under-test"
FRAME_TEMPLATE = "{capture_id}.node.{node_id}.{port}.jpg"
PAYLOAD = {"order_id": "o-1", "body": {"qc_stage": "qc2", "order": {
    "items": [{"shape": "roof", "color": "red"}]}}}


def jpeg(width, height, color=(10, 20, 30)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "JPEG")
    return buffer.getvalue()


FULL_FRAME = jpeg(1600, 1200)
SENT_FRAME = jpeg(1280, 960)
LEGEND = jpeg(50, 40, (200, 200, 0))


def inspection_node(**parameters):
    fields = {"prompt": "Check {json:trigger.payload_json.body.order.items}.",
              "answer_format": "inspection", "render_prompt": True,
              "model": "us.amazon.nova-pro-v1:0", "max_tokens": 600,
              "expected_count_path": "trigger.payload_json.body.order.items",
              "reference_payload_path": "file:///aws_dda/images/source.jpg",
              "allowed_uri_prefixes": "file:///aws_dda/images/"}
    fields.update(parameters)
    return backfill_module.TunableNode(
        node_id=NODE_ID, node_type="bedrock_inference", parameters=fields)


def answer_text(verdict):
    return json.dumps({"verdict": verdict, "confidence": 0.8, "reason": "r",
                       "objects": []})


def outcome(verdict="pass", **extra):
    entry = {"attempts": 1, "latency_ms": 900, "model": "m",
             "box_scale": "pixels", "expected_count": 1,
             "input_width": 1280, "input_height": 960,
             "outcome": verdict, "verdict": verdict, "confidence": 0.8,
             "reason": "r", "objects": [], "objects_px": [],
             "text": answer_text(verdict)}
    entry.update(extra)
    return entry


def write_run(tmp_path, ports, node_outcome):
    """Node frames plus the persisted run metadata; returns the metadata
    as the backfill reads it back."""
    output_dir = str(tmp_path / "run")
    os.makedirs(output_dir, exist_ok=True)
    for port, data in ports.items():
        with open(os.path.join(output_dir, FRAME_TEMPLATE.format(
                capture_id=CAPTURE_ID, node_id=NODE_ID, port=port)),
                "wb") as handle:
            handle.write(data)
    metadata = {
        "trigger": {"topic": "plant/line1/evaluate",
                    "payload": json.dumps(PAYLOAD), "payload_json": PAYLOAD,
                    "qos": 1},
        "bedrock": {NODE_ID: node_outcome},
    }
    with open(os.path.join(output_dir, CAPTURE_ID + ".json"), "w") as handle:
        json.dump(metadata, handle)
    return output_dir, run_artifacts.read_run_metadata(output_dir, CAPTURE_ID)


def build(node, output_dir, metadata, node_outcome):
    return backfill_module.build_sample(
        node, "wf-1", "3", "exec-1", output_dir, CAPTURE_ID, metadata,
        node_outcome, started_at=1_794_510_000)


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.puts = []

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None,
                   **_):
        self.puts.append(Key)
        self.objects[Key] = Body
        return {}


# ---------------------------------------------------------------------------
# AC-8
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_pass_and_fail_entries_record_the_inspection_verdict(
        tmp_path, verdict):
    entry = outcome(verdict)
    output_dir, metadata = write_run(tmp_path, {"in": FULL_FRAME}, entry)
    sample, reason = build(inspection_node(), output_dir, metadata, entry)
    assert reason is None
    sidecar = build_sidecar(sample, DEVICE, PREFIX)
    assert sidecar["recorded"]["isAnomalous"] is (verdict == "fail")
    assert sidecar["recorded"]["verdict"] == verdict
    assert sidecar["recorded"]["confidence"] == 0.8
    assert sidecar["recorded"]["answer"] == answer_text(verdict)
    assert sidecar["answerFormat"] == "inspection"
    assert sidecar["source"] == "backfill"


def test_an_error_body_without_an_error_key_is_skipped(tmp_path):
    """``error_verdict: fail`` records ``outcome: "error"`` with a fail
    verdict and no ``error`` key: never a judged pair."""
    entry = outcome("fail", outcome="error", confidence=0.0,
                    reason="could not load its reference image: gone")
    del entry["text"]
    output_dir, metadata = write_run(tmp_path, {"in": FULL_FRAME}, entry)
    sample, reason = build(inspection_node(), output_dir, metadata, entry)
    assert sample is None
    assert reason == backfill_module.SKIP_ERROR_OUTCOME


def test_the_input_frame_is_preferred_to_the_capture(tmp_path):
    entry = outcome()
    output_dir, metadata = write_run(
        tmp_path, {"in": FULL_FRAME, "input": SENT_FRAME}, entry)
    sample, _ = build(inspection_node(), output_dir, metadata, entry)
    assert sample.input_bytes == SENT_FRAME
    sidecar = build_sidecar(sample, DEVICE, PREFIX)
    assert (sidecar["input"]["width"], sidecar["input"]["height"]) == (
        1280, 960)

    other = tmp_path / "other"
    other.mkdir()
    output_dir, metadata = write_run(other, {"in": SENT_FRAME}, entry)
    sample, _ = build(inspection_node(), output_dir, metadata, entry)
    assert sample.input_bytes == SENT_FRAME


def test_an_inspection_node_with_anomaly_mode_false_is_selected():
    graph = {"nodes": [
        {"id": "check", "type": "bedrock_inference",
         "parameters": {"answer_format": "inspection", "anomaly_mode": False}},
        {"id": "free", "type": "bedrock_inference",
         "parameters": {"anomaly_mode": False}},
        {"id": "legacy", "type": "bedrock_inference", "parameters": {}},
    ]}
    assert [node.node_id for node in backfill_module.tunable_nodes(graph)] == [
        "check", "legacy"]


def test_the_sidecar_carries_the_snippet_and_the_reference_size_only(
        tmp_path):
    entry = outcome()
    output_dir, metadata = write_run(
        tmp_path, {"in": FULL_FRAME, "reference": LEGEND}, entry)
    sample, reason = build(inspection_node(), output_dir, metadata, entry)
    assert reason is None
    sidecar = build_sidecar(sample, DEVICE, PREFIX)
    snippet = json.loads(sidecar["metadataSnippetJson"])
    assert list(snippet) == ["trigger"]
    assert list(snippet["trigger"]) == ["topic", "payload", "payload_json",
                                        "qos"]
    assert list(snippet["trigger"]["payload_json"]) == ["order_id", "body"]
    assert list(snippet["trigger"]["payload_json"]["body"]["order"]["items"]
                [0]) == ["shape", "color"]
    assert sorted(sidecar["reference"]) == [
        "bytes", "height", "key", "sha256", "width"]
    assert (sidecar["reference"]["width"],
            sidecar["reference"]["height"]) == (50, 40)
    assert "metadataSnippet" not in sidecar


# ---------------------------------------------------------------------------
# AC-34: list-form runs
# ---------------------------------------------------------------------------

def recorded_references(*kinds):
    return [{"label": "Reference {0}".format(index), "kind": kind,
             "format": "jpeg" if kind == "image" else "pdf",
             "source": "file:///aws_dda/reference/r{0}".format(index),
             "sha256": "0" * 64, "bytes": 10}
            for index, kind in enumerate(kinds, start=1)]


def test_a_two_image_list_is_exported_with_its_frames_in_order(tmp_path):
    first, second = jpeg(30, 20, (1, 1, 1)), jpeg(40, 30, (2, 2, 2))
    entry = outcome(references=recorded_references("image", "image"))
    output_dir, metadata = write_run(
        tmp_path, {"in": FULL_FRAME, "reference_1": first,
                   "reference_2": second}, entry)
    node = inspection_node(reference_payload_path=(
        "file:///aws_dda/reference/r1.jpg\nfile:///aws_dda/reference/r2.jpg"))
    sample, reason = build(node, output_dir, metadata, entry)
    assert reason is None
    assert sample.reference_bytes is None

    s3 = FakeS3()
    exporter = SampleExporter(
        ExportConfig(bucket="tuning-samples-test", prefix=PREFIX),
        s3_factory=lambda: s3, thing_name=DEVICE)
    try:
        exporter.enqueue(sample)
        assert exporter.wait_idle(10.0)
    finally:
        exporter.stop(5.0)
    base = PREFIX + "wf-1/check/" + DEVICE + "/exec-1"
    assert s3.puts == [base + ".input.jpg", base + ".reference-1.jpg",
                       base + ".reference-2.jpg", base + ".json"]
    assert s3.objects[base + ".reference-1.jpg"] == first
    assert s3.objects[base + ".reference-2.jpg"] == second
    sidecar = json.loads(s3.objects[base + ".json"])
    assert "reference" not in sidecar
    assert [sorted(item) for item in sidecar["references"]] == [
        ["bytes", "format", "height", "index", "key", "kind", "label",
         "sha256", "width"]] * 2
    assert [(item["width"], item["height"]) for item in
            sidecar["references"]] == [(30, 20), (40, 30)]
    assert [item["label"] for item in sidecar["references"]] == [
        "Reference 1", "Reference 2"]


def test_a_run_that_sent_a_document_is_skipped(tmp_path):
    entry = outcome(references=recorded_references("image", "document"))
    output_dir, metadata = write_run(
        tmp_path, {"in": FULL_FRAME, "reference_1": LEGEND}, entry)
    sample, reason = build(inspection_node(), output_dir, metadata, entry)
    assert sample is None
    assert reason == "document_reference_not_persisted"
    assert reason == backfill_module.SKIP_DOCUMENT_REFERENCE


def test_a_run_with_a_missing_reference_frame_is_skipped(tmp_path):
    entry = outcome(references=recorded_references("image", "image"))
    output_dir, metadata = write_run(
        tmp_path, {"in": FULL_FRAME, "reference_1": LEGEND}, entry)
    sample, reason = build(inspection_node(), output_dir, metadata, entry)
    assert sample is None
    assert reason == "reference_frame_missing"
    assert reason == backfill_module.SKIP_REFERENCE_FRAME_MISSING


def test_a_run_without_recorded_references_takes_the_reference_frame(
        tmp_path):
    """A run of a LocalServer that recorded no ``references``: the
    ``reference`` frame if present, with the inspection verdict."""
    entry = outcome("fail")
    output_dir, metadata = write_run(
        tmp_path, {"in": FULL_FRAME, "reference": LEGEND}, entry)
    sample, reason = build(inspection_node(), output_dir, metadata, entry)
    assert reason is None
    assert sample.reference_bytes == LEGEND
    assert build_sidecar(sample, DEVICE, PREFIX)["recorded"][
        "isAnomalous"] is True
