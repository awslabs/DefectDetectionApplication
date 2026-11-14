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
"""Several reference files per inspection node: images and documents
(AC-28 to AC-33 device halves, AC-28a, AC-28b, AC-29 device half).

The real ``BedrockInferenceProcessor`` with the default invoker on a
stubbed ``boto3.client`` (or on a real bedrock-runtime client under
``botocore.stub.Stubber``), or with an injected invoker; a collector
exporter or the real ``SampleExporter`` on a dict-backed S3 stub. Files
live in a temporary directory allowed by a ``file://`` prefix. No AWS
call, no network.

At c299a6e1 the inspection path never reads the list (one image is
sent), the legacy path reads a list as one dotted path, and the
document fetcher does not exist.
"""
import base64
import hashlib
import io
import json
import logging
import os

import boto3
import botocore.session
import cv2
import numpy as np
import pytest
from botocore.stub import Stubber
from PIL import Image

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine import output_bindings
from workflow_engine.node_status import NodeStatusCollector
from workflow_engine.output_bindings import (
    BedrockInferenceError,
    BedrockInferenceProcessor,
    RunContext,
)
from workflow_engine.payload_fetch import PayloadReferenceError
from workflow_engine.tuning.sample_export import (
    ExportConfig,
    ExportContext,
    ExportedSample,
    SampleExporter,
    set_sample_exporter,
)
from workflow_engine.vendor.workflow_core.anomaly_invocation import (
    INSPECTION_JSON_INSTRUCTION,
    build_bedrock_invocation,
)

FRAME_NAME = "bedrock_frame_camera.jpg"
CAPTURE_ID = "cap-1"
NODE_ID = "check"
NOVA = "us.amazon.nova-pro-v1:0"
PREFIX = "workflow-tuning/samples/"
DEVICE = "device-under-test"
CONTEXT = ExportContext(workflow_id="wf-1", version="3", execution_id="exec-1")
PAYLOAD = {"order_id": "o-1"}
NOTES = "Reference 1 is the legend. Judge only the Input image.\n".encode()
SHEET = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
LEGACY_ANSWER = '{"is_anomalous": false, "confidence": 0.9}'


def answer():
    return json.dumps({"verdict": "pass", "confidence": 0.9, "reason": "ok",
                       "objects": [{"name": "part", "qc": "OK", "reason": "r",
                                    "bounding_box": {"x_min": 1, "y_min": 1,
                                                     "x_max": 9, "y_max": 9}}]})


def write_frame(directory, width=64, height=48):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 1] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    path = os.path.join(str(directory), FRAME_NAME)
    assert cv2.imwrite(path, frame)
    with open(path, "rb") as handle:
        return handle.read()


def encode(image, fmt, **options):
    buffer = io.BytesIO()
    image.save(buffer, fmt, **options)
    return buffer.getvalue()


LEGEND_PNG = encode(Image.new("RGB", (400, 300), (250, 240, 10)), "PNG")
LEGEND_JPEG = encode(Image.new("RGB", (400, 300), (250, 240, 10)), "JPEG")


def reference_dir(tmp_path, **files):
    refs = tmp_path / "refs"
    refs.mkdir(exist_ok=True)
    for name, data in files.items():
        (refs / name.replace("_", ".")).write_bytes(data)
    return str(refs)


def uri(refs, name):
    return "file://" + refs + "/" + name


def parameters(refs, lines, **overrides):
    values = {
        "prompt": "Inspect.", "answer_format": "inspection", "model": NOVA,
        "region": "us-west-2", "reference_payload_path": "\n".join(lines),
        "allowed_uri_prefixes": "file://" + refs + "/",
        "max_image_dimension": 1280, "on_error": "record",
    }
    values.update(overrides)
    return values


def three_lines(refs):
    return [uri(refs, "legend.png"), uri(refs, "notes.txt"),
            uri(refs, "sheet.pdf")]


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


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.puts = []

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None,
                   **_):
        self.puts.append((Key, ContentType))
        self.objects[Key] = Body
        return {}


