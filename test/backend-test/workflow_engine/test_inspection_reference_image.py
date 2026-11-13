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
"""The reference image reaches Bedrock on the inspection path (AC-10 to
AC-14), and the legacy path accepts a typed URI.

The real ``BedrockInferenceProcessor`` with a scripted invoker (or the
default invoker on a stubbed ``boto3.client`` where the content blocks
matter) and a collector exporter. A's parameter values are re-rooted to
a temporary directory, except where the literal values are checked with
the fetch stubbed. No AWS call, no network.

At c299a6e1 the inspection path sends one image and never reads the
reference, and the legacy path reads a typed URI as a dotted path.
"""
import hashlib
import io
import json
import os

import boto3
import cv2
import numpy as np
import pytest
from PIL import Image

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine import output_bindings, payload_fetch
from workflow_engine.node_status import NodeStatusCollector
from workflow_engine.output_bindings import (
    BedrockInferenceError,
    BedrockInferenceProcessor,
    RunContext,
)
from workflow_engine.tuning.sample_export import (
    ExportContext,
    build_sidecar,
    set_sample_exporter,
)
from workflow_engine.vendor.workflow_core.anomaly_invocation import (
    INSPECTION_JSON_INSTRUCTION,
)

FRAME_NAME = "bedrock_frame_camera.jpg"
CAPTURE_ID = "cap-1"
NODE_ID = "check"
NOVA = "us.amazon.nova-pro-v1:0"
CONTEXT = ExportContext(workflow_id="wf-1", version="3", execution_id="exec-1")
PAYLOAD = {"order_id": "o-1", "body": {"qc_stage": "qc2", "order": {
    "items": [{"shape": "roof", "color": "red"}]}}}
LEGACY_ANSWER = '{"is_anomalous": false, "confidence": 0.9}'


def write_frame(directory, width=64, height=48):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 2] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    path = os.path.join(str(directory), FRAME_NAME)
    assert cv2.imwrite(path, frame)
    return path


def encode(image, fmt, **options):
    buffer = io.BytesIO()
    image.save(buffer, fmt, **options)
    return buffer.getvalue()


def rgb_jpeg(width=320, height=240, color=(200, 180, 40)):
    return encode(Image.new("RGB", (width, height), color), "JPEG")


def inspection_answer(count=1):
    return json.dumps({
        "verdict": "pass", "confidence": 0.9, "reason": "ok",
        "objects": [{"name": "p{0}".format(i), "qc": "OK", "reason": "r",
                     "bounding_box": {"x_min": 1, "y_min": 1, "x_max": 9,
                                      "y_max": 9}} for i in range(count)]})


class ScriptedInvoker:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, *args, **options):
        self.calls.append((args, options))
        item = self.script.pop(0) if self.script else inspection_answer()
        if isinstance(item, BaseException):
            raise item
        return item


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


def images_dir(tmp_path, legend=None, name="source.jpg"):
    images = tmp_path / "images"
    images.mkdir(exist_ok=True)
    if legend is not None:
        (images / name).write_bytes(legend)
    return str(images)


def a_parameters(images, **overrides):
    """A's ``check`` parameters (generic values) re-rooted to ``images``."""
    parameters = {
        "prompt": "Check the part.", "answer_format": "inspection",
        "model": NOVA, "region": "us-west-2",
        "reference_payload_path": "file://" + images + "/source.jpg",
        "allowed_uri_prefixes": "file://" + images + "/",
        "on_error": "record", "error_verdict": "error", "attempts": 2,
        "attempt_timeout_s": 6, "budget_s": 11, "max_image_dimension": 1280,
        "render_prompt": True,
        "expected_count_path": "trigger.payload_json.body.order.items",
        "box_scale": "normalized_1000", "max_tokens": 600,
    }
    parameters.update(overrides)
    return parameters


def legacy_parameters(**overrides):
    parameters = {"prompt": "Compare the input with the reference.",
                  "model": NOVA, "region": "us-west-2"}
    parameters.update(overrides)
    return parameters


