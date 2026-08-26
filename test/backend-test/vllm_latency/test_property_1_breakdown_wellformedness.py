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
"""Property test for Generation_Phase_Breakdown construction (task 1.2).

# Feature: vllm-workflow-latency-optimization, Property 1: Breakdown well-formedness and measurement honesty

*For any* completed Generation_Call driven through a fake engine — with or
without engine-level metrics, with or without image data, including all-zero
timings and partially unreadable outputs — the recorded
Generation_Phase_Breakdown SHALL have every phase field either a non-negative
integer of milliseconds or marked unavailable; SHALL mark every metric the
engine did not expose as unavailable (never an estimated value); SHALL never
report all phases as zero simultaneously (that case becomes all-unavailable);
SHALL report prompt and output token counts matching the final output's token
id lists; SHALL report the image token count equal to the number of image
placeholder token ids in the prompt when an image was supplied and the
placeholder id is readable; and SHALL mark the image token count not
applicable exactly when the request carried no image data.

**Validates: Requirements 1.1, 1.3, 1.4, 1.6, 5.1, 5.5**

The module under test (``vllm_runtime.generation_metrics``) is pure, so the
test drives :func:`build_breakdown` directly with hypothesis-generated fake
``RequestOutput``-shaped objects (``SimpleNamespace`` plus hostile stand-ins
with raising attributes / raising ``__len__``), covering:

* V0-style ``metrics`` objects with usable, unreadable, negative and
  all-equal timestamps;
* V1-style ``metrics is None`` with manager monotonic timestamps (usable
  and hostile), including all-equal timestamps;
* missing / ``None`` / non-sequence / hostile token id lists and outputs;
* image supplied or not, placeholder token id readable or not.

The expected values are recomputed by an independent oracle in this file:
a metric is *expected* only when its source timestamps are readable finite
non-bool numbers forming a non-negative interval — anything else must be
marked unavailable, never estimated (measurement honesty).
"""
import math
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from vllm_runtime.generation_metrics import (
    NOT_APPLICABLE,
    UNAVAILABLE,
    build_breakdown,
)

_METRIC_FIELDS = ("arrival_time", "first_scheduled_time",
                  "first_token_time", "finished_time")

_PAYLOAD_KEYS = {
    "queueing_ms", "prefill_ms", "decode_ms", "prefill_includes_queueing",
    "prompt_tokens", "output_tokens", "image_tokens", "truncated",
}


# ---------------------------------------------------------------------------
# Hostile stand-ins ("partially unreadable outputs")
# ---------------------------------------------------------------------------

class _RaisingAttrs:
    """Every attribute read raises — an unreadable engine object."""

    def __getattr__(self, name):
        raise RuntimeError("hostile attribute: {}".format(name))


class _BadLen:
    """``len()`` raises — a token id list that cannot be counted."""

    def __len__(self):
        raise RuntimeError("hostile len")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_USABLE_TS = st.floats(min_value=-1e3, max_value=1e6,
                       allow_nan=False, allow_infinity=False)
_HOSTILE_TS = st.sampled_from(
    [None, float("nan"), float("inf"), float("-inf"), True, False, "soon"])
_TS = st.one_of(_USABLE_TS, _HOSTILE_TS)

_TOKEN_ID = st.integers(min_value=0, max_value=50)
_TOKEN_LIST = st.lists(_TOKEN_ID, max_size=40)
_TOKEN_IDS_SOURCE = st.one_of(
    _TOKEN_LIST,                       # readable list
    st.none(),                         # explicitly missing
    st.just("missing"),                # attribute absent entirely
    st.integers(min_value=0, max_value=9),   # no len(), not iterable
    st.builds(_BadLen),                # len() raises
)

_FINISH_REASON = st.sampled_from(
    ["length", "stop", "abort", "", None, 123, True])


@st.composite
def _metrics_objects(draw):
    """V0-style metrics: absent, fully usable, all-equal (all-zero phases),
    partially unreadable, or entirely hostile."""
    kind = draw(st.sampled_from(
        ["none", "none", "full", "full", "equal", "partial", "raising"]))
    if kind == "none":
        return None
    if kind == "raising":
        return _RaisingAttrs()
    if kind == "equal":
        base = draw(_USABLE_TS)
        return SimpleNamespace(**{name: base for name in _METRIC_FIELDS})
    if kind == "partial":
        present = draw(st.sets(st.sampled_from(_METRIC_FIELDS)))
        return SimpleNamespace(
            **{name: draw(_TS) for name in _METRIC_FIELDS
               if name in present})
    # "full": each timestamp independently usable or hostile
    return SimpleNamespace(**{name: draw(_TS) for name in _METRIC_FIELDS})


