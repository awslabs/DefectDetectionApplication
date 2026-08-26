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
"""Per-request Generation_Phase_Breakdown construction (spec:
vllm-workflow-latency-optimization, design section "1.
vllm_runtime/generation_metrics.py").

A pure, engine-agnostic module: it imports no vLLM and never touches an
engine. :func:`build_breakdown` turns one completed Generation_Call's raw
observations — the manager's monotonic timestamps plus the final
``RequestOutput``-shaped object — into a :class:`GenerationPhaseBreakdown`
(queueing / prefill / decode in milliseconds, prompt / output / image token
counts, truncation), which is what the run log and the additive
``generation_metrics`` API field render (Requirements 1.1, 1.3, 3.5, 5.1).

MEASUREMENT HONESTY is the module's contract (Requirement 1.4): every phase
value is a real monotonic-clock measurement, layered by source:

* V0 engine frontend: ``final_output.metrics`` carries ``arrival_time`` /
  ``first_scheduled_time`` / ``first_token_time`` / ``finished_time``
  (vLLM records them with ``time.monotonic()``), giving the full
  queueing / prefill / decode split.
* V1 engine frontend (JP7): ``metrics is None``, so the manager's own
  monotonic timestamps apply — ``queueing_ms`` is honestly ``None``
  (unavailable) and the submission-to-first-token interval is reported as
  prefill with ``prefill_includes_queueing = True``: a genuine measurement,
  labelled as such, never an estimate.

Anything unreadable or negative is marked unavailable (``None``), never
guessed; an all-present-and-zero phase set is treated as an instrumentation
error and becomes all-unavailable (Requirement 1.6). No public function in
this module raises on hostile input — a bad source degrades the affected
field to unavailable.
"""
import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Rendered form of an unavailable (``None``) field in payloads and log
#: lines — unavailable fields are always present, never dropped (R1.2).
UNAVAILABLE = "unavailable"

#: Rendered form of the image token count when the request carried no image
#: data (``image_tokens_applicable is False``, R1.1, R5.5).
NOT_APPLICABLE = "n/a"

#: The vLLM ``finish_reason`` value that means the Output_Token_Budget was
#: reached (R3.5).
TRUNCATION_FINISH_REASON = "length"


@dataclass(frozen=True)
class GenerationPhaseBreakdown:
    """One completed Generation_Call's phase and token decomposition.

    Invariants (see the design's Data Models section):

    * every ``*_ms`` field is ``None`` (unavailable) or a non-negative
      ``int`` of milliseconds from a monotonic source (R1.3, R1.4);
    * ``image_tokens_applicable is False`` iff the request carried no image
      data; in that case ``image_tokens`` is ignored and serialized as
      ``"n/a"`` (R1.1, R5.5);
    * the three phases are never all zero simultaneously — the builder
      converts that case to all-unavailable (R1.6);
    * ``prefill_includes_queueing`` is ``True`` only on the fallback
      (manager-clock) path where ``queueing_ms is None``.
    """
    queueing_ms: Optional[int]
    prefill_ms: Optional[int]
    decode_ms: Optional[int]
    prompt_tokens: Optional[int]
    output_tokens: Optional[int]
    image_tokens: Optional[int]
    image_tokens_applicable: bool
    truncated: Optional[bool]
    prefill_includes_queueing: bool

    def to_payload(self) -> Dict[str, Any]:
        """JSON-ready dict for the additive ``generation_metrics`` API field
        and the node outcome metadata. ``None`` renders as ``"unavailable"``
        and a non-applicable image count as ``"n/a"``; every field is always
        present (R1.2, R1.4)."""
        return {
            "queueing_ms": _render(self.queueing_ms),
            "prefill_ms": _render(self.prefill_ms),
            "decode_ms": _render(self.decode_ms),
            "prefill_includes_queueing": self.prefill_includes_queueing,
            "prompt_tokens": _render(self.prompt_tokens),
            "output_tokens": _render(self.output_tokens),
            "image_tokens": (_render(self.image_tokens)
                             if self.image_tokens_applicable
                             else NOT_APPLICABLE),
            "truncated": _render(self.truncated),
        }

    def to_log_line(self, node_id: Any, model_name: Any) -> str:
        """The run-log emission line (R1.2), e.g.::

            LLM generation breakdown (node llm_inference_1, model
            qwen3-vl-8b): queueing 12 ms, prefill 842 ms, decode 16571 ms,
            prompt tokens 1180, image tokens 1024, output tokens 256;
            output truncated at the output token budget (256)

        Unavailable fields render as ``unavailable``, a non-applicable
        image count as ``n/a`` — no field is ever dropped. The truncation
        statement appears exactly when ``truncated`` is ``True`` (R3.5,
        R3.6); ``False`` renders as "output not truncated" and ``None`` as
        "truncation unavailable" so the field is never silently omitted.
        """
        prefill_label = ("prefill (includes queueing)"
                         if self.prefill_includes_queueing else "prefill")
        image = (_render_count(self.image_tokens)
                 if self.image_tokens_applicable else NOT_APPLICABLE)
        if self.truncated is True:
            if self.output_tokens is not None:
                truncation = ("output truncated at the output token budget "
                              "({})".format(self.output_tokens))
            else:
                truncation = "output truncated at the output token budget"
        elif self.truncated is False:
            truncation = "output not truncated"
        else:
            truncation = "truncation {}".format(UNAVAILABLE)
        return (
            "LLM generation breakdown (node {node}, model {model}): "
            "queueing {queueing}, {prefill_label} {prefill}, "
            "decode {decode}, prompt tokens {prompt}, image tokens {image}, "
            "output tokens {output}; {truncation}".format(
                node=node_id,
                model=model_name,
                queueing=_render_ms(self.queueing_ms),
                prefill_label=prefill_label,
                prefill=_render_ms(self.prefill_ms),
                decode=_render_ms(self.decode_ms),
                prompt=_render_count(self.prompt_tokens),
                image=image,
                output=_render_count(self.output_tokens),
                truncation=truncation,
            )
        )


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _render(value: Any) -> Any:
    """``None`` -> the ``"unavailable"`` marker; everything else unchanged."""
    return UNAVAILABLE if value is None else value


