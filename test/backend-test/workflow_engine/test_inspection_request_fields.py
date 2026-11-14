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
"""``additional_model_request_fields`` together with the inspection
sample export and reference files: the device half of the integration of
the two features (design section 25).

- The list form's request holds the image and document blocks AND the
  node's ``additionalModelRequestFields``; without the parameter the same
  request is sent minus that key.
- Under the attempt policy every attempt passes the invoker both
  ``attachments=`` and ``additional_model_request_fields=``; a single
  legend passes only the fields keyword.
- A value that is not a JSON object stops the node before the call, with
  the references configured, and exports no sample.
- The exported sample is the same with or without the fields: the fields
  change the request, not the sample contract.
- The legacy path sends a fixed reference and the fields.
- The default invoker sends what the shared builder builds for a list with
  fields.

At c299a6e1 the inspection path sends one image and no fields, the legacy
path cannot load a fixed reference, and ``ReferenceFile`` does not exist.
"""
import io
import json
import os

import boto3
import cv2
import numpy as np
import pytest
from PIL import Image

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from test_bedrock_attempt_policy import (  # noqa: F401 - stub_boto3 is a fixture
    DEMO_POLICY,
    GOOD,
    NON_DEFAULT_OPTIONS,
    FakeClock,
    ScriptedInvoker as PolicyInvoker,
    run as run_policy,
    stub_boto3,
)
from workflow_engine.node_status import NodeStatusCollector
from workflow_engine.output_bindings import (
    BedrockInferenceProcessor,
    RunContext,
    _default_bedrock_invoker,
)
from workflow_engine.tuning.sample_export import (
    ExportContext,
    build_sidecar,
    set_sample_exporter,
)
from workflow_engine.vendor.workflow_core.anomaly_invocation import (
    build_bedrock_invocation,
)

FIELDS = "additional_model_request_fields"
#: A field Amazon Nova accepts (the catalog's second example).
TOP_K = '{"inferenceConfig": {"topK": 20}}'
TOP_K_COMPACT = '{"inferenceConfig":{"topK":20}}'
TOP_K_OBJECT = {"inferenceConfig": {"topK": 20}}
FRAME_NAME = "bedrock_frame_camera.jpg"
NODE_ID = "check"
NOVA = "us.amazon.nova-pro-v1:0"
DEVICE = "device-under-test"
PREFIX = "workflow-tuning/samples/"
CONTEXT = ExportContext(workflow_id="wf-1", version="3", execution_id="exec-1")
NOTES = "Reference 1 is the legend. Judge only the Input image.\n".encode()
SHEET = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
LEGACY_ANSWER = '{"is_anomalous": false, "confidence": 0.9}'
LIST_BLOCKS = ["text", "text", "image", "text", "image", "text", "document",
               "text", "document"]


def answer():
    return json.dumps({"verdict": "pass", "confidence": 0.9, "reason": "ok",
                       "objects": [{"name": "part", "qc": "OK", "reason": "r",
                                    "bounding_box": {"x_min": 1, "y_min": 1,
                                                     "x_max": 9, "y_max": 9}}]})


def encode(image, fmt):
    buffer = io.BytesIO()
    image.save(buffer, fmt)
    return buffer.getvalue()


LEGEND_PNG = encode(Image.new("RGB", (400, 300), (250, 240, 10)), "PNG")
LEGEND_JPEG = encode(Image.new("RGB", (400, 300), (250, 240, 10)), "JPEG")


def write_frame(directory, width=64, height=48):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 1] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    assert cv2.imwrite(os.path.join(str(directory), FRAME_NAME), frame)


def reference_dir(tmp_path, **files):
    refs = tmp_path / "refs"
    refs.mkdir(exist_ok=True)
    for name, data in files.items():
        (refs / name.replace("_", ".")).write_bytes(data)
    return str(refs)


def uri(refs, name):
    return "file://" + refs + "/" + name


def three_lines(refs):
    return "\n".join([uri(refs, "legend.png"), uri(refs, "notes.txt"),
                      uri(refs, "sheet.pdf")])


def list_parameters(refs, **overrides):
    values = {
        "prompt": "Inspect.", "answer_format": "inspection", "model": NOVA,
        "region": "us-west-2", "reference_payload_path": three_lines(refs),
        "allowed_uri_prefixes": "file://" + refs + "/",
        "max_image_dimension": 1280, "on_error": "record",
    }
    values.update(overrides)
    return values


def block_kinds(kwargs):
    return [next(iter(block)) for block in kwargs["messages"][0]["content"]]


