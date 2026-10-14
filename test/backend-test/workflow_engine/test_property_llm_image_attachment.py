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
"""Property test for the LlmInferenceProcessor's frame attachment (task 2.3).

**Feature: edge-vlm-image-inference, Property 5: Processor image attachment
trichotomy**

*For any* ``llm_inference`` binding and any of the three ``capturePaths``
shapes — (a) ``in`` mapped to a path whose resolved file exists, (b) ``in``
mapped to ``None`` or ``capturePaths`` absent, (c) ``in`` mapped to a path
whose resolved file is missing/unreadable — the processor SHALL respectively
(a) invoke the injected invoker exactly once with the file's bytes
base64-encoded as the image argument and the ``{work_dir}`` placeholder
resolved, (b) invoke exactly once with the pre-feature 3-argument request,
(c) invoke zero times and merge a Node_Error_Record naming the node and the
path under ``metadata['llm'][nodeId]``, with remaining bindings still
processed.

**Validates: Requirements 2.1, 2.2, 2.3, 2.5, 6.1**

Runs with the hypothesis profiles registered in this directory's conftest
(``engine-fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import base64
import os
import shutil
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine_test_utils import DEVICE_ARCH

from workflow_engine.output_bindings import LlmInferenceProcessor

# The three capturePaths shapes of the trichotomy; shape (b) has two
# concrete spellings ("absent" = no capturePaths key at all, i.e. a
# pre-feature compiled document — Requirement 6.1 — and "none" =
# ``{'in': None}`` for an unfed port).
_SHAPES = ("readable", "absent", "none", "unreadable")

_JPEG_MAGIC = b"\xff\xd8\xff"

_jpeg_bytes = st.binary(min_size=0, max_size=256).map(
    lambda tail: _JPEG_MAGIC + tail
)


@st.composite
def _binding_specs(draw):
    """1..4 llm_inference binding specs, each with a drawn trichotomy shape
    and (for readable files) random JPEG-ish payload bytes."""
    n = draw(st.integers(min_value=1, max_value=4))
    specs = []
    for index in range(n):
        shape = draw(st.sampled_from(_SHAPES))
        payload = draw(_jpeg_bytes) if shape == "readable" else None
        specs.append((shape, payload))
    return specs


def _make_document(bindings):
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


class _ArityRecordingInvoker:
    """Injectable Text_Generation_API invoker recording every call's raw
    positional args — the arity distinguishes an image-carrying invocation
    ``(model_name, prompt, parameters, image_b64)`` from the pre-feature
    text-only 3-arg form."""

    def __init__(self, text="generated answer"):
        self.text = text
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.text


def _binding(index, shape, work_dir, payload):
    node_id = "llm{0}".format(index)
    binding = {
        "nodeId": node_id,
        "binding": "llm_inference",
        "parameters": {
            "modelName": "qwen2-vl-{0}".format(index),
            "prompt_template": "Describe part {0}.".format(index),
            "max_tokens": 128,
            "temperature": 0.7,
            "top_p": 1.0,
        },
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
    }
    resolved_path = None
    if shape == "readable":
        filename = "vlm_frame_{0}.jpg".format(index)
        resolved_path = os.path.join(work_dir, filename)
        with open(resolved_path, "wb") as f:
            f.write(payload)
        binding["capturePaths"] = {"in": "{work_dir}/" + filename}
    elif shape == "none":
        binding["capturePaths"] = {"in": None}
    elif shape == "unreadable":
        filename = "missing_frame_{0}.jpg".format(index)
        resolved_path = os.path.join(work_dir, filename)
        # Never written: the resolved file cannot be read.
        binding["capturePaths"] = {"in": "{work_dir}/" + filename}
    # shape == "absent": no capturePaths key at all (pre-feature package).
    return binding, resolved_path


@given(specs=_binding_specs())
@settings(deadline=None)
def test_image_attachment_trichotomy(specs):
    """**Feature: edge-vlm-image-inference, Property 5: Processor image
    attachment trichotomy**

    **Validates: Requirements 2.1, 2.2, 2.3, 2.5, 6.1**
    """
    work_dir = tempfile.mkdtemp(prefix="llm-image-trichotomy-")
    try:
        bindings = []
        resolved_paths = []
        for index, (shape, payload) in enumerate(specs):
            binding, resolved_path = _binding(
                index, shape, work_dir, payload)
            bindings.append(binding)
            resolved_paths.append(resolved_path)

        document = _make_document(bindings)
        invoker = _ArityRecordingInvoker(text="answer")
        processor = LlmInferenceProcessor(invoker=invoker)

        metadata = processor.process(document, {"seed": 1}, work_dir)

        # Build the expected invocation sequence: one call per readable or
        # text-only binding, in binding order; zero calls for unreadable
        # ones (Requirement 2.3).
        expected_calls = []
        for index, (shape, payload) in enumerate(specs):
            model_name = "qwen2-vl-{0}".format(index)
            prompt = "Describe part {0}.".format(index)
            parameters = bindings[index]["parameters"]
            if shape == "readable":
                # (a) exactly one invocation carrying the file's bytes
                # base64-encoded as the 4th positional argument, with the
                # {work_dir} placeholder resolved before reading (2.1).
                expected_calls.append((
                    model_name, prompt, parameters,
                    base64.b64encode(payload).decode("ascii"),
                ))
            elif shape in ("absent", "none"):
                # (b) exactly one invocation with the pre-feature 3-arg
                # form — request identical to pre-feature behavior
                # (2.2, 6.1).
                expected_calls.append((model_name, prompt, parameters))
        assert invoker.calls == expected_calls

        # Per-node outcomes: every binding produced an outcome — remaining
        # bindings after an unreadable frame are still processed (2.5).
        assert set(metadata["llm"].keys()) == {
            "llm{0}".format(i) for i in range(len(specs))
        }
        for index, (shape, _payload) in enumerate(specs):
            node_id = "llm{0}".format(index)
            outcome = metadata["llm"][node_id]
            if shape == "unreadable":
                # (c) a Node_Error_Record naming the node and the resolved
                # path (2.3); the generated text key is absent.
                assert set(outcome.keys()) == {"error"}
                assert node_id in outcome["error"]
                assert resolved_paths[index] in outcome["error"]
                assert "in" in outcome["error"]
            else:
                assert outcome == {"generated_text": "answer"}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
