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
"""Device half of the inspection replay parity golden (AC-6, AC-6a).

The real ``BedrockInferenceProcessor`` runs an inspection-format node
with its DEFAULT invoker against a recording ``boto3.client`` stub, and
the real ``SampleExporter`` uploads the sample through a recording S3
stub. The clock, the execution ids and the device name are fixed. The
recorded region and Converse kwargs, the exact sidecar body the uploader
wrote and the digests of the uploaded images and documents are compared
with the committed golden
``edge-cv-portal/backend/tests/fixtures/inspection_replay_parity.json``,
which the Portal's replay is checked against in its own lane. Regenerate
it with ``DDA_WRITE_INSPECTION_PARITY_FIXTURE=1`` and review the diff.

Two cases share one node: ``single_legend`` (one legend line, as A's
node) and ``mixed_references`` (the legend, a text file and a PDF,
"Reference 1" to "Reference 3"). The trigger payload carries order items
whose keys are not sorted and the integral float ``2.0``, which the
prompt renders.

Substitutions, applied before both writing and comparing: each
reference's ``source`` (a temporary path) becomes
``file:///fixture/<file name>``, and every image or document block's
``source.bytes`` becomes ``{"sha256", "length"}``. ``objects`` maps every
uploaded image and document key (the sidecar is ``sidecarBody``) to its
``{"sha256", "length"}``.

At c299a6e1 the inspection path exports nothing:
``assert len(uploaded_sidecars) == 1`` fails before the golden compare.
"""
import hashlib
import json
import os
import shutil
import types

import boto3

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.node_status import NodeStatusCollector
from workflow_engine.output_bindings import (
    BedrockInferenceProcessor,
    RunContext,
)
from workflow_engine.tuning import sample_export as export_module
from workflow_engine.tuning.sample_export import (
    ExportConfig,
    ExportContext,
    SampleExporter,
    set_sample_exporter,
)

WRITE_ENV = "DDA_WRITE_INSPECTION_PARITY_FIXTURE"
STEM = "inspection_replay_parity"
FIXTURE_NAME = STEM + ".json"
REGENERATE = (
    "DDA_WRITE_INSPECTION_PARITY_FIXTURE=1 python -m pytest "
    "test/backend-test/workflow_engine/test_inspection_replay_parity_device.py"
    " (device lane), then review the diff")
FIXED_CLOCK = 1794510000
DEVICE = "device-under-test"
PREFIX = "workflow-tuning/samples/"
WORKFLOW_ID = "wf-parity"
VERSION = "7"
NODE_ID = "check"
CAPTURE_ID = "cap-parity"
FRAME_NAME = "bedrock_frame_camera.jpg"
SUBSTITUTED_ROOT = "file:///fixture/"

#: The node of design §20.2, without its two reference parameters.
NODE_PARAMETERS = {
    "answer_format": "inspection",
    "render_prompt": True,
    "box_scale": "pixels",
    "max_image_dimension": 1280,
    "max_tokens": 600,
    "model": "us.amazon.nova-pro-v1:0",
    "region": "us-west-2",
    "prompt": ("Order {trigger.payload_json.order_id}: the parts "
               "{json:trigger.payload_json.body.order.items} at scale "
               "{trigger.payload_json.body.scale}, on an image "
               "{image.width} px wide."),
    "system_prompt": "You inspect stage {trigger.payload_json.body.qc_stage}.",
    "expected_count_path": "trigger.payload_json.body.order.items",
}
PAYLOAD = {
    "order_id": "o-1",
    "body": {
        "qc_stage": "qc2",
        "scale": 2.0,
        "order": {"items": [
            {"shape": "roof", "color": "red"},
            {"shape": "panel", "color": "blue"},
        ]},
    },
}
#: (case name, execution id, reference files in list order).
CASES = (
    ("single_legend", "exec-single-legend", (STEM + ".legend.jpg",)),
    ("mixed_references", "exec-mixed-references",
     (STEM + ".legend.jpg", STEM + ".notes.txt", STEM + ".sheet.pdf")),
)
ANSWER = json.dumps({
    "verdict": "pass", "confidence": 0.91, "reason": "both parts match",
    "objects": [
        {"name": "roof", "qc": "OK", "reason": "red, left",
         "bounding_box": {"x_min": 60, "y_min": 80, "x_max": 260,
                          "y_max": 220}},
        {"name": "panel", "qc": "OK", "reason": "blue, right",
         "bounding_box": {"x_min": 340, "y_min": 200, "x_max": 580,
                          "y_max": 400}},
    ],
})


def fixtures_dir():
    """``edge-cv-portal/backend/tests/fixtures``, found by walking up from
    this file to the repository root."""
    here = os.path.dirname(os.path.abspath(__file__))
    current = here
    while True:
        candidate = os.path.join(
            current, "edge-cv-portal", "backend", "tests", "fixtures")
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(current)
        assert parent != current, (
            "no edge-cv-portal/backend/tests/fixtures above " + here)
        current = parent


def digest(data):
    return {"sha256": hashlib.sha256(data).hexdigest(), "length": len(data)}


def substitute_bytes(value):
    """``value`` with every ``source.bytes`` replaced by its digest."""
    if isinstance(value, dict):
        return {key: (digest(item) if key == "bytes"
                      and isinstance(item, (bytes, bytearray))
                      else substitute_bytes(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_bytes(item) for item in value]
    return value


class RecordingBedrock:
    def __init__(self):
        self.regions = []
        self.calls = []

    def client(self, service, region_name=None, config=None, **_):
        assert service == "bedrock-runtime"
        self.regions.append(region_name)
        return self

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"role": "assistant",
                                       "content": [{"text": ANSWER}]}}}