class StubBedrockClient:
    def __init__(self, recorder, text):
        self.recorder = recorder
        self.text = text

    def converse(self, **kwargs):
        self.recorder.append(kwargs)
        return {"output": {"message": {"content": [{"text": self.text}]}}}


@pytest.fixture
def collector():
    exporter = Collector()
    set_sample_exporter(exporter)
    yield exporter
    set_sample_exporter(None)


@pytest.fixture
def converse(monkeypatch):
    """The default invoker's ``boto3.client``, recording every
    ``converse(**kwargs)`` and the region it was built for."""
    calls = []
    regions = []

    def client(service, region_name=None, config=None, **_):
        assert service == "bedrock-runtime"
        regions.append(region_name)
        return StubBedrockClient(calls, answer())

    monkeypatch.setattr(boto3, "client", client)
    return calls


def run(invoker, params, tmp_path, payload=None, export_context=CONTEXT):
    payload = PAYLOAD if payload is None else payload
    trigger = {"topic": "plant/line1/evaluate", "payload": json.dumps(payload),
               "payload_json": json.loads(json.dumps(payload))}
    context = RunContext(
        tag_values={"trigger": trigger}, output_dir=str(tmp_path),
        capture_id=CAPTURE_ID,
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
        export_context=export_context)


def sha(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# AC-29 (device half): kinds and document checks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,data,fmt,expected", [
    ("bad.pdf", b"not a pdf", "pdf",
     "is not a PDF (it does not start with %PDF-)"),
    ("bad.docx", b"not a zip", "docx",
     "is not a DOCX/XLSX file (it is not a ZIP archive)"),
    ("bad.doc", b"not ole2", "doc",
     "is not a DOC/XLS file (it is not an OLE2 compound file)"),
    ("bad.txt", b"\xff\xfe\xfa\xfb", "txt", "is not UTF-8 text"),
    ("empty.md", b" \n\t\n", "md", "is empty"),
], ids=["pdf", "docx", "doc", "txt", "empty_md"])
def test_document_checks_name_the_reference_and_the_check(
        tmp_path, name, data, fmt, expected):
    from workflow_engine.payload_fetch import fetch_reference_document
    refs = reference_dir(tmp_path)
    (tmp_path / "refs" / name).write_bytes(data)
    with pytest.raises(PayloadReferenceError) as raised:
        fetch_reference_document(uri(refs, name), fmt, ["file://" + refs + "/"])
    assert str(raised.value) == "reference '{0}' {1}".format(
        uri(refs, name), expected)


def test_a_utf8_csv_with_a_byte_order_mark_passes(tmp_path):
    from workflow_engine.payload_fetch import fetch_reference_document
    data = b"\xef\xbb\xbfshape,color\nroof,red\n"
    refs = reference_dir(tmp_path, parts_csv=data)
    assert fetch_reference_document(
        uri(refs, "parts.csv"), "csv", ["file://" + refs + "/"]) == data