@st.composite
def _cases(draw):
    """One complete build_breakdown input set."""
    # Manager monotonic timestamps: independent, or all-equal (zero phases).
    if draw(st.booleans()):
        t_submit, t_first, t_last = (draw(_TS) for _ in range(3))
    else:
        base = draw(_USABLE_TS)
        t_submit = t_first = t_last = base

    prompt_token_ids = draw(_TOKEN_IDS_SOURCE)

    outputs_kind = draw(st.sampled_from(
        ["first", "first", "first", "empty", "none", "missing",
         "not_indexable", "raising_first"]))
    if outputs_kind == "first":
        first_fields = {"finish_reason": draw(_FINISH_REASON)}
        token_ids = draw(_TOKEN_IDS_SOURCE)
        if not (isinstance(token_ids, str) and token_ids == "missing"):
            first_fields["token_ids"] = token_ids
        outputs = [SimpleNamespace(**first_fields)]
    elif outputs_kind == "raising_first":
        outputs = [_RaisingAttrs()]
    elif outputs_kind == "empty":
        outputs = []
    elif outputs_kind == "none":
        outputs = None
    elif outputs_kind == "not_indexable":
        outputs = 3
    else:
        outputs = "missing"

    final_kind = draw(st.sampled_from(
        ["namespace"] * 5 + ["raising"]))
    if final_kind == "raising":
        final_output = _RaisingAttrs()
    else:
        fields = {"metrics": draw(_metrics_objects())}
        if prompt_token_ids != "missing":
            fields["prompt_token_ids"] = prompt_token_ids
        if not (isinstance(outputs, str) and outputs == "missing"):
            fields["outputs"] = outputs
        final_output = SimpleNamespace(**fields)

    image_supplied = draw(st.booleans())
    # Placeholder id: readable int (sometimes forced to occur in the
    # prompt), or unreadable (None / bool / text / negative-but-still-int
    # is fine and simply may not match).
    if (isinstance(prompt_token_ids, list) and prompt_token_ids
            and draw(st.booleans())):
        placeholder = draw(st.sampled_from(prompt_token_ids))
    else:
        placeholder = draw(st.one_of(
            st.integers(min_value=-5, max_value=60),
            st.none(), st.booleans(), st.sampled_from(["<image>"])))

    return {
        "t_submit": t_submit,
        "t_first": t_first,
        "t_last": t_last,
        "final_output": final_output,
        "image_supplied": image_supplied,
        "image_placeholder_token_id": placeholder,
    }


# ---------------------------------------------------------------------------
# Independent oracle (measurement honesty)
# ---------------------------------------------------------------------------

def _safe_read(obj, name):
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001 - hostile objects by design
        return None


