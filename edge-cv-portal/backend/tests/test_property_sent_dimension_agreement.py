"""
Property-based test for sent-dimension agreement at the shared `llm:`
chokepoint (`dda_llm_prelabel.generate_llm_prelabel`).

Spec: llm-model-token-and-image-sizing, task 4.4.

**Feature: llm-model-token-and-image-sizing, Property 6: Prompt
dimensions equal the dimensions of the image actually sent**
**Validates: Requirements 7.1, 7.2, 8.2**

The claim under test: for any source image and any Downscale_Setting,
the pixel dimensions embedded in the Detection_Prompt and the dimensions
used to validate Coordinate_Guidance both equal the pixel dimensions of
the image bytes present in the request's target image block, and are
independent of the dimensions of any attached Few_Shot_Example image.

Harness (from the design's Property 6 test strategy): moto (the
conftest `aws_stack`, reached through the module-scoped `prelabel`
fixture) plus **one** stub Converse client
(`test_dda_llm_prelabel.RecordingConverseClient`), bound per example
with `_Patcher` since Hypothesis cannot consume function-scoped
fixtures.

Generator notes:

- Source dimensions and settings as in Property 4
  (test_property_image_downscaler.py): general `1..4000 x 1..4000`
  pairs, extreme aspect ratios, exact-bound cases drawn from
  MAX_IMAGE_EDGE_OPTIONS, and the setting from all seven values of
  Requirement 5.1.
- Example sets (0..3 images) whose stored widths and heights are drawn
  to be **deliberately different** from the target's Source_Dimensions
  and Sent_Dimensions, from each other, and from every number the
  prompt legitimately contains (MAX_DETECTIONS, POLYGON_MIN_VERTICES) —
  so any appearance of an example dimension in the request text is a
  genuine leak.
- The stub replies with one box placed exactly on the boundary of a
  dimension pair drawn from `st.sampled_from(['sent', 'source'])`.

Per-example assertions:

1. The dimension sentence in the prompt names the decoded size of the
   target image block's bytes, exactly once, and no other dimension
   sentence appears anywhere in the request text (Req 7.1, 8.2).
2. Guidance exactly on the sent boundary is accepted, while guidance on
   the source boundary — beyond the sent bound whenever a resize
   occurred — is rejected as unusable model output whose reason names
   the sent bounds (Req 7.2).
3. No example's stored width or height appears anywhere in the request
   text (Req 8.2).
"""
import functools
import io
import re
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PIL import Image

from dda_llm_guidance import MAX_DETECTIONS, POLYGON_MIN_VERTICES
from dda_llm_image import DOWNSCALE_OFF, MAX_IMAGE_EDGE_OPTIONS
from test_dda_llm_prelabel import RecordingConverseClient, guidance
from test_property_llm_autolabel_preservation import _Patcher

MODEL_ID = "us.amazon.nova-pro-v1:0"
# Digit-free on purpose: the only numbers in the request text are then
# the prompt's own constants and the target's dimension sentence, so the
# example-dimension absence check (Req 8.2) cannot false-positive.
LABELS = ["scratch", "dent"]
PROMPT = "Find every scratch on the panel."

# Deliberately different from every budget-free default so a leak of the
# Global_Max_Tokens would be visible in other suites; here it only pins
# the stubbed Bedrock_Configuration shape.
GLOBAL_MAX_TOKENS = 4096

# The one dimension sentence build_detection_prompt embeds (Req 7.1).
_DIMENSION_SENTENCE = re.compile(
    r"The image is (\d+) pixels wide and (\d+) pixels tall")


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def prelabel(aws_stack):
    """The real dda_llm_prelabel imported inside the moto mock."""
    import dda_llm_prelabel
    return dda_llm_prelabel


def _bind_client(prelabel, patcher, stub):
    """Bind the stub Converse client and a pinned Bedrock_Configuration
    into the chokepoint, the test_dda_llm_prelabel convention."""
    patcher.setattr(prelabel, "get_bedrock_configuration", lambda: {
        "model_id": MODEL_ID,
        "region": "us-west-2",
        "max_tokens": GLOBAL_MAX_TOKENS,
        "temperature": None,
        "top_p": None,
        "timeout_seconds": 240,
    })
    patcher.setattr(prelabel, "get_bedrock_client",
                    lambda region, timeout: stub)