class RecordingInvoker:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.text


class Collector:
    def __init__(self):
        self.samples = []

    def enqueue(self, sample):
        self.samples.append(sample)


class StubBedrockClient:
    def __init__(self, recorder):
        self.recorder = recorder

    def converse(self, **kwargs):
        self.recorder.append(kwargs)
        return {"output": {"message": {"content": [{"text": answer()}]}}}


@pytest.fixture
def collector():
    exporter = Collector()
    set_sample_exporter(exporter)
    yield exporter
    set_sample_exporter(None)


@pytest.fixture
def converse(monkeypatch):
    """The default invoker's ``boto3.client``, recording every
    ``converse(**kwargs)``."""
    calls = []

    def client(service, region_name=None, config=None, **_):
        assert service == "bedrock-runtime"
        return StubBedrockClient(calls)

    monkeypatch.setattr(boto3, "client", client)
    return calls


def run(invoker, params, tmp_path):
    payload = {"order_id": "o-1"}
    trigger = {"topic": "plant/line1/evaluate", "payload": json.dumps(payload),
               "payload_json": dict(payload)}
    context = RunContext(
        tag_values={"trigger": trigger}, output_dir=str(tmp_path),
        capture_id="cap-1",
        node_status=NodeStatusCollector(extra_node_ids=[NODE_ID]))
    binding = {
        "nodeId": NODE_ID, "binding": "bedrock_inference",
        "parameters": dict(params), "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": [],
        "capturePaths": {"in": "{work_dir}/" + FRAME_NAME, "reference": None},
    }
    return BedrockInferenceProcessor(invoker=invoker).process(
        {"schemaVersion": 1, "executorBindings": [binding]},
        dict(context.tag_values), str(tmp_path), run_context=context,
        export_context=CONTEXT)


def test_the_list_form_request_carries_documents_and_request_fields(
        tmp_path, converse, collector):
    """One Converse request holds the list form's image and document blocks
    and the node's additionalModelRequestFields; the same node without the
    parameter sends the same request minus that one key."""
    write_frame(tmp_path)
    refs = reference_dir(tmp_path, legend_png=LEGEND_PNG, notes_txt=NOTES,
                         sheet_pdf=SHEET)
    run(None, list_parameters(refs), tmp_path)
    run(None, list_parameters(refs, **{FIELDS: TOP_K}), tmp_path)

    without, with_fields = converse
    assert block_kinds(with_fields) == LIST_BLOCKS
    assert with_fields["additionalModelRequestFields"] == TOP_K_OBJECT
    assert "additionalModelRequestFields" not in without
    assert {key: value for key, value in with_fields.items()
            if key != "additionalModelRequestFields"} == without


def test_every_attempt_carries_the_attachments_and_the_request_fields(
        tmp_path):
    """Under the attempt policy both attempts pass the invoker the policy
    options, the list form's attachments and the compact fields."""
    refs = reference_dir(tmp_path, legend_png=LEGEND_PNG, notes_txt=NOTES,
                         sheet_pdf=SHEET)
    clock = FakeClock()
    invoker = PolicyInvoker(clock, (1.0, RuntimeError("boom")), (1.0, GOOD))
    entry = run_policy(
        tmp_path, clock, invoker, reference_payload_path=three_lines(refs),
        allowed_uri_prefixes="file://" + refs + "/", **DEMO_POLICY,
        **{FIELDS: TOP_K})

    assert (entry["outcome"], entry["attempts"]) == ("pass", 2)
    first, second = invoker.calls
    assert first == second
    _args, options = first
    assert sorted(options) == sorted(
        list(NON_DEFAULT_OPTIONS) + ["attachments", FIELDS])
    assert options[FIELDS] == TOP_K_COMPACT
    assert [(item.label, item.kind) for item in options["attachments"]] == [
        ("Input image", "image"), ("Reference 1", "image"),
        ("Reference 2", "document"), ("Reference 3", "document")]


def test_a_single_legend_passes_only_the_request_fields_keyword(tmp_path):
    """A's form (one legend line): two images, positional as today, and the
    fields keyword; no attachments keyword."""
    refs = reference_dir(tmp_path, legend_jpg=LEGEND_JPEG)
    clock = FakeClock()
    invoker = PolicyInvoker(clock, (0.1, GOOD))
    entry = run_policy(
        tmp_path, clock, invoker, reference_payload_path=uri(refs, "legend.jpg"),
        allowed_uri_prefixes="file://" + refs + "/", **{FIELDS: TOP_K})

    assert entry["outcome"] == "pass"
    (args, options), = invoker.calls
    assert options == {FIELDS: TOP_K_COMPACT}
    assert [label for label, _data in args[2]] == [
        "Input image", "Reference image"]


