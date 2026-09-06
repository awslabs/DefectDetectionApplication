"""
Property-based test for Few_Shot_Example downscaling across the two real
`llm:` request paths.

Spec: llm-model-token-and-image-sizing, task 9.4.

**Feature: llm-model-token-and-image-sizing, Property 9: Few-shot
selection and image bounds are unchanged by downscaling**
**Validates: Requirements 8.1, 8.3, 8.4, 8.7, 8.8, 10.7**

The claim under test: for any stored example set (at most 10 good and 10
bad in stored order), any Model_Image_Limit of at least 1, and any
Downscale_Setting, the attached example list equals the first
`max(0, Model_Image_Limit - 1)` entries of good examples in stored order
followed by bad examples in stored order, the total image count of the
request is at least 1 and at most the Model_Image_Limit, each attached
example carries the downscaled bytes of that example image for the
selected setting — the source bytes exactly for Downscale_Off, and a
longer edge at most the selected Max_Image_Edge otherwise — and the
selection is identical in the Preview_API and the Auto_Labeler paths.

Harness (the test_property_preview_worker_request_identity.py pattern):
moto (the conftest `aws_stack`) plus **one** stub Converse client
(`test_dda_llm_prelabel.RecordingConverseClient`) serving both real
entry points — `dda_autolabel_worker._generate_llm_prelabel` (the
per-dataset-image body of the SQS worker) and
`dda_labeling._run_preview_sample` (the per-Sample_Image body of the
preview executor) — bound per example with `_Patcher`, since Hypothesis
cannot consume function-scoped fixtures. Example references are full
`s3://bucket/key` URIs, the spelling the wizard stores, for which both
paths resolve identically.

What makes each claim sharp:

- **Selection is independent of the Downscale_Setting** (Req 8.3): the
  expected attached list is recomputed in the test from the generated
  stored order alone — good in stored order, then bad in stored order,
  first `max(0, limit - 1)` entries — with no reference to the setting
  and no call into `select_few_shot_examples`. Request content equality
  against that expectation across every generated setting (including
  `limit == 1`, which attaches nothing) is the independence claim.
- **Omitted examples are never read and never downscaled**: both
  modules' `get_s3_client_for_bucket` seams are wrapped to record every
  `get_object`, and the shared chokepoint's `downscale_image` binding is
  wrapped to record every call. On top of that, every omitted example is
  seeded as *poison* — header-only bytes declaring dimensions above
  every Max_Image_Edge option — so a wrongly-attached or wrongly-
  downscaled omitted example cannot fail silently: under a bound the
  downscaler would have to decode it and explode, and at Downscale_Off
  its poison bytes could never equal an expected block.
- **Attribution**: every seeded image has globally distinct width and
  height values (a reserved-set draw) and distinct fill content, so each
  request block is attributable to exactly one stored example through
  its bytes, and the recorded S3 read order pins the attachment order
  independently of the block bytes.
- **Downscaled with the target's one setting, exactly once each**
  (Req 8.1): under a bound, the recorded `downscale_image` call sequence
  per path must be exactly `[target] + attached` in order, every call
  carrying the run's single Max_Image_Edge; at Downscale_Off the
  recorded sequence must be empty (the source bytes are not merely
  equal — nothing was ever re-encoded, Req 8.7).
- **The layout is unchanged by the setting**: the whole content list is
  compared against an expectation built purely from the attached
  designations — header, per-example identification text, image block
  pairs, target intro, target image, prompt — whose *structure* does not
  mention the setting; only image bytes and the prompt's dimension
  sentence vary with it, exactly as designed.

Expectation provenance: the downscale algebra itself (formula,
determinism, pass-through) is Property 4's, so expected block bytes are
computed by calling the real `dda_llm_image.downscale_image` directly on
each seeded source; what this property pins is that the pipeline
attaches *those* bytes for *those* examples in *that* order on *both*
paths. The Requirement 6.4 floor formula is nevertheless re-derived
in-test and asserted against the decoded dimensions of every attached
block, so the Req 8.8 bound never rests on the module under test.
"""
import functools
import io
import json
import os
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PIL import Image