def run(invoker, parameters, tmp_path, payload=None):
    payload = PAYLOAD if payload is None else payload
    trigger = {"topic": "plant/line1/evaluate", "payload": json.dumps(payload),
               "payload_json": json.loads(json.dumps(payload))}
    context = RunContext(
        tag_values={"trigger": trigger}, output_dir=str(tmp_path),
        capture_id=CAPTURE_ID,
        node_status=NodeStatusCollector(extra_node_ids=[NODE_ID]))
    binding = {
        "nodeId": NODE_ID, "binding": "bedrock_inference",
        "parameters": dict(parameters), "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": [],
        "capturePaths": {"in": "{work_dir}/" + FRAME_NAME, "reference": None},
    }
    processor = BedrockInferenceProcessor(invoker=invoker)
    return processor.process(
        {"schemaVersion": 1, "executorBindings": [binding]},
        dict(context.tag_values), str(tmp_path), run_context=context,
        export_context=CONTEXT)


def sha(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# AC-10: the reference reaches Bedrock
# ---------------------------------------------------------------------------

def test_a_style_request_carries_the_input_then_the_reference_image(
        tmp_path):
    legend = rgb_jpeg()
    images = images_dir(tmp_path, legend)
    write_frame(tmp_path, 1600, 1200)
    invoker = ScriptedInvoker()
    metadata = run(invoker, a_parameters(images), tmp_path)

    (args, _), = invoker.calls
    request_images = args[2]
    assert len(request_images) == 2
    assert [label for label, _ in request_images] == [
        "Input image", "Reference image"]
    assert sha(request_images[1][1]) == sha(legend)
    entry = metadata["bedrock"][NODE_ID]
    assert entry["outcome"] == "pass"
    assert entry["references"] == [{
        "label": "Reference image", "kind": "image", "format": "jpeg",
        "source": "file://" + images + "/source.jpg", "sha256": sha(legend),
        "bytes": len(legend)}]
    persisted = os.path.join(
        str(tmp_path), "{0}.node.{1}.reference.jpg".format(CAPTURE_ID, NODE_ID))
    with open(persisted, "rb") as handle:
        assert handle.read() == legend


# ---------------------------------------------------------------------------
# AC-11: both reference forms
# ---------------------------------------------------------------------------

def test_a_literal_values_fetch_the_legend_through_the_allow_list(
        tmp_path, monkeypatch):
    legend = rgb_jpeg(200, 100)
    calls = []

    def fetch(value, allowed_prefixes=None, s3_client=None):
        calls.append((value, tuple(allowed_prefixes or ())))
        return legend

    monkeypatch.setattr(output_bindings, "fetch_reference_bytes", fetch)
    write_frame(tmp_path)
    invoker = ScriptedInvoker()
    run(invoker, a_parameters(
        "/unused",
        reference_payload_path="file:///aws_dda/images/source.jpg",
        allowed_uri_prefixes="file:///aws_dda/images/"), tmp_path)

    assert calls == [("file:///aws_dda/images/source.jpg",
                      ("file:///aws_dda/images/",))]
    (args, _), = invoker.calls
    assert len(args[2]) == 2
    assert args[2][1][1] == legend


def test_a_dotted_path_to_a_file_uri_yields_two_images(tmp_path):
    legend = rgb_jpeg()
    images = images_dir(tmp_path, legend)
    write_frame(tmp_path)
    payload = json.loads(json.dumps(PAYLOAD))
    payload["body"]["reference_image"] = "file://" + images + "/source.jpg"
    invoker = ScriptedInvoker()
    run(invoker, a_parameters(
        images, reference_payload_path="body.reference_image"),
        tmp_path, payload=payload)

    (args, _), = invoker.calls
    assert len(args[2]) == 2
    assert args[2][1][1] == legend


def test_the_legacy_path_accepts_the_literal_form(tmp_path):
    legend = rgb_jpeg()
    images = images_dir(tmp_path, legend)
    write_frame(tmp_path)
    invoker = ScriptedInvoker(LEGACY_ANSWER)
    metadata = run(invoker, legacy_parameters(
        reference_payload_path="file://" + images + "/source.jpg",
        allowed_uri_prefixes="file://" + images + "/"), tmp_path)

    entry = metadata["bedrock"][NODE_ID]
    assert "error" not in entry
    (args, _), = invoker.calls
    assert [label for label, _ in args[2]] == ["Input image", "Reference image"]
    assert args[2][1][1] == legend
    assert entry["is_anomalous"] is False


def test_the_legacy_path_prepares_a_static_png_but_never_a_payload_image(
        tmp_path):
    """A static PNG is sent as a JPEG and never downscaled; a payload
    reference keeps today's bytes (a PNG stays the PNG)."""
    png = encode(Image.new("RGB", (2000, 1500), (30, 60, 90)), "PNG")
    images = images_dir(tmp_path, png, name="legend.png")
    write_frame(tmp_path)
    invoker = ScriptedInvoker(LEGACY_ANSWER, LEGACY_ANSWER)
    run(invoker, legacy_parameters(
        reference_payload_path="file://" + images + "/legend.png",
        allowed_uri_prefixes="file://" + images + "/",
        max_image_dimension=1280), tmp_path)
    (args, _), = invoker.calls
    assert len(args[2]) == 2
    sent = args[2][1][1]
    assert sent[:3] == b"\xff\xd8\xff"
    with Image.open(io.BytesIO(sent)) as decoded:
        assert decoded.size == (2000, 1500)

    payload = {"refs": [{"image": "file://" + images + "/legend.png"}]}
    run(invoker, legacy_parameters(
        reference_payload_path="refs.0.image",
        allowed_uri_prefixes="file://" + images + "/"), tmp_path,
        payload=payload)
    args, _ = invoker.calls[1]
    assert args[2][1][1] == png


# ---------------------------------------------------------------------------
# AC-12: reference failures
# ---------------------------------------------------------------------------

def _failure_setup(tmp_path, case):
    legend = rgb_jpeg()
    images = images_dir(tmp_path, legend)
    if case == "missing":
        return images, {"reference_payload_path": "file://" + images + "/absent.jpg"}, \
            "could not read local reference"
    if case == "empty_allow_list":
        return images, {"allowed_uri_prefixes": ""}, \
            "requires an explicit file:// entry"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.jpg").write_bytes(legend)
    if case == "outside":
        return images, {"reference_payload_path":
                        "file://" + str(elsewhere) + "/secret.jpg"}, \
            "outside the node's allowed file:// prefixes"
    if case == "symlink":
        os.symlink(str(elsewhere / "secret.jpg"),
                   os.path.join(images, "link.jpg"))
        return images, {"reference_payload_path":
                        "file://" + images + "/link.jpg"}, \
            "outside the node's allowed file:// prefixes"
    (tmp_path / "images" / "notes.jpg").write_bytes(b"plain text, not a JPEG")
    return images, {"reference_payload_path": "file://" + images + "/notes.jpg"}, \
        "is not a decodable image"


@pytest.mark.parametrize("case", [
    "missing", "empty_allow_list", "outside", "symlink", "not_an_image"])
def test_a_reference_failure_makes_no_call_and_no_sample(
        tmp_path, collector, case):
    images, overrides, reason = _failure_setup(tmp_path, case)
    write_frame(tmp_path)
    invoker = ScriptedInvoker()
    metadata = run(invoker, a_parameters(images, **overrides), tmp_path)

    bedrock_calls = len(invoker.calls)
    assert bedrock_calls == 0
    assert collector.samples == []
    entry = metadata["bedrock"][NODE_ID]
    assert entry["outcome"] == "error"
    assert entry["error"].startswith("could not load its reference image: ")
    assert reason in entry["error"]

    with pytest.raises(BedrockInferenceError) as raised:
        run(invoker, a_parameters(images, on_error="fail_run", **overrides),
            tmp_path)
    assert str(raised.value).startswith(
        "Bedrock inference node 'check' failed: could not load its reference "
        "image: ")
    assert str(raised.value).count("Bedrock inference node") == 1
    assert invoker.calls == []


# ---------------------------------------------------------------------------
# AC-13: reference handling
# ---------------------------------------------------------------------------

def _rgba_png():
    image = Image.new("RGBA", (120, 80), (0, 0, 0, 0))
    image.paste((0, 0, 0, 255), (40, 20, 80, 60))
    return encode(image, "PNG")


def _p_png():
    image = Image.new("P", (120, 80), 0)
    image.putpalette([0, 0, 0, 0, 0, 0] + [0] * 762)
    image.paste(1, (40, 20, 80, 60))
    return encode(image, "PNG", transparency=0)


def _exif_6_jpeg():
    exif = Image.Exif()
    exif[0x0112] = 6
    return encode(Image.new("RGB", (300, 200), (10, 200, 30)), "JPEG",
                  exif=exif.tobytes())


HANDLING = {
    "rgb_jpeg": (lambda: rgb_jpeg(320, 240), [], (320, 240)),
    "png": (lambda: encode(Image.new("RGB", (160, 90), (1, 2, 3)), "PNG"),
            ["reencoded_jpeg"], (160, 90)),
    "rgba_png": (_rgba_png, ["flattened_alpha", "reencoded_jpeg"], (120, 80)),
    "p_png": (_p_png, ["flattened_alpha", "reencoded_jpeg"], (120, 80)),
    "exif_6": (_exif_6_jpeg, ["exif_transposed"], (200, 300)),
    "large": (lambda: rgb_jpeg(3000, 1500), ["downscaled"], (1280, 640)),
    "small": (lambda: rgb_jpeg(40, 30), [], (40, 30)),
}


@pytest.mark.parametrize("case", sorted(HANDLING))
def test_reference_handling(tmp_path, collector, case):
    make, transform, size = HANDLING[case]
    legend = make()
    name = "legend.png" if legend[:4] == b"\x89PNG" else "legend.jpg"
    images = images_dir(tmp_path, legend, name=name)
    write_frame(tmp_path)
    invoker = ScriptedInvoker()
    run(invoker, a_parameters(
        images, reference_payload_path="file://" + images + "/" + name),
        tmp_path)

    (args, _), = invoker.calls
    request_images = args[2]
    assert len(request_images) == 2
    sent = request_images[1][1]
    assert sent[:3] == b"\xff\xd8\xff"
    with Image.open(io.BytesIO(sent)) as decoded:
        assert decoded.size == size
        if case in ("rgba_png", "p_png"):
            rgb = decoded.convert("RGB")
            assert all(channel >= 250 for channel in rgb.getpixel((5, 5)))
            assert all(channel <= 5 for channel in rgb.getpixel((60, 40)))
    (sample,) = collector.samples
    reference = build_sidecar(sample, "device-under-test", "p/")["reference"]
    assert reference["transform"] == transform
    assert reference["sourceSha256"] == sha(legend)
    assert reference["sha256"] == sha(sent)
    assert (reference["width"], reference["height"]) == size
    if not transform:
        assert sent == legend
        assert reference["sha256"] == reference["sourceSha256"]


# ---------------------------------------------------------------------------
# AC-14: no comparison wording
# ---------------------------------------------------------------------------

class StubBedrockClient:
    def __init__(self, recorder, answer):
        self.recorder = recorder
        self.answer = answer

    def converse(self, **kwargs):
        self.recorder.append(kwargs)
        return {"output": {"message": {"content": [{"text": self.answer}]}}}


def test_no_comparison_wording_is_added(tmp_path, monkeypatch):
    legend = rgb_jpeg()
    images = images_dir(tmp_path, legend)
    write_frame(tmp_path)
    converse = []
    monkeypatch.setattr(
        boto3, "client",
        lambda service, region_name=None, config=None, **_: StubBedrockClient(
            converse, inspection_answer()))
    # invoker=None: the processor's default invoker builds the request.
    run(None, a_parameters(
        images, prompt="Stage {trigger.payload_json.body.qc_stage}."),
        tmp_path)

    (kwargs,) = converse
    content = kwargs["messages"][0]["content"]
    assert len(content) == 5
    assert content[0] == {
        "text": "Stage qc2.\n\n" + INSPECTION_JSON_INSTRUCTION}
    assert content[1] == {"text": "Input image:"}
    assert set(content[2]) == {"image"}
    assert content[3] == {"text": "Reference image:"}
    assert content[4]["image"]["source"]["bytes"] == legend


def test_reference_uri_schemes_equal_the_fetchers():
    """The shared static-reference schemes are exactly the device's."""
    from workflow_engine.vendor.workflow_core.anomaly_invocation import (
        REFERENCE_URI_SCHEMES,
    )
    assert set(REFERENCE_URI_SCHEMES) == set(payload_fetch._URI_SCHEMES)
