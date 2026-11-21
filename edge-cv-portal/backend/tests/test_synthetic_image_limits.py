"""Source images outside the generation model's input size limits
(fix-synthetic-image-size-limits).

Pure part (no AWS): the per-model ``input_image_limits`` declared on the
Model_Catalog, the ``oversize_handling`` generation parameter and the
fit / violation / description helpers in ``synthetic_core``.

Worker part (moto stack from conftest.py + synthetic_env.py): a 4608x3288
source -- 15,151,104 pixels, the exact count that made all 50 variations
of session 6115b94c fail against the Stability inpaint model's 9,437,184
pixel maximum -- is scaled to fit before the model call and the generated
image is restored to 4608x3288; the ``fail`` handling records the reason
without calling the model; in-limit and undecodable sources are sent
unchanged; the generate endpoint rejects an unknown handling value before
any plan persists.
"""
import base64
import io
import json
import math
import os
import sys

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "functions"))

from synthetic_core import (  # noqa: E402
    MODEL_CATALOG, ValidationError, describe_image_limits,
    fit_within_limits, image_limit_violation, resolve_oversize_handling,
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


def _catalog_entry(model_id):
    return next(e for e in MODEL_CATALOG if e["model_id"] == model_id)


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


def _image_size(image_bytes):
    from PIL import Image
    with Image.open(io.BytesIO(image_bytes)) as img:
        return img.size


def _b64_image_size(image_b64):
    return _image_size(base64.b64decode(image_b64))


class _SizedBedrockRuntime:
    """Stubbed bedrock-runtime client: records every invoke_model call
    (modelId + parsed body) and answers with a solid PNG of the request
    image's size, as the real models do."""

    def __init__(self, provider):
        self.provider = provider
        self.calls = []

    def request_image_b64(self, body):
        if self.provider == "stability":
            return body["image"]
        return body["imageVariationParams"]["images"][0]

    def invoke_model(self, **kwargs):
        body = json.loads(kwargs["body"])
        self.calls.append({"modelId": kwargs["modelId"], "body": body})
        width, height = _b64_image_size(self.request_image_b64(body))
        image_b64 = base64.b64encode(_png_bytes(width, height)).decode()
        if self.provider == "stability":
            payload = {"images": [image_b64], "seeds": ["1"],
                       "finish_reasons": [None]}
        else:
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
        assert previews[0]["status"] == "completed"
        assert "resized_for_model" not in previews[0]

    def test_amazon_oversized_source_resized_and_restored(self, senv,
                                                          actor):
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

        preview = previews[0]
        staged = senv.s3.get_object(
            Bucket=senv.bucket, Key=preview["staging_key"])["Body"].read()
        assert _image_size(staged) == (4608, 3288)
        assert preview["resized_for_model"] == {
            "source_width": 4608, "source_height": 3288,
            "model_width": width, "model_height": height,
        }

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