def test_a_fields_value_that_is_not_an_object_stops_a_list_node_before_the_call(
        tmp_path, collector):
    """With references configured and loadable, a non-object value is still
    a pre-call failure: no Bedrock call, the error recorded, no sample."""
    write_frame(tmp_path)
    refs = reference_dir(tmp_path, legend_png=LEGEND_PNG, notes_txt=NOTES,
                         sheet_pdf=SHEET)
    invoker = RecordingInvoker(answer())
    metadata = run(invoker, list_parameters(refs, **{FIELDS: "[1, 2]"}),
                   tmp_path)

    entry = metadata["bedrock"][NODE_ID]
    assert invoker.calls == []
    assert (entry["outcome"], entry["attempts"]) == ("error", 0)
    assert ("additional_model_request_fields must be a JSON object, got list"
            in entry["error"])
    assert collector.samples == []


def test_the_exported_sample_is_the_same_with_or_without_request_fields(
        tmp_path, converse, collector):
    """The fields change the request only: both runs export the same
    sample (bytes sent, prompt fingerprint, snippet, references) and the
    same sidecar, and the exported bytes are the request's blocks."""
    write_frame(tmp_path)
    refs = reference_dir(tmp_path, legend_png=LEGEND_PNG, notes_txt=NOTES,
                         sheet_pdf=SHEET)
    run(None, list_parameters(refs), tmp_path)
    run(None, list_parameters(refs, **{FIELDS: TOP_K}), tmp_path)

    without, with_fields = collector.samples
    assert with_fields == without
    assert build_sidecar(with_fields, DEVICE, PREFIX, exported_at=1) == \
        build_sidecar(without, DEVICE, PREFIX, exported_at=1)
    sent = [next(iter(block.values()))["source"]["bytes"]
            for block in converse[1]["messages"][0]["content"]
            if "text" not in block]
    assert sent == [with_fields.input_bytes] + [
        item.data for item in with_fields.references]


def test_the_legacy_path_sends_a_fixed_reference_and_the_request_fields(
        tmp_path):
    """An anomaly-mode node with a fixed file:// reference: the invoker gets
    both images and the fields keyword, and the verdict is read."""
    write_frame(tmp_path)
    refs = reference_dir(tmp_path, legend_jpg=LEGEND_JPEG)
    invoker = RecordingInvoker(LEGACY_ANSWER)
    metadata = run(invoker, {
        "prompt": "Compare.", "model": NOVA, "anomaly_mode": True,
        "reference_payload_path": uri(refs, "legend.jpg"),
        "allowed_uri_prefixes": "file://" + refs + "/", FIELDS: TOP_K,
    }, tmp_path)

    (args, kwargs), = invoker.calls
    assert [label for label, _data in args[2]] == [
        "Input image", "Reference image"]
    assert kwargs == {FIELDS: TOP_K_COMPACT}
    assert metadata["is_anomalous"] is False


def test_the_default_invoker_sends_what_the_shared_builder_builds(stub_boto3):
    """A list with documents and fields: the transport's kwargs equal the
    shared Invocation_Builder's converse_kwargs()."""
    from workflow_engine.vendor.workflow_core.anomaly_invocation import (
        KIND_DOCUMENT,
        KIND_IMAGE,
        ReferenceFile,
    )
    parameters = {"model": NOVA, "region": "us-west-2", "prompt": "Inspect.",
                  "answer_format": "inspection", "system_prompt": "Be terse.",
                  "max_tokens": 512, FIELDS: TOP_K}
    invocation = build_bedrock_invocation(
        parameters, b"\xff\xd8input", references=[
            ReferenceFile(KIND_IMAGE, "jpeg", LEGEND_JPEG),
            ReferenceFile(KIND_DOCUMENT, "txt", NOTES),
            ReferenceFile(KIND_DOCUMENT, "pdf", SHEET)])
    _default_bedrock_invoker(
        invocation.model, invocation.prompt, list(invocation.images),
        invocation.region, invocation.max_tokens, invocation.system_prompt,
        attachments=invocation.attachments,
        additional_model_request_fields=(
            invocation.additional_model_request_fields))

    sent = stub_boto3["client"].kwargs[0]
    assert sent == invocation.converse_kwargs()
    assert sent["additionalModelRequestFields"] == TOP_K_OBJECT
    assert block_kinds(sent) == LIST_BLOCKS