from dda_llm_guidance import build_detection_prompt
from dda_llm_image import DOWNSCALE_OFF, MAX_IMAGE_EDGE_OPTIONS
from dda_llm_image import downscale_image as real_downscale_image
from dda_llm_request import FEW_SHOT_HEADER, FEW_SHOT_TARGET_INTRO
from test_dda_autolabel_worker import (
    ARTIFACTS_BUCKET,
    AutolabelEnv,
    DATASET_BUCKET,
    SAM_FUNCTION,
    jpeg_bytes,
    png_bytes,
)
from test_dda_llm_prelabel import RecordingConverseClient, guidance
from test_property_llm_autolabel_preservation import _Patcher

REGION = "us-east-1"
MODEL_ID = "us.amazon.nova-pro-v1:0"
MODEL = f"llm:{MODEL_ID}"

# Fixed request inputs: Property 9 quantifies over example sets, limits
# and settings — not over modalities or prompt text (Property 5's and
# the predecessor identity property's business).
MODALITY = "ObjectDetection"
LABELS = ["scratch", "dent"]
PROMPT = "Find every scratch on the panel."

GOOD = "good"
BAD = "bad"

EXAMPLES_PREFIX = "labeling-examples/"

# Pinned Bedrock_Configuration for both paths, the identity-test shape.
# maxTokens is replaced by the resolver (no selection, no mapping: the
# default) and is out of this property's scope.
BEDROCK_CONFIG = {
    "model_id": MODEL_ID,
    "region": "us-west-2",
    "max_tokens": 4096,
    "temperature": None,
    "top_p": None,
    "timeout_seconds": 240,
}


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def worker(aws_stack):
    """The real dda_autolabel_worker imported inside the moto mock."""
    os.environ["SAM_WORKER_FUNCTION_NAME"] = SAM_FUNCTION
    sys.modules.pop("dda_autolabel_worker", None)
    import dda_autolabel_worker

    dda_autolabel_worker.SAM_WORKER_FUNCTION_NAME = SAM_FUNCTION
    dda_autolabel_worker.PORTAL_ARTIFACTS_BUCKET = ARTIFACTS_BUCKET

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=DATASET_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    return dda_autolabel_worker


@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling
    return dda_labeling


@pytest.fixture(scope="module")
def prelabel(aws_stack):
    """The shared invocation module both paths delegate to."""
    import dda_llm_prelabel
    return dda_llm_prelabel


class _EnvPatcher(_Patcher):
    """`_Patcher` plus environment variables, restored together.

    `LLM_MODEL_IMAGE_LIMITS` is read per call by both modules, so it is
    the seam through which a generated Model_Image_Limit reaches both
    paths identically (the task-context configuration mechanism).
    """

    def __init__(self):
        super().__init__()
        self._env = []

    def setenv(self, name, value):
        self._env.append((name, os.environ.get(name)))
        os.environ[name] = value

    def delenv(self, name):
        self._env.append((name, os.environ.get(name)))
        os.environ.pop(name, None)

    def undo(self):
        for name, original in reversed(self._env):
            if original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original
        self._env = []
        super().undo()


# ------------------------------------------------------------------ seams

