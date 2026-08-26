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
"""Example tests for the run-log Generation_Phase_Breakdown line format
(vllm-workflow-latency-optimization task 5.5).

Asserts the concrete, byte-exact formatted line produced by
``workflow_engine.output_bindings._format_generation_metrics_line`` for:

* the design document's representative truncated breakdown (node, model,
  all three phases, all three token counts, truncation clause naming the
  Output_Token_Budget);
* an untruncated breakdown ("output not truncated");
* unavailable phase/count renderings (including missing payload keys
  degrading to ``unavailable`` and a non-True/False ``truncated`` value
  rendering as "truncation unavailable");
* the ``n/a`` image-token rendering for an image-less request;
* the "prefill (includes queueing)" label on the manager-clock fallback
  path.

Also cross-checks that ``GenerationPhaseBreakdown.to_log_line`` and the
payload-side formatter driven through ``to_payload()`` produce the same
line.

**Validates: Requirements 1.2, 3.5**
"""
from vllm_runtime.generation_metrics import GenerationPhaseBreakdown
from workflow_engine.output_bindings import _format_generation_metrics_line

_NODE = "llm_inference_1"
_MODEL = "qwen3-vl-8b"


def _representative_payload():
    """The design document's representative truncated breakdown payload
    (design.md, Data Models, "Run-log emission line")."""
    return {
        "queueing_ms": 12,
        "prefill_ms": 842,
        "decode_ms": 16571,
        "prefill_includes_queueing": False,
        "prompt_tokens": 1180,
        "output_tokens": 256,
        "image_tokens": 1024,
        "truncated": True,
    }


def test_representative_truncated_breakdown_line():
    """The design's representative example renders byte-exactly, with the
    truncation clause naming the Output_Token_Budget (256)."""
    line = _format_generation_metrics_line(
        _NODE, _MODEL, _representative_payload())
    assert line == (
        "LLM generation breakdown (node llm_inference_1, model qwen3-vl-8b): "
        "queueing 12 ms, prefill 842 ms, decode 16571 ms, "
        "prompt tokens 1180, image tokens 1024, output tokens 256; "
        "output truncated at the output token budget (256)"
    )


def test_untruncated_breakdown_line():
    """``truncated: false`` renders the "output not truncated" clause."""
    payload = _representative_payload()
    payload["truncated"] = False
    payload["output_tokens"] = 42
    line = _format_generation_metrics_line(_NODE, _MODEL, payload)
    assert line == (
        "LLM generation breakdown (node llm_inference_1, model qwen3-vl-8b): "
        "queueing 12 ms, prefill 842 ms, decode 16571 ms, "
        "prompt tokens 1180, image tokens 1024, output tokens 42; "
        "output not truncated"
    )


def test_unavailable_fields_render_and_are_never_dropped():
    """``"unavailable"`` payload values render verbatim (no " ms" suffix),
    and a non-True/False ``truncated`` renders "truncation unavailable"."""
    payload = {
        "queueing_ms": "unavailable",
        "prefill_ms": "unavailable",
        "decode_ms": "unavailable",
        "prefill_includes_queueing": False,
        "prompt_tokens": "unavailable",
        "output_tokens": "unavailable",
        "image_tokens": "unavailable",
        "truncated": "unavailable",
    }
    line = _format_generation_metrics_line(_NODE, _MODEL, payload)
    assert line == (
        "LLM generation breakdown (node llm_inference_1, model qwen3-vl-8b): "
        "queueing unavailable, prefill unavailable, "
        "decode unavailable, prompt tokens unavailable, "
        "image tokens unavailable, output tokens unavailable; "
        "truncation unavailable"
    )