def _render_ms(value: Optional[int]) -> str:
    return UNAVAILABLE if value is None else "{} ms".format(value)


def _render_count(value: Optional[int]) -> str:
    return UNAVAILABLE if value is None else str(value)


# ---------------------------------------------------------------------------
# Tolerant source readers (this module never raises on hostile input)
# ---------------------------------------------------------------------------

def _safe_attr(obj: Any, name: str) -> Any:
    """``getattr`` that degrades any failure (including raising properties)
    to ``None`` — the same discipline the manager's best-effort readers use."""
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001 - tolerant by contract
        return None


def _finite_number(value: Any) -> Optional[float]:
    """The value as a finite ``float``, or ``None`` when it is not a usable
    real number (bools excluded, NaN/inf refused): an unreadable timestamp
    marks its durations unavailable rather than producing a guessed value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except Exception:  # noqa: BLE001 - tolerant by contract
        return None
    if not math.isfinite(result):
        return None
    return result


def _duration_ms(start: Any, end: Any) -> Optional[int]:
    """``end - start`` in whole milliseconds: ``max(0, round(s * 1000))``.
    An unreadable endpoint or a negative interval (a monotonic source cannot
    honestly produce one) marks the duration unavailable (R1.4)."""
    start_s = _finite_number(start)
    end_s = _finite_number(end)
    if start_s is None or end_s is None:
        return None
    delta = end_s - start_s
    if delta < 0:
        return None
    return max(0, int(round(delta * 1000)))


def _safe_token_count(token_ids: Any) -> Optional[int]:
    """Best-effort ``len(token_ids)`` as a non-negative int, else ``None``."""
    if token_ids is None:
        return None
    try:
        count = len(token_ids)
    except Exception:  # noqa: BLE001 - tolerant by contract
        return None
    try:
        count = int(count)
    except Exception:  # noqa: BLE001 - tolerant by contract
        return None
    return count if count >= 0 else None


def _first_output(final_output: Any) -> Any:
    """``final_output.outputs[0]`` best-effort, else ``None``."""
    outputs = _safe_attr(final_output, "outputs")
    if outputs is None:
        return None
    try:
        return outputs[0]
    except Exception:  # noqa: BLE001 - tolerant by contract
        return None


def _count_placeholder_tokens(token_ids: Any,
                              placeholder_token_id: Any) -> Optional[int]:
    """Occurrences of the image placeholder token id in the prompt token
    ids, best-effort: an unreadable placeholder id or token list yields
    ``None`` (unavailable), never a guess (R1.4, R5.1)."""
    if (isinstance(placeholder_token_id, bool)
            or not isinstance(placeholder_token_id, int)):
        return None
    if token_ids is None:
        return None
    try:
        return sum(1 for token in token_ids if token == placeholder_token_id)
    except Exception:  # noqa: BLE001 - tolerant by contract
        return None


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_breakdown(
    *,
    t_submit: float,
    t_first: Optional[float],
    t_last: Optional[float],
    final_output: Any,
    image_supplied: bool,
    image_placeholder_token_id: Optional[int],
) -> GenerationPhaseBreakdown:
    """Build the Generation_Phase_Breakdown for one completed
    Generation_Call.

    ``t_submit`` / ``t_first`` / ``t_last`` are the manager's
    ``time.monotonic()`` captures (just before ``engine.generate``, at the
    first yielded output, at the last). ``final_output`` is the final
    ``RequestOutput``-shaped object; ``image_supplied`` states whether the
    request carried image data; ``image_placeholder_token_id`` is the
    model's image placeholder token id when readable, else ``None``.

    Phase sources are layered (R1.3, R1.4):

    * When ``final_output.metrics`` exposes all four usable timestamps
      (V0 frontend): ``queueing = first_scheduled - arrival``,
      ``prefill = first_token - first_scheduled``,
      ``decode = finished - first_token``.
    * Otherwise (V1: ``metrics is None``): ``queueing_ms = None``
      (unavailable), ``prefill_ms = t_first - t_submit`` with
      ``prefill_includes_queueing = True``, ``decode_ms = t_last - t_first``.

    Each duration is ``max(0, round(seconds * 1000))``; unreadable or
    negative sources mark the field unavailable. An all-present-and-zero
    phase set becomes all-unavailable (R1.6). Token counts are best-effort;
    no image means ``image_tokens_applicable = False`` (R1.1, R5.5).
    Truncation is ``finish_reason == "length"``; unreadable ``finish_reason``
    yields ``None`` (R3.5).
    """
    queueing_ms: Optional[int] = None
    prefill_ms: Optional[int] = None
    decode_ms: Optional[int] = None
    prefill_includes_queueing = False

    metrics = _safe_attr(final_output, "metrics")
    engine_metrics_usable = False
    if metrics is not None:
        arrival = _finite_number(_safe_attr(metrics, "arrival_time"))
        scheduled = _finite_number(_safe_attr(metrics, "first_scheduled_time"))
        first_token = _finite_number(_safe_attr(metrics, "first_token_time"))
        finished = _finite_number(_safe_attr(metrics, "finished_time"))
        if None not in (arrival, scheduled, first_token, finished):
            engine_metrics_usable = True
            queueing_ms = _duration_ms(arrival, scheduled)
            prefill_ms = _duration_ms(scheduled, first_token)
            decode_ms = _duration_ms(first_token, finished)

    if not engine_metrics_usable:
        # Manager monotonic fallback (V1 frontend): queueing is honestly
        # unavailable; submission-to-first-token is a genuine measurement
        # reported as prefill and labelled as including queueing.
        queueing_ms = None
        prefill_ms = _duration_ms(t_submit, t_first)
        decode_ms = _duration_ms(t_first, t_last)
        prefill_includes_queueing = True

    # All-zero guard (R1.6): all phases present and simultaneously zero is
    # an instrumentation error, reported as all-unavailable, never as data.
    if queueing_ms == 0 and prefill_ms == 0 and decode_ms == 0:
        queueing_ms = None
        prefill_ms = None
        decode_ms = None

    prompt_token_ids = _safe_attr(final_output, "prompt_token_ids")
    prompt_tokens = _safe_token_count(prompt_token_ids)

    first = _first_output(final_output)
    output_tokens = (_safe_token_count(_safe_attr(first, "token_ids"))
                     if first is not None else None)

    image_tokens_applicable = bool(image_supplied)
    if image_tokens_applicable:
        image_tokens = _count_placeholder_tokens(prompt_token_ids,
                                                 image_placeholder_token_id)
    else:
        image_tokens = None

    finish_reason = (_safe_attr(first, "finish_reason")
                     if first is not None else None)
    if isinstance(finish_reason, str):
        truncated: Optional[bool] = (finish_reason
                                     == TRUNCATION_FINISH_REASON)
    else:
        truncated = None

    return GenerationPhaseBreakdown(
        queueing_ms=queueing_ms,
        prefill_ms=prefill_ms,
        decode_ms=decode_ms,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        image_tokens=image_tokens,
        image_tokens_applicable=image_tokens_applicable,
        truncated=truncated,
        prefill_includes_queueing=prefill_includes_queueing,
    )
