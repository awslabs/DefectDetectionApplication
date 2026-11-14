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
"""The device reads reference files uploaded in the workflow editor from
the workflow version's directory (AC-41, and ``read_packaged_reference``).

A temporary package directory (``manifest.json`` with ``referenceFiles``
and ``references/{sha256}.{ext}``) is the run's
``RunContext.artifact_path``; the real ``BedrockInferenceProcessor`` runs
with a scripted invoker or the default invoker on a stubbed
``boto3.client``. The executor test drives ``WorkflowExecutor.execute``
with a spy processor, as test_workflow_executor_detections.py does. No
AWS call, no network.

At c299a6e1 ``RunContext`` has no ``artifact_path`` (``TypeError``), the
inspection path never reads the list, and ``read_packaged_reference``
does not exist.
"""
import hashlib
import io
import json
import os
import time
from unittest.mock import patch

import boto3
import cv2
import numpy as np
import pytest
from PIL import Image

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import gst_plugins, pipeline_executor
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.node_status import NodeStatusCollector
from workflow_engine.output_bindings import (
    BedrockInferenceProcessor,
    RunContext,
)
from workflow_engine.payload_fetch import PayloadReferenceError
from workflow_engine.pipeline_executor import WorkflowExecutor
from workflow_engine.tuning.sample_export import (
    ExportContext,
    build_sidecar,
    set_sample_exporter,
)

FRAME_NAME = "bedrock_frame_camera.jpg"
CAPTURE_ID = "cap-1"
NODE_ID = "check"
NOVA = "us.amazon.nova-pro-v1:0"
CONTEXT = ExportContext(workflow_id="wf-1", version="3", execution_id="exec-1")
PAYLOAD = {"order_id": "o-1"}
SHEET = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
LEGACY_ANSWER = '{"is_anomalous": false, "confidence": 0.9}'


def encode(image, fmt):
    buffer = io.BytesIO()
    image.save(buffer, fmt)
    return buffer.getvalue()


LEGEND_PNG = encode(Image.new("RGB", (240, 160), (20, 120, 220)), "PNG")
LEGEND_JPEG = encode(Image.new("RGB", (240, 160), (220, 120, 20)), "JPEG")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def upload_name(data, ext):
    return "{0}.{1}".format(sha(data), ext)


def make_package(tmp_path, files, manifest=True):
    """A workflow version directory holding ``files`` ({ext: bytes})
    under references/ and listing them in manifest.json; returns
    ``(artifact_path, {ext: name})``."""
    artifact_path = tmp_path / "pkg"
    (artifact_path / "references").mkdir(parents=True, exist_ok=True)
    names = {}
    listing = {}
    for ext, data in files.items():
        name = upload_name(data, ext)
        names[ext] = name
        (artifact_path / "references" / name).write_bytes(data)
        listing["references/" + name] = {"sha256": sha(data),
                                         "bytes": len(data)}
    if manifest:
        (artifact_path / "manifest.json").write_text(json.dumps(
            {"workflowId": "wf-1", "referenceFiles": listing}))
    return str(artifact_path), names


def answer():
    return json.dumps({"verdict": "pass", "confidence": 0.9, "reason": "ok",
                       "objects": [{"name": "part", "qc": "OK", "reason": "r",
                                    "bounding_box": {"x_min": 1, "y_min": 1,
                                                     "x_max": 9, "y_max": 9}}]})


class ScriptedInvoker:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, *args, **options):
        self.calls.append((args, options))
        return self.script.pop(0) if self.script else answer()


class Collector:
    def __init__(self):
        self.samples = []

    def enqueue(self, sample):
        self.samples.append(sample)


@pytest.fixture
def collector():
    exporter = Collector()
    set_sample_exporter(exporter)
    yield exporter
    set_sample_exporter(None)


def write_frame(directory):
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    frame[:, :, 0] = 200
    path = os.path.join(str(directory), FRAME_NAME)
    assert cv2.imwrite(path, frame)


def parameters(lines, **overrides):
    values = {"prompt": "Inspect.", "answer_format": "inspection",
              "model": NOVA, "region": "us-west-2",
              "reference_payload_path": "\n".join(lines),
              "allowed_uri_prefixes": "", "max_image_dimension": 1280,
              "on_error": "record"}
    values.update(overrides)
    return values


def make_context(tmp_path, **fields):
    trigger = {"topic": "plant/line1/evaluate",
               "payload": json.dumps(PAYLOAD), "payload_json": dict(PAYLOAD)}
    return RunContext(
        tag_values={"trigger": trigger}, output_dir=str(tmp_path),
        capture_id=CAPTURE_ID,
        node_status=NodeStatusCollector(extra_node_ids=[NODE_ID]), **fields)


