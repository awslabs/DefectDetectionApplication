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
"""Inspection-format Bedrock nodes export workflow-tuning samples (AC-1,
AC-2, AC-6b and AC-6c, device halves).

The real ``BedrockInferenceProcessor`` on the ``answer_format:
inspection`` path, with a scripted invoker, a collector exporter
installed as the process-wide exporter and, where the uploaded sidecar
body matters, the real ``SampleExporter`` on a dict-backed S3 stub. No
AWS call, no network.

At c299a6e1 the inspection path never calls an exporter, so every
sample count is 0; the gate-order test imports the new snippet builders
inside the test and fails on them.
"""
import base64
import json
import os
import re

import cv2
import numpy as np
import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine import output_bindings
from workflow_engine.node_status import NodeStatusCollector
from workflow_engine.output_bindings import (
    BedrockInferenceError,
    BedrockInferenceProcessor,
    RunContext,
)
from workflow_engine.tuning import sample_export as export_module
from workflow_engine.tuning.sample_export import (
    ExportConfig,
    ExportContext,
    SampleExporter,
    build_sidecar,
    object_keys,
    set_sample_exporter,
)
from workflow_engine.vendor.workflow_core.anomaly_invocation import (
    parse_inspection_answer,
    prompt_fingerprint,
)

FRAME_NAME = "bedrock_frame_camera.jpg"
CAPTURE_ID = "cap-1"
NODE_ID = "check"
NOVA = "us.amazon.nova-pro-v1:0"
PREFIX = "workflow-tuning/samples/"
DEVICE = "device-under-test"
BUCKET = "tuning-samples-test"
CONTEXT = ExportContext(workflow_id="wf-1", version="3", execution_id="exec-1")
PAYLOAD = {
    "order_id": "o-1",
    "body": {
        "qc_stage": "qc2",
        "order": {"items": [
            {"shape": "roof", "color": "red"},
            {"shape": "panel", "color": "blue"},
        ]},
    },
}
COUNT_PATH = "trigger.payload_json.body.order.items"


def write_frame(directory, width=64, height=48, name=FRAME_NAME):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    frame[:, :, 1] = 90
    path = os.path.join(str(directory), name)
    assert cv2.imwrite(path, frame)
    return path


