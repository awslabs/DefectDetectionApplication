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
"""Unit tests for the LLM_Binding's image downscaling (task 6.1,
vllm-workflow-latency-optimization Requirements 5.3-5.8).

- ``resolve_max_image_dimension``: absent/None ⇒ unconfigured and silent;
  integral >= 1 (bool excluded, integral floats accepted) ⇒ value;
  anything else ⇒ unconfigured plus an invalid notice (R5.8).
- ``downscale_image_bytes``: longer edge > max_dim ⇒ resized so the
  longer edge equals max_dim, aspect preserved, JPEG re-encode; longer
  edge <= max_dim ⇒ ORIGINAL bytes, byte-identical (never upscale,
  R5.7); undecodable input raises.
- ``_run_one`` wiring: configured ⇒ both the 'in' and 'reference' frames
  are downscaled after read and before base64 encoding (R5.3);
  unconfigured ⇒ byte-identical pre-feature bytes (R5.6); invalid value
  ⇒ unmodified bytes plus one WARNING naming the value (R5.8); a
  downscale failure ⇒ WARNING naming node and failure and the ORIGINAL
  bytes are sent, node outcome unchanged (R5.4).
"""
import base64
import io

import pytest
from PIL import Image

from workflow_engine_test_utils import DEVICE_ARCH

from workflow_engine.output_bindings import (
    LlmInferenceProcessor,
    downscale_image_bytes,
    resolve_max_image_dimension,
)