def run(invoker, params, tmp_path, context):
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


# ---------------------------------------------------------------------------
# read_packaged_reference: the six checks
# ---------------------------------------------------------------------------

def test_read_packaged_reference_returns_the_verified_bytes(tmp_path):
    from workflow_engine.payload_fetch import read_packaged_reference
    artifact_path, names = make_package(tmp_path, {"png": LEGEND_PNG})
    with open(os.path.join(artifact_path, "manifest.json")) as handle:
        manifest = json.load(handle)
    assert read_packaged_reference(
        artifact_path, names["png"], manifest, 8 * 1024 * 1024) == LEGEND_PNG


def _broken_package(tmp_path, case):
    """``(artifact_path, name, manifest, expected message)``."""
    artifact_path, names = make_package(tmp_path, {"png": LEGEND_PNG})
    name = names["png"]
    line = "upload://" + name
    with open(os.path.join(artifact_path, "manifest.json")) as handle:
        manifest = json.load(handle)
    entry = manifest["referenceFiles"]["references/" + name]
    if case == "no_artifact_path":
        return None, name, manifest, (
            "this run has no workflow package directory, so uploaded "
            "reference files cannot be read")
    if case == "no_manifest":
        return artifact_path, name, None, (
            "could not read the workflow package's manifest.json")
    if case == "not_listed":
        manifest["referenceFiles"] = {}
        return artifact_path, name, manifest, (
            "the workflow package's manifest.json does not list uploaded "
            "reference file " + line)
    if case == "listed_digest_differs":
        entry["sha256"] = "0" * 64
        return artifact_path, name, manifest, (
            "the workflow package's manifest.json does not list uploaded "
            "reference file " + line)
    path = os.path.join(artifact_path, "references", name)
    if case == "missing":
        os.remove(path)
        return artifact_path, name, manifest, (
            "uploaded reference file {0} is missing from the workflow "
            "package".format(line))
    if case == "symlink_outside":
        outside = tmp_path / "outside.png"
        outside.write_bytes(LEGEND_PNG)
        os.remove(path)
        os.symlink(str(outside), path)
        return artifact_path, name, manifest, (
            "uploaded reference file {0} is missing from the workflow "
            "package".format(line))
    if case == "not_regular":
        os.remove(path)
        os.mkdir(path)
        return artifact_path, name, manifest, (
            "uploaded reference file {0} is not a regular file".format(line))
    if case == "size_differs":
        entry["bytes"] = len(LEGEND_PNG) + 1
        return artifact_path, name, manifest, (
            "uploaded reference file {0} is {1} bytes; the manifest records "
            "{2}".format(line, len(LEGEND_PNG), len(LEGEND_PNG) + 1))
    tampered = bytearray(LEGEND_PNG)
    tampered[-1] ^= 0xFF
    with open(path, "wb") as handle:
        handle.write(bytes(tampered))
    return artifact_path, name, manifest, (
        "uploaded reference file {0} does not match its checksum: manifest "
        "records {1}, file is {2}".format(line, sha(LEGEND_PNG),
                                          sha(bytes(tampered))))


PACKAGE_FAILURES = [
    "no_artifact_path", "no_manifest", "not_listed", "listed_digest_differs",
    "missing", "symlink_outside", "not_regular", "size_differs",
    "bytes_differ"]


@pytest.mark.parametrize("case", PACKAGE_FAILURES)
def test_read_packaged_reference_failures(tmp_path, case):
    from workflow_engine.payload_fetch import read_packaged_reference
    artifact_path, name, manifest, expected = _broken_package(tmp_path, case)
    with pytest.raises(PayloadReferenceError) as raised:
        read_packaged_reference(artifact_path, name, manifest, 8 * 1024 * 1024)
    assert str(raised.value) == expected


def test_read_packaged_reference_applies_the_size_cap(tmp_path):
    from workflow_engine.payload_fetch import read_packaged_reference
    artifact_path, names = make_package(tmp_path, {"pdf": SHEET})
    with open(os.path.join(artifact_path, "manifest.json")) as handle:
        manifest = json.load(handle)
    with pytest.raises(PayloadReferenceError) as raised:
        read_packaged_reference(artifact_path, names["pdf"], manifest,
                                len(SHEET) - 1)
    assert str(raised.value) == (
        "uploaded reference file upload://{0} exceeds the {1}-byte size "
        "cap".format(names["pdf"], len(SHEET) - 1))