def test_documents_go_through_the_reference_gates(tmp_path):
    from workflow_engine.payload_fetch import (
        MAX_DOCUMENT_BYTES,
        fetch_reference_document,
    )
    refs = reference_dir(tmp_path, sheet_pdf=SHEET)
    with pytest.raises(PayloadReferenceError) as raised:
        fetch_reference_document(uri(refs, "sheet.pdf"), "pdf", [])
    assert "requires an explicit file:// entry" in str(raised.value)
    with pytest.raises(PayloadReferenceError) as raised:
        fetch_reference_document(
            uri(refs, "sheet.pdf"), "pdf", ["file:///nonexistent-root/"])
    assert "outside the node's allowed file:// prefixes" in str(raised.value)
    (tmp_path / "refs" / "big.txt").write_bytes(b"a" * (MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(PayloadReferenceError) as raised:
        fetch_reference_document(
            uri(refs, "big.txt"), "txt", ["file://" + refs + "/"])
    assert "exceeds the 4500000-byte size cap" in str(raised.value)


def test_a_payload_line_named_like_a_pdf_is_an_image(tmp_path):
    write_frame(tmp_path)
    payload = {"refs": [{"pdf": base64.b64encode(LEGEND_JPEG).decode()}]}
    invoker = ScriptedInvoker()
    run(invoker, parameters("/unused", ["refs.0.pdf"], allowed_uri_prefixes=""),
        tmp_path, payload=payload)
    (args, options), = invoker.calls
    assert [label for label, _ in args[2]] == ["Input image", "Reference image"]
    assert args[2][1][1] == LEGEND_JPEG
    assert "attachments" not in options


# ---------------------------------------------------------------------------
# AC-28 / AC-28a: order, labels and blocks through the default invoker
# ---------------------------------------------------------------------------

def test_an_image_txt_pdf_list_sends_nine_blocks(tmp_path, converse):
    refs = reference_dir(tmp_path, legend_png=LEGEND_PNG, notes_txt=NOTES,
                         sheet_pdf=SHEET)
    frame = write_frame(tmp_path)
    run(None, parameters(refs, three_lines(refs)), tmp_path)

    (kwargs,) = converse
    content = kwargs["messages"][0]["content"]
    assert len(content) == 9
    from workflow_engine.output_bindings import prepare_reference_image
    assert content[0] == {"text": "Inspect.\n\n" + INSPECTION_JSON_INSTRUCTION}
    assert content[1] == {"text": "Input image:"}
    assert content[2] == {"image": {"format": "jpeg",
                                    "source": {"bytes": frame}}}
    assert content[3] == {"text": "Reference 1:"}
    prepared, transform, _ = prepare_reference_image(LEGEND_PNG, 1280)
    assert transform == ("reencoded_jpeg",)
    assert content[4] == {"image": {"format": "jpeg",
                                    "source": {"bytes": prepared}}}
    assert content[5] == {"text": "Reference 2:"}
    assert content[6] == {"document": {"format": "txt", "name": "Reference 2",
                                       "source": {"bytes": NOTES}}}
    assert content[7] == {"text": "Reference 3:"}
    assert content[8] == {"document": {"format": "pdf", "name": "Reference 3",
                                       "source": {"bytes": SHEET}}}
    documents = [block for block in content if "document" in block]
    assert len(documents) == 2


def test_one_image_line_is_todays_two_image_request(tmp_path, converse):
    refs = reference_dir(tmp_path, legend_jpg=LEGEND_JPEG)
    frame = write_frame(tmp_path)
    params = parameters(refs, [uri(refs, "legend.jpg")])
    run(None, params, tmp_path)

    (kwargs,) = converse
    expected = build_bedrock_invocation(params, frame, LEGEND_JPEG)
    assert kwargs == expected.converse_kwargs()
    assert kwargs["messages"][0]["content"][3] == {"text": "Reference image:"}


def test_one_pdf_line_is_reference_1_and_a_document_block(tmp_path, converse):
    refs = reference_dir(tmp_path, sheet_pdf=SHEET)
    write_frame(tmp_path)
    run(None, parameters(refs, [uri(refs, "sheet.pdf")]), tmp_path)

    (kwargs,) = converse
    content = kwargs["messages"][0]["content"]
    assert content[3:] == [
        {"text": "Reference 1:"},
        {"document": {"format": "pdf", "name": "Reference 1",
                      "source": {"bytes": SHEET}}},
    ]


def test_blank_lines_and_spaces_give_the_trimmed_request(tmp_path, converse):
    refs = reference_dir(tmp_path, legend_jpg=LEGEND_JPEG, notes_txt=NOTES)
    write_frame(tmp_path)
    lines = [uri(refs, "legend.jpg"), uri(refs, "notes.txt")]
    run(None, parameters(refs, lines), tmp_path)
    padded = "  {0}  \n\n   \r\n {1}\t\n".format(*lines)
    run(None, parameters(refs, [], reference_payload_path=padded), tmp_path)

    first, second = converse
    assert len(first["messages"][0]["content"]) == 7
    assert first == second


def test_an_invoker_without_the_attachments_keyword_fails_before_the_call(
        tmp_path, collector):
    refs = reference_dir(tmp_path, legend_png=LEGEND_PNG, notes_txt=NOTES,
                         sheet_pdf=SHEET)
    write_frame(tmp_path)
    calls = []

    def two_image_invoker(model, prompt, images, region, max_tokens,
                          system_prompt=None):
        calls.append(images)
        return answer()

    metadata = run(two_image_invoker, parameters(refs, three_lines(refs)),
                   tmp_path)
    assert calls == []
    assert collector.samples == []
    assert metadata["bedrock"][NODE_ID]["error"] == (
        "the configured Bedrock invoker cannot send a list of reference files")


def test_the_single_image_form_keeps_todays_invoker_call(tmp_path):
    refs = reference_dir(tmp_path, legend_jpg=LEGEND_JPEG)
    write_frame(tmp_path)
    calls = []

    def two_image_invoker(model, prompt, images, region, max_tokens):
        calls.append((model, prompt, images, region, max_tokens))
        return answer()

    run(two_image_invoker, parameters(refs, [uri(refs, "legend.jpg")]),
        tmp_path)
    (call,) = calls
    assert [label for label, _ in call[2]] == ["Input image", "Reference image"]
    assert call[2][1][1] == LEGEND_JPEG
    assert call[3] == "us-west-2"


# ---------------------------------------------------------------------------
# AC-28b: botocore validates the request
# ---------------------------------------------------------------------------

def test_botocore_validates_the_document_blocks(tmp_path, monkeypatch):
    refs = reference_dir(tmp_path, legend_png=LEGEND_PNG, notes_txt=NOTES,
                         sheet_pdf=SHEET)
    write_frame(tmp_path)
    session = botocore.session.get_session()
    client = session.create_client(
        "bedrock-runtime", region_name="us-west-2",
        aws_access_key_id="testing", aws_secret_access_key="testing")
    sent = []
    client.meta.events.register(
        "provide-client-params.bedrock-runtime.Converse",
        lambda params, **_: sent.append(params))
    stubber = Stubber(client)
    stubber.add_response("converse", {
        "output": {"message": {"role": "assistant",
                               "content": [{"text": answer()}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        "metrics": {"latencyMs": 12},
    })
    stubber.activate()
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)
    metadata = run(None, parameters(refs, three_lines(refs)), tmp_path)

    (params,) = sent
    content = params["messages"][0]["content"]
    assert [sorted(block) for block in content] == [
        ["text"], ["text"], ["image"], ["text"], ["image"], ["text"],
        ["document"], ["text"], ["document"]]
    stubber.assert_no_pending_responses()
    assert metadata["bedrock"][NODE_ID]["outcome"] == "pass"


# ---------------------------------------------------------------------------
# AC-30: limits, with no Bedrock call
# ---------------------------------------------------------------------------

def test_six_documents_fail_before_any_file_is_read(tmp_path, collector):
    refs = reference_dir(tmp_path)
    write_frame(tmp_path)
    invoker = ScriptedInvoker()
    metadata = run(invoker, parameters(
        refs, [uri(refs, "n{0}.txt".format(i)) for i in range(6)]), tmp_path)
    assert invoker.calls == []
    assert collector.samples == []
    assert metadata["bedrock"][NODE_ID]["error"] == (
        "reference_payload_path lists 6 documents; Bedrock Converse accepts "
        "at most 5 per request")


def test_twenty_images_fail_before_any_file_is_read(tmp_path, collector):
    refs = reference_dir(tmp_path)
    write_frame(tmp_path)
    invoker = ScriptedInvoker()
    metadata = run(invoker, parameters(
        refs, [uri(refs, "i{0}.jpg".format(i)) for i in range(20)]), tmp_path)
    assert invoker.calls == []
    assert collector.samples == []
    assert metadata["bedrock"][NODE_ID]["error"] == (
        "reference_payload_path lists 20 images; with the input image that is "
        "more than the 20 images Bedrock Converse accepts per request")


def test_an_oversized_text_document_fails(tmp_path, collector):
    refs = reference_dir(tmp_path, big_txt=b"a" * 4_500_001)
    write_frame(tmp_path)
    invoker = ScriptedInvoker()
    metadata = run(invoker, parameters(refs, [uri(refs, "big.txt")]), tmp_path)
    assert invoker.calls == []
    assert collector.samples == []
    error = metadata["bedrock"][NODE_ID]["error"]
    assert error.startswith(
        "could not load reference 1 of 1 ({0}): ".format(uri(refs, "big.txt")))
    assert error.endswith("exceeds the 4500000-byte size cap")


def _noise_jpeg(width, height):
    rng = np.random.default_rng(7)
    frame = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(
        ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 100])
    assert ok
    return encoded.tobytes()


@pytest.mark.parametrize("case", ["bytes", "width"])
def test_an_image_over_the_sent_limits_fails(tmp_path, collector, case):
    data = (_noise_jpeg(1800, 1400) if case == "bytes"
            else encode(Image.new("RGB", (8001, 8), (9, 9, 9)), "JPEG"))
    if case == "bytes":
        assert 3_750_000 < len(data) <= 8 * 1024 * 1024
    refs = reference_dir(tmp_path, big_jpg=data)
    write_frame(tmp_path)
    invoker = ScriptedInvoker()
    metadata = run(invoker, parameters(
        refs, [uri(refs, "big.jpg")], max_image_dimension=0), tmp_path)
    assert invoker.calls == []
    assert collector.samples == []
    size = "1800\u00d71400" if case == "bytes" else "8001\u00d78"
    assert metadata["bedrock"][NODE_ID]["error"] == (
        "its reference image is {0} bytes and {1} px as sent; Bedrock Converse "
        "accepts at most 3.75 MB and 8000 px per side".format(len(data), size))


# ---------------------------------------------------------------------------
# AC-31: every file must load
# ---------------------------------------------------------------------------

def test_the_first_reference_that_cannot_load_stops_the_loading(
        tmp_path, collector, monkeypatch):
    refs = reference_dir(tmp_path, legend_png=LEGEND_PNG, sheet_pdf=SHEET)
    write_frame(tmp_path)
    opened = []
    for name in ("fetch_reference_bytes", "fetch_reference_document"):
        original = getattr(output_bindings, name)

        def recorder(value, *args, _original=original, **kwargs):
            opened.append(value)
            return _original(value, *args, **kwargs)

        monkeypatch.setattr(output_bindings, name, recorder)
    invoker = ScriptedInvoker()
    metadata = run(invoker, parameters(refs, three_lines(refs)), tmp_path)

    assert invoker.calls == []
    assert collector.samples == []
    notes = uri(refs, "notes.txt")
    assert metadata["bedrock"][NODE_ID]["error"] == (
        "could not load reference 2 of 3 ({0}): could not read local "
        "reference '{0}': No such file or directory".format(notes))
    assert opened == [uri(refs, "legend.png"), notes]

    with pytest.raises(BedrockInferenceError) as raised:
        run(invoker, parameters(refs, three_lines(refs), on_error="fail_run"),
            tmp_path)
    assert str(raised.value).startswith(
        "Bedrock inference node 'check' failed: could not load reference 2 "
        "of 3 ")
    assert str(raised.value).count("Bedrock inference node") == 1


@pytest.mark.parametrize("case", ["outside", "symlink"])
def test_a_denied_reference_in_a_list_fails_the_same_way(
        tmp_path, collector, case):
    refs = reference_dir(tmp_path, legend_png=LEGEND_PNG, sheet_pdf=SHEET)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "notes.txt").write_bytes(NOTES)
    if case == "symlink":
        os.symlink(str(elsewhere / "notes.txt"), os.path.join(refs, "notes.txt"))
        second = uri(refs, "notes.txt")
    else:
        second = "file://" + str(elsewhere) + "/notes.txt"
    write_frame(tmp_path)
    invoker = ScriptedInvoker()
    metadata = run(invoker, parameters(
        refs, [uri(refs, "legend.png"), second, uri(refs, "sheet.pdf")]),
        tmp_path)
    assert invoker.calls == []
    assert collector.samples == []
    error = metadata["bedrock"][NODE_ID]["error"]
    assert error.startswith("could not load reference 2 of 3 ({0}): ".format(
        second))
    assert "outside the node's allowed file:// prefixes" in error


# ---------------------------------------------------------------------------
# AC-32: the legacy path takes one image
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lines", [
    ["legend.jpg", "notes.txt"], ["sheet.pdf"]], ids=["two_lines", "one_pdf"])