def jpeg_bytes(width=48, height=32, value=120):
    frame = np.full((height, width, 3), value, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", frame)
    assert ok
    return encoded.tobytes()


def answer(verdict="pass", count=2, confidence=0.9):
    objects = [
        {"name": "part{0}".format(index),
         "qc": "OK" if verdict == "pass" else "NOK", "reason": "r",
         "bounding_box": {"x_min": 100, "y_min": 100,
                          "x_max": 400, "y_max": 400}}
        for index in range(count)]
    return json.dumps({"verdict": verdict, "confidence": confidence,
                       "reason": "checked", "objects": objects})


class ScriptedInvoker:
    """Returns (or raises) the scripted items in order and records every
    call's positional arguments and keyword options."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, *args, **options):
        self.calls.append((args, options))
        item = self.script.pop(0) if self.script else answer()
        if isinstance(item, BaseException):
            raise item
        return item


class Collector:
    """An exporter double recording every offered sample."""

    def __init__(self):
        self.samples = []

    def enqueue(self, sample):
        self.samples.append(sample)


class FakeS3:
    """Records every put_object in order."""

    def __init__(self):
        self.objects = {}
        self.puts = []

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None,
                   **_):
        self.puts.append((Key, ContentType))
        self.objects[Key] = Body
        return {}


@pytest.fixture
def collector():
    exporter = Collector()
    set_sample_exporter(exporter)
    yield exporter
    set_sample_exporter(None)


@pytest.fixture
def uploader():
    s3 = FakeS3()
    exporter = SampleExporter(
        ExportConfig(bucket=BUCKET, prefix=PREFIX), s3_factory=lambda: s3,
        thing_name=DEVICE)
    set_sample_exporter(exporter)
    yield exporter, s3
    exporter.stop(5.0)
    set_sample_exporter(None)


def legend_dir(tmp_path, write=True):
    images = tmp_path / "images"
    images.mkdir(exist_ok=True)
    legend = images / "source.jpg"
    if write:
        legend.write_bytes(jpeg_bytes(320, 240, 200))
    return str(images), str(legend)


def a_parameters(images, **overrides):
    """A's deployed ``check`` parameters (generic values), with the
    legend re-rooted to a temporary directory."""
    parameters = {
        "prompt": ("Stage {trigger.payload_json.body.qc_stage}: check "
                   "{json:trigger.payload_json.body.order.items} on the "
                   "{image.width}x{image.height} image."),
        "answer_format": "inspection", "model": NOVA, "region": "us-west-2",
        "reference_payload_path": "file://" + images + "/source.jpg",
        "allowed_uri_prefixes": "file://" + images + "/",
        "on_error": "record", "error_verdict": "error",
        "attempts": 2, "attempt_timeout_s": 6, "budget_s": 11,
        "max_image_dimension": 1280, "render_prompt": True,
        "expected_count_path": COUNT_PATH, "box_scale": "normalized_1000",
        "max_tokens": 600,
    }
    parameters.update(overrides)
    return parameters


def make_binding(parameters):
    return {
        "nodeId": NODE_ID, "binding": "bedrock_inference",
        "parameters": dict(parameters), "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": [],
        "capturePaths": {"in": "{work_dir}/" + FRAME_NAME, "reference": None},
    }


def make_context(tmp_path, payload=None):
    payload = PAYLOAD if payload is None else payload
    trigger = {"topic": "plant/line1/evaluate", "payload": json.dumps(payload),
               "payload_json": json.loads(json.dumps(payload)), "qos": 1}
    return RunContext(
        tag_values={"trigger": trigger}, output_dir=str(tmp_path),
        capture_id=CAPTURE_ID,
        node_status=NodeStatusCollector(extra_node_ids=[NODE_ID]))


def run(invoker, parameters, tmp_path, payload=None, export_context=CONTEXT):
    context = make_context(tmp_path, payload)
    processor = BedrockInferenceProcessor(invoker=invoker)
    document = {"schemaVersion": 1,
                "executorBindings": [make_binding(parameters)]}
    return processor.process(
        document, dict(context.tag_values), str(tmp_path),
        run_context=context, export_context=export_context)


def image_size(data):
    frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    return frame.shape[1], frame.shape[0]


def uploaded_sidecar(s3):
    bodies = [s3.objects[key] for key, _ in s3.puts if key.endswith(".json")]
    assert len(bodies) == 1
    return bodies[0].decode("utf-8")


# ---------------------------------------------------------------------------
# AC-1: one sample after a parsed answer, with the bytes sent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_a_style_run_exports_one_sample_with_the_bytes_sent(
        tmp_path, collector, verdict):
    """A's parameters re-rooted: the sample's input is the downscaled
    frame sent (not the capture), its reference the legend sent, and the
    sidecar records the inspection verdict and the metadata snippet."""
    images, legend = legend_dir(tmp_path)
    capture = write_frame(tmp_path, 1600, 1200)
    parameters = a_parameters(images)
    invoker = ScriptedInvoker(answer(verdict, confidence=0.83))
    run(invoker, parameters, tmp_path)

    assert len(collector.samples) == 1
    sample = collector.samples[0]
    (args, _), = invoker.calls
    sent = args[2]
    assert sample.input_bytes == sent[0][1]
    with open(capture, "rb") as handle:
        assert sample.input_bytes != handle.read()
    assert image_size(sample.input_bytes) == (1280, 960)
    with open(legend, "rb") as handle:
        legend_bytes = handle.read()
    assert sample.reference_bytes == sent[1][1] == legend_bytes

    sidecar = build_sidecar(sample, DEVICE, PREFIX)
    assert sidecar["answerFormat"] == "inspection"
    assert sidecar["recorded"]["verdict"] == verdict
    assert sidecar["recorded"]["isAnomalous"] is (verdict == "fail")
    assert sidecar["recorded"]["confidence"] == pytest.approx(0.83)
    assert sidecar["recorded"]["parseError"] is None
    assert sidecar["promptFingerprint"] == prompt_fingerprint(parameters)
    assert sidecar["input"]["width"] == 1280
    assert sidecar["input"]["height"] == 960
    assert "metadataSnippet" not in sidecar
    snippet = json.loads(sidecar["metadataSnippetJson"])
    payload_json = snippet["trigger"]["payload_json"]
    assert list(payload_json) == list(PAYLOAD)
    assert list(payload_json["body"]["order"]["items"][0]) == [
        "shape", "color"]


# ---------------------------------------------------------------------------
# AC-2: export edge cases
# ---------------------------------------------------------------------------

def test_all_attempts_malformed_export_the_last_text_and_its_parse_error(
        tmp_path, collector):
    """Every attempt malformed: one sample with no verdict, the last text
    and the parser's message without the run-record prefix."""
    images, _ = legend_dir(tmp_path)
    write_frame(tmp_path)
    last = json.dumps({"verdict": "maybe", "confidence": 0.5,
                       "reason": "?", "objects": []})
    run(ScriptedInvoker("not json at all", last), a_parameters(images),
        tmp_path)

    assert len(collector.samples) == 1
    sample = collector.samples[0]
    with pytest.raises(ValueError) as parser:
        parse_inspection_answer(last, 2, "normalized_1000")
    assert sample.answer == last
    assert sample.parse_error == str(parser.value)
    assert not sample.parse_error.startswith("malformed Bedrock answer")
    recorded = build_sidecar(sample, DEVICE, PREFIX)["recorded"]
    assert recorded["verdict"] is None
    assert recorded["isAnomalous"] is None
    assert recorded["confidence"] is None
    assert recorded["parseError"] == str(parser.value)


def test_a_transport_error_after_a_malformed_answer_keeps_its_text(
        tmp_path, collector):
    images, _ = legend_dir(tmp_path)
    write_frame(tmp_path)
    run(ScriptedInvoker("{}", RuntimeError("connection reset")),
        a_parameters(images), tmp_path)

    assert len(collector.samples) == 1
    sample = collector.samples[0]
    assert sample.answer == "{}"
    with pytest.raises(ValueError) as parser:
        parse_inspection_answer("{}", 2, "normalized_1000")
    assert sample.parse_error == str(parser.value)


def test_an_empty_answer_counts_as_returned_text(tmp_path, collector):
    images, _ = legend_dir(tmp_path)
    write_frame(tmp_path)
    run(ScriptedInvoker(""), a_parameters(images, attempts=1), tmp_path)

    assert len(collector.samples) == 1
    sample = collector.samples[0]
    assert sample.answer == ""
    with pytest.raises(ValueError) as parser:
        parse_inspection_answer("", 2, "normalized_1000")
    assert sample.parse_error == str(parser.value)


def test_transport_errors_only_export_nothing(tmp_path, collector):
    """No attempt returned text: no sample. The same node answering
    exports one."""
    images, _ = legend_dir(tmp_path)
    write_frame(tmp_path)
    run(ScriptedInvoker(RuntimeError("timeout"), RuntimeError("timeout")),
        a_parameters(images), tmp_path)
    assert collector.samples == []

    run(ScriptedInvoker(answer()), a_parameters(images), tmp_path)
    assert len(collector.samples) == 1


@pytest.mark.parametrize("case", [
    "unresolved_placeholder", "unresolvable_count", "reference", "disabled"])
def test_pre_call_failures_and_disabled_export_nothing(
        tmp_path, collector, case):
    """Failures before the call and ``enabled: false`` make no call and
    export nothing; the same node, configured right, exports one."""
    images, legend = legend_dir(tmp_path)
    write_frame(tmp_path)
    broken = {
        "unresolved_placeholder": {
            "prompt": "Check {trigger.payload_json.missing}."},
        "unresolvable_count": {
            "expected_count_path": "trigger.payload_json.body.none"},
        "reference": {
            "reference_payload_path": "file://" + images + "/absent.jpg"},
        "disabled": {"enabled": False},
    }[case]
    invoker = ScriptedInvoker(answer())
    run(invoker, a_parameters(images, **broken), tmp_path)
    assert invoker.calls == []
    assert collector.samples == []

    run(ScriptedInvoker(answer()), a_parameters(images), tmp_path)
    assert len(collector.samples) == 1


def test_fail_run_exports_before_raising(tmp_path, collector):
    images, _ = legend_dir(tmp_path)
    write_frame(tmp_path)
    with pytest.raises(BedrockInferenceError) as raised:
        run(ScriptedInvoker("nope", "still nope"),
            a_parameters(images, on_error="fail_run"), tmp_path)
    assert "Bedrock inference node 'check' failed: malformed Bedrock answer" \
        in str(raised.value)
    assert len(collector.samples) == 1
    assert collector.samples[0].answer == "still nope"


def test_without_an_exporter_or_a_context_the_snippet_is_never_built(
        tmp_path, monkeypatch):
    """The cheap gates come first: with no installed exporter, or no
    export context, the snippet builder is never called."""
    from workflow_engine.tuning.sample_export import (  # noqa: F401
        inline_reference_paths,
        inspection_metadata_snippet_json,
    )
    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        return inspection_metadata_snippet_json(*args, **kwargs)

    monkeypatch.setattr(export_module, "inspection_metadata_snippet_json", spy)
    images, _ = legend_dir(tmp_path)
    write_frame(tmp_path)
    set_sample_exporter(None)
    run(ScriptedInvoker(answer()), a_parameters(images), tmp_path)
    assert calls == []

    exporter = Collector()
    set_sample_exporter(exporter)
    try:
        run(ScriptedInvoker(answer()), a_parameters(images), tmp_path,
            export_context=None)
        assert calls == []
        assert exporter.samples == []
        run(ScriptedInvoker(answer()), a_parameters(images), tmp_path)
        assert len(calls) == 1
        assert len(exporter.samples) == 1
    finally:
        set_sample_exporter(None)


# ---------------------------------------------------------------------------
# AC-6b: signed query values are masked in the sidecar, sent unchanged
# ---------------------------------------------------------------------------

SIGNED_URL = (
    "https://objects.example.com/parts/o-1.jpg?X-Amz-Algorithm=AWS4-HMAC-"
    "SHA256&X-Amz-Credential=AKIDEXAMPLE%2F20261113%2Fus-west-2%2Fs3%2F"
    "aws4_request&X-Amz-Date=20261113T090000Z&X-Amz-Expires=300&"
    "X-Amz-SignedHeaders=host&X-Amz-Signature=0f1e2d3c4b5a69788796a5b4c3d2e1f0")


def test_signed_query_values_are_masked_in_the_sidecar_body(
        tmp_path, uploader):
    exporter, s3 = uploader
    images, _ = legend_dir(tmp_path)
    write_frame(tmp_path)
    payload = json.loads(json.dumps(PAYLOAD))
    payload["image_url"] = SIGNED_URL
    invoker = ScriptedInvoker(answer())
    run(invoker,
        a_parameters(images, prompt="Image at {trigger.payload_json.image_url}."),
        tmp_path, payload=payload)
    assert exporter.wait_idle(10.0)

    (args, _), = invoker.calls
    assert SIGNED_URL in args[1]
    body = uploaded_sidecar(s3)
    assert "0f1e2d3c4b5a69788796a5b4c3d2e1f0" not in body
    assert "AKIDEXAMPLE" not in body
    snippet = json.loads(json.loads(body)["metadataSnippetJson"])
    masked = snippet["trigger"]["payload_json"]["image_url"]
    values = re.findall(r"(X-Amz-[A-Za-z]+)=([^&]*)", masked)
    assert len(values) == 6
    assert all(value == "***" for _, value in values)


# ---------------------------------------------------------------------------
# AC-6c: inline reference data never reaches the sidecar
# ---------------------------------------------------------------------------

def test_inline_reference_data_never_reaches_the_sidecar(tmp_path, uploader):
    exporter, s3 = uploader
    write_frame(tmp_path)
    reference = jpeg_bytes(40, 30, 77)
    inline = base64.b64encode(reference).decode("ascii")
    payload = {"refs": [{"image": inline}], "order_id": "o-1"}
    invoker = ScriptedInvoker(answer(count=1))
    run(invoker,
        a_parameters("/unused", prompt="Order {trigger.payload_json.order_id}.",
                     reference_payload_path="refs.0.image",
                     allowed_uri_prefixes="", expected_count_path=""),
        tmp_path, payload=payload)
    assert exporter.wait_idle(10.0)

    (args, _), = invoker.calls
    assert [label for label, _ in args[2]] == ["Input image", "Reference image"]
    assert args[2][1][1] == reference
    body = uploaded_sidecar(s3)
    assert inline not in body
    sidecar = json.loads(body)
    snippet = json.loads(sidecar["metadataSnippetJson"])
    assert "payload" not in snippet["trigger"]
    assert snippet["trigger"]["payload_json"] == {
        "refs": [{}], "order_id": "o-1"}
    assert sidecar["metadataSnippetOmitted"] == [
        "trigger.payload", "trigger.payload_json.refs.0.image"]


def test_a_uri_payload_reference_keeps_the_payload(
        tmp_path, uploader, monkeypatch):
    exporter, s3 = uploader
    write_frame(tmp_path)
    reference = jpeg_bytes(40, 30, 99)
    fetched = []

    def fetch(value, allowed_prefixes=None, s3_client=None):
        fetched.append(value)
        return reference

    monkeypatch.setattr(output_bindings, "fetch_reference_bytes", fetch)
    uri = "s3://bucket-a/refs/o-1.jpg"
    payload = {"refs": [{"image": uri}], "order_id": "o-1"}
    run(ScriptedInvoker(answer(count=1)),
        a_parameters("/unused", prompt="Order {trigger.payload_json.order_id}.",
                     reference_payload_path="refs.0.image",
                     allowed_uri_prefixes="", expected_count_path=""),
        tmp_path, payload=payload)
    assert exporter.wait_idle(10.0)

    assert fetched == [uri]
    sidecar = json.loads(uploaded_sidecar(s3))
    snippet = json.loads(sidecar["metadataSnippetJson"])
    assert json.loads(snippet["trigger"]["payload"]) == payload
    assert snippet["trigger"]["payload_json"]["refs"][0]["image"] == uri
    assert "metadataSnippetOmitted" not in sidecar
    _, _, reference_key = object_keys(PREFIX, exporter_sample_shape(), DEVICE)
    assert s3.objects[reference_key] == reference
    assert sidecar["reference"]["source"] == uri


def exporter_sample_shape():
    """The identifiers the uploaded keys are built from."""
    return export_module.ExportedSample(
        workflow_id=CONTEXT.workflow_id, node_id=NODE_ID,
        node_type="bedrock_inference", execution_id=CONTEXT.execution_id,
        input_bytes=b"x", reference_bytes=b"x")
