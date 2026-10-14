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
"""Property test for the LLM_Binding run-log breakdown emission (task 5.4).

# Feature: vllm-workflow-latency-optimization, Property 3: Run-log breakdown emission with iff-truncation

*For any* generation_metrics payload delivered by the invoker to the
LLM_Binding, the processor SHALL emit exactly one run-log line containing
every breakdown field — rendering unavailable fields as "unavailable" and a
non-applicable image token count as "n/a" — and that line SHALL state that
the output was truncated at the Output_Token_Budget if and only if the
payload reports truncation.

**Validates: Requirements 1.2, 3.5, 3.6**

The test drives ``workflow_engine.output_bindings.LlmInferenceProcessor``
end to end through an injected metrics-aware invoker (the
``MetricsAwareInvoker`` harness pattern from
``test_llm_generation_metrics_path.py``) that delivers a
hypothesis-generated ``generation_metrics`` payload through the
``metrics_sink`` exactly like the real invoker's 200 path. Payloads are
shaped like ``GenerationPhaseBreakdown.to_payload()`` output — ints or
``"unavailable"`` for phases and counts, ``"n/a"`` additionally for the
image token count, ``True``/``False``/``"unavailable"`` for ``truncated``,
a bool for ``prefill_includes_queueing`` — plus edge shapes with any
subset of keys missing (a missing key must degrade to ``"unavailable"``,
never drop the field from the line).

Log records are captured with a ``logging.Handler`` attached inside the
test body (``caplog`` and hypothesis do not mix): the run-log capture
mechanism attaches handlers to this same logging tree, so a record
reaching the module logger is exactly what lands in the run log while
capture is active (R1.2).

The expected rendering of each field is recomputed by an independent
oracle in this file, mirroring the documented payload contract: an
int/float renders as ``"<v> ms"`` for phases and ``str(v)`` for counts;
anything else (including a missing key) renders as its string form with
missing degrading to ``"unavailable"``; the prefill label carries
``(includes queueing)`` exactly when the payload flag is truthy. The
truncation statement ``output truncated at the output token budget``
appears if and only if the payload's ``truncated`` is exactly ``True``
(R3.5, R3.6).
"""
import logging

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.output_bindings import LlmInferenceProcessor

NODE_ID = "llm1"
MODEL_NAME = "opt-125m"
BREAKDOWN_MARKER = "LLM generation breakdown"
TRUNCATION_STATEMENT = "output truncated at the output token budget"

_MS_KEYS = ("queueing_ms", "prefill_ms", "decode_ms")
_COUNT_KEYS = ("prompt_tokens", "output_tokens")
_ALL_KEYS = _MS_KEYS + _COUNT_KEYS + (
    "image_tokens", "truncated", "prefill_includes_queueing")


# ---------------------------------------------------------------------------
# Harness (MetricsAwareInvoker pattern from test_llm_generation_metrics_path)
# ---------------------------------------------------------------------------

class MetricsAwareInvoker:
    """Fake invoker declaring the keyword-only ``metrics_sink``,
    delivering the generated payload through it like the real invoker's
    200 path, then returning the generated text."""

    def __init__(self, metrics):
        self.metrics = metrics

    def __call__(self, model_name, prompt, parameters, *,
                 metrics_sink=None):
        if metrics_sink is not None:
            metrics_sink(self.metrics)
        return "generated answer"