# ---------------------------------------------------------- image builders

@functools.lru_cache(maxsize=64)
def _image_bytes(container, width, height):
    """A real, fully decodable image at exactly (width, height).

    Uniform content: this property is about dimensions, never pixel
    content, and uniform images encode fast enough for 4000-pixel
    sources at 100 examples. Cached because bytes are immutable and the
    chokepoint never mutates them.
    """
    buffer = io.BytesIO()
    image = Image.new("RGB", (width, height), (120, 40, 200))
    if container == "png":
        image.save(buffer, format="PNG", compress_level=1)
    else:
        image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def _expected_sent(width, height, setting):
    """The Sent_Dimensions the Requirement 6.4 formula calls for,
    recomputed here independently of the module (the Property 4 style),
    so the boundary guidance can be placed exactly on the sent bound."""
    if setting is DOWNSCALE_OFF or max(width, height) <= setting:
        return width, height
    longer = max(width, height)
    return (max(1, width * setting // longer),
            max(1, height * setting // longer))


# -------------------------------------------------------------- strategies

# Source dimensions as in Property 4: general pairs, extreme aspect
# ratios, and exact-bound cases where an option value is a dimension.
_dimensions = st.one_of(
    st.tuples(st.integers(1, 4000), st.integers(1, 4000)),
    st.tuples(st.integers(3000, 4000), st.integers(1, 5)),
    st.tuples(st.integers(1, 5), st.integers(3000, 4000)),
    st.tuples(st.sampled_from(MAX_IMAGE_EDGE_OPTIONS), st.integers(1, 4000)),
    st.tuples(st.integers(1, 4000), st.sampled_from(MAX_IMAGE_EDGE_OPTIONS)),
)
_settings = st.sampled_from((DOWNSCALE_OFF,) + MAX_IMAGE_EDGE_OPTIONS)
_containers = st.sampled_from(("png", "jpeg"))


def _free_dim(draw, reserved):
    """One example dimension, deliberately different from everything in
    `reserved` (the target's source and sent dimensions, the prompt's
    constants, and every previously drawn example dimension). Bumping
    past collisions instead of filtering keeps shrinking cheap."""
    value = draw(st.integers(min_value=4, max_value=900))
    while value in reserved:
        value += 1
    reserved.add(value)
    return value


@st.composite
def _agreement_cases(draw):
    """A target image, a Downscale_Setting, an example set whose stored
    dimensions differ from the target's and from each other, and the
    boundary pair ('sent' | 'source') the stub's guidance lands on."""
    width, height = draw(_dimensions)
    setting = draw(_settings)
    sent_width, sent_height = _expected_sent(width, height, setting)
    reserved = {width, height, sent_width, sent_height,
                MAX_DETECTIONS, POLYGON_MIN_VERTICES}
    examples = []
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        example_width = _free_dim(draw, reserved)
        example_height = _free_dim(draw, reserved)
        examples.append(SimpleNamespace(
            width=example_width,
            height=example_height,
            container=draw(_containers),
            designation=draw(st.sampled_from(("good", "bad"))),
        ))
    return SimpleNamespace(
        width=width, height=height, setting=setting,
        container=draw(_containers),
        sent_width=sent_width, sent_height=sent_height,
        examples=examples,
        boundary=draw(st.sampled_from(["sent", "source"])),
    )


# --------------------------------------------------------------- Property 6

# Feature: llm-model-token-and-image-sizing, Property 6: Prompt
# dimensions equal the dimensions of the image actually sent
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(case=_agreement_cases())
def test_property_prompt_dimensions_equal_the_sent_image_dimensions(
        prelabel, case):
    """
    **Feature: llm-model-token-and-image-sizing, Property 6: Prompt
    dimensions equal the dimensions of the image actually sent**

    For any source image and any Downscale_Setting, the pixel dimensions
    embedded in the Detection_Prompt and the dimensions used to validate
    Coordinate_Guidance both equal the pixel dimensions of the image
    bytes present in the request's target image block, and are
    independent of the dimensions of any attached Few_Shot_Example
    image.

    **Validates: Requirements 7.1, 7.2, 8.2**
    """
    boundary_pair = ((case.sent_width, case.sent_height)
                     if case.boundary == "sent"
                     else (case.width, case.height))
    resized = (case.sent_width, case.sent_height) != (case.width, case.height)
    # The source boundary lies beyond the sent bound exactly when a
    # resize occurred (the longer edge shrank strictly); otherwise the
    # two pairs coincide and both draws sit on the sent boundary.
    expect_accept = case.boundary == "sent" or not resized

    stub = RecordingConverseClient(reply=guidance([{
        "class": LABELS[0],
        "box": {"left": 0, "top": 0,
                "width": boundary_pair[0], "height": boundary_pair[1]},
    }]))

    patcher = _Patcher()
    try:
        _bind_client(prelabel, patcher, stub)
        result, error = None, None
        try:
            result = prelabel.generate_llm_prelabel(
                model_identifier=MODEL_ID,
                modality="ObjectDetection",
                label_set=list(LABELS),
                detection_prompt=PROMPT,
                per_label_prompts=None,
                image_bytes=_image_bytes(case.container, case.width,
                                         case.height),
                image_key=("imgs/target.png" if case.container == "png"
                           else "imgs/target.jpg"),
                width=case.width,
                height=case.height,
                few_shot_images=[
                    {"bytes": _image_bytes(example.container, example.width,
                                           example.height),
                     "format": example.container,
                     "designation": example.designation}
                    for example in case.examples],
                downscale_setting=case.setting,
            )
        except prelabel.LlmPrelabelError as exc:
            error = exc
    finally:
        patcher.undo()

    # Exactly one invocation, whatever the outcome — validation happens
    # after the request was issued, so the request is capturable in both
    # branches.
    assert len(stub.calls) == 1, (
        f"expected exactly one Converse call, got {len(stub.calls)}"
        + (f" (failed before invocation: {error.reason})" if error else ""))
    content = stub.calls[0]["messages"][0]["content"]
    image_blocks = [block for block in content if "image" in block]
    text_blocks = [block["text"] for block in content if "text" in block]

    # Every example was really attached, so the independence claim below
    # is non-vacuous; the target image block is the last image block.
    assert len(image_blocks) == len(case.examples) + 1
    with Image.open(io.BytesIO(
            image_blocks[-1]["image"]["source"]["bytes"])) as sent_image:
        block_width, block_height = sent_image.size

    # The image actually sent has the Sent_Dimensions of the Requirement
    # 6.4 formula — which is what places the generated boundary guidance
    # exactly on the sent bound rather than merely inside it.
    assert (block_width, block_height) == (case.sent_width, case.sent_height)

    # (1) Req 7.1, 8.2: the prompt's dimension sentence names the decoded
    # size of the target image block's bytes — and it is the only
    # dimension sentence anywhere in the request text, so no other pixel
    # dimensions (the source's on a resize, any example's ever) are
    # embedded for the image.
    all_text = "\n".join(text_blocks)
    assert _DIMENSION_SENTENCE.findall(all_text) == [
        (str(block_width), str(block_height))]

    # (2) Req 7.2: coordinates are validated against the sent bounds,
    # inclusively — guidance exactly on the sent boundary is accepted,
    # guidance beyond it (the source boundary after a resize) is
    # rejected, naming the sent bounds.
    if expect_accept:
        assert error is None, (
            f"guidance on the sent boundary {boundary_pair} must be "
            f"accepted, got {error.category}: {error.reason}")
        assert result.prelabel is not None
        assert (result.sent_width, result.sent_height) == (block_width,
                                                           block_height)
    else:
        assert error is not None, (
            f"guidance on the source boundary {boundary_pair} lies beyond "
            f"the sent bound ({block_width}x{block_height}) and must be "
            f"rejected, got {result!r}")
        assert error.category == prelabel.CATEGORY_UNUSABLE_MODEL_OUTPUT
        assert (f"outside the image bounds "
                f"{block_width}x{block_height}") in error.reason

    # (3) Req 8.2: no example's stored width or height appears anywhere
    # in the request text. The generator reserved every legitimate
    # number (the target's source and sent dimensions, MAX_DETECTIONS,
    # POLYGON_MIN_VERTICES) and the example ordinals stop at 3 below the
    # 4-pixel floor, so any match is a genuine leak.
    for example in case.examples:
        for dimension in (example.width, example.height):
            assert re.search(rf"\b{dimension}\b", all_text) is None, (
                f"example dimension {dimension} leaked into the request "
                f"text")