class RecordingS3:
    def __init__(self):
        self.puts = []

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None,
                   **_):
        self.puts.append((Key, bytes(Body)))
        return {}


def run_case(tmp_path, monkeypatch, name, execution_id, references):
    """Run one case; returns ``(bedrock, s3, refs_dir)``."""
    fixtures = fixtures_dir()
    work_dir = tmp_path / name
    refs_dir = work_dir / "refs"
    refs_dir.mkdir(parents=True)
    shutil.copyfile(os.path.join(fixtures, STEM + ".input.jpg"),
                    str(work_dir / FRAME_NAME))
    for file_name in references:
        shutil.copyfile(os.path.join(fixtures, file_name),
                        str(refs_dir / file_name))
    parameters = dict(NODE_PARAMETERS)
    parameters["reference_payload_path"] = "\n".join(
        "file://{0}/{1}".format(refs_dir, file_name)
        for file_name in references)
    parameters["allowed_uri_prefixes"] = "file://{0}/".format(refs_dir)

    bedrock = RecordingBedrock()
    monkeypatch.setattr(boto3, "client", bedrock.client)
    monkeypatch.setattr(export_module, "time", types.SimpleNamespace(
        time=lambda: FIXED_CLOCK, sleep=lambda seconds: None))
    s3 = RecordingS3()
    exporter = SampleExporter(
        ExportConfig(bucket="tuning-samples-test", prefix=PREFIX),
        s3_factory=lambda: s3, thing_name=DEVICE)
    set_sample_exporter(exporter)
    try:
        trigger = {"topic": "plant/line1/evaluate",
                   "payload": json.dumps(PAYLOAD),
                   "payload_json": json.loads(json.dumps(PAYLOAD)), "qos": 1}
        context = RunContext(
            tag_values={"trigger": trigger}, output_dir=str(work_dir),
            capture_id=CAPTURE_ID,
            node_status=NodeStatusCollector(extra_node_ids=[NODE_ID]))
        binding = {
            "nodeId": NODE_ID, "binding": "bedrock_inference",
            "parameters": parameters, "upstreamNodeIds": ["cam"],
            "downstreamNodeIds": [],
            "capturePaths": {"in": "{work_dir}/" + FRAME_NAME,
                             "reference": None},
        }
        BedrockInferenceProcessor().process(
            {"schemaVersion": 1, "executorBindings": [binding]},
            dict(context.tag_values), str(work_dir), run_context=context,
            export_context=ExportContext(
                workflow_id=WORKFLOW_ID, version=VERSION,
                execution_id=execution_id))
        assert exporter.wait_idle(10.0)
    finally:
        exporter.stop(5.0)
        set_sample_exporter(None)
    return bedrock, s3, str(refs_dir)


def case_record(name, bedrock, s3, refs_dir):
    uploaded_sidecars = [body for key, body in s3.puts if key.endswith(".json")]
    assert len(uploaded_sidecars) == 1
    body = uploaded_sidecars[0].decode("utf-8")
    body = body.replace("file://" + refs_dir + "/", SUBSTITUTED_ROOT)
    assert refs_dir not in body
    (kwargs,) = bedrock.calls
    (region,) = bedrock.regions
    return {
        "name": name,
        "sidecarBody": body,
        "objects": {key: digest(data) for key, data in s3.puts
                    if not key.endswith(".json")},
        "region": region,
        "converseKwargs": substitute_bytes(kwargs),
    }


def test_device_request_and_sidecar_match_the_golden(tmp_path, monkeypatch):
    cases = []
    for name, execution_id, references in CASES:
        bedrock, s3, refs_dir = run_case(
            tmp_path, monkeypatch, name, execution_id, references)
        cases.append(case_record(name, bedrock, s3, refs_dir))
    document = {"schema": 1, "regenerateWith": REGENERATE, "cases": cases}
    encoded = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    path = os.path.join(fixtures_dir(), FIXTURE_NAME)
    if os.environ.get(WRITE_ENV) == "1":
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(encoded)
    assert os.path.exists(path), (
        "{0} is missing; generate it with {1}=1".format(path, WRITE_ENV))
    with open(path, "r", encoding="utf-8") as handle:
        committed = json.load(handle)
    assert committed == json.loads(encoded), (
        "the device's inspection request or sample drifted from {0}; "
        "regenerate it with {1}=1 and review the diff".format(
            FIXTURE_NAME, WRITE_ENV))

    single, mixed = cases
    sidecar = json.loads(single["sidecarBody"])
    assert sidecar["reference"]["source"] == (
        SUBSTITUTED_ROOT + STEM + ".legend.jpg")
    snippet = json.loads(sidecar["metadataSnippetJson"])
    assert list(snippet["trigger"]["payload_json"]["body"]["order"]["items"]
                [0]) == ["shape", "color"]
    assert isinstance(snippet["trigger"]["payload_json"]["body"]["scale"],
                      float)
    content = mixed["converseKwargs"]["messages"][0]["content"]
    assert [next(iter(block)) for block in content] == [
        "text", "text", "image", "text", "image", "text", "document", "text",
        "document"]
    assert "at scale 2.0, on an image" in content[0]["text"]
