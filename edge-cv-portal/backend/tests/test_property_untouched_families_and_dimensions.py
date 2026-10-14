"""
Untouched-families and dimension-determination property test.

Spec: llm-model-token-and-image-sizing, task 9.6.

**Feature: llm-model-token-and-image-sizing, Property 12: Untouched model
families and dimension determination are unchanged** — *For any*
Labeling_Job configuration using the `sam` model or a `bedrock:` model, the
creation validation outcome, the model request content, and the generated
Pre_Label SHALL equal the pre-feature behavior, with every image sent at its
Source_Dimensions and the Image_Downscaler invoked for no image; and *for
any* byte string, the Source_Dimensions determination SHALL return the same
result the pre-feature PNG IHDR and JPEG SOF header parsing returned, with
an undeterminable-dimension image treated as Downscale_Off and yielding the
pre-feature prompt content and Pre_Label outcome.

**Validates: Requirements 7.6, 7.10, 10.4**

How the property is asserted
----------------------------
Half one (the untouched families, Req 10.4) drives the **real** code paths —
`create_dda_job` for the creation validation outcome and the
`dda_autolabel_worker` SQS handler for the request content and the
Pre_Label — over `sam` and `bedrock:<id>` configurations on which
`downscale_max_edge` and `token_budget` are deliberately **planted**, both
on the submission's `auto_label` document and on the persisted job record.
The oracles are frozen copies of the pre-feature code, not calls into the
code under test: `_baseline_bedrock_prompt` / `_baseline_bedrock_content`
are the pre-change `bedrock:` request builders (the same pinning
test_property_llm_autolabel_preservation.py uses), and the ambient
Bedrock_Configuration carries a poison `max_tokens` of 424242 — above the
Model_Token_Limit_Ceiling, so no plantable Token_Budget_Selection could ever
reproduce it and `inferenceConfig.maxTokens` equalling the poison value
proves the planted budget was never applied. A spy on the `downscale_image`
binding (both the `dda_llm_image` module attribute and `dda_llm_prelabel`'s
imported binding) records **zero** invocations, and every image block's
bytes are asserted byte-equal to the seeded S3 object — "every image sent at
its Source_Dimensions".

Half two (dimension determination, Req 7.6) compares
`dda_llm_image.declared_dimensions` — and both of its thin delegations,
`dda_autolabel_worker._image_dimensions` and
`dda_labeling._preview_image_dimensions` — for exact equality against
`_pre_feature_image_dimensions`, a **pinned verbatim copy of the
pre-feature `dda_autolabel_worker._image_dimensions`** vendored into this
file (extracted from git history: the last commit before this feature),
over arbitrary byte strings, constructed valid PNG/JPEG headers,
multi-segment JPEGs, truncations and single-byte corruptions.

The undeterminable-dimension path (Req 7.10): for byte strings the pinned
parser rejects, the worker's full `llm:` request path is driven once per
Downscale_Setting (Downscale_Off plus every Max_Image_Edge option planted on
the record) and the task must fail with the pre-feature
`'unsupported image content: could not determine image dimensions for
coordinate guidance'` reason **character-for-character**, with no model
invocation and zero Image_Downscaler calls — the pre-feature Pre_Label
outcome exactly.

Harness reuse (Hypothesis cannot consume function-scoped fixtures): the
module-scoped `worker` / `dda` fixtures follow
test_property_llm_autolabel_preservation.py, per-example environments are
built inside the test bodies from `AutolabelEnv` / `CreateJobEnv`, and
`_Patcher` stands in for monkeypatch.
"""
import json
import struct
import sys
import uuid
from types import SimpleNamespace
from typing import Optional, Tuple

import boto3
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import dda_llm_image
from dda_llm_image import MAX_IMAGE_EDGE_OPTIONS, declared_dimensions
from dda_llm_request import MODEL_TOKEN_LIMIT_CEILING
from test_dda_autolabel_worker import (
    AutolabelEnv,
    DATASET_BUCKET,
    SAM_FUNCTION,
    jpeg_bytes,
    png_bytes,
)
from test_dda_labeling_create_job import (
    CreateJobEnv,
    FakeCognitoClient,
    FakeLambdaClient,
    POOL_ID,
    REGION,
    messages,
)
from test_dda_labeling_create_job import DATASET_BUCKET as CREATE_BUCKET
from test_property_llm_autolabel_preservation import _Patcher