def test_a_legacy_list_or_document_records_the_list_error(tmp_path, lines):
    refs = reference_dir(tmp_path, legend_jpg=LEGEND_JPEG, notes_txt=NOTES,
                         sheet_pdf=SHEET)
    write_frame(tmp_path)
    invoker = ScriptedInvoker(LEGACY_ANSWER)
    params = parameters(refs, [uri(refs, line) for line in lines])
    del params["answer_format"]
    metadata = run(invoker, params, tmp_path)
    error = metadata["bedrock"][NODE_ID]["error"]
    assert "only answer_format inspection" in error
    assert error == (
        "reference_payload_path lists {0} reference(s) or a document; only "
        "answer_format inspection accepts several references or "
        "documents".format(len(lines)))
    assert invoker.calls == []


def test_a_legacy_node_with_one_image_line_sends_todays_request(tmp_path):
    """Preservation: a legacy node with one payload image line sends the
    two-image request it sends today, with the reference bytes unchanged."""
    frame = write_frame(tmp_path)
    payload = {"refs": [{"image": base64.b64encode(LEGEND_PNG).decode()}]}
    invoker = ScriptedInvoker(LEGACY_ANSWER)
    params = {"prompt": "Compare.", "model": NOVA, "region": "us-west-2",
              "reference_payload_path": "refs.0.image"}
    run(invoker, params, tmp_path, payload=payload)
    (args, options), = invoker.calls
    assert options == {}
    assert args[2] == [("Input image", frame), ("Reference image", LEGEND_PNG)]