# ---------------------------------------------------------------------------
# AC-41: the processor reads packaged files
# ---------------------------------------------------------------------------

class StubBedrockClient:
    def __init__(self, recorder):
        self.recorder = recorder

    def converse(self, **kwargs):
        self.recorder.append(kwargs)
        return {"output": {"message": {"content": [{"text": answer()}]}}}


def test_an_uploaded_png_and_pdf_are_sent_from_the_package(
        tmp_path, monkeypatch):
    artifact_path, names = make_package(
        tmp_path, {"png": LEGEND_PNG, "pdf": SHEET})
    write_frame(tmp_path)
    converse = []
    monkeypatch.setattr(boto3, "client",
                        lambda *args, **kwargs: StubBedrockClient(converse))
    lines = ["upload://" + names["png"], "upload://" + names["pdf"]]
    metadata = run(None, parameters(lines), tmp_path,
                   make_context(tmp_path, artifact_path=artifact_path))

    from workflow_engine.output_bindings import prepare_reference_image
    assert metadata["bedrock"][NODE_ID]["outcome"] == "pass"
    (kwargs,) = converse
    content = kwargs["messages"][0]["content"]
    assert content[3] == {"text": "Reference 1:"}
    assert content[4] == {"image": {"format": "jpeg", "source": {
        "bytes": prepare_reference_image(LEGEND_PNG, 1280)[0]}}}
    assert content[5] == {"text": "Reference 2:"}
    assert content[6] == {"document": {"format": "pdf", "name": "Reference 2",
                                       "source": {"bytes": SHEET}}}
    assert len(content) == 7


def test_a_single_uploaded_jpeg_sends_two_images_and_records_its_line(
        tmp_path, collector):
    artifact_path, names = make_package(tmp_path, {"jpg": LEGEND_JPEG})
    write_frame(tmp_path)
    invoker = ScriptedInvoker()
    line = "upload://" + names["jpg"]
    run(invoker, parameters([line]), tmp_path,
        make_context(tmp_path, artifact_path=artifact_path))

    (args, _), = invoker.calls
    request_images = args[2]
    assert len(request_images) == 2
    assert [label for label, _ in request_images] == [
        "Input image", "Reference image"]
    assert request_images[1][1] == LEGEND_JPEG
    (sample,) = collector.samples
    reference = build_sidecar(sample, "device-under-test", "p/")["reference"]
    assert reference["source"] == line
    assert reference["sourceSha256"] == names["jpg"].split(".")[0]
    assert reference["transform"] == []


@pytest.mark.parametrize("case", PACKAGE_FAILURES[:1] + ["unreadable_manifest"]
                         + PACKAGE_FAILURES[2:])
def test_a_package_failure_makes_no_call_and_names_the_line(
        tmp_path, collector, case):
    """Each failure fails before the call, with no call, no sample and an
    error naming the uploaded line (the list form names every entry)."""
    if case == "unreadable_manifest":
        artifact_path, names = make_package(tmp_path, {"png": LEGEND_PNG})
        with open(os.path.join(artifact_path, "manifest.json"), "w") as handle:
            handle.write("{not json")
        name = names["png"]
        reason = "could not read the workflow package's manifest.json"
    else:
        artifact_path, name, manifest, reason = _broken_package(tmp_path, case)
        if manifest is not None and artifact_path is not None:
            with open(os.path.join(artifact_path, "manifest.json"), "w") as out:
                json.dump(manifest, out)
    sheet = tmp_path / "pkg" / "references" / upload_name(SHEET, "pdf")
    sheet.write_bytes(SHEET)
    write_frame(tmp_path)
    fields = {} if artifact_path is None else {"artifact_path": artifact_path}
    invoker = ScriptedInvoker()
    line = "upload://" + name
    metadata = run(invoker, parameters(
        [line, "upload://" + upload_name(SHEET, "pdf")]), tmp_path,
        make_context(tmp_path, **fields))

    assert invoker.calls == []
    assert collector.samples == []
    assert metadata["bedrock"][NODE_ID]["error"] == (
        "could not load reference 1 of 2 ({0}): {1}".format(line, reason))