class _RecordingS3Client:
    """S3 client proxy recording every (Bucket, Key) read — the
    test_dda_autolabel_worker_few_shot.py convention, applied here to
    both modules so an omitted reference read on either path is
    visible."""

    def __init__(self, inner, calls):
        self._inner = inner
        self._calls = calls

    def get_object(self, **kwargs):
        self._calls.append((kwargs.get("Bucket"), kwargs.get("Key")))
        return self._inner.get_object(**kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _recording_s3_factory(real_factory, calls):
    def factory(usecase, bucket, session_name="portal-s3-access"):
        return _RecordingS3Client(real_factory(usecase, bucket, session_name),
                                  calls)
    return factory


# ---------------------------------------------------------- image builders

@functools.lru_cache(maxsize=512)
def _image_bytes(container, width, height, color_index):
    """A real, fully decodable image at exactly (width, height), with a
    fill color derived from `color_index` so no two images in one case
    share content even after downscaling maps them near one another.
    Cached because bytes are immutable and never mutated downstream."""
    color = (10 + (color_index * 31) % 230,
             10 + (color_index * 57) % 230,
             10 + (color_index * 83) % 230)
    buffer = io.BytesIO()
    image = Image.new("RGB", (width, height), color)
    if container == "png":
        image.save(buffer, format="PNG", compress_level=1)
    else:
        image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def _expected_scaled(width, height, bound):
    """The Requirement 6.4 floor formula, re-derived independently of
    the module under test."""
    if max(width, height) <= bound:
        return width, height
    longer = max(width, height)
    return (max(1, width * bound // longer),
            max(1, height * bound // longer))


# -------------------------------------------------------------- strategies

def _free_dim(draw, reserved, lo, hi):
    """One pixel dimension, distinct from every previously drawn value
    in this case. Bumping past collisions instead of filtering keeps
    shrinking cheap; bumps are bounded well below every range gap that
    matters (a fitting edge can never cross the smallest bound, an
    oversize or poison edge can never fall back under its bound)."""
    value = draw(st.integers(min_value=lo, max_value=hi))
    while value in reserved:
        value += 1
    reserved.add(value)
    return value


@st.composite
def _downscaling_cases(draw):
    """A stored example set (0-10 good, 0-10 bad, interleaved stored
    order), a Model_Image_Limit (1 weighted, spanning below / at / above
    the largest possible example count), a Downscale_Setting from all
    seven values of Requirement 5.1, and a target image.

    Which stored entries will attach is derived here, before any image
    is sized, from the stored order and the limit alone: attached-to-be
    entries get real decodable images (a mix of edges under and over the
    selected bound, so both the pass-through and the re-encode branches
    of Req 8.7/8.8 are exercised); omitted-to-be entries get poison
    header-only bytes declaring dimensions above every option.
    """
    setting = draw(st.sampled_from((DOWNSCALE_OFF,) + MAX_IMAGE_EDGE_OPTIONS))
    limit = draw(st.one_of(st.just(1), st.integers(min_value=1, max_value=25)))
    n_good = draw(st.integers(min_value=0, max_value=10))
    n_bad = draw(st.integers(min_value=0, max_value=10))
    order = draw(st.permutations([GOOD] * n_good + [BAD] * n_bad))

    # The attachment prefix, computed with no reference to the setting:
    # good in stored order, then bad in stored order, first `limit - 1`.
    good_indices = [i for i, d in enumerate(order) if d == GOOD]
    bad_indices = [i for i, d in enumerate(order) if d == BAD]
    attached_indices = set((good_indices + bad_indices)[:max(0, limit - 1)])

    reserved = set()

    def real_dimensions():
        """Real image dimensions: oversize keeps the off edge small so
        decode-and-re-encode stays fast at 100 examples."""
        if setting is not DOWNSCALE_OFF and draw(st.booleans()):
            long_edge = _free_dim(draw, reserved, setting + 1, setting + 320)
            short_edge = _free_dim(draw, reserved, 4, 64)
            if draw(st.booleans()):
                return long_edge, short_edge
            return short_edge, long_edge
        return (_free_dim(draw, reserved, 4, 400),
                _free_dim(draw, reserved, 4, 400))

    counters = {GOOD: 0, BAD: 0}
    stored = []
    for index, designation in enumerate(order):
        position = counters[designation]
        counters[designation] += 1
        attached = index in attached_indices
        if attached:
            width, height = real_dimensions()
        else:
            # Poison: above every Max_Image_Edge option, undecodable
            # beyond the header.
            width = _free_dim(draw, reserved, 2500, 3900)
            height = _free_dim(draw, reserved, 2500, 3900)
        stored.append(SimpleNamespace(
            designation=designation, position=position,
            extension=draw(st.sampled_from((".png", ".jpg"))),
            width=width, height=height, attached=attached))

    target_width, target_height = real_dimensions()
    return SimpleNamespace(
        setting=setting, limit=limit, stored=stored,
        target_extension=draw(st.sampled_from((".png", ".jpg"))),
        target_width=target_width, target_height=target_height)


def _image_blocks(call):
    return [block["image"] for block in call["messages"][0]["content"]
            if "image" in block]


# --------------------------------------------------------------- Property 9

# Feature: llm-model-token-and-image-sizing, Property 9: Few-shot
# selection and image bounds are unchanged by downscaling
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(case=_downscaling_cases())
def test_property_few_shot_selection_and_bounds_unchanged_by_downscaling(
        aws_stack, worker, dda, prelabel, case):
    """
    **Feature: llm-model-token-and-image-sizing, Property 9: Few-shot
    selection and image bounds are unchanged by downscaling**

    For any stored example set (at most 10 good and 10 bad in stored
    order), any Model_Image_Limit of at least 1, and any
    Downscale_Setting, the attached example list equals the first
    `max(0, Model_Image_Limit - 1)` entries of good examples in stored
    order followed by bad examples in stored order, the total image
    count of the request is at least 1 and at most the
    Model_Image_Limit, each attached example carries the downscaled
    bytes of that example image for the selected setting — the source
    bytes exactly for Downscale_Off, and a longer edge at most the
    selected Max_Image_Edge otherwise — and the selection is identical
    in the Preview_API and the Auto_Labeler paths.

    **Validates: Requirements 8.1, 8.3, 8.4, 8.7, 8.8, 10.7**
    """
    bound = case.setting
    patcher = _EnvPatcher()
    stub = RecordingConverseClient(reply=guidance([]))
    downscale_calls = []
    worker_reads, preview_reads = [], []
    try:
        env = AutolabelEnv(aws_stack, worker, patcher)
        usecase = dda.get_usecase(env.usecase_id)

        # One stub Converse client behind both paths' own bindings.
        def client_factory(region, timeout_seconds):
            return stub

        patcher.setattr(worker, "get_bedrock_client", client_factory)
        patcher.setattr(dda, "get_bedrock_client", client_factory)
        patcher.setattr(prelabel, "get_bedrock_client", client_factory)
        patcher.setattr(prelabel, "get_bedrock_configuration",
                        lambda: dict(BEDROCK_CONFIG))

        # The chokepoint's downscale seam, wrapped not replaced: records
        # (bytes, setting) per call, then delegates to the real
        # Image_Downscaler, so the requests stay real.
        def spying_downscale(image_bytes, image_format, downscale_setting,
                             **kwargs):
            downscale_calls.append((image_bytes, downscale_setting))
            return real_downscale_image(image_bytes, image_format,
                                        downscale_setting, **kwargs)

        patcher.setattr(prelabel, "downscale_image", spying_downscale)

        # Record every S3 read on each path's own client seam.
        patcher.setattr(worker, "get_s3_client_for_bucket",
                        _recording_s3_factory(
                            worker.get_s3_client_for_bucket, worker_reads))
        patcher.setattr(dda, "get_s3_client_for_bucket",
                        _recording_s3_factory(
                            dda.get_s3_client_for_bucket, preview_reads))

        # Deterministic per-model configuration: the generated
        # Model_Image_Limit through the environment seam both modules
        # read per call, and no Model_Token_Limits at all (budget is out
        # of this property's scope; both paths resolve the default).
        patcher.setenv("LLM_MODEL_IMAGE_LIMITS",
                       json.dumps({MODEL_ID: case.limit}))
        patcher.delenv("LLM_MODEL_TOKEN_LIMITS")
        patcher.setattr(worker, "SETTINGS_TABLE", None)
        patcher.setattr(dda, "SETTINGS_TABLE", None)
        worker._reset_model_token_limits_cache()
        dda._reset_model_token_limits_cache()

        # ------------------------------------------------------ seed S3
        target_format = ("png" if case.target_extension == ".png"
                         else "jpeg")
        target_key = (f"datasets/{uuid.uuid4().hex[:8]}/"
                      f"target{case.target_extension}")
        target_bytes = _image_bytes(target_format, case.target_width,
                                    case.target_height, 0)
        env.s3.put_object(Bucket=DATASET_BUCKET, Key=target_key,
                          Body=target_bytes)

        base = f"{EXAMPLES_PREFIX}{uuid.uuid4().hex[:8]}"
        stored_refs, examples = [], []
        for index, meta in enumerate(case.stored):
            key = (f"{base}/{meta.designation}/"
                   f"{meta.position}{meta.extension}")
            fmt = "png" if meta.extension == ".png" else "jpeg"
            if meta.attached:
                body = _image_bytes(fmt, meta.width, meta.height, index + 1)
            else:
                body = (png_bytes(meta.width, meta.height) if fmt == "png"
                        else jpeg_bytes(meta.width, meta.height))
            env.s3.put_object(Bucket=DATASET_BUCKET, Key=key, Body=body)
            stored_refs.append({"ref": f"s3://{DATASET_BUCKET}/{key}",
                                "designation": meta.designation,
                                "position": meta.position})
            examples.append(SimpleNamespace(key=key, fmt=fmt, body=body,
                                            meta=meta))

        # ------------------------------------- the expected attachment
        # Good in stored order, then bad in stored order, first
        # `max(0, limit - 1)` — recomputed here with no reference to the
        # Downscale_Setting and no call into the module's selection.
        candidates = ([ex for ex in examples if ex.meta.designation == GOOD]
                      + [ex for ex in examples if ex.meta.designation == BAD])
        slots = max(0, case.limit - 1)
        attached, omitted = candidates[:slots], candidates[slots:]
        # Generator self-check: exactly the attached-to-be entries got
        # real images.
        assert all(ex.meta.attached for ex in attached)
        assert not any(ex.meta.attached for ex in omitted)

        if bound is DOWNSCALE_OFF:
            expected_target_bytes = target_bytes
            sent_width, sent_height = case.target_width, case.target_height
            expected_example_bytes = [ex.body for ex in attached]
        else:
            expected_target_bytes, sent_width, sent_height = (
                real_downscale_image(target_bytes, target_format, bound))
            # The module's target dimensions match the in-test formula.
            assert (sent_width, sent_height) == _expected_scaled(
                case.target_width, case.target_height, bound)
            expected_example_bytes = [
                real_downscale_image(ex.body, ex.fmt, bound)[0]
                for ex in attached]

        # The full expected content list. Its *structure* — header, one
        # identification text block immediately before each example
        # image, target intro, target image, prompt — is built from the
        # attached designations alone, so a passing equality at every
        # generated setting is the layout-unchanged claim.
        expected_prompt = build_detection_prompt(
            MODALITY, LABELS, PROMPT, sent_width, sent_height, None)
        expected_content = []
        if attached:
            expected_content.append({"text": FEW_SHOT_HEADER})
            ordinals = {GOOD: 0, BAD: 0}
            for ex, block_bytes in zip(attached, expected_example_bytes):
                ordinals[ex.meta.designation] += 1
                label = ("Good example" if ex.meta.designation == GOOD
                         else "Bad example")
                expected_content.append(
                    {"text": f"{label} {ordinals[ex.meta.designation]}:"})
                expected_content.append(
                    {"image": {"format": ex.fmt,
                               "source": {"bytes": block_bytes}}})
            expected_content.append({"text": FEW_SHOT_TARGET_INTRO})
        expected_content.append(
            {"image": {"format": target_format,
                       "source": {"bytes": expected_target_bytes}}})
        expected_content.append({"text": expected_prompt})

        # ------------------------------------------- drive both paths
        job = {
            "job_id": f"labeling-{uuid.uuid4().hex[:8]}",
            "usecase_id": env.usecase_id,
            "task_type": MODALITY,
            "label_set": list(LABELS),
            "skip_verification": False,
            "auto_label": {
                "enabled": True,
                "model": MODEL,
                "detection_prompt": PROMPT,
                "few_shot": {"enabled": True,
                             "examples": [dict(ref) for ref in stored_refs]},
                # Absent for Downscale_Off — the shape create_dda_job
                # persists (Req 5.7).
                **({} if bound is DOWNSCALE_OFF
                   else {"downscale_max_edge": bound}),
            },
        }
        message = {
            "job_id": job["job_id"],
            "task_id": f"task-{uuid.uuid4().hex[:8]}",
            "image_s3_uri": f"s3://{DATASET_BUCKET}/{target_key}",
            "modality": MODALITY,
            "label_set": list(LABELS),
            "model": MODEL,
            "detection_prompt": PROMPT,
        }
        worker._generate_llm_prelabel(message, job, MODEL_ID)
        worker_downscales = list(downscale_calls)

        run_document = {
            "job_id": f"PREVIEW#preview-{uuid.uuid4().hex[:8]}",
            "task_id": "RUN",
            "usecase_id": env.usecase_id,
            "model": MODEL,
            "task_type": MODALITY,
            "label_set": list(LABELS),
            "detection_prompt": PROMPT,
            "few_shot_enabled": True,
            "few_shot_examples": [dict(ref) for ref in stored_refs],
            # Absent for Downscale_Off — the shape
            # _write_preview_run_item records (Req 5.3).
            **({} if bound is DOWNSCALE_OFF
               else {"downscale_max_edge": bound}),
        }
        del downscale_calls[:]
        dda._run_preview_sample(run_document, {}, usecase, DATASET_BUCKET,
                                target_key)
        preview_downscales = list(downscale_calls)
    finally:
        patcher.undo()
        # Leave no stale memo behind for suites that read the settings
        # table through their own SETTINGS_TABLE binding.
        worker._reset_model_token_limits_cache()
        dda._reset_model_token_limits_cache()

    # Exactly one invocation per path: worker first, then preview.
    assert len(stub.calls) == 2, (
        f"expected one Converse call per path, got {len(stub.calls)}")
    worker_call, preview_call = stub.calls

    # Req 8.4: byte-identical example bytes, identical formats, identical
    # ordering — the whole message content, in fact — across the two
    # paths, notwithstanding their different read mechanisms.
    assert worker_call["messages"] == preview_call["messages"], (
        "preview and Auto_Labeler few-shot content differ under "
        f"setting {bound!r}")
    assert worker_call["modelId"] == preview_call["modelId"] == MODEL_ID

    for path, call in (("worker", worker_call), ("preview", preview_call)):
        content = call["messages"][0]["content"]
        # Req 8.1, 8.3, 8.7, 8.8 and the layout: the attached list is the
        # setting-independent good-then-bad prefix, each attached example
        # carries its own downscaled (or byte-identical source) bytes in
        # its key-derived format, identified and ordered as Requirement
        # 8.6 fixes, with the prompt naming the target's Sent_Dimensions.
        assert content == expected_content, (
            f"{path} content differs from the expected selection/layout "
            f"under setting {bound!r} (limit {case.limit}, "
            f"{len(attached)} attached, {len(omitted)} omitted)")

        # Req 10.7 / 8.3: at least the target, at most the limit.
        image_count = len(_image_blocks(call))
        assert 1 <= image_count <= case.limit
        assert image_count == len(attached) + 1

    # Req 8.8, module-independently: decode every attached block actually
    # sent; each fits the bound, never exceeds its source, and lands on
    # the Requirement 6.4 floor formula. (At Downscale_Off the content
    # equality above already pinned source bytes exactly — Req 8.7.)
    if bound is not DOWNSCALE_OFF:
        blocks = _image_blocks(worker_call)
        for ex, block in zip(attached, blocks[:-1]):
            with Image.open(io.BytesIO(block["source"]["bytes"])) as image:
                block_width, block_height = image.size
            assert max(block_width, block_height) <= bound
            assert block_width <= ex.meta.width
            assert block_height <= ex.meta.height
            assert (block_width, block_height) == _expected_scaled(
                ex.meta.width, ex.meta.height, bound)
        with Image.open(io.BytesIO(
                blocks[-1]["source"]["bytes"])) as image:
            assert image.size == (sent_width, sent_height)

    # Selection before downscaling, exactly once per image, never for an
    # omitted example: under a bound each path downscales the target then
    # each attached example in attachment order and nothing else; at
    # Downscale_Off the downscaler is never called at all, so the source
    # bytes were not merely reproduced — they were never re-encoded.
    if bound is DOWNSCALE_OFF:
        assert worker_downscales == []
        assert preview_downscales == []
    else:
        expected_sequence = ([(target_bytes, bound)]
                             + [(ex.body, bound) for ex in attached])
        assert worker_downscales == expected_sequence
        assert preview_downscales == expected_sequence

    # Omitted examples are never read, on either path: the recorded
    # example-prefix reads are exactly the attached keys in attachment
    # order. (The poison seeding backs this up: a wrongly-read omitted
    # example could not have produced the passing requests above.)
    attached_keys = [ex.key for ex in attached]
    worker_example_reads = [key for _bucket, key in worker_reads
                            if key.startswith(EXAMPLES_PREFIX)]
    preview_example_reads = [key for _bucket, key in preview_reads
                             if key.startswith(EXAMPLES_PREFIX)]
    assert worker_example_reads == attached_keys
    assert preview_example_reads == attached_keys