SAM_MODEL = "sam"
BEDROCK_MODEL = "bedrock:anthropic.claude-3-haiku"
BEDROCK_MODEL_ID = "anthropic.claude-3-haiku"
LLM_MODEL_ID = "us.amazon.nova-pro-v1:0"
LLM_MODEL = f"llm:{LLM_MODEL_ID}"

# Pre-feature model/modality creation matrix (dda-data-labeling Req 8.8):
# the validation outcome planted sizing values must not disturb (Req 10.4).
BASELINE_MODALITIES = {
    SAM_MODEL: ("Segmentation", "ObjectDetection"),
    BEDROCK_MODEL: ("Classification", "ObjectDetection"),
}

LABEL_POOL = ["scratch", "dent", "crack", "rust"]

# The pre-feature reason for an `llm:` target whose Source_Dimensions
# cannot be determined, pinned character-for-character (Req 7.10). This
# literal must NOT be imported from the code under test.
PINNED_UNDETERMINED_REASON = (
    "unsupported image content: could not determine image dimensions "
    "for coordinate guidance")

# Deterministic ambient Bedrock_Configuration for the `bedrock:` half.
# `max_tokens` is a poison value above Model_Token_Limit_Ceiling (128000):
# every plantable Token_Budget_Selection is at most the ceiling, so a
# request whose maxTokens equals the poison value provably derived it from
# the Global_Max_Tokens and not from the planted budget (Req 10.4).
POISON_GLOBAL_MAX_TOKENS = 424242
BEDROCK_CONFIG = {
    "model_id": BEDROCK_MODEL_ID,
    "region": "us-west-2",
    "max_tokens": POISON_GLOBAL_MAX_TOKENS,
    "temperature": None,
    "top_p": None,
    "timeout_seconds": 60,
}


# --------------------------------------------------- frozen pre-change code