class _RecordingHandler(logging.Handler):
    """Collects every record reaching the module logger — the same tree
    the run-log capture attaches to while a run is active (R1.2)."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def make_document():
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": "arm64",
        "segments": [],
        "executorBindings": [{
            "nodeId": NODE_ID,
            "binding": "llm_inference",
            "parameters": {
                "modelName": MODEL_NAME,
                "prompt_template": "Summarize the run.",
                "max_tokens": 128,
            },
            "upstreamNodeIds": ["cam"],
            "downstreamNodeIds": ["mqtt"],
        }],
        "pluginDependencies": [],
    }


# ---------------------------------------------------------------------------
# Strategies: to_payload()-shaped dicts plus missing-key edge shapes
# ---------------------------------------------------------------------------

_INT_VALUE = st.integers(min_value=0, max_value=10 ** 6)
_MS_VALUE = st.one_of(_INT_VALUE, st.just("unavailable"))
_COUNT_VALUE = st.one_of(_INT_VALUE, st.just("unavailable"))
_IMAGE_VALUE = st.one_of(_INT_VALUE, st.just("unavailable"), st.just("n/a"))
_TRUNCATED_VALUE = st.sampled_from([True, False, "unavailable"])


@st.composite
def _payloads(draw):
    full = {
        "queueing_ms": draw(_MS_VALUE),
        "prefill_ms": draw(_MS_VALUE),
        "decode_ms": draw(_MS_VALUE),
        "prefill_includes_queueing": draw(st.booleans()),
        "prompt_tokens": draw(_COUNT_VALUE),
        "output_tokens": draw(_COUNT_VALUE),
        "image_tokens": draw(_IMAGE_VALUE),
        "truncated": draw(_TRUNCATED_VALUE),
    }
    # Edge shapes: any subset of keys missing (including all of them);
    # a missing field must still appear in the line as "unavailable".
    dropped = draw(st.sets(st.sampled_from(_ALL_KEYS)))
    return {key: value for key, value in full.items()
            if key not in dropped}


# ---------------------------------------------------------------------------
# Independent rendering oracle (documented payload contract)
# ---------------------------------------------------------------------------

def _rendered_ms(payload, key):
    value = payload.get(key, "unavailable")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "{0} ms".format(value)
    return str(value)


def _rendered_count(payload, key):
    return str(payload.get(key, "unavailable"))


def _expected_prefill_label(payload):
    return ("prefill (includes queueing)"
            if payload.get("prefill_includes_queueing") else "prefill")


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

# Feature: vllm-workflow-latency-optimization, Property 3: Run-log breakdown emission with iff-truncation
@settings(max_examples=150, deadline=None)
@given(payload=_payloads())
def test_property_3_runlog_breakdown_emission_with_iff_truncation(payload):
    """**Feature: vllm-workflow-latency-optimization, Property 3: Run-log
    breakdown emission with iff-truncation**

    **Validates: Requirements 1.2, 3.5, 3.6**
    """
    processor = LlmInferenceProcessor(
        invoker=MetricsAwareInvoker(metrics=payload))
    module_logger = logging.getLogger("workflow_engine.output_bindings")
    handler = _RecordingHandler()
    previous_level = module_logger.level
    module_logger.addHandler(handler)
    module_logger.setLevel(logging.DEBUG)
    try:
        metadata = processor.process(make_document(), {})
    finally:
        module_logger.removeHandler(handler)
        module_logger.setLevel(previous_level)

    # The binding itself succeeded — the emission path was reached.
    assert metadata["llm"][NODE_ID]["generated_text"] == "generated answer"

    # R1.2: exactly one run-log breakdown line, at INFO (run-log capture
    # records INFO while active).
    breakdown_records = [
        record for record in handler.records
        if BREAKDOWN_MARKER in record.getMessage()
    ]
    assert len(breakdown_records) == 1
    record = breakdown_records[0]
    assert record.levelno == logging.INFO
    line = record.getMessage()

    # The line names the node and the model.
    assert line.startswith(
        "LLM generation breakdown (node {0}, model {1}): ".format(
            NODE_ID, MODEL_NAME))

    # R1.2: every breakdown field is present with its rendering —
    # unavailable fields (missing keys included) as "unavailable", a
    # non-applicable image token count as "n/a" — never dropped.
    assert "queueing {0}, ".format(_rendered_ms(payload, "queueing_ms")) \
        in line
    assert "{0} {1}, decode ".format(
        _expected_prefill_label(payload),
        _rendered_ms(payload, "prefill_ms")) in line
    assert "decode {0}, ".format(_rendered_ms(payload, "decode_ms")) in line
    assert "prompt tokens {0}, ".format(
        _rendered_count(payload, "prompt_tokens")) in line
    assert "image tokens {0}, ".format(
        _rendered_count(payload, "image_tokens")) in line
    assert "output tokens {0};".format(
        _rendered_count(payload, "output_tokens")) in line

    # R3.5 / R3.6: the truncation statement appears if and only if the
    # payload reports truncation (truncated exactly True).
    assert (TRUNCATION_STATEMENT in line) == \
        (payload.get("truncated") is True)