def test_the_manifest_is_read_once_per_call_and_only_for_upload_lines(
        tmp_path, monkeypatch):
    reads = []
    original = BedrockInferenceProcessor._read_package_manifest

    def counting(artifact_path):
        reads.append(artifact_path)
        return original(artifact_path)

    monkeypatch.setattr(BedrockInferenceProcessor, "_read_package_manifest",
                        staticmethod(counting))
    artifact_path, names = make_package(
        tmp_path, {"png": LEGEND_PNG, "pdf": SHEET})
    write_frame(tmp_path)
    invoker = ScriptedInvoker()
    run(invoker, parameters(
        ["upload://" + names["png"], "upload://" + names["pdf"]]), tmp_path,
        make_context(tmp_path, artifact_path=artifact_path))
    assert reads == [artifact_path]

    legend = tmp_path / "images"
    legend.mkdir()
    (legend / "legend.jpg").write_bytes(LEGEND_JPEG)
    run(invoker, parameters(
        ["file://" + str(legend) + "/legend.jpg"],
        allowed_uri_prefixes="file://" + str(legend) + "/"), tmp_path,
        make_context(tmp_path, artifact_path=artifact_path))
    assert reads == [artifact_path]
    assert len(invoker.calls) == 2


def test_a_legacy_node_with_one_uploaded_image_sends_two_images(tmp_path):
    artifact_path, names = make_package(tmp_path, {"png": LEGEND_PNG})
    write_frame(tmp_path)
    invoker = ScriptedInvoker(LEGACY_ANSWER)
    params = {"prompt": "Compare.", "model": NOVA, "region": "us-west-2",
              "reference_payload_path": "upload://" + names["png"]}
    metadata = run(invoker, params, tmp_path,
                   make_context(tmp_path, artifact_path=artifact_path))
    assert "error" not in metadata["bedrock"][NODE_ID]
    (args, _), = invoker.calls
    assert [label for label, _ in args[2]] == ["Input image", "Reference image"]
    sent = args[2][1][1]
    assert sent[:3] == b"\xff\xd8\xff"
    with Image.open(io.BytesIO(sent)) as decoded:
        assert decoded.size == (240, 160)


# ---------------------------------------------------------------------------
# AC-41: the executor passes the registration's version directory
# ---------------------------------------------------------------------------

class SpyBedrockProcessor:
    def __init__(self):
        self.calls = []

    def bindings(self, document):
        return [binding for binding in (document.get("executorBindings") or [])
                if binding.get("binding") == "bedrock_inference"]

    def process(self, document, tag_values, work_dir, run_context=None):
        self.calls.append(run_context)
        return dict(tag_values)


class CapturingPipelineManager:
    def run_pipeline(self, pipeline_str, frame_data=None,
                     latency_metrics=None, status_sink=None):
        for token in pipeline_str.split():
            if token.startswith("location=") and token.endswith(".jpg"):
                with open(token[len("location="):], "wb") as handle:
                    handle.write(b"\xff\xd8fake-input-jpeg\xff\xd9")
        return {}


def _document():
    return {
        "schemaVersion": 1, "workflowId": "wf-1", "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [{
            "name": "s0", "from": None, "linkTo": None,
            "elements": [
                {"nodeId": "cam", "factory": "videotestsrc", "args": {}},
                {"nodeId": None, "factory": "videoconvert", "args": {}},
                {"nodeId": None, "factory": "jpegenc", "args": {}},
                {"nodeId": None, "factory": "multifilesink",
                 "args": {"location": "{work_dir}/bedrock_frame_cam.jpg"}},
            ],
        }],
        "executorBindings": [{
            "nodeId": "b1", "binding": "bedrock_inference",
            "parameters": {"model": NOVA, "prompt": "Compare."},
            "upstreamNodeIds": ["cam"], "downstreamNodeIds": [],
            "capturePaths": {"in": "{work_dir}/bedrock_frame_cam.jpg"},
        }],
        "pluginDependencies": [],
    }


def test_the_executor_passes_the_registrations_artifact_path(tmp_path):
    session_factory = make_session_factory()
    artifact_path = write_artifact_set(tmp_path / "art", compiled=_document())
    session = session_factory()
    try:
        session.add(WorkflowRegistration(
            id="wf-1:3", workflow_id="wf-1", version="3", arch=DEVICE_ARCH,
            artifact_path=str(artifact_path), status="registered",
            registered_at=int(time.time())))
        session.add(WorkflowExecution(
            id="exec-1", registration_id="wf-1:3",
            started_at=int(time.time()), status="pending"))
        session.commit()
    finally:
        session.close()
    spy = SpyBedrockProcessor()
    with patch.object(gst_plugins, "_scan_registry", return_value=True), \
            patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT",
                         os.path.join(str(tmp_path), "captures")):
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: CapturingPipelineManager(),
            post_run_handler=lambda reg, doc, tags: None,
            bedrock_processor=spy,
        ).execute("exec-1")

    (run_context,) = spy.calls
    assert run_context.artifact_path == str(artifact_path)
    assert run_context.capture_id == "wf-1-exec-1"
