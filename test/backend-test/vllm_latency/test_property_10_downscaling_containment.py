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
"""Property test for the LLM_Binding's image-downscaling configuration
handling and failure containment (task 6.3).

# Feature: vllm-workflow-latency-optimization, Property 10: Downscaling configuration and failure containment

*For any* captured image bytes: an unconfigured downscaling option SHALL
send the bytes byte-identical to pre-feature behavior with no warning; an
invalid configured value (non-positive or non-numeric) SHALL send the
bytes unmodified and emit exactly one run-log warning naming the invalid
value; and a failing downscale operation SHALL send the original bytes,
emit a run-log warning naming the failure, and produce a node outcome of
the same shape and terminal effect as a run without the failure.

**Validates: Requirements 5.4, 5.6, 5.8**

The test hypothesis-generates the configuration variant (absent, valid
integral values including integral floats, invalid values of several
types), the captured 'in' frame (a real decodable JPEG of random small
dimensions, or undecodable garbage bytes), and an optional 'reference'
frame with its own validity, then drives ``LlmInferenceProcessor._run_one``
through ``process`` with a recording fake invoker and asserts per the
property statement. Every run with a valid or failing downscale is paired
with a no-failure baseline run (same configuration, decodable frames) so
the "same node outcome shape and terminal effect" clause is checked
against a direct oracle rather than a hardcoded expectation.

Hypothesis and the pytest ``tmp_path``/``caplog`` fixtures do not mix
(function-scoped fixtures are reused across examples), so the frames are
staged in a ``tempfile.TemporaryDirectory`` and log records are captured
with a ``logging.Handler`` attached inside the test body per example.
"""
import base64
import io
import logging
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from workflow_engine.output_bindings import LlmInferenceProcessor

NODE_ID = "llm1"