def _usable(value):
    """A readable finite real number (bools excluded)."""
    return (isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _expected_ms(start, end):
    """Honest duration: both endpoints usable and interval non-negative,
    else unavailable — never an estimate."""
    if not _usable(start) or not _usable(end):
        return None
    delta = float(end) - float(start)
    if delta < 0:
        return None
    return max(0, int(round(delta * 1000)))


def _expected_phases(case):
    """(queueing, prefill, decode, prefill_includes_queueing) after the
    all-zero guard."""
    final_output = case["final_output"]
    metrics = _safe_read(final_output, "metrics")
    engine_usable = False
    if metrics is not None:
        stamps = [_safe_read(metrics, name) for name in _METRIC_FIELDS]
        engine_usable = all(_usable(value) for value in stamps)
    if engine_usable:
        arrival, scheduled, first_token, finished = stamps
        queueing = _expected_ms(arrival, scheduled)
        prefill = _expected_ms(scheduled, first_token)
        decode = _expected_ms(first_token, finished)
        includes_queueing = False
    else:
        queueing = None
        prefill = _expected_ms(case["t_submit"], case["t_first"])
        decode = _expected_ms(case["t_first"], case["t_last"])
        includes_queueing = True
    if queueing == 0 and prefill == 0 and decode == 0:
        queueing = prefill = decode = None
    return queueing, prefill, decode, includes_queueing


def _expected_tokens(case):
    """(prompt_tokens, output_tokens, truncated) from the token id lists."""
    final_output = case["final_output"]
    prompt_ids = _safe_read(final_output, "prompt_token_ids")
    prompt_tokens = len(prompt_ids) if isinstance(prompt_ids, list) else None

    outputs = _safe_read(final_output, "outputs")
    first = outputs[0] if isinstance(outputs, list) and outputs else None
    if first is None:
        return prompt_tokens, None, None
    token_ids = _safe_read(first, "token_ids")
    output_tokens = len(token_ids) if isinstance(token_ids, list) else None
    finish_reason = _safe_read(first, "finish_reason")
    truncated = ((finish_reason == "length")
                 if isinstance(finish_reason, str) else None)
    return prompt_tokens, output_tokens, truncated


def _expected_image_tokens(case):
    """Image token count = placeholder occurrences in the prompt when the
    image was supplied and both the placeholder id and the prompt token id
    list are readable; otherwise unavailable."""
    if not case["image_supplied"]:
        return None
    placeholder = case["image_placeholder_token_id"]
    if isinstance(placeholder, bool) or not isinstance(placeholder, int):
        return None
    prompt_ids = _safe_read(case["final_output"], "prompt_token_ids")
    if not isinstance(prompt_ids, list):
        return None
    return sum(1 for token in prompt_ids if token == placeholder)


def _is_wellformed_ms(value):
    return value is None or (isinstance(value, int)
                             and not isinstance(value, bool)
                             and value >= 0)


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

# Feature: vllm-workflow-latency-optimization, Property 1: Breakdown well-formedness and measurement honesty
@settings(max_examples=200, deadline=None)
@given(case=_cases())
def test_property_1_breakdown_wellformedness_and_measurement_honesty(case):
    """**Feature: vllm-workflow-latency-optimization, Property 1: Breakdown
    well-formedness and measurement honesty**

    **Validates: Requirements 1.1, 1.3, 1.4, 1.6, 5.1, 5.5**
    """
    breakdown = build_breakdown(**case)

    # R1.3: every phase field is a non-negative integer of milliseconds or
    # marked unavailable (None).
    assert _is_wellformed_ms(breakdown.queueing_ms)
    assert _is_wellformed_ms(breakdown.prefill_ms)
    assert _is_wellformed_ms(breakdown.decode_ms)

    # R1.6: never all phases zero simultaneously (becomes all-unavailable).
    assert not (breakdown.queueing_ms == 0
                and breakdown.prefill_ms == 0
                and breakdown.decode_ms == 0)

    # R1.4: measurement honesty — every phase equals the independently
    # recomputed honest measurement; anything the engine did not expose is
    # unavailable, never an estimated value.
    exp_queueing, exp_prefill, exp_decode, exp_includes = \
        _expected_phases(case)
    assert breakdown.queueing_ms == exp_queueing
    assert breakdown.prefill_ms == exp_prefill
    assert breakdown.decode_ms == exp_decode
    assert breakdown.prefill_includes_queueing is exp_includes

    # R1.1: prompt and output token counts match the final output's token
    # id lists (unavailable when unreadable); truncation from finish_reason.
    exp_prompt, exp_output, exp_truncated = _expected_tokens(case)
    assert breakdown.prompt_tokens == exp_prompt
    assert breakdown.output_tokens == exp_output
    assert breakdown.truncated == exp_truncated

    # R5.1 / R5.5 / R1.1: image token count equals the placeholder-id
    # occurrences when an image was supplied and the id is readable;
    # not applicable exactly when the request carried no image data.
    assert breakdown.image_tokens_applicable is bool(case["image_supplied"])
    assert breakdown.image_tokens == _expected_image_tokens(case)
    if not case["image_supplied"]:
        assert breakdown.image_tokens is None

    # Serialized form: every field present, None rendered as "unavailable",
    # non-applicable image count rendered as "n/a" (R1.1, R1.4, R5.5).
    payload = breakdown.to_payload()
    assert set(payload.keys()) == _PAYLOAD_KEYS
    for field in ("queueing_ms", "prefill_ms", "decode_ms",
                  "prompt_tokens", "output_tokens", "truncated"):
        attr = getattr(breakdown, field)
        assert payload[field] == (UNAVAILABLE if attr is None else attr)
    if breakdown.image_tokens_applicable:
        assert payload["image_tokens"] == (
            UNAVAILABLE if breakdown.image_tokens is None
            else breakdown.image_tokens)
    else:
        assert payload["image_tokens"] == NOT_APPLICABLE
