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
"""Property test for the LLM_Binding Output_Token_Budget resolution
(task 5.3).

# Feature: vllm-workflow-latency-optimization, Property 7: Output_Token_Budget resolution is total and explicit

*For any* configured ``max_tokens`` value — a valid integral number >= 1,
absent, or invalid (non-numeric, non-positive, boolean, non-integral) —
the LLM_Binding SHALL send a request whose ``max_tokens`` equals the
configured value when valid and exactly 256 otherwise, and SHALL log a
substitution notice naming the rejected value if and only if the
configured value was present and invalid.

**Validates: Requirements 3.1, 3.3, 3.4**

The test hypothesis-generates ``max_tokens`` configurations across four
scenarios — valid (ints and integral floats >= 1), absent (the parameter
key omitted entirely), explicitly-configured ``None``, and invalid
(booleans, non-positive numbers, non-integral floats, nan/inf, strings,
lists, dicts) — drives ``LlmInferenceProcessor`` over a compiled document
with a recording invoker (the harness pattern of
``test/backend-test/workflow_engine/test_llm_generation_metrics_path.py``),
and asserts on the exact request parameters the invoker received plus the
WARNING records captured through a ``logging.Handler`` attached inside
the test body (caplog and hypothesis do not mix across examples).

Explicitly-configured ``None`` is indistinguishable from an absent
parameter at the ``parameters.get("max_tokens")`` seam, and the resolver
defines ``None -> (256, None)`` — no substitution notice — so the test
asserts exactly that (the resolver's documented "absent" semantics).
"""
import logging
import math

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.output_bindings import (
    DEFAULT_OUTPUT_TOKEN_BUDGET,
    LlmInferenceProcessor,
)

#: Sentinel for "no max_tokens parameter at all" (key omitted).
_ABSENT = object()

#: The substitution notice's stable identifying fragment
#: (resolve_output_token_budget's message).
_NOTICE_FRAGMENT = "invalid max_tokens value"


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_VALID_INTS = st.integers(min_value=1, max_value=10**9)

#: Integral floats >= 1.0 — accepted as their int value.
_VALID_INTEGRAL_FLOATS = _VALID_INTS.map(float)

_VALID = st.one_of(_VALID_INTS, _VALID_INTEGRAL_FLOATS)

_INVALID = st.one_of(
    # Booleans are explicitly excluded from the numeric acceptance,
    # even though bool is an int subclass and True == 1.
    st.booleans(),
    # Non-positive integers (0, negatives).
    st.integers(min_value=-10**9, max_value=0),
    # Non-positive floats (includes 0.0, -0.0, negative integral and
    # non-integral values).
    st.floats(max_value=0.0, allow_nan=False, allow_infinity=False),
    # Positive but non-integral floats.
    st.floats(
        min_value=1.0, max_value=10**9,
        allow_nan=False, allow_infinity=False,
    ).filter(lambda value: not value.is_integer()),
    # Non-finite floats.
    st.sampled_from([float("nan"), float("inf"), float("-inf")]),
    # Non-numeric types.
    st.text(max_size=12),
    st.lists(st.integers(min_value=-5, max_value=5), max_size=3),
    st.dictionaries(st.text(max_size=4),
                    st.integers(min_value=-5, max_value=5), max_size=2),
)

#: Tagged scenarios: (kind, configured value).
_SCENARIOS = st.one_of(
    st.just(("absent", _ABSENT)),
    st.just(("none", None)),
    st.tuples(st.just("valid"), _VALID),
    st.tuples(st.just("invalid"), _INVALID),
)


# ---------------------------------------------------------------------------
# Harness (adapted from workflow_engine/test_llm_generation_metrics_path.py)
# ---------------------------------------------------------------------------