#: Undecodable prefix: no image format Pillow knows starts with a NUL
#: byte, so any frame built on it makes ``downscale_image_bytes`` raise.
_GARBAGE_PREFIX = b"\x00not-an-image"


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Valid configured values: integral numbers >= 1 (bool excluded,
#: integral floats accepted as their int value).
_VALID_VALUES = st.one_of(
    st.integers(min_value=1, max_value=4096),
    st.integers(min_value=1, max_value=4096).map(float),
)

#: Invalid configured values: non-positive, non-numeric, boolean,
#: non-integral. Fixed samples so the repr-in-warning check is stable.
_INVALID_VALUES = st.sampled_from([
    0, -1, -7, 0.0, 0.5, -2.5, True, False, "640", "abc",
    float("nan"), float("inf"), [], {},
])

_CONFIG = st.one_of(
    st.tuples(st.just("absent"), st.none()),
    st.tuples(st.just("valid"), _VALID_VALUES),
    st.tuples(st.just("invalid"), _INVALID_VALUES),
)

_DIMS = st.tuples(st.integers(min_value=1, max_value=48),
                  st.integers(min_value=1, max_value=48))

_GARBAGE_TAILS = st.binary(min_size=0, max_size=16)


# ---------------------------------------------------------------------------
# Harness (mirrors test/backend-test/workflow_engine/
# test_llm_image_downscaling.py)
# ---------------------------------------------------------------------------

def _jpeg_bytes(width, height, color=(30, 90, 160)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _llm_binding(capture_paths, **params):
    parameters = {
        "modelName": "qwen2-vl-2b",
        "prompt_template": "Describe the image.",
        "max_tokens": 128,
    }
    parameters.update(params)
    return {
        "nodeId": NODE_ID,
        "binding": "llm_inference",
        "parameters": parameters,
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
        "capturePaths": capture_paths,
    }


def _make_document(binding):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": "x86_64",
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
        "executorBindings": [binding],
        "pluginDependencies": [],
    }


class _RecordingInvoker:
    def __init__(self, text="generated answer"):
        self.text = text
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.text


class _RecordCollector(logging.Handler):
    """Collects records from the bindings logger — attached inside the
    test body because caplog and hypothesis do not mix across examples."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _execute(config_variant, config_value, in_bytes, ref_bytes):
    """Stage the frames, run the binding, and return
    (metadata, invoker_calls, log_records)."""
    params = {}
    if config_variant != "absent":
        params["max_image_dimension"] = config_value

    collector = _RecordCollector()
    bindings_logger = logging.getLogger("workflow_engine.output_bindings")
    previous_level = bindings_logger.level
    bindings_logger.addHandler(collector)
    bindings_logger.setLevel(logging.DEBUG)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            (work_dir / "frame_in.jpg").write_bytes(in_bytes)
            capture_paths = {"in": "{work_dir}/frame_in.jpg"}
            if ref_bytes is not None:
                (work_dir / "frame_ref.jpg").write_bytes(ref_bytes)
                capture_paths["reference"] = "{work_dir}/frame_ref.jpg"
            invoker = _RecordingInvoker()
            processor = LlmInferenceProcessor(invoker=invoker)
            document = _make_document(
                _llm_binding(capture_paths, **params))
            metadata = processor.process(document, {"seed": 1}, str(work_dir))
    finally:
        bindings_logger.removeHandler(collector)
        bindings_logger.setLevel(previous_level)
    return metadata, invoker.calls, collector.records


def _invalid_notice_warnings(records):
    return [r for r in records
            if r.levelno == logging.WARNING
            and "invalid max_image_dimension value" in r.getMessage()]


def _downscale_failure_warnings(records):
    return [r for r in records
            if r.levelno == logging.WARNING
            and "downscaling the captured" in r.getMessage()
            and "failed" in r.getMessage()]


def _all_downscaling_records(records):
    return [r for r in records
            if "max_image_dimension" in r.getMessage()
            or "downscal" in r.getMessage()]


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

# Feature: vllm-workflow-latency-optimization, Property 10: Downscaling configuration and failure containment
@settings(max_examples=100, deadline=None)
@given(
    config=_CONFIG,
    in_dims=_DIMS,
    in_garbage=st.booleans(),
    ref_present=st.booleans(),
    ref_dims=_DIMS,
    ref_garbage=st.booleans(),
    garbage_tail=_GARBAGE_TAILS,
)
def test_property_10_downscaling_configuration_and_failure_containment(
        config, in_dims, in_garbage, ref_present, ref_dims, ref_garbage,
        garbage_tail):
    """**Feature: vllm-workflow-latency-optimization, Property 10:
    Downscaling configuration and failure containment**

    **Validates: Requirements 5.4, 5.6, 5.8**
    """
    config_variant, config_value = config

    good_in = _jpeg_bytes(*in_dims)
    good_ref = _jpeg_bytes(*ref_dims, color=(200, 40, 40))
    garbage = _GARBAGE_PREFIX + garbage_tail

    in_bytes = garbage if in_garbage else good_in
    ref_bytes = None
    if ref_present:
        ref_bytes = garbage if ref_garbage else good_ref

    metadata, calls, records = _execute(
        config_variant, config_value, in_bytes, ref_bytes)

    # Terminal effect: process() returned normally, exactly one
    # invocation happened with the pre-feature arity, and the node
    # outcome landed under metadata['llm'][node] without an error key.
    assert len(calls) == 1
    assert len(calls[0]) == (5 if ref_present else 4)
    outcome = metadata["llm"][NODE_ID]
    assert outcome == {"generated_text": "generated answer"}

    sent_in = base64.b64decode(calls[0][3])
    sent_ref = base64.b64decode(calls[0][4]) if ref_present else None

    invalid_warnings = _invalid_notice_warnings(records)
    failure_warnings = _downscale_failure_warnings(records)

    if config_variant == "absent":
        # R5.6: unconfigured ⇒ byte-identical bytes, no downscaling
        # warning (or any downscaling-related record) at all.
        assert sent_in == in_bytes
        if ref_present:
            assert sent_ref == ref_bytes
        assert _all_downscaling_records(records) == []

    elif config_variant == "invalid":
        # R5.8: invalid configured value ⇒ treated as unconfigured
        # (bytes unmodified) plus exactly one WARNING naming the value;
        # no downscale is attempted, so no failure warning either.
        assert sent_in == in_bytes
        if ref_present:
            assert sent_ref == ref_bytes
        assert len(invalid_warnings) == 1
        message = invalid_warnings[0].getMessage()
        assert repr(config_value) in message
        assert NODE_ID in message
        assert failure_warnings == []

    else:  # valid
        # R5.4: a failing downscale (undecodable frame) sends the
        # ORIGINAL bytes and emits one WARNING per failing frame naming
        # the node and the failure; decodable frames produce no
        # containment warning. No invalid-value notice in any case.
        assert invalid_warnings == []
        expected_failures = 0
        if in_garbage:
            expected_failures += 1
            assert sent_in == in_bytes
            assert any(NODE_ID in w.getMessage() and "'in'" in w.getMessage()
                       for w in failure_warnings)
        if ref_present and ref_garbage:
            expected_failures += 1
            assert sent_ref == ref_bytes
            assert any(NODE_ID in w.getMessage()
                       and "'reference'" in w.getMessage()
                       for w in failure_warnings)
        assert len(failure_warnings) == expected_failures

        # Paired no-failure baseline (same configuration, decodable
        # frames): the failing run's node outcome shape and terminal
        # effect equal the no-failure case's.
        baseline_metadata, baseline_calls, _ = _execute(
            config_variant, config_value, good_in,
            good_ref if ref_present else None)
        baseline_outcome = baseline_metadata["llm"][NODE_ID]
        assert outcome == baseline_outcome
        assert set(outcome.keys()) == set(baseline_outcome.keys())
        assert len(baseline_calls) == 1
        assert len(baseline_calls[0]) == len(calls[0])
        assert set(metadata.keys()) == set(baseline_metadata.keys())
