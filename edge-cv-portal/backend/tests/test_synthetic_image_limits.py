"""Source images outside the generation model's input size limits
(fix-synthetic-image-size-limits).

Pure part (no AWS): the per-model ``input_image_limits`` declared on the
Model_Catalog, the ``oversize_handling`` generation parameter and the
fit / violation / description helpers in ``synthetic_core``.

Worker part (moto stack from conftest.py + synthetic_env.py): a 4608x3288
source -- 15,151,104 pixels, the exact count that made all 50 variations
of session 6115b94c fail against the Stability inpaint model's 9,437,184
pixel maximum -- is scaled to fit before the model call and the generated
image is restored to 4608x3288; an Amazon image-variation request for a
resized source asks for the permitted output size closest to the source's
aspect ratio (the models otherwise answer a fixed square, which the
restore would stretch); the ``fail`` handling records the reason without
calling the model; in-limit and undecodable sources are sent unchanged;
the generate endpoint rejects an unknown handling value before any plan
persists.

Integration part: approving and integrating a restored 4608x3288 preview
annotates it from the image header (mask path, no pixel decode) or from a
pixel diff bounded to DIFF_PIXEL_BUDGET with the box scaled back, so the
1024 MB Lambda is not exhausted by full-size previews.
"""
import base64
import io
import json
import math
import os
import sys
import uuid

import pytest
from botocore.exceptions import ClientError
from hypothesis import example, given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "functions"))

from synthetic_core import (  # noqa: E402
    MODEL_CATALOG, STABILITY_RESPONSE_MAX_BYTES, ValidationError,
    build_amazon_request_body, build_stability_inpaint_request_body,
    describe_image_limits, fit_within_limits, image_limit_violation,
    image_variation_output_size, resolve_oversize_handling, scale_bbox,
    stability_output_format,
)
from synthetic_env import SyntheticEnv  # noqa: E402

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures",
                            "synthetic_source_4608x3288.jpg")

STABILITY_MODEL_ID = "stability.stable-image-inpaint-v1:0"
STABILITY_INVOCATION_ID = "us.stability.stable-image-inpaint-v1:0"
NOVA_MODEL_ID = "amazon.nova-canvas-v1:0"
TITAN_MODEL_ID = "amazon.titan-image-generator-v2:0"

STABILITY_LIMITS = {"min_side": 64, "min_pixels": 4096,
                    "max_pixels": 9437184}
NOVA_LIMITS = {"min_side": 320, "max_side": 4096, "max_pixels": 4194303}
TITAN_LIMITS = {"max_side": 1408}
# IMAGE_VARIATION output sizes the Amazon models permit (Nova user guide
# "Supported image resolutions"; Titan Image Generator parameters table)
# and the fixed square each answers when no size is requested.
NOVA_OUTPUT_RULES = {"side_multiple": 16, "min_side": 320, "max_side": 4096,
                     "max_pixels": 4194303}
TITAN_OUTPUT_SIZES = [
    (1024, 1024), (768, 768), (512, 512), (768, 1152), (384, 576),
    (1152, 768), (576, 384), (768, 1280), (384, 640), (1280, 768),
    (640, 384), (896, 1152), (448, 576), (1152, 896), (576, 448),
    (768, 1408), (384, 704), (1408, 768), (704, 384), (640, 1408),
    (320, 704), (1408, 640), (704, 320), (1152, 640), (1173, 640),
]
AMAZON_DEFAULT_OUTPUT = {NOVA_MODEL_ID: (1024, 1024),
                         TITAN_MODEL_ID: (1408, 1408)}


def _catalog_entry(model_id):
    return next(e for e in MODEL_CATALOG if e["model_id"] == model_id)


def _anisotropy(source_size, output_size):
    """How far restoring ``output_size`` to ``source_size`` departs from a
    uniform scale: 0 for identical aspect ratios."""
    return abs((source_size[0] / output_size[0]) /
               (source_size[1] / output_size[1]) - 1)


def _log_aspect_distance(source_size, output_size):
    """|log(aspect of ``output_size``) - log(aspect of ``source_size``)|:
    the symmetric measure ``image_variation_output_size`` minimises.
    ``_anisotropy`` (ratio minus one) is asymmetric about the source's
    ratio, so for two near-tied candidates on opposite sides of it the two
    measures can disagree on the order, e.g. (835, 1951): (832, 1952) at
    0.004112 vs (832, 1936) at 0.004119 here, 0.004120 vs 0.004110 there."""
    return abs(math.log(output_size[0] / output_size[1]) -
               math.log(source_size[0] / source_size[1]))


# ============================================================ pure logic

class TestCatalogLimits:
    def test_catalog_declares_the_vendor_input_limits(self):
        assert _catalog_entry(STABILITY_MODEL_ID)["input_image_limits"] == \
            STABILITY_LIMITS
        assert _catalog_entry(NOVA_MODEL_ID)["input_image_limits"] == \
            NOVA_LIMITS
        assert _catalog_entry(TITAN_MODEL_ID)["input_image_limits"] == \
            TITAN_LIMITS

    def test_limit_descriptions(self):
        assert describe_image_limits(STABILITY_LIMITS) == (
            "images between 4,096 and 9,437,184 pixels in total, with each "
            "side at least 64 px")
        assert describe_image_limits(NOVA_LIMITS) == (
            "images up to 4,194,303 pixels in total, with each side between "
            "320 and 4,096 px")
        assert describe_image_limits(TITAN_LIMITS) == (
            "images with the longest side at most 1,408 px")
        assert describe_image_limits({}) == "images of any size"


def _dimensions(max_aspect):
    """(width, height) in 1..8000 whose aspect ratio is at most
    ``max_aspect`` (None: unconstrained)."""
    sides = st.integers(min_value=1, max_value=8000)
    if max_aspect is None:
        return st.tuples(sides, sides)
    return sides.flatmap(lambda w: st.tuples(
        st.just(w),
        st.integers(min_value=max(1, math.ceil(w / max_aspect)),
                    max_value=min(8000, math.floor(w * max_aspect)))))


