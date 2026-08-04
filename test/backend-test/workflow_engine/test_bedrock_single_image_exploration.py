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
"""Bug condition exploration test: single available primary image is
sufficient for bedrock_inference.

Property 1: Bug Condition - Single available primary image is sufficient

For any bedrock_inference binding whose `in` frame is captured and
readable while the `reference` frame is unavailable (capturePaths
reference None, key absent, or file missing on disk), the processor
SHALL invoke the injected invoker exactly once with images ==
[("Input image", <in bytes>)], SHALL NOT raise for the missing
reference, and SHALL merge the parsed answer into the run metadata
identically to the two-image path.

**Validates: Requirements 2.1, 2.2** (bugfix.md 1.1, 1.2, 1.3)

EXPECTED TO FAIL on unfixed code: the current _run_one hard-requires
both frames and raises BedrockInferenceError for the unavailable
reference. Failure of this test confirms the defect exists.
"""
import os

import pytest

from workflow_engine_test_utils import DEVICE_ARCH

from workflow_engine.output_bindings import BedrockInferenceProcessor

JPEG_BYTES_IN = b"\xff\xd8fake-input-jpeg\xff\xd9"


def make_binding(capture_paths):
    return {
        "nodeId": "bedrock1",
        "binding": "bedrock_inference",
        "parameters": {
            "model": "us.amazon.nova-lite-v1:0",
            "prompt": "Inspect the image.",
            "region": "us-east-1",
            "max_tokens": 256,
        },
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
        "capturePaths": capture_paths,
    }


def make_document(binding):
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
                    {"nodeId": None, "factory": "videoconvert", "args": {}},
                    {"nodeId": None, "factory": "jpegenc", "args": {}},
                    {"nodeId": None, "factory": "multifilesink",
                     "args": {"location": "{work_dir}/bedrock_frame_cam.jpg"}},
                ],
            },
        ],
        "executorBindings": [binding],
        "pluginDependencies": ["python:boto3"],
    }


def write_in_frame(work_dir):
    with open(os.path.join(work_dir, "bedrock_frame_cam.jpg"), "wb") as f:
        f.write(JPEG_BYTES_IN)


class RecordingInvoker:
    """Injectable Bedrock invoker recording the call and returning a
    canned model answer."""

    def __init__(self, answer='{"is_anomalous": true, "confidence": 0.9}'):
        self.answer = answer
        self.calls = []

    def __call__(self, model, prompt, images, region, max_tokens):
        self.calls.append({
            "model": model,
            "prompt": prompt,
            "images": list(images),
            "region": region,
            "max_tokens": max_tokens,
        })
        return self.answer


# The three reference-unavailable shapes from isBugCondition (design.md):
# (a) compiler's unfed-port shape: capturePaths.reference is None
# (b) reference key absent entirely from capturePaths
# (c) reference path present but the file missing on disk
REFERENCE_UNAVAILABLE_SHAPES = [
    pytest.param(
        {"in": "{work_dir}/bedrock_frame_cam.jpg", "reference": None},
        id="reference-none",
    ),
    pytest.param(
        {"in": "{work_dir}/bedrock_frame_cam.jpg"},
        id="reference-key-absent",
    ),
    pytest.param(
        {"in": "{work_dir}/bedrock_frame_cam.jpg",
         "reference": "{work_dir}/bedrock_frame_ref.jpg"},
        id="reference-file-missing",
    ),
]


class TestSingleAvailablePrimaryImageIsSufficient:
    """Property 1: Bug Condition - single available primary image is
    sufficient.

    **Validates: Requirements 2.1, 2.2**
    """

    @pytest.mark.parametrize("capture_paths", REFERENCE_UNAVAILABLE_SHAPES)
    def test_single_image_inference_succeeds_when_reference_unavailable(
        self, tmp_path, capture_paths
    ):
        # The `in` frame exists and is readable in every case; the
        # reference frame is unavailable in each of the three shapes.
        write_in_frame(str(tmp_path))
        invoker = RecordingInvoker(
            answer='{"is_anomalous": true, "confidence": 0.9}')
        processor = BedrockInferenceProcessor(invoker=invoker)
        document = make_document(make_binding(capture_paths))

        # Expected (fixed) behavior: process() succeeds — no
        # BedrockInferenceError for the unavailable reference.
        metadata = processor.process(document, {"existing": "kept"},
                                     str(tmp_path))

        # Invoker called exactly once with only the primary image.
        assert len(invoker.calls) == 1
        assert invoker.calls[0]["images"] == [
            ("Input image", JPEG_BYTES_IN),
        ]

        # Parsed answer merged into the returned metadata identically
        # to the two-image path. Intended contract change
        # (bedrock-response-mode Requirement 5): anomaly mode now ALSO
        # records the raw answer text as bedrock_text /
        # bedrock.{nodeId}.text.
        assert metadata == {
            "existing": "kept", "is_anomalous": True, "confidence": 0.9,
            "bedrock_text": invoker.answer,
            "bedrock": {"bedrock1": {"text": invoker.answer}}}