# ---------------------------------------------------------------------------
# AC-33: the list-form sample, artifacts and metadata
# ---------------------------------------------------------------------------

def test_a_list_form_sample_uploads_every_reference_before_the_sidecar(
        tmp_path, converse):
    s3 = FakeS3()
    exporter = SampleExporter(
        ExportConfig(bucket="tuning-samples-test", prefix=PREFIX),
        s3_factory=lambda: s3, thing_name=DEVICE)
    set_sample_exporter(exporter)
    try:
        refs = reference_dir(tmp_path, legend_png=LEGEND_PNG, notes_txt=NOTES,
                             sheet_pdf=SHEET)
        write_frame(tmp_path)
        metadata = run(None, parameters(refs, three_lines(refs)), tmp_path)
        assert exporter.wait_idle(10.0)
    finally:
        exporter.stop(5.0)
        set_sample_exporter(None)

    base = PREFIX + "wf-1/check/" + DEVICE + "/exec-1"
    assert s3.puts == [
        (base + ".input.jpg", "image/jpeg"),
        (base + ".reference-1.jpg", "image/jpeg"),
        (base + ".reference-2.txt", "text/plain; charset=utf-8"),
        (base + ".reference-3.pdf", "application/pdf"),
        (base + ".json", "application/json"),
    ]
    sidecar = json.loads(s3.objects[base + ".json"])
    assert "reference" not in sidecar
    sent_legend = s3.objects[base + ".reference-1.jpg"]
    assert sidecar["references"] == [
        {"index": 1, "label": "Reference 1", "kind": "image", "format": "jpeg",
         "key": base + ".reference-1.jpg", "sha256": sha(sent_legend),
         "bytes": len(sent_legend), "width": 400, "height": 300,
         "source": uri(refs, "legend.png"), "sourceSha256": sha(LEGEND_PNG),
         "transform": ["reencoded_jpeg"]},
        {"index": 2, "label": "Reference 2", "kind": "document",
         "format": "txt", "key": base + ".reference-2.txt", "sha256": sha(NOTES),
         "bytes": len(NOTES), "source": uri(refs, "notes.txt"),
         "sourceSha256": sha(NOTES)},
        {"index": 3, "label": "Reference 3", "kind": "document",
         "format": "pdf", "key": base + ".reference-3.pdf", "sha256": sha(SHEET),
         "bytes": len(SHEET), "source": uri(refs, "sheet.pdf"),
         "sourceSha256": sha(SHEET)},
    ]
    assert s3.objects[base + ".reference-2.txt"] == NOTES
    assert s3.objects[base + ".reference-3.pdf"] == SHEET

    names = sorted(os.listdir(str(tmp_path)))
    assert "cap-1.node.check.reference_1.jpg" in names
    assert not [name for name in names if "reference_2" in name
                or "reference_3" in name or name.endswith((".txt", ".pdf"))]
    with open(os.path.join(str(tmp_path), "cap-1.node.check.reference_1.jpg"),
              "rb") as handle:
        assert handle.read() == sent_legend
    recorded = metadata["bedrock"][NODE_ID]["references"]
    assert [sorted(item) for item in recorded] == [
        ["bytes", "format", "kind", "label", "sha256", "source"]] * 3
    assert [item["label"] for item in recorded] == [
        "Reference 1", "Reference 2", "Reference 3"]
    assert recorded[2] == {"label": "Reference 3", "kind": "document",
                           "format": "pdf", "source": uri(refs, "sheet.pdf"),
                           "sha256": sha(SHEET), "bytes": len(SHEET)}