# Aspect ratios the models accept (Stability 1:2.5-2.5:1, Nova 1:4-4:1,
# Titan unbounded): uniform scaling cannot fix an aspect ratio, so the
# property is stated over the inputs the models could accept at all.
_cases = st.sampled_from([
    (STABILITY_LIMITS, 2.5), (NOVA_LIMITS, 4.0), (TITAN_LIMITS, None),
]).flatmap(lambda la: st.tuples(st.just(la[0]), _dimensions(la[1])))


@settings(deadline=None)
@given(case=_cases)
@example(case=(STABILITY_LIMITS, (4608, 3288)))
@example(case=(STABILITY_LIMITS, (64, 48)))
@example(case=(NOVA_LIMITS, (8000, 2000)))
@example(case=(TITAN_LIMITS, (8000, 1)))
def test_fit_within_limits_yields_in_limit_dimensions(case):
    """**Feature: fix-synthetic-image-size-limits, Property 1:
    fit_within_limits yields in-limit dimensions**

    For every model's limits and every (width, height) the model's aspect
    bounds allow: the fitted dimensions satisfy the limits, keep the aspect
    ratio within 2 % (unless a side is below 64 px, where integer rounding
    dominates), are the input itself when it is already within the limits,
    and are a fixed point of the function.
    """
    limits, (width, height) = case
    fitted = fit_within_limits(width, height, limits)
    fitted_width, fitted_height = fitted

    assert fitted_width >= 1 and fitted_height >= 1
    assert image_limit_violation(fitted_width, fitted_height, limits) is None
    if min(fitted_width, fitted_height) >= 64:
        ratio = (fitted_width / fitted_height) / (width / height)
        assert abs(ratio - 1) <= 0.02
    if image_limit_violation(width, height, limits) is None:
        assert fitted == (width, height)
    assert fit_within_limits(fitted_width, fitted_height, limits) == fitted


def test_oversized_sample_fits_to_3636x2594_under_stability_limits():
    assert fit_within_limits(4608, 3288, STABILITY_LIMITS) == (3636, 2594)
    assert 3636 * 2594 == 9_431_784 <= 9_437_184


def test_tiny_source_is_scaled_up_to_the_minimums():
    width, height = fit_within_limits(64, 48, STABILITY_LIMITS)
    assert min(width, height) >= 64
    assert width * height >= 4096


def test_violation_names_the_pixel_count_and_the_bound():
    violation = image_limit_violation(4608, 3288, STABILITY_LIMITS)
    assert "15,151,104" in violation and "9,437,184" in violation
    assert image_limit_violation(3636, 2594, STABILITY_LIMITS) is None
    assert image_limit_violation(4608, 3288, {}) is None
    # 64x48 is 3,072 px: the pixel minimum is reported before the side one.
    assert "3,072 pixels is below the minimum of 4,096" in \
        image_limit_violation(64, 48, STABILITY_LIMITS)
    # 200x40 is 8,000 px: only the shortest side violates.
    assert "shortest side (40 px)" in image_limit_violation(
        200, 40, STABILITY_LIMITS)
    assert "longest side (4608 px)" in image_limit_violation(
        4608, 1000, TITAN_LIMITS)


class TestResolveOversizeHandling:
    def test_missing_or_none_selects_the_default(self):
        assert resolve_oversize_handling({}) == "resize_restore"
        assert resolve_oversize_handling(
            {"oversize_handling": None}) == "resize_restore"
        assert resolve_oversize_handling(None) == "resize_restore"

    def test_known_values_pass_through(self):
        assert resolve_oversize_handling(
            {"oversize_handling": "fail"}) == "fail"
        assert resolve_oversize_handling(
            {"oversize_handling": "resize_restore"}) == "resize_restore"

    def test_unknown_value_is_a_validation_error(self):
        with pytest.raises(ValidationError, match="oversize_handling"):
            resolve_oversize_handling({"oversize_handling": "bogus"})


# ------------------------------- Amazon image-variation output sizes

class TestImageVariationOutputSize:
    def test_catalog_declares_the_vendor_output_rules(self):
        assert _catalog_entry(NOVA_MODEL_ID)["image_variation_output"] == \
            NOVA_OUTPUT_RULES
        assert [tuple(s) for s in _catalog_entry(TITAN_MODEL_ID)[
            "image_variation_output"]["sizes"]] == TITAN_OUTPUT_SIZES
        assert "image_variation_output" not in _catalog_entry(
            STABILITY_MODEL_ID)
        assert image_variation_output_size(2424, 1729, None) is None
        assert image_variation_output_size(2424, 1729, {}) is None

    def test_sample_source_on_nova_asks_for_2416x1728(self):
        """4608x3288 fits Nova's input limits at 2424x1729; the only
        16-px grid size around it under the pixel budget is 2416x1728,
        0.24 % from the source's aspect ratio."""
        assert fit_within_limits(4608, 3288, NOVA_LIMITS) == (2424, 1729)
        assert image_variation_output_size(
            2424, 1729, NOVA_OUTPUT_RULES) == (2416, 1728)
        assert _anisotropy((4608, 3288), (2416, 1728)) < 0.005

    def test_sample_source_on_titan_asks_for_the_closest_permitted_size(
            self):
        """4608x3288 (1.40:1) fits Titan's input limit at 1408x1004; of
        the permitted output sizes 1152x768 (3:2) is the closest aspect
        ratio, leaving a 6.6 % residual the restore cannot remove."""
        assert fit_within_limits(4608, 3288, TITAN_LIMITS) == (1408, 1004)
        rules = _catalog_entry(TITAN_MODEL_ID)["image_variation_output"]
        assert image_variation_output_size(1408, 1004, rules) == (1152, 768)
        assert 0.065 < _anisotropy((4608, 3288), (1152, 768)) < 0.07
        # Portrait and square inputs pick the mirrored / square entries.
        assert image_variation_output_size(1004, 1408, rules) == (768, 1152)
        assert image_variation_output_size(900, 900, rules) == (1024, 1024)

    def test_request_body_carries_the_output_size_for_variation_only(self):
        nova = _catalog_entry(NOVA_MODEL_ID)
        body = build_amazon_request_body(
            nova, "image_variation", "p", "b64", 7, {}, None,
            output_size=(2416, 1728))
        assert body["imageGenerationConfig"]["width"] == 2416
        assert body["imageGenerationConfig"]["height"] == 1728
        body = build_amazon_request_body(
            nova, "inpainting", "p", "b64", 7, {}, "the region",
            output_size=(2416, 1728))
        assert "width" not in body["imageGenerationConfig"]
        assert "height" not in body["imageGenerationConfig"]
        # Without an output size the body is the pre-change one.
        assert build_amazon_request_body(
            nova, "image_variation", "p", "b64", 7, {}, None) == \
            build_amazon_request_body(
                nova, "image_variation", "p", "b64", 7, {}, None,
                output_size=None)