class RecordingInvoker:
    """Pre-feature 3-parameter fake snapshotting the request parameters
    at call time, so the asserted ``max_tokens`` is exactly what the
    binding placed on the request."""

    def __init__(self, text="generated answer"):
        self.text = text
        self.calls = []

    def __call__(self, model_name, prompt, parameters):
        self.calls.append((model_name, prompt, dict(parameters)))
        return self.text


class _RecordCollector(logging.Handler):
    """Collects every record emitted through the bindings logger —
    attached inside the test body because caplog and hypothesis do not
    mix across examples."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _llm_binding(kind, raw, node_id="llm1"):
    parameters = {
        "modelName": "opt-125m",
        "prompt_template": "Summarize the run.",
    }
    if kind != "absent":
        parameters["max_tokens"] = raw
    return {
        "nodeId": node_id,
        "binding": "llm_inference",
        "parameters": parameters,
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
    }


def _make_document(bindings):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": "x86_64",
        "segments": [],
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }


def _process_recording(kind, raw):
    """Run one binding through the processor, returning the recorded
    invoker calls and the captured log records."""
    invoker = RecordingInvoker()
    processor = LlmInferenceProcessor(invoker=invoker)
    collector = _RecordCollector()
    bindings_logger = logging.getLogger("workflow_engine.output_bindings")
    previous_level = bindings_logger.level
    bindings_logger.addHandler(collector)
    bindings_logger.setLevel(logging.DEBUG)
    try:
        metadata = processor.process(
            _make_document([_llm_binding(kind, raw)]), {})
    finally:
        bindings_logger.removeHandler(collector)
        bindings_logger.setLevel(previous_level)
    return invoker.calls, collector.records, metadata


def _expected(kind, raw):
    """Independent oracle: (expected max_tokens, notice expected?)."""
    if kind == "valid":
        return int(raw), False
    if kind == "invalid":
        return DEFAULT_OUTPUT_TOKEN_BUDGET, True
    # absent / explicitly-configured None: documented default, silent.
    return DEFAULT_OUTPUT_TOKEN_BUDGET, False


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

# Feature: vllm-workflow-latency-optimization, Property 7: Output_Token_Budget resolution is total and explicit
@settings(max_examples=200, deadline=None)
@given(scenario=_SCENARIOS)
def test_property_7_output_token_budget_resolution_total_and_explicit(
        scenario):
    """**Feature: vllm-workflow-latency-optimization, Property 7:
    Output_Token_Budget resolution is total and explicit**

    **Validates: Requirements 3.1, 3.3, 3.4**
    """
    kind, raw = scenario
    expected_budget, expect_notice = _expected(kind, raw)

    calls, records, metadata = _process_recording(kind, raw)

    # The binding always reached the invoker exactly once (resolution is
    # total — no configured value fails the node before invocation).
    assert len(calls) == 1
    model_name, prompt, sent_parameters = calls[0]
    assert metadata["llm"]["llm1"] == {"generated_text": "generated answer"}

    # Explicit max_tokens on every request: the configured value when
    # valid (R3.1), exactly 256 otherwise (R3.3, R3.4), always a plain
    # int (never bool, never float).
    assert "max_tokens" in sent_parameters
    sent = sent_parameters["max_tokens"]
    assert type(sent) is int
    assert sent == expected_budget
    if kind == "valid":
        # Equality with the configured value, robust across the int and
        # integral-float acceptance forms.
        assert not math.isnan(float(raw))
        assert sent == raw

    # Substitution notice iff present-and-invalid (R3.3): one WARNING
    # naming the node, the rejected value verbatim (repr), and the
    # substituted 256-token default. Valid / absent / None: no notice at
    # any level.
    notices = [record for record in records
               if _NOTICE_FRAGMENT in record.getMessage()]
    if expect_notice:
        assert len(notices) == 1
        record = notices[0]
        assert record.levelno == logging.WARNING
        message = record.getMessage()
        assert "llm1" in message
        assert repr(raw) in message
        assert str(DEFAULT_OUTPUT_TOKEN_BUDGET) in message
    else:
        assert notices == []