def test_missing_payload_keys_degrade_to_unavailable():
    """A hostile/empty payload never drops a field: every missing key
    degrades to ``unavailable`` and truncation to its unavailable form."""
    line = _format_generation_metrics_line(_NODE, _MODEL, {})
    assert line == (
        "LLM generation breakdown (node llm_inference_1, model qwen3-vl-8b): "
        "queueing unavailable, prefill unavailable, "
        "decode unavailable, prompt tokens unavailable, "
        "image tokens unavailable, output tokens unavailable; "
        "truncation unavailable"
    )


def test_truncated_without_usable_output_tokens_omits_budget_number():
    """``truncated: true`` with an unavailable output-token count still
    states the truncation, just without the budget number."""
    payload = _representative_payload()
    payload["output_tokens"] = "unavailable"
    line = _format_generation_metrics_line(_NODE, _MODEL, payload)
    assert line == (
        "LLM generation breakdown (node llm_inference_1, model qwen3-vl-8b): "
        "queueing 12 ms, prefill 842 ms, decode 16571 ms, "
        "prompt tokens 1180, image tokens 1024, output tokens "
        "unavailable; output truncated at the output token budget"
    )


def test_image_less_request_renders_image_tokens_na():
    """An image-less request's payload carries ``image_tokens: "n/a"``
    (R5.5) and the line renders it verbatim."""
    payload = _representative_payload()
    payload["image_tokens"] = "n/a"
    payload["truncated"] = False
    line = _format_generation_metrics_line(_NODE, _MODEL, payload)
    assert line == (
        "LLM generation breakdown (node llm_inference_1, model qwen3-vl-8b): "
        "queueing 12 ms, prefill 842 ms, decode 16571 ms, "
        "prompt tokens 1180, image tokens n/a, output tokens 256; "
        "output not truncated"
    )


def test_prefill_includes_queueing_label():
    """The manager-clock fallback path (queueing unavailable,
    ``prefill_includes_queueing: true``) relabels the prefill phase."""
    payload = {
        "queueing_ms": "unavailable",
        "prefill_ms": 900,
        "decode_ms": 15000,
        "prefill_includes_queueing": True,
        "prompt_tokens": 1180,
        "output_tokens": 30,
        "image_tokens": 1024,
        "truncated": False,
    }
    line = _format_generation_metrics_line(_NODE, _MODEL, payload)
    assert line == (
        "LLM generation breakdown (node llm_inference_1, model qwen3-vl-8b): "
        "queueing unavailable, prefill (includes queueing) 900 ms, "
        "decode 15000 ms, prompt tokens 1180, image tokens 1024, "
        "output tokens 30; output not truncated"
    )


def test_payload_formatter_matches_dataclass_to_log_line():
    """Driving the payload-side formatter with ``to_payload()`` reproduces
    ``GenerationPhaseBreakdown.to_log_line`` byte-exactly, for the
    representative, fallback/n-a, and all-unavailable breakdowns."""
    breakdowns = [
        # Representative truncated breakdown (design example).
        GenerationPhaseBreakdown(
            queueing_ms=12, prefill_ms=842, decode_ms=16571,
            prompt_tokens=1180, output_tokens=256, image_tokens=1024,
            image_tokens_applicable=True, truncated=True,
            prefill_includes_queueing=False),
        # Fallback path, image-less, untruncated.
        GenerationPhaseBreakdown(
            queueing_ms=None, prefill_ms=900, decode_ms=15000,
            prompt_tokens=64, output_tokens=30, image_tokens=None,
            image_tokens_applicable=False, truncated=False,
            prefill_includes_queueing=True),
        # Everything unavailable.
        GenerationPhaseBreakdown(
            queueing_ms=None, prefill_ms=None, decode_ms=None,
            prompt_tokens=None, output_tokens=None, image_tokens=None,
            image_tokens_applicable=True, truncated=None,
            prefill_includes_queueing=False),
    ]
    for breakdown in breakdowns:
        expected = breakdown.to_log_line(_NODE, _MODEL)
        actual = _format_generation_metrics_line(
            _NODE, _MODEL, breakdown.to_payload())
        assert actual == expected