# Nova inputs: sides 320..4096, aspect within 4:1, fewer than 4,194,304
# pixels (a 4096 px side leaves no such height: 4096x1024 is the budget).
_nova_inputs = st.integers(min_value=320, max_value=4095).flatmap(
    lambda w: st.tuples(st.just(w), st.integers(
        min_value=max(320, math.ceil(w / 4)),
        max_value=min(4096, 4 * w, 4194303 // w))))


@settings(deadline=None)
@given(size=_nova_inputs)
@example(size=(2424, 1729))
@example(size=(320, 320))
@example(size=(4095, 1023))
@example(size=(2047, 2049))
@example(size=(835, 1951))
def test_nova_output_size_is_permitted_and_close_to_the_input_aspect(size):
    """**Feature: fix-synthetic-image-size-limits, Property 2: the
    requested Nova image-variation output is permitted and preserves the
    input's aspect ratio**

    For every input within Nova's input limits (and its 4:1 aspect bound),
    the requested output has sides that are multiples of 16 within
    320..4096, fewer than 4,194,304 pixels, lies within one grid step of
    the input on each side, is the input itself when that is on the grid,
    and departs from the input's aspect ratio (in log-ratio distance, the
    measure the selection minimises) by no more than rounding both sides
    down to the grid would.
    """
    width, height = size
    output = image_variation_output_size(width, height, NOVA_OUTPUT_RULES)
    assert output is not None
    out_w, out_h = output
    assert out_w % 16 == 0 and out_h % 16 == 0
    assert 320 <= min(out_w, out_h) and max(out_w, out_h) <= 4096
    assert out_w * out_h < 4_194_304
    assert abs(out_w - width) <= 16 and abs(out_h - height) <= 16
    if width % 16 == 0 and height % 16 == 0:
        assert output == (width, height)
    floor_only = ((width // 16) * 16, (height // 16) * 16)
    assert _log_aspect_distance((width, height), output) <= \
        _log_aspect_distance((width, height), floor_only) + 1e-12


@settings(deadline=None)
@given(width=st.integers(min_value=1, max_value=1408),
       height=st.integers(min_value=1, max_value=1408))
@example(width=1408, height=1004)
@example(width=1, height=1408)
def test_titan_output_size_is_the_permitted_size_closest_in_aspect(
        width, height):
    """**Feature: fix-synthetic-image-size-limits, Property 3: the
    requested Titan image-variation output is the permitted size closest
    to the input's aspect ratio**

    For every input within Titan's input limit, the requested output is
    one of the documented sizes and no other documented size has an
    aspect ratio closer to the input's.
    """
    rules = _catalog_entry(TITAN_MODEL_ID)["image_variation_output"]
    output = image_variation_output_size(width, height, rules)
    assert output in TITAN_OUTPUT_SIZES
    target = math.log(width / height)
    chosen_distance = abs(math.log(output[0] / output[1]) - target)
    for candidate in TITAN_OUTPUT_SIZES:
        assert abs(math.log(candidate[0] / candidate[1]) - target) >= \
            chosen_distance - 1e-12


class TestScaleBbox:
    def test_identity_when_sizes_are_equal(self):
        box = {"left": 10, "top": 20, "width": 5, "height": 6}
        assert scale_bbox(box, (100, 80), (100, 80)) == box

    def test_full_grid_maps_onto_the_full_image(self):
        diff_size = fit_within_limits(4608, 3288, {"max_pixels": 1 << 20})
        assert scale_bbox(
            {"left": 0, "top": 0, "width": diff_size[0],
             "height": diff_size[1]}, diff_size, (4608, 3288)) == {
                "left": 0, "top": 0, "width": 4608, "height": 3288}

    def test_scaled_box_covers_the_region_and_stays_inside_the_image(self):
        scaled = scale_bbox({"left": 3, "top": 2, "width": 1, "height": 1},
                            (10, 10), (45, 33))
        assert scaled["left"] <= 3 * 4.5 and scaled["top"] <= 2 * 3.3
        assert scaled["left"] + scaled["width"] >= 4 * 4.5
        assert scaled["top"] + scaled["height"] >= 3 * 3.3
        assert scaled["left"] + scaled["width"] <= 45
        assert scaled["top"] + scaled["height"] <= 33


class TestStabilityOutputFormat:
    """fix-synthetic-response-cap: a source scaled *down* to Stability's
    pixel cap asks for JPEG output (a PNG answer at 9.43 MP is 19-20 MB
    base64, above the 16,777,216-byte response cap); in-limit and
    scaled-up sources keep the lossless PNG default."""

    def test_downscaled_live_source_asks_for_jpeg(self):
        assert stability_output_format((4608, 3288), (3636, 2594)) == "jpeg"

    def test_in_limit_source_keeps_png(self):
        assert stability_output_format((128, 96), (128, 96)) == "png"

    def test_upscaled_tiny_source_keeps_png(self):
        fitted = fit_within_limits(64, 48, STABILITY_LIMITS)
        assert fitted[0] * fitted[1] > 64 * 48
        assert stability_output_format((64, 48), fitted) == "png"

    def test_square_downscaled_source_asks_for_jpeg(self):
        assert stability_output_format((4000, 4000), (3072, 3072)) == "jpeg"

    def test_request_body_carries_the_requested_output_format(self):
        body = build_stability_inpaint_request_body(
            "p", "aW1n", "bWFzaw==", 7, output_format="jpeg")
        assert body["output_format"] == "jpeg"
        assert set(body) == {"image", "mask", "prompt", "seed",
                             "output_format"}

    def test_response_cap_is_the_live_value(self):
        assert STABILITY_RESPONSE_MAX_BYTES == 16_777_216

    @settings(deadline=None)
    @given(size=_dimensions(2.5))
    def test_property_jpeg_exactly_for_sources_above_the_pixel_cap(
            self, size):
        """**Feature: fix-synthetic-response-cap, Property 1: JPEG exactly
        for sources above the Stability pixel cap**: with the fitted size
        the worker sends, the format is JPEG iff the source has more pixels
        than the cap, and then the fitted pixel count is within the cap."""
        fitted = fit_within_limits(*size, STABILITY_LIMITS)
        expected = "jpeg" if size[0] * size[1] > 9_437_184 else "png"
        assert stability_output_format(size, fitted) == expected
        if expected == "jpeg":
            assert 0 < fitted[0] * fitted[1] <= 9_437_184


# ======================================================= worker (moto)

@pytest.fixture(scope="module")
def senv(aws_stack):
    return SyntheticEnv(aws_stack)


@pytest.fixture
def actor(senv):
    """(usecase_id, DataScientist user) pair, fresh per test."""
    usecase_id = senv.create_usecase()
    return usecase_id, senv.actor_with_role(usecase_id, "DataScientist")


def _patched(senv, name, replacement):
    """Context-manager-free patch helper: returns a restore callable."""
    sd = senv.synthetic_data
    original = getattr(sd, name)
    setattr(sd, name, replacement)
    return lambda: setattr(sd, name, original)


def _fixture_bytes():
    with open(FIXTURE_PATH, "rb") as handle:
        return handle.read()


def _png_bytes(width, height):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 120, 120)).save(
        buffer, format="PNG")
    return buffer.getvalue()


def _image_bytes(width, height, fmt):
    """A solid-colour image of ``width`` x ``height`` encoded as ``fmt``
    (``"PNG"`` or ``"JPEG"``)."""
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 120, 120)).save(
        buffer, format=fmt)
    return buffer.getvalue()


def _image_size(image_bytes):
    from PIL import Image
    with Image.open(io.BytesIO(image_bytes)) as img:
        return img.size


def _b64_image_size(image_b64):
    return _image_size(base64.b64decode(image_b64))


# Bytes per pixel of the Stability answer by output format, taken from the
# live failure and the real model rather than from the stub's solid-colour
# image (a flat PNG compresses to nothing). PNG: the densest of the five
# live payloads of session 6115b94c, 20,276,590 bytes for 9,431,784 px
# (3636x2594). JPEG: the real model's JPEG answers for the live 4608x3288
# sample fitted to 3636x2594, measured on 2026-11-22 against
# us.stability.stable-image-inpaint-v1:0 (seeds 7 and 8: 2,684,074 and
# 2,713,859 bytes = 0.2846 and 0.2877 B/px; payloads 3,578,819 and
# 3,618,531 bytes, 21 % of the cap), rounded up.
LIVE_PNG_BYTES_PER_PIXEL = 1.61
JPEG_BYTES_PER_PIXEL = 0.29


def _stability_payload_bytes(width, height, output_format):
    """Size of the Stability response JSON for an image of ``width`` x
    ``height`` in ``output_format``: the base64 of the image bytes (4/3,
    padded) plus about 70 bytes of JSON scaffolding."""
    density = (JPEG_BYTES_PER_PIXEL if output_format == "jpeg"
               else LIVE_PNG_BYTES_PER_PIXEL)
    return 4 * ((int(width * height * density) + 2) // 3) + 70


class _SizedBedrockRuntime:
    """Stubbed bedrock-runtime client: records every invoke_model call
    (modelId + parsed body) and answers with a solid image sized as the
    real models size their output: Stability inpaint and Amazon INPAINTING
    after the input image; Amazon IMAGE_VARIATION after the requested
    ``imageGenerationConfig`` width/height, or the model's fixed square
    (Nova 1024x1024, Titan 1408x1408) when none is requested.

    The Stability answer honours the request's ``output_format`` and is
    refused like the live service when its payload, computed at the live
    PNG density (or the JPEG density), would exceed the 16,777,216-byte
    response cap; the call records ``output_format`` and ``payload``."""

    def __init__(self, provider):
        self.provider = provider
        self.calls = []

    def output_size(self, model_id, body):
        if self.provider == "stability":
            return _b64_image_size(body["image"])
        if body["taskType"] == "INPAINTING":
            return _b64_image_size(body["inPaintingParams"]["image"])
        config = body.get("imageGenerationConfig") or {}
        if "width" in config or "height" in config:
            return config["width"], config["height"]
        return AMAZON_DEFAULT_OUTPUT[model_id]

    def invoke_model(self, **kwargs):
        body = json.loads(kwargs["body"])
        width, height = self.output_size(kwargs["modelId"], body)
        call = {"modelId": kwargs["modelId"], "body": body,
                "output_size": (width, height)}
        self.calls.append(call)
        if self.provider == "stability":
            fmt = body.get("output_format", "png")
            payload_size = _stability_payload_bytes(width, height, fmt)
            call["output_format"] = fmt
            call["payload"] = payload_size
            if payload_size > STABILITY_RESPONSE_MAX_BYTES:
                raise ClientError({"Error": {
                    "Code": "ValidationException",
                    "Message": json.dumps({"detail": (
                        f"Response payload size {payload_size} bytes "
                        f"exceeds the maximum allowed size of 16777216 "
                        f"bytes. Please try adjusting the seed, using a "
                        f"different input image, or requesting JPEG "
                        f"output_format")}, separators=(",", ":")),
                }}, "InvokeModel")
            image_b64 = base64.b64encode(_image_bytes(
                width, height, "JPEG" if fmt == "jpeg" else "PNG")).decode()
            payload = {"images": [image_b64], "seeds": ["1"],
                       "finish_reasons": [None]}
        else:
            image_b64 = base64.b64encode(_png_bytes(width, height)).decode()
            payload = {"images": [image_b64]}
        return {"body": io.BytesIO(json.dumps(payload).encode())}


class _PassThroughBedrockRuntime:
    """Stub that never decodes the request image: records the body and
    returns a 64x48 PNG."""

    def __init__(self):
        self.calls = []

    def invoke_model(self, **kwargs):
        self.calls.append({"modelId": kwargs["modelId"],
                           "body": json.loads(kwargs["body"])})
        payload = {"images": [base64.b64encode(_png_bytes(64, 48)).decode()]}
        return {"body": io.BytesIO(json.dumps(payload).encode())}


def _run_worker(senv, usecase_id, model_id, source_class, source_bytes,
                runtime, generation_params=None, extension="jpg"):
    """Persist a one-task session whose single source is ``source_bytes``,
    run the worker against ``runtime`` and return
    (worker result, previews, session META)."""
    session_id = senv.put_session_meta(
        usecase_id, status="generating", generation_model_id=model_id,
        source_class=source_class, generation_pass=1,
        generation_params=generation_params or {"variation_count": 1},
    )
    source_key = f"datasets/limits/{session_id}.{extension}"
    senv.s3.put_object(Bucket=senv.bucket, Key=source_key, Body=source_bytes)
    senv.sessions_table.update_item(
        Key={"session_id": session_id, "sk": "META"},
        UpdateExpression="SET generation_plan = :p",
        ExpressionAttributeValues={
            ":p": [{
                "task_index": 0, "source_index": 0,
                "source_image": {"bucket": senv.bucket, "key": source_key},
                "variation_index": 0,
                "model_id": model_id,
                "resolved_prompt": "a scratch on the part", "seed": 7,
                "params": {},
            }],
        })

    restore = _patched(senv, "_bedrock_runtime", runtime)
    try:
        result = senv.synthetic_data.run_generation_worker({
            "internal_action": "generation_worker",
            "session_id": session_id,
            "generation_pass": 1,
        })
    finally:
        restore()

    items = senv.sessions_table.query(
        KeyConditionExpression="session_id = :s",
        ExpressionAttributeValues={":s": session_id})["Items"]
    previews = senv.synthetic_data._from_ddb(
        [i for i in items if str(i["sk"]).startswith("PREVIEW#")])
    meta = senv.synthetic_data._from_ddb(
        next(i for i in items if i["sk"] == "META"))
    return result, previews, meta


class TestWorkerSizeLimitHandling:
    def test_oversized_stability_source_is_resized_for_model_and_restored(
            self, senv, actor):
        """The 4608x3288 source is sent to Stability within its limits
        (image and mask at the same fitted size) and the generated image is
        restored to the source dimensions; mask_region stays in source
        coordinates."""
        usecase_id, _ = actor
        runtime = _SizedBedrockRuntime("stability")
        result, previews, _ = _run_worker(
            senv, usecase_id, STABILITY_MODEL_ID, "normal",
            _fixture_bytes(), runtime)

        assert result == {"status": "done", "completed": 1, "failed": 0}
        assert [c["modelId"] for c in runtime.calls] == [
            STABILITY_INVOCATION_ID]
        body = runtime.calls[0]["body"]
        width, height = _b64_image_size(body["image"])
        assert _b64_image_size(body["mask"]) == (width, height)
        assert width * height <= 9_437_184, (
            f"request image is {width}x{height} = {width * height:,} px, "
            f"above the model's 9,437,184 px maximum")
        assert min(width, height) >= 64

        preview = previews[0]
        assert preview["status"] == "completed"
        staged = senv.s3.get_object(
            Bucket=senv.bucket, Key=preview["staging_key"])["Body"].read()
        assert _image_size(staged) == (4608, 3288)
        rect = preview["mask_region"]
        assert 0 <= rect["left"] and rect["left"] + rect["width"] <= 4608
        assert 0 <= rect["top"] and rect["top"] + rect["height"] <= 3288
        assert preview["resized_for_model"] == {
            "source_width": 4608, "source_height": 3288,
            "model_width": width, "model_height": height,
        }

    def test_fail_handling_records_reason_without_invoking_model(
            self, senv, actor):
        usecase_id, _ = actor
        runtime = _SizedBedrockRuntime("stability")
        result, previews, meta = _run_worker(
            senv, usecase_id, STABILITY_MODEL_ID, "normal",
            _fixture_bytes(), runtime,
            generation_params={"variation_count": 1,
                               "oversize_handling": "fail"})

        assert runtime.calls == []
        assert result["completed"] == 0 and result["failed"] == 1
        preview = previews[0]
        assert preview["status"] == "failed"
        reason = preview["failure_reason"]
        for text in ("4608x3288", "15,151,104", "9,437,184",
                     STABILITY_MODEL_ID):
            assert text in reason
        assert meta["last_failure"]["reason"] == reason
        assert meta["status"] == "awaiting_review"

    def test_downscaled_stability_source_requests_jpeg_output_under_the_response_cap(  # noqa: E501
            self, senv, actor):
        """fix-synthetic-response-cap, the live failure of session 6115b94c
        pass 2: the 4608x3288 source fitted to 3636x2594 is accepted by
        Stability but its PNG answer (19-20 MB base64) exceeds the
        16,777,216-byte response cap, so every variation failed. The
        worker asks for JPEG output for the downscaled source, keeps the
        fitted size, and still stages a PNG restored to 4608x3288."""
        usecase_id, _ = actor
        runtime = _SizedBedrockRuntime("stability")
        result, previews, _ = _run_worker(
            senv, usecase_id, STABILITY_MODEL_ID, "normal",
            _fixture_bytes(), runtime)

        assert previews[0].get("failure_reason") is None, \
            previews[0].get("failure_reason")
        assert result == {"status": "done", "completed": 1, "failed": 0}
        assert len(runtime.calls) == 1
        call = runtime.calls[0]
        assert call["body"]["output_format"] == "jpeg"
        assert _b64_image_size(call["body"]["image"]) == (3636, 2594)
        assert call["payload"] < STABILITY_RESPONSE_MAX_BYTES

        from PIL import Image
        staged = senv.s3.get_object(
            Bucket=senv.bucket, Key=previews[0]["staging_key"])["Body"].read()
        with Image.open(io.BytesIO(staged)) as img:
            assert img.format == "PNG"
            assert img.size == (4608, 3288)
        assert previews[0]["resized_for_model"] == {
            "source_width": 4608, "source_height": 3288,
            "model_width": 3636, "model_height": 2594,
        }

    def test_upscaled_tiny_stability_source_keeps_png_output(
            self, senv, actor):
        """A source below Stability's minimums is scaled *up*: the answer
        is small, so the lossless PNG default is kept and the staged image
        is a PNG at the source size."""
        usecase_id, _ = actor
        runtime = _SizedBedrockRuntime("stability")
        result, previews, _ = _run_worker(
            senv, usecase_id, STABILITY_MODEL_ID, "normal",
            _png_bytes(64, 48), runtime, extension="png")

        assert result["completed"] == 1
        call = runtime.calls[0]
        assert call["body"]["output_format"] == "png"
        width, height = _b64_image_size(call["body"]["image"])
        assert width * height >= 4_096 and min(width, height) >= 64

        from PIL import Image
        assert previews[0]["status"] == "completed"
        staged = senv.s3.get_object(
            Bucket=senv.bucket, Key=previews[0]["staging_key"])["Body"].read()
        with Image.open(io.BytesIO(staged)) as img:
            assert img.format == "PNG"
            assert img.size == (64, 48)
        assert "resized_for_model" in previews[0]

    def test_in_limit_source_is_sent_unchanged(self, senv, actor):
        usecase_id, _ = actor
        source = _png_bytes(128, 96)
        runtime = _SizedBedrockRuntime("stability")
        result, previews, _ = _run_worker(
            senv, usecase_id, STABILITY_MODEL_ID, "normal", source, runtime,
            extension="png")

        assert result["completed"] == 1
        assert runtime.calls[0]["body"]["image"] == \
            base64.b64encode(source).decode()
        assert runtime.calls[0]["body"]["output_format"] == "png"
        assert previews[0]["status"] == "completed"
        assert "resized_for_model" not in previews[0]

    def test_amazon_oversized_source_resized_and_restored(self, senv,
                                                          actor):
        """A 4608x3288 'defect' source on Nova (image variation) is sent
        within the input limits with an output size on Nova's 16-px grid
        matching its aspect ratio, so the restore to 4608x3288 is uniform
        (without the explicit size Nova answers 1024x1024 and the restore
        would stretch it 1.40x horizontally)."""
        usecase_id, _ = actor
        runtime = _SizedBedrockRuntime("amazon")
        result, previews, _ = _run_worker(
            senv, usecase_id, NOVA_MODEL_ID, "defect", _fixture_bytes(),
            runtime)

        assert result["completed"] == 1
        body = runtime.calls[0]["body"]
        assert body["taskType"] == "IMAGE_VARIATION"
        width, height = _b64_image_size(
            body["imageVariationParams"]["images"][0])
        assert 320 <= min(width, height) and max(width, height) <= 4096
        assert width * height <= 4_194_303
        output = runtime.calls[0]["output_size"]
        assert _anisotropy((4608, 3288), output) < 0.005, (
            f"the model answered {output[0]}x{output[1]}; restoring it to "
            f"4608x3288 is not a uniform scale")
        config = body["imageGenerationConfig"]
        assert (config["width"], config["height"]) == output == (2416, 1728)

        preview = previews[0]
        staged = senv.s3.get_object(
            Bucket=senv.bucket, Key=preview["staging_key"])["Body"].read()
        assert _image_size(staged) == (4608, 3288)
        assert preview["resized_for_model"] == {
            "source_width": 4608, "source_height": 3288,
            "model_width": width, "model_height": height,
        }

    def test_titan_oversized_source_asks_for_the_closest_permitted_size(
            self, senv, actor):
        """Titan only generates the documented sizes: the request asks for
        the one closest to the source's aspect ratio (1152x768 for
        4608x3288) and the restore carries only that 6.6 % residual,
        instead of the 1.40x stretch of the default 1408x1408 square."""
        usecase_id, _ = actor
        runtime = _SizedBedrockRuntime("amazon")
        result, previews, _ = _run_worker(
            senv, usecase_id, TITAN_MODEL_ID, "defect", _fixture_bytes(),
            runtime)

        assert result["completed"] == 1
        body = runtime.calls[0]["body"]
        assert body["taskType"] == "IMAGE_VARIATION"
        width, height = _b64_image_size(
            body["imageVariationParams"]["images"][0])
        assert (width, height) == (1408, 1004)
        output = runtime.calls[0]["output_size"]
        assert _anisotropy((4608, 3288), output) < 0.07, (
            f"the model answered {output[0]}x{output[1]}; restoring it to "
            f"4608x3288 stretches it beyond the closest permitted size's "
            f"6.6 % residual")
        config = body["imageGenerationConfig"]
        assert (config["width"], config["height"]) == output == (1152, 768)

        preview = previews[0]
        staged = senv.s3.get_object(
            Bucket=senv.bucket, Key=preview["staging_key"])["Body"].read()
        assert _image_size(staged) == (4608, 3288)
        assert preview["resized_for_model"] == {
            "source_width": 4608, "source_height": 3288,
            "model_width": 1408, "model_height": 1004,
        }

    def test_amazon_inpainting_requests_no_output_size(self, senv, actor):
        """A 'normal' source on Nova is inpainted: the output follows the
        input size, so no width/height is requested (the Nova guide says
        not to for INPAINTING) and the restore is uniform."""
        usecase_id, _ = actor
        runtime = _SizedBedrockRuntime("amazon")
        result, previews, _ = _run_worker(
            senv, usecase_id, NOVA_MODEL_ID, "normal", _fixture_bytes(),
            runtime)

        assert result["completed"] == 1
        body = runtime.calls[0]["body"]
        assert body["taskType"] == "INPAINTING"
        assert _b64_image_size(body["inPaintingParams"]["image"]) == \
            (2424, 1729)
        assert "width" not in body["imageGenerationConfig"]
        assert "height" not in body["imageGenerationConfig"]
        staged = senv.s3.get_object(
            Bucket=senv.bucket,
            Key=previews[0]["staging_key"])["Body"].read()
        assert _image_size(staged) == (4608, 3288)

    def test_in_limit_amazon_variation_keeps_the_default_output(
            self, senv, actor):
        """An in-limit source is sent byte-for-byte without an output size;
        Nova's 1024x1024 answer is staged as-is, as before."""
        usecase_id, _ = actor
        source = _png_bytes(640, 480)
        runtime = _SizedBedrockRuntime("amazon")
        result, previews, _ = _run_worker(
            senv, usecase_id, NOVA_MODEL_ID, "defect", source, runtime,
            extension="png")

        assert result["completed"] == 1
        body = runtime.calls[0]["body"]
        assert body["imageVariationParams"]["images"] == [
            base64.b64encode(source).decode()]
        assert "width" not in body["imageGenerationConfig"]
        staged = senv.s3.get_object(
            Bucket=senv.bucket,
            Key=previews[0]["staging_key"])["Body"].read()
        assert _image_size(staged) == (1024, 1024)
        assert "resized_for_model" not in previews[0]

    def test_undecodable_amazon_source_is_passed_through(self, senv, actor):
        """Bytes Pillow cannot decode skip the limit step and reach the
        model unchanged (today's behaviour)."""
        usecase_id, _ = actor
        runtime = _PassThroughBedrockRuntime()
        result, previews, _ = _run_worker(
            senv, usecase_id, NOVA_MODEL_ID, "defect", b"src", runtime,
            extension="png")

        assert result["completed"] == 1
        images = runtime.calls[0]["body"]["imageVariationParams"]["images"]
        assert images == [base64.b64encode(b"src").decode()]
        assert previews[0]["status"] == "completed"
        assert "resized_for_model" not in previews[0]


class TestRestoreImageSize:
    """``_restore_image_size`` always yields a PNG: the staged ``.png``
    object is a PNG whatever format the model answered in (a JPEG answer is
    transcoded even at matching size); a PNG at the target size is returned
    byte-for-byte. Sizes are scaled down by 10 from the live 3636x2594 ->
    4608x3288 case: the ratio is what matters."""

    @staticmethod
    def _restored(senv, image_bytes, size):
        from PIL import Image
        restored = senv.synthetic_data._restore_image_size(image_bytes, size)
        with Image.open(io.BytesIO(restored)) as img:
            return restored, img.format, img.size

    def test_smaller_jpeg_answer_is_restored_as_png(self, senv):
        restored, fmt, size = self._restored(
            senv, _image_bytes(364, 259, "JPEG"), (461, 329))
        assert (fmt, size) == ("PNG", (461, 329))

    def test_jpeg_answer_at_the_target_size_is_transcoded_to_png(self, senv):
        answer = _image_bytes(461, 329, "JPEG")
        restored, fmt, size = self._restored(senv, answer, (461, 329))
        assert restored != answer
        assert (fmt, size) == ("PNG", (461, 329))

    def test_png_answer_at_the_target_size_is_returned_unchanged(self, senv):
        answer = _image_bytes(461, 329, "PNG")
        restored, fmt, size = self._restored(senv, answer, (461, 329))
        assert restored == answer
        assert (fmt, size) == ("PNG", (461, 329))


class TestGenerateOversizeHandling:
    def test_generate_validates_oversize_handling(self, senv, actor):
        """An unknown value is a 400 before any plan persists; a known one
        is stored on the session's generation_params."""
        usecase_id, user = actor
        session_id = senv.put_session_meta(
            usecase_id, source_class="normal",
            generation_model_id=STABILITY_MODEL_ID)
        restore = _patched(senv, "_invoke_worker_async", lambda payload: None)
        try:
            status, body = senv.invoke(
                "POST", "/synthetic/sessions/{id}/generate", user,
                session_id=session_id,
                body={"generation_params": {"oversize_handling": "bogus"}})
            assert status == 400
            assert "oversize_handling" in body["error"]
            meta = senv.sessions_table.get_item(
                Key={"session_id": session_id, "sk": "META"})["Item"]
            assert meta["status"] == "draft"
            assert "generation_plan" not in meta

            status, body = senv.invoke(
                "POST", "/synthetic/sessions/{id}/generate", user,
                session_id=session_id,
                body={"generation_params": {"oversize_handling": "fail"}})
            assert status == 202
            meta = senv.sessions_table.get_item(
                Key={"session_id": session_id, "sk": "META"})["Item"]
            assert meta["generation_params"]["oversize_handling"] == "fail"
        finally:
            restore()


# ================================================== integration (moto)

RESTORED_SIZE = (4608, 3288)
DEFECT_RECT = {"left": 1000, "top": 800, "width": 400, "height": 300}


def _painted_png_bytes(source_bytes, rect):
    """``source_bytes`` decoded, with ``rect`` painted magenta, as PNG."""
    from PIL import Image, ImageDraw
    with Image.open(io.BytesIO(source_bytes)) as img:
        painted = img.convert("RGB")
    ImageDraw.Draw(painted).rectangle(
        [rect["left"], rect["top"], rect["left"] + rect["width"] - 1,
         rect["top"] + rect["height"] - 1], fill=(255, 0, 255))
    buffer = io.BytesIO()
    painted.save(buffer, format="PNG", compress_level=1)
    return buffer.getvalue()


def _integrate_restored_preview(senv, usecase_id, user, model_id,
                                source_bytes, generated_bytes, **preview):
    """Session awaiting review with one approved preview whose staged
    image is ``generated_bytes`` (restored to the source size); runs the
    integrate endpoint and returns the single appended manifest record."""
    run_id = uuid.uuid4().hex[:12]
    target_prefix = f"datasets/restored-{run_id}/"
    manifest_key = f"{target_prefix}manifests/train.manifest"
    source_key = f"datasets/limits/{run_id}-source.jpg"
    senv.s3.put_object(Bucket=senv.bucket, Key=source_key, Body=source_bytes)
    session_id = senv.put_session_meta(
        usecase_id, status="awaiting_review", defect_type="scratch",
        generation_model_id=model_id,
        target_dataset_prefix=target_prefix,
        target_manifest_key=manifest_key)
    preview_id = senv.put_preview(
        session_id, approval_state="approved", source_image_key=source_key,
        resized_for_model={
            "source_width": RESTORED_SIZE[0],
            "source_height": RESTORED_SIZE[1],
            "model_width": 3636, "model_height": 2594},
        **preview)
    senv.s3.put_object(
        Bucket=senv.bucket,
        Key=f"synthetic-staging/{session_id}/{preview_id}.png",
        Body=generated_bytes)

    status, body = senv.invoke(
        "POST", "/synthetic/sessions/{id}/integrate", user,
        session_id=session_id)
    assert status == 200, body
    content = senv.s3.get_object(
        Bucket=senv.bucket, Key=manifest_key)["Body"].read().decode()
    records = [json.loads(line) for line in content.splitlines() if line]
    assert len(records) == 1
    return records[0]


class TestIntegrationOfRestoredPreviews:
    def test_mask_region_preview_is_annotated_without_decoding_pixels(
            self, senv, actor):
        """A restored 4608x3288 Stability preview is annotated from its
        header and mask_region: no per-pixel grid is built."""
        usecase_id, user = actor
        decode_calls = []
        restore = _patched(
            senv, "_decode_image_pixels",
            lambda *args, **kwargs: decode_calls.append(args) or None)
        try:
            record = _integrate_restored_preview(
                senv, usecase_id, user, STABILITY_MODEL_ID,
                _fixture_bytes(), _png_bytes(*RESTORED_SIZE),
                generation_method="inpainting", mask_region=DEFECT_RECT)
        finally:
            restore()

        assert decode_calls == []
        assert record["synthetic-defect"]["image_size"] == [{
            "width": 4608, "height": 3288, "depth": 3}]
        annotation = record["synthetic-defect"]["annotations"][0]
        assert {k: annotation[k] for k in DEFECT_RECT} == DEFECT_RECT
        assert record["synthetic-defect-metadata"][
            "bounding-box-source"] == "inpainting_mask"

    def test_image_diff_preview_is_annotated_at_a_bounded_size(
            self, senv, actor):
        """A restored 4608x3288 Nova preview is diffed against its source
        on grids of at most DIFF_PIXEL_BUDGET pixels and the box is scaled
        back to the preview's coordinates."""
        usecase_id, user = actor
        sd = senv.synthetic_data
        source = _fixture_bytes()
        grid_sizes = []
        original_bbox_from_diff = sd.bbox_from_diff

        def recording_bbox_from_diff(source_px, generated_px, **kwargs):
            grid_sizes.append(((len(source_px[0]), len(source_px)),
                               (len(generated_px[0]), len(generated_px))))
            return original_bbox_from_diff(source_px, generated_px, **kwargs)

        restore = _patched(senv, "bbox_from_diff", recording_bbox_from_diff)
        try:
            record = _integrate_restored_preview(
                senv, usecase_id, user, NOVA_MODEL_ID, source,
                _painted_png_bytes(source, DEFECT_RECT),
                generation_method="image_variation")
        finally:
            restore()

        assert len(grid_sizes) == 1
        (source_grid, generated_grid), = grid_sizes
        assert source_grid == generated_grid
        assert source_grid[0] * source_grid[1] <= sd.DIFF_PIXEL_BUDGET
        assert source_grid[0] < 4608 and source_grid[1] < 3288

        assert record["synthetic-defect"]["image_size"] == [{
            "width": 4608, "height": 3288, "depth": 3}]
        assert record["synthetic-defect-metadata"][
            "bounding-box-source"] == "image_diff"
        box = record["synthetic-defect"]["annotations"][0]
        # The box covers the painted rectangle and overshoots it by at most
        # two grid cells per side (one source pixel at the diff scale is
        # 4608 / grid width source pixels).
        tolerance = 2 * 4608 / source_grid[0]
        right, bottom = box["left"] + box["width"], box["top"] + box["height"]
        assert box["left"] <= DEFECT_RECT["left"] < box["left"] + tolerance
        assert box["top"] <= DEFECT_RECT["top"] < box["top"] + tolerance
        rect_right = DEFECT_RECT["left"] + DEFECT_RECT["width"]
        rect_bottom = DEFECT_RECT["top"] + DEFECT_RECT["height"]
        assert rect_right <= right < rect_right + tolerance
        assert rect_bottom <= bottom < rect_bottom + tolerance