def _pre_feature_image_dimensions(
        image_bytes: bytes) -> Optional[Tuple[int, int]]:
    """Pinned verbatim copy of the pre-feature
    `dda_autolabel_worker._image_dimensions` (the algorithm in place before
    llm-model-token-and-image-sizing relocated it to
    `dda_llm_image.declared_dimensions`), vendored here as the oracle for
    Requirement 7.6. The body below must never be edited to match a changed
    implementation — it IS the pre-feature behavior.
    """
    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n' and len(image_bytes) >= 24:
        width, height = struct.unpack('>II', image_bytes[16:24])
        return (width, height) if width and height else None
    if image_bytes[:2] == b'\xff\xd8':
        index = 2
        while index + 9 <= len(image_bytes):
            if image_bytes[index] != 0xFF:
                index += 1
                continue
            marker = image_bytes[index + 1]
            # Padding / standalone markers carry no length segment.
            if marker in (0xFF, 0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            if index + 4 > len(image_bytes):
                break
            segment_length = struct.unpack(
                '>H', image_bytes[index + 2:index + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if index + 9 <= len(image_bytes):
                    height, width = struct.unpack(
                        '>HH', image_bytes[index + 5:index + 9])
                    return (width, height) if width and height else None
                break
            index += 2 + segment_length
    return None


def _baseline_bedrock_prompt(modality, label_set, dimensions,
                             per_label_prompts=None):
    """Frozen copy of the pre-feature `dda_autolabel_worker._build_prompt`
    (dda-data-labeling Req 8.2, 9.4) — the `bedrock:` prompt this feature
    must leave byte-identical, with the ObjectDetection dimensions being
    the Source_Dimensions."""
    labels = ", ".join(label_set)
    if modality == "Classification":
        lines = [
            "You are labeling images for a defect-detection dataset.",
            "Decide which single label applies to the image.",
            f"Allowed labels: {labels}.",
        ]
    else:
        width, height = dimensions
        lines = [
            "You are labeling images for a defect-detection dataset.",
            "Locate every object belonging to the allowed classes and "
            "report its bounding box in pixel coordinates.",
            f"Allowed class names: {labels}.",
            f"The image is {width} pixels wide and {height} pixels tall; "
            "every box must lie entirely within these bounds.",
        ]
    if per_label_prompts:
        for label in label_set:
            prompt = per_label_prompts.get(label)
            if prompt:
                lines.append(f"Guidance for label '{label}': {prompt}")
    if modality == "Classification":
        lines.append(
            "Respond with ONLY a JSON object of the form "
            '{"label": "<one of the allowed labels>"} and no other text.')
    else:
        lines.append(
            "Respond with ONLY a JSON object of the form "
            '{"boxes": [{"class": "<allowed class>", "left": <px>, '
            '"top": <px>, "width": <px>, "height": <px>}]} and no other '
            'text. Use {"boxes": []} when no object is present.')
    return "\n".join(lines)


def _baseline_bedrock_content(image_bytes, image_key, prompt):
    """Frozen copy of the pre-feature `bedrock:` Converse content list:
    the source image block — bytes byte-for-byte, format derived from the
    object key — followed by the prompt text block, nothing else
    (Req 10.4)."""
    return [
        {"image": {
            "format": "png" if image_key.lower().endswith(".png") else "jpeg",
            "source": {"bytes": image_bytes},
        }},
        {"text": prompt},
    ]


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def worker(aws_stack):
    """The real dda_autolabel_worker imported inside the moto mock."""
    import os
    os.environ["SAM_WORKER_FUNCTION_NAME"] = SAM_FUNCTION
    sys.modules.pop("dda_autolabel_worker", None)
    import dda_autolabel_worker

    dda_autolabel_worker.SAM_WORKER_FUNCTION_NAME = SAM_FUNCTION

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=DATASET_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    return dda_autolabel_worker


@pytest.fixture(scope="module")
def prelabel(aws_stack):
    """The shared invocation module whose `downscale_image` binding the
    spy replaces (alongside the `dda_llm_image` module attribute)."""
    import dda_llm_prelabel
    return dda_llm_prelabel


@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, with
    fake Cognito and Lambda clients, plus the dataset bucket — the
    test_dda_labeling_create_job convention."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

    fake_cognito = FakeCognitoClient()
    dda_labeling.cognito_client = fake_cognito
    dda_labeling.USER_POOL_ID = POOL_ID

    fake_lambda = FakeLambdaClient()
    dda_labeling.lambda_client = fake_lambda

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=CREATE_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass

    return SimpleNamespace(module=dda_labeling, cognito=fake_cognito,
                           lambda_client=fake_lambda)


def _spy_on_downscaler(patcher, prelabel):
    """A spy over the Image_Downscaler seam: both the `dda_llm_image`
    module attribute and the chokepoint's imported binding, so any
    invocation is recorded whichever spelling reaches it (Req 10.4:
    the Image_Downscaler is invoked for no image)."""
    downscale_calls = []
    real_downscale = prelabel.downscale_image

    def spying_downscale(*args, **kwargs):
        downscale_calls.append((args, kwargs))
        return real_downscale(*args, **kwargs)

    patcher.setattr(prelabel, "downscale_image", spying_downscale)
    patcher.setattr(dda_llm_image, "downscale_image", spying_downscale)
    return downscale_calls


def _plant_sizing(env, job_id, model, planted_downscale, planted_budget):
    """Rewrite the persisted job's auto_label document with both sizing
    values deliberately planted beside the pre-feature keys — the record
    shape an untouched family must never read (Req 10.4)."""
    env.stack.tables.labeling_jobs.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET auto_label = :al",
        ExpressionAttributeValues={":al": {
            "enabled": True,
            "model": model,
            "downscale_max_edge": planted_downscale,
            "token_budget": planted_budget,
        }})


def _assert_no_sizing_message(body):
    """No validation message may mention either sizing value (the
    pre-feature validation outcome, Req 10.4)."""
    joined = messages(body).lower()
    assert "downscale" not in joined, (
        f"validation mentioned the Downscale_Setting: {joined!r}")
    assert "token" not in joined, (
        f"validation mentioned the Token_Budget_Selection: {joined!r}")


# -------------------------------------------------------------- generators

# Planted Downscale_Setting values: every valid Max_Image_Edge option (the
# discriminating case — a value that would resize if wrongly honored) plus
# malformed spellings. DynamoDB-safe (no floats).
_planted_downscale = st.one_of(
    st.sampled_from(MAX_IMAGE_EDGE_OPTIONS),
    st.sampled_from([True, False, "1024", "off", 100, -512]),
)

# Planted Token_Budget_Selection values: valid in-range integers (which
# would replace maxTokens if wrongly honored) plus malformed spellings.
_planted_budget = st.one_of(
    st.integers(min_value=1, max_value=MODEL_TOKEN_LIMIT_CEILING),
    st.sampled_from([True, False, "20000", 0,
                     MODEL_TOKEN_LIMIT_CEILING + 1]),
)


@st.composite
def _creation_cases(draw):
    """A `sam` / `bedrock:` job submission across all three modalities and
    the Label_Set shapes, with both sizing values planted on the submitted
    auto_label document (and, half the time, at the body top level too)."""
    model = draw(st.sampled_from([SAM_MODEL, BEDROCK_MODEL]))
    task_type = draw(st.sampled_from(
        ["Classification", "Segmentation", "ObjectDetection"]))
    label_set = (None if task_type == "Classification"
                 else draw(st.lists(st.sampled_from(LABEL_POOL), min_size=1,
                                    max_size=3, unique=True)))
    return SimpleNamespace(
        model=model,
        task_type=task_type,
        label_set=label_set,
        planted_downscale=draw(_planted_downscale),
        planted_budget=draw(_planted_budget),
        plant_top_level=draw(st.booleans()),
    )


@st.composite
def _bedrock_worker_cases(draw):
    """A `bedrock:` labeling-time case with planted sizing values:
    modality, labels, Source_Dimensions (extending above every
    Max_Image_Edge option, so a wrongly-honored bound could not pass
    unnoticed), a valid model reply and its pre-feature Pre_Label."""
    modality = draw(st.sampled_from(["Classification", "ObjectDetection"]))
    label_set = draw(st.lists(st.sampled_from(LABEL_POOL), min_size=1,
                              max_size=3, unique=True))
    width = draw(st.one_of(st.integers(min_value=40, max_value=500),
                           st.integers(min_value=501, max_value=2600)))
    height = draw(st.one_of(st.integers(min_value=40, max_value=500),
                            st.integers(min_value=501, max_value=2600)))
    extension = draw(st.sampled_from([".png", ".jpg", ".jpeg", ".JPG"]))

    if modality == "Classification":
        label = draw(st.sampled_from(label_set))
        reply = json.dumps({"label": label})
        prelabel = {"modality": "Classification", "label": label}
    else:
        boxes = []
        for _ in range(draw(st.integers(min_value=0, max_value=2))):
            box_width = draw(st.integers(min_value=1, max_value=width // 2))
            box_height = draw(st.integers(min_value=1, max_value=height // 2))
            boxes.append({
                "class": draw(st.sampled_from(label_set)),
                "left": draw(st.integers(min_value=0,
                                         max_value=width - box_width)),
                "top": draw(st.integers(min_value=0,
                                        max_value=height - box_height)),
                "width": box_width,
                "height": box_height,
            })
        reply = json.dumps({"boxes": boxes})
        prelabel = {
            "modality": "ObjectDetection",
            "boxes": [{"class": box["class"],
                       "left": float(box["left"]), "top": float(box["top"]),
                       "width": float(box["width"]),
                       "height": float(box["height"])}
                      for box in boxes],
            "image_width": width,
            "image_height": height,
        }
    return SimpleNamespace(
        modality=modality, label_set=label_set, width=width, height=height,
        extension=extension, reply=reply, prelabel=prelabel,
        planted_downscale=draw(_planted_downscale),
        planted_budget=draw(_planted_budget))


@st.composite
def _sam_worker_cases(draw):
    """A `sam` labeling-time case with planted sizing values: modality
    plus the SAM worker payload and the pre-feature class-agnostic
    Pre_Label it produces."""
    modality = draw(st.sampled_from(["Segmentation", "ObjectDetection"]))
    label_set = draw(st.lists(st.sampled_from(LABEL_POOL), min_size=1,
                              max_size=3, unique=True))
    width = draw(st.integers(min_value=40, max_value=2600))
    height = draw(st.integers(min_value=40, max_value=2600))
    regions = []
    expected = []
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        rle = " ".join(str(draw(st.integers(min_value=0, max_value=99)))
                       for _ in range(4))
        # SAM is class-agnostic: whatever class the worker reports is
        # discarded (dda-data-labeling Req 8.2).
        region = {"class": draw(st.sampled_from([None, "spurious"])),
                  "rle": rle}
        expected_region = {"class": None, "rle": rle}
        if draw(st.booleans()):
            region["score"] = 1
            expected_region["score"] = 1
        regions.append(region)
        expected.append(expected_region)
    payload = {"regions": regions, "image_width": width,
               "image_height": height}
    prelabel = {"modality": modality, "regions": expected,
                "image_width": width, "image_height": height}
    return SimpleNamespace(
        modality=modality, label_set=label_set, width=width, height=height,
        payload=payload, prelabel=prelabel,
        planted_downscale=draw(_planted_downscale),
        planted_budget=draw(_planted_budget))


# JPEG structural pieces exercising the segment walk of both parser copies:
# a length-carrying application segment, a DHT (a marker inside the SOF
# range that is NOT an SOF), a comment, standalone/restart markers, fill
# bytes, and a stray non-marker byte.
_jpeg_segments = st.sampled_from([
    b"\xff\xe0" + struct.pack(">H", 6) + b"JFIF",
    b"\xff\xc4" + struct.pack(">H", 4) + b"\x00\x01",
    b"\xff\xfe" + struct.pack(">H", 5) + b"abc",
    b"\xff\xd3",
    b"\xff\x01",
    b"\xff\xff",
    b"\x00",
])


@st.composite
def _structured_image_bytes(draw):
    """Constructed valid, truncated and corrupt PNG/JPEG headers,
    including zero-dimension declarations and multi-segment JPEGs."""
    kind = draw(st.sampled_from(["png", "jpeg", "jpeg_segments"]))
    if kind == "png":
        width = draw(st.integers(min_value=0, max_value=2 ** 31 - 1))
        height = draw(st.integers(min_value=0, max_value=2 ** 31 - 1))
        data = png_bytes(width, height)
    elif kind == "jpeg":
        width = draw(st.integers(min_value=0, max_value=65535))
        height = draw(st.integers(min_value=0, max_value=65535))
        data = jpeg_bytes(width, height)
    else:
        width = draw(st.integers(min_value=0, max_value=65535))
        height = draw(st.integers(min_value=0, max_value=65535))
        segments = b"".join(draw(st.lists(_jpeg_segments, max_size=4)))
        data = (b"\xff\xd8" + segments
                + b"\xff\xc0" + struct.pack(">H", 11) + b"\x08"
                + struct.pack(">HH", height, width))
    mutation = draw(st.sampled_from(["none", "truncate", "corrupt"]))
    if mutation == "truncate" and data:
        data = data[:draw(st.integers(min_value=0, max_value=len(data)))]
    elif mutation == "corrupt" and data:
        position = draw(st.integers(min_value=0, max_value=len(data) - 1))
        replacement = draw(st.integers(min_value=0, max_value=255))
        data = (data[:position] + bytes([replacement])
                + data[position + 1:])
    return data


# Arbitrary byte strings plus the constructed corpus (Req 7.6).
_dimension_corpus = st.one_of(st.binary(max_size=512),
                              _structured_image_bytes())

# Byte strings the pre-feature parser rejects (Req 7.10): explicit
# unparseable shapes plus arbitrary bytes, filtered through the pinned
# copy so the corpus is undeterminable by construction.
_unparseable_bytes = st.one_of(
    st.sampled_from([
        b"",
        b"not an image at all",
        b"\xff\xd8",                            # JPEG SOI, no SOF
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0d",   # PNG signature, truncated
        png_bytes(0, 100),                      # zero width declared
        jpeg_bytes(0, 0),                       # zero dimensions declared
    ]),
    st.binary(max_size=64),
).filter(lambda data: _pre_feature_image_dimensions(data) is None)


@st.composite
def _undeterminable_cases(draw):
    """An `llm:` job whose target image's Source_Dimensions cannot be
    determined, with a valid token budget planted so the failure can only
    be attributed to the image."""
    return SimpleNamespace(
        modality=draw(st.sampled_from(
            ["Classification", "Segmentation", "ObjectDetection"])),
        label_set=draw(st.lists(st.sampled_from(LABEL_POOL), min_size=1,
                                max_size=3, unique=True)),
        image_body=draw(_unparseable_bytes),
        extension=draw(st.sampled_from([".png", ".jpg", ".jpeg"])),
        planted_budget=draw(st.integers(
            min_value=1, max_value=MODEL_TOKEN_LIMIT_CEILING)),
    )


# =========================================================================== #
# Property 12, creation half: `sam` / `bedrock:` validation with planted
# sizing values
# =========================================================================== #

@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(case=_creation_cases())
def test_property_creation_validation_unchanged_with_planted_sizing(
        aws_stack, dda, case):
    """Feature: llm-model-token-and-image-sizing, Property 12: Untouched
    model families and dimension determination are unchanged — creation
    half: *for any* `sam` or `bedrock:` submission with both sizing values
    deliberately planted, the creation validation outcome SHALL equal the
    pre-feature model/modality matrix with no message referring to either
    value, and an accepted record's auto_label document SHALL carry neither
    sizing key.

    **Validates: Requirements 10.4**
    """
    env = CreateJobEnv(aws_stack, dda)
    env.put_images(["a.jpg"])

    auto_label = {
        "enabled": True,
        "model": case.model,
        "downscale_max_edge": case.planted_downscale,
        "token_budget": case.planted_budget,
    }
    overrides = {}
    if case.plant_top_level:
        overrides["downscale_max_edge"] = case.planted_downscale
        overrides["token_budget"] = case.planted_budget

    status, body = env.create(task_type=case.task_type,
                              label_set=case.label_set,
                              auto_label=auto_label, **overrides)

    if case.task_type not in BASELINE_MODALITIES[case.model]:
        # The pre-feature rejection, for the pre-feature reason alone.
        assert status == 400, (
            f"{case.model}/{case.task_type} must be rejected: {body!r}")
        assert "does not support" in messages(body)
        _assert_no_sizing_message(body)
        env.assert_nothing_persisted()
        return

    assert status == 201, (
        f"{case.model}/{case.task_type} must be accepted whatever sizing "
        f"values the submission plants: {body!r}")

    # The pre-feature auto_label record, exactly: neither sizing key, no
    # few_shot, no detection_prompt (Req 10.4).
    job = env.get_job(body["job_id"])
    assert job["auto_label"] == {"enabled": True, "model": case.model}, (
        f"auto_label drifted from the pre-feature record for {case.model}: "
        f"{job['auto_label']!r}")


# =========================================================================== #
# Property 12, labeling half: `bedrock:` requests and Pre_Labels with
# planted sizing values
# =========================================================================== #

@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(case=_bedrock_worker_cases())
def test_property_bedrock_request_and_prelabel_unchanged_with_planted_sizing(
        aws_stack, worker, prelabel, case):
    """Feature: llm-model-token-and-image-sizing, Property 12: Untouched
    model families and dimension determination are unchanged — `bedrock:`
    labeling half: *for any* `bedrock:` job record with both sizing values
    deliberately planted, the Converse request SHALL equal the pinned
    pre-feature request (image bytes byte-equal to the seeded object at
    Source_Dimensions, maxTokens from the Global_Max_Tokens and never the
    planted budget), the Pre_Label SHALL equal the pre-feature Pre_Label,
    and the Image_Downscaler SHALL be invoked for no image.

    **Validates: Requirements 10.4**
    """
    patcher = _Patcher()
    try:
        env = AutolabelEnv(aws_stack, worker, patcher)
        downscale_calls = _spy_on_downscaler(patcher, prelabel)
        # The ambient global configuration with the poison max_tokens.
        patcher.setattr(worker, "get_bedrock_configuration",
                        lambda: dict(BEDROCK_CONFIG))

        job_id = env.make_job(task_type=case.modality,
                              label_set=case.label_set, model=BEDROCK_MODEL)
        _plant_sizing(env, job_id, BEDROCK_MODEL, case.planted_downscale,
                      case.planted_budget)

        key = f"imgs/{uuid.uuid4()}{case.extension}"
        body = (png_bytes(case.width, case.height)
                if case.extension.lower() == ".png"
                else jpeg_bytes(case.width, case.height))
        image_uri = env.put_image(key, body=body)
        task_id = env.make_task(job_id, image_uri)
        bedrock, recorded = env.use_bedrock(replies=[case.reply])
        sam = env.use_sam(payload={})

        result = env.run([env.record(job_id, task_id, image_uri,
                                     case.modality, case.label_set,
                                     BEDROCK_MODEL)])

        assert result == {"batchItemFailures": []}

        # Exactly one Converse call carrying the pinned pre-feature
        # request: the seeded bytes byte-for-byte at Source_Dimensions,
        # then the pre-feature prompt (Req 10.4).
        assert len(bedrock.calls) == 1
        assert sam.invocations == []
        call = bedrock.calls[0]
        assert call["modelId"] == BEDROCK_MODEL_ID
        dimensions = (None if case.modality == "Classification"
                      else (case.width, case.height))
        expected_prompt = _baseline_bedrock_prompt(
            case.modality, case.label_set, dimensions)
        assert call["messages"] == [{
            "role": "user",
            "content": _baseline_bedrock_content(body, key, expected_prompt),
        }], "request content drifted from the pre-feature baseline"

        # maxTokens is the Global_Max_Tokens — the poison value no planted
        # Token_Budget_Selection could produce — and the client timeout is
        # the global configuration's, unchanged (Req 10.4).
        assert call["inferenceConfig"] == {
            "maxTokens": POISON_GLOBAL_MAX_TOKENS}
        assert recorded["region"] == BEDROCK_CONFIG["region"]
        assert recorded["timeout_seconds"] == BEDROCK_CONFIG[
            "timeout_seconds"]

        # Zero Image_Downscaler invocations for any image (Req 10.4).
        assert downscale_calls == [], (
            f"the Image_Downscaler must never run for a bedrock: job: "
            f"{downscale_calls!r}")

        # The pre-feature Pre_Label, byte-for-byte.
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Available", (
            f"planted sizing values must never fail a bedrock: job "
            f"(downscale={case.planted_downscale!r}, "
            f"budget={case.planted_budget!r}): "
            f"{task.get('prelabel_error')!r}")
        assert env.prelabel_json(job_id, task_id) == case.prelabel
    finally:
        patcher.undo()


# =========================================================================== #
# Property 12, labeling half: `sam` requests and Pre_Labels with planted
# sizing values
# =========================================================================== #

@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(case=_sam_worker_cases())
def test_property_sam_request_and_prelabel_unchanged_with_planted_sizing(
        aws_stack, worker, prelabel, case):
    """Feature: llm-model-token-and-image-sizing, Property 12: Untouched
    model families and dimension determination are unchanged — `sam`
    labeling half: *for any* `sam` job record with both sizing values
    deliberately planted, the SAM invocation SHALL equal the pre-feature
    invocation (one synchronous invoke carrying only the presigned image
    URL, no Converse call), the Pre_Label SHALL equal the pre-feature
    class-agnostic Pre_Label, and the Image_Downscaler SHALL be invoked
    for no image.

    **Validates: Requirements 10.4**
    """
    patcher = _Patcher()
    try:
        env = AutolabelEnv(aws_stack, worker, patcher)
        downscale_calls = _spy_on_downscaler(patcher, prelabel)

        job_id = env.make_job(task_type=case.modality,
                              label_set=case.label_set, model=SAM_MODEL)
        _plant_sizing(env, job_id, SAM_MODEL, case.planted_downscale,
                      case.planted_budget)

        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png",
                                  width=case.width, height=case.height)
        task_id = env.make_task(job_id, image_uri)
        bedrock, _ = env.use_bedrock()
        sam = env.use_sam(payload=case.payload)

        result = env.run([env.record(job_id, task_id, image_uri,
                                     case.modality, case.label_set,
                                     SAM_MODEL)])

        assert result == {"batchItemFailures": []}

        # The pre-feature SAM invocation: exactly one synchronous invoke
        # carrying only the presigned image URL, and no Converse call at
        # all (Req 10.4).
        assert bedrock.calls == []
        assert len(sam.invocations) == 1
        invocation = sam.invocations[0]
        assert invocation["FunctionName"] == SAM_FUNCTION
        assert invocation["InvocationType"] == "RequestResponse"
        payload = json.loads(invocation["Payload"])
        assert set(payload) == {"image_s3_presigned_url"}

        # Zero Image_Downscaler invocations for any image (Req 10.4).
        assert downscale_calls == [], (
            f"the Image_Downscaler must never run for a sam job: "
            f"{downscale_calls!r}")

        # The pre-feature class-agnostic Pre_Label, byte-for-byte.
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Available", (
            f"planted sizing values must never fail a sam job "
            f"(downscale={case.planted_downscale!r}, "
            f"budget={case.planted_budget!r}): "
            f"{task.get('prelabel_error')!r}")
        assert env.prelabel_json(job_id, task_id) == case.prelabel
    finally:
        patcher.undo()


# =========================================================================== #
# Property 12, dimension half: the parser against its pre-feature copy
# =========================================================================== #

@settings(max_examples=100, deadline=None)
@given(data=_dimension_corpus)
def test_property_dimension_determination_matches_pre_feature_parser(
        aws_stack, worker, dda, data):
    """Feature: llm-model-token-and-image-sizing, Property 12: Untouched
    model families and dimension determination are unchanged — dimension
    half: *for any* byte string, `dda_llm_image.declared_dimensions` and
    both thin delegations (`dda_autolabel_worker._image_dimensions`,
    `dda_labeling._preview_image_dimensions`) SHALL return exactly the
    result the pinned verbatim copy of the pre-feature `_image_dimensions`
    returns.

    **Validates: Requirements 7.6**
    """
    expected = _pre_feature_image_dimensions(data)

    assert declared_dimensions(data) == expected, (
        f"declared_dimensions diverged from the pre-feature parser for "
        f"{data!r}")
    assert worker._image_dimensions(data) == expected, (
        f"dda_autolabel_worker._image_dimensions diverged from the "
        f"pre-feature parser for {data!r}")
    assert dda.module._preview_image_dimensions(data) == expected, (
        f"dda_labeling._preview_image_dimensions diverged from the "
        f"pre-feature parser for {data!r}")


# =========================================================================== #
# Property 12, undeterminable half: the pre-feature reason for every
# Downscale_Setting
# =========================================================================== #

@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(case=_undeterminable_cases())
def test_property_undeterminable_dimensions_keep_pre_feature_outcome(
        aws_stack, worker, prelabel, dda, case):
    """Feature: llm-model-token-and-image-sizing, Property 12: Untouched
    model families and dimension determination are unchanged —
    undeterminable half: *for any* byte string the pre-feature parser
    rejects and *every* Downscale_Setting planted on the record
    (Downscale_Off and each Max_Image_Edge option), the `llm:` request path
    SHALL fail the image with the pre-feature reason character-for-character,
    SHALL invoke no model, SHALL write no Pre_Label, and SHALL invoke the
    Image_Downscaler for no image.

    **Validates: Requirements 7.10**
    """
    # The preview path pins the identical pre-feature reason (Req 7.10's
    # cross-path half): the constant must never drift from the literal.
    assert (dda.module.PREVIEW_UNSUPPORTED_IMAGE_REASON
            == PINNED_UNDETERMINED_REASON)

    patcher = _Patcher()
    try:
        env = AutolabelEnv(aws_stack, worker, patcher)
        downscale_calls = _spy_on_downscaler(patcher, prelabel)
        bedrock, _ = env.use_bedrock()

        key = f"imgs/{uuid.uuid4()}{case.extension}"
        image_uri = env.put_image(key, body=case.image_body)

        for setting in (None,) + MAX_IMAGE_EDGE_OPTIONS:
            job_id = env.make_job(task_type=case.modality,
                                  label_set=case.label_set, model=LLM_MODEL)
            _plant_sizing(env, job_id, LLM_MODEL, setting,
                          case.planted_budget)
            task_id = env.make_task(job_id, image_uri)

            result = env.run([env.record(
                job_id, task_id, image_uri, case.modality, case.label_set,
                LLM_MODEL, detection_prompt="Find every visible defect")])

            # The generation failure is absorbed per record, exactly as
            # before the feature.
            assert result == {"batchItemFailures": []}

            # The pre-feature outcome: Failed with the pre-feature reason
            # character-for-character, and no Pre_Label object (Req 7.10).
            task = env.get_task(job_id, task_id)
            assert task["prelabel_status"] == "Failed", (
                f"an undeterminable-dimension image must fail "
                f"(setting={setting!r}): {task!r}")
            assert task["prelabel_error"] == PINNED_UNDETERMINED_REASON, (
                f"the failure reason drifted from the pre-feature literal "
                f"for setting {setting!r}: {task['prelabel_error']!r}")
            assert not env.prelabel_exists(job_id, task_id)

        # No model was ever invoked and the Image_Downscaler never ran,
        # whatever the planted Downscale_Setting (Req 7.10).
        assert bedrock.calls == []
        assert downscale_calls == [], (
            f"the Image_Downscaler must never run for an image whose "
            f"dimensions cannot be determined: {downscale_calls!r}")
    finally:
        patcher.undo()