class StalledExporter(SampleExporter):
    """A real exporter whose upload worker never starts."""

    def start(self):
        return None


def test_the_queue_is_bounded_by_bytes_with_its_own_warning(caplog):
    from workflow_engine.tuning.sample_export import MAX_QUEUE_BYTES
    assert MAX_QUEUE_BYTES == 268435456
    blob = bytes(8 * 1024 * 1024)
    exporter = StalledExporter(
        ExportConfig(bucket="tuning-samples-test", prefix=PREFIX),
        s3_factory=lambda: FakeS3(), thing_name=DEVICE)

    def sample(index, data=blob):
        return ExportedSample(
            workflow_id="wf-1", node_id=NODE_ID, node_type="bedrock_inference",
            execution_id="exec-{0}".format(index), input_bytes=data)

    with caplog.at_level(logging.WARNING):
        for index in range(32):
            exporter.enqueue(sample(index))
        assert exporter.queue_depth == 32
        assert caplog.records == []
        exporter.enqueue(sample(32))
    assert exporter.queue_depth == 32
    assert [record.getMessage() for record in caplog.records] == [
        "Tuning sample export queue holds more than 268435456 bytes of "
        "samples; dropped the oldest sample (execution exec-0, node check)"]

    caplog.clear()
    small = StalledExporter(
        ExportConfig(bucket="tuning-samples-test", prefix=PREFIX),
        s3_factory=lambda: FakeS3(), thing_name=DEVICE, queue_size=2)
    with caplog.at_level(logging.WARNING):
        for index in range(3):
            small.enqueue(sample(index, b"\xff\xd8x"))
    assert [record.getMessage() for record in caplog.records] == [
        "Tuning sample export queue is full (2 entries); dropped the oldest "
        "sample (execution exec-0, node check)"]