def jpeg_bytes(width, height, color=(30, 90, 160)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def decoded_size(data):
    with Image.open(io.BytesIO(data)) as image:
        return image.size


def llm_binding(capture_paths=None, node_id="llm1", **params):
    parameters = {
        "modelName": "qwen2-vl-2b",
        "prompt_template": "Describe the image.",
        "max_tokens": 128,
    }
    parameters.update(params)
    binding = {
        "nodeId": node_id,
        "binding": "llm_inference",
        "parameters": parameters,
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
    }
    if capture_paths is not None:
        binding["capturePaths"] = capture_paths
    return binding


def make_document(bindings):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [
            {
                "name": "s0",
                "from": None,
                "linkTo": None,
                "elements": [
                    {"nodeId": "cam", "factory": "videotestsrc", "args": {}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }


class RecordingInvoker:
    def __init__(self, text="generated answer"):
        self.text = text
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.text


def run_one(capture_paths, work_dir, **params):
    invoker = RecordingInvoker()
    processor = LlmInferenceProcessor(invoker=invoker)
    document = make_document(
        [llm_binding(capture_paths=capture_paths, **params)])
    metadata = processor.process(document, {"seed": 1}, work_dir)
    return metadata["llm"]["llm1"], invoker.calls


# ---------------------------------------------------------------------------
# resolve_max_image_dimension (R5.8)
# ---------------------------------------------------------------------------

class TestResolveMaxImageDimension:
    def test_absent_is_unconfigured_and_silent(self):
        assert resolve_max_image_dimension(None) == (None, None)

    def test_valid_int_is_accepted(self):
        assert resolve_max_image_dimension(1) == (1, None)
        assert resolve_max_image_dimension(640) == (640, None)

    def test_integral_float_is_accepted_as_int(self):
        max_dim, notice = resolve_max_image_dimension(512.0)
        assert max_dim == 512
        assert isinstance(max_dim, int)
        assert notice is None

    @pytest.mark.parametrize("raw", [
        0, -1, 0.0, -2.5, 1.5, True, False, "640", "abc",
        float("nan"), float("inf"), [], {},
    ])
    def test_invalid_is_unconfigured_with_notice(self, raw):
        max_dim, notice = resolve_max_image_dimension(raw)
        assert max_dim is None
        assert notice is not None
        assert repr(raw) in notice

    def test_notice_names_the_rejected_value(self):
        _max_dim, notice = resolve_max_image_dimension("huge")
        assert "'huge'" in notice
        assert "max_image_dimension" in notice


# ---------------------------------------------------------------------------
# downscale_image_bytes (R5.3, R5.7)
# ---------------------------------------------------------------------------

class TestDownscaleImageBytes:
    def test_landscape_longer_edge_downscaled_to_max(self):
        result = downscale_image_bytes(jpeg_bytes(400, 200), 100)
        assert decoded_size(result) == (100, 50)

    def test_portrait_longer_edge_downscaled_to_max(self):
        result = downscale_image_bytes(jpeg_bytes(120, 480), 120)
        assert decoded_size(result) == (30, 120)

    def test_reencoded_output_is_jpeg(self):
        result = downscale_image_bytes(jpeg_bytes(400, 200), 100)
        with Image.open(io.BytesIO(result)) as image:
            assert image.format == "JPEG"

    def test_at_threshold_returns_original_bytes(self):
        data = jpeg_bytes(100, 50)
        assert downscale_image_bytes(data, 100) is data

    def test_below_threshold_never_upscales(self):
        data = jpeg_bytes(60, 40)
        assert downscale_image_bytes(data, 640) is data

    def test_undecodable_bytes_raise(self):
        with pytest.raises(Exception):
            downscale_image_bytes(b"not-an-image", 100)

    def test_non_rgb_source_is_encodable(self):
        # PNG with alpha decodes to RGBA, which JPEG cannot encode
        # directly; the helper normalizes the mode.
        buffer = io.BytesIO()
        Image.new("RGBA", (300, 100), (10, 20, 30, 128)).save(
            buffer, format="PNG")
        result = downscale_image_bytes(buffer.getvalue(), 150)
        assert decoded_size(result) == (150, 50)


# ---------------------------------------------------------------------------
# _run_one wiring (R5.3, R5.4, R5.5, R5.6, R5.8)
# ---------------------------------------------------------------------------

class TestRunOneWiring:
    def write_frames(self, tmp_path, in_size=(400, 200), ref_size=(300, 600)):
        in_bytes = jpeg_bytes(*in_size)
        ref_bytes = jpeg_bytes(*ref_size, color=(200, 40, 40))
        (tmp_path / "frame_in.jpg").write_bytes(in_bytes)
        (tmp_path / "frame_ref.jpg").write_bytes(ref_bytes)
        return in_bytes, ref_bytes

    CAPTURE_BOTH = {"in": "{work_dir}/frame_in.jpg",
                    "reference": "{work_dir}/frame_ref.jpg"}

    def test_configured_downscales_both_frames(self, tmp_path):
        # R5.3: both the 'in' and the 'reference' frame are downscaled
        # before base64 encoding.
        self.write_frames(tmp_path)
        outcome, calls = run_one(
            self.CAPTURE_BOTH, str(tmp_path), max_image_dimension=100)
        assert outcome == {"generated_text": "generated answer"}
        assert len(calls) == 1
        image_b64, reference_b64 = calls[0][3], calls[0][4]
        assert decoded_size(base64.b64decode(image_b64)) == (100, 50)
        assert decoded_size(base64.b64decode(reference_b64)) == (50, 100)

    def test_unconfigured_is_byte_identical(self, tmp_path, caplog):
        # R5.6: no option configured ⇒ the encoded bytes are
        # byte-identical to the captured frames, and no downscaling
        # warning is emitted.
        in_bytes, ref_bytes = self.write_frames(tmp_path)
        with caplog.at_level("WARNING"):
            _outcome, calls = run_one(self.CAPTURE_BOTH, str(tmp_path))
        assert base64.b64decode(calls[0][3]) == in_bytes
        assert base64.b64decode(calls[0][4]) == ref_bytes
        assert not any(
            "max_image_dimension" in record.message
            or "downscal" in record.message
            for record in caplog.records
        )

    def test_smaller_frames_are_never_upscaled(self, tmp_path):
        # R5.7: frames whose longer edge is <= the configured maximum
        # ride byte-identical — never upscaled.
        in_bytes, ref_bytes = self.write_frames(
            tmp_path, in_size=(80, 40), ref_size=(30, 60))
        _outcome, calls = run_one(
            self.CAPTURE_BOTH, str(tmp_path), max_image_dimension=640)
        assert base64.b64decode(calls[0][3]) == in_bytes
        assert base64.b64decode(calls[0][4]) == ref_bytes

    def test_invalid_value_unmodified_bytes_and_one_warning(
        self, tmp_path, caplog
    ):
        # R5.8: invalid configured value ⇒ treated as unconfigured
        # (bytes unmodified) plus exactly one WARNING naming the value.
        in_bytes, ref_bytes = self.write_frames(tmp_path)
        with caplog.at_level("WARNING"):
            outcome, calls = run_one(
                self.CAPTURE_BOTH, str(tmp_path), max_image_dimension=-5)
        assert outcome == {"generated_text": "generated answer"}
        assert base64.b64decode(calls[0][3]) == in_bytes
        assert base64.b64decode(calls[0][4]) == ref_bytes
        warnings = [
            record for record in caplog.records
            if record.levelname == "WARNING"
            and "max_image_dimension" in record.message
        ]
        assert len(warnings) == 1
        assert "-5" in warnings[0].message

    def test_downscale_failure_sends_original_bytes_with_warning(
        self, tmp_path, caplog
    ):
        # R5.4: an undecodable captured frame makes the downscale raise;
        # the WARNING names the node and the failure, the ORIGINAL bytes
        # are sent, and the node reaches the same outcome.
        garbage = b"\xff\xd8\xff\xe0not-really-a-jpeg"
        (tmp_path / "frame_in.jpg").write_bytes(garbage)
        with caplog.at_level("WARNING"):
            outcome, calls = run_one(
                {"in": "{work_dir}/frame_in.jpg"}, str(tmp_path),
                max_image_dimension=100)
        assert outcome == {"generated_text": "generated answer"}
        assert base64.b64decode(calls[0][3]) == garbage
        warnings = [
            record for record in caplog.records
            if record.levelname == "WARNING"
            and "downscal" in record.message
        ]
        assert len(warnings) == 1
        assert "llm1" in warnings[0].message

    def test_no_image_means_no_downscaling_attempt(self, tmp_path, caplog):
        # R5.5: no captured image ⇒ no downscaling attempt (and no
        # downscaling warning), pre-feature text-only arity preserved.
        with caplog.at_level("WARNING"):
            outcome, calls = run_one(None, str(tmp_path),
                                     max_image_dimension=100)
        assert outcome == {"generated_text": "generated answer"}
        assert len(calls) == 1
        assert len(calls[0]) == 3
        assert not any(
            "downscal" in record.message for record in caplog.records
        )
