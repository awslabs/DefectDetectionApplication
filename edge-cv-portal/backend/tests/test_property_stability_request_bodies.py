"""Property tests for the Stability request body and response extraction
(stability-generation-models, tasks 3.3 and 3.4).

Pure-logic tests over synthetic_core: no AWS mocks.
"""
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from synthetic_core import (
    SEED_MODULUS,
    StabilityGenerationError,
    build_stability_inpaint_request_body,
    extract_stability_result,
)


b64_texts = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
             "0123456789+/=",
    min_size=1, max_size=120)

prompts = st.text(min_size=1, max_size=200)

task_seeds = st.integers(min_value=0, max_value=SEED_MODULUS - 1)

# Every capability-excluded parameter name that must never appear
# (Req 2.4): negative_prompt and any guidance/cfg key.
_EXCLUDED_KEYS = {"negative_prompt", "cfg_scale", "cfgScale", "guidance",
                  "guidance_scale"}


# ---------------------------------------------------------------------------
# Task 3.3
#
# **Feature: stability-generation-models, Property 3: Stability inpaint
# request body exact schema and seed passthrough**
#
# _For any_ prompt, source image base64 string, mask image base64 string,
# and Task_Seed in 0..858,993,459, build_stability_inpaint_request_body
# produces a body whose key set is exactly {image, mask, prompt, seed,
# output_format} with each value equal to the corresponding input, the
# seed unmodified, and no capability-excluded parameter (negative_prompt,
# guidance/cfg keys) ever present; when the seed is None the seed key is
# omitted and the remaining key set is exact.
#
# **Validates: Requirements 2.3, 2.4, 7.2**
# ---------------------------------------------------------------------------

@settings(deadline=None)
@example(prompt="p", source_b64="aW1n", mask_b64="bWFzaw==", seed=0)
@example(prompt="p", source_b64="aW1n", mask_b64="bWFzaw==",
         seed=SEED_MODULUS - 1)
@given(prompt=prompts, source_b64=b64_texts, mask_b64=b64_texts,
       seed=task_seeds)
def test_stability_body_exact_schema_and_seed_passthrough(
        prompt, source_b64, mask_b64, seed):
    """Exact key set {image, mask, prompt, seed, output_format}, every
    value equal to its input, seed unmodified, no excluded parameters
    (Requirements 2.3, 2.4, 7.2)."""
    body = build_stability_inpaint_request_body(prompt, source_b64,
                                                mask_b64, seed)

    assert set(body) == {"image", "mask", "prompt", "seed",
                         "output_format"}
    assert body["image"] == source_b64
    assert body["mask"] == mask_b64
    assert body["prompt"] == prompt
    assert body["seed"] == seed
    assert isinstance(body["seed"], int)
    assert body["output_format"] == "png"
    assert not (_EXCLUDED_KEYS & set(body))


@settings(deadline=None)
@given(prompt=prompts, source_b64=b64_texts, mask_b64=b64_texts)
def test_stability_body_omits_seed_when_none(prompt, source_b64, mask_b64):
    """seed=None omits the seed key; the remaining key set is exact
    (Requirements 2.3, 2.4, 7.2)."""
    body = build_stability_inpaint_request_body(prompt, source_b64,
                                                mask_b64, None)

    assert set(body) == {"image", "mask", "prompt", "output_format"}
    assert body["image"] == source_b64
    assert body["mask"] == mask_b64
    assert body["prompt"] == prompt
    assert not (_EXCLUDED_KEYS & set(body))


# ---------------------------------------------------------------------------
# Task 3.4
#
# **Feature: stability-generation-models, Property 4: Stability response
# extraction is total over payload shapes**
#
# _For any_ Stability response payload with an images list and a
# finish_reasons list: when finish_reasons[0] is null and images is
# non-empty, extract_stability_result returns exactly images[0]; when
# finish_reasons[0] is any non-null reason string or images is empty, it
# raises a task failure whose message contains the reported reason.
#
# **Validates: Requirements 2.5, 2.6**
# ---------------------------------------------------------------------------

# The documented finish_reasons values (design.md research summary).
finish_reason_values = st.sampled_from([
    "Filter reason: prompt",
    "Filter reason: output image",
    "Filter reason: input image",
    "Inference error",
])

image_lists = st.lists(b64_texts, min_size=1, max_size=3)


@settings(deadline=None)
@given(images=image_lists,
       seeds=st.lists(st.text(min_size=1, max_size=12), max_size=3))
def test_extraction_returns_first_image_on_success(images, seeds):
    """finish_reasons[0] null + non-empty images -> exactly images[0]
    (Requirement 2.5)."""
    payload = {"images": images, "seeds": seeds,
               "finish_reasons": [None] * len(images)}
    assert extract_stability_result(payload) == images[0]


@settings(deadline=None)
@example(reason="Inference error", images=[])
@given(reason=finish_reason_values, images=st.lists(b64_texts, max_size=3))
def test_extraction_raises_with_reported_reason(reason, images):
    """Any documented non-null finish reason raises a task failure whose
    message contains the reported reason, regardless of the images list
    (Requirement 2.6)."""
    payload = {"images": images, "seeds": [],
               "finish_reasons": [reason]}
    with pytest.raises(StabilityGenerationError) as excinfo:
        extract_stability_result(payload)
    assert reason in str(excinfo.value)


@settings(deadline=None)
@given(finish_reasons=st.sampled_from([[], [None]]))
def test_extraction_raises_on_empty_images(finish_reasons):
    """An empty images list raises a task failure even when no filter
    reason is reported (Requirement 2.6)."""
    payload = {"images": [], "seeds": [],
               "finish_reasons": finish_reasons}
    with pytest.raises(StabilityGenerationError):
        extract_stability_result(payload)
