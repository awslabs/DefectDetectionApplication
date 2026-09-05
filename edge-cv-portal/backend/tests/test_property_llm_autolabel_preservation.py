"""
Preservation property test for the untouched auto-label paths.

Spec: llm-autolabel-prompt-tuning, Task 2.3.

**Feature: llm-autolabel-prompt-tuning, Property 5: Untouched model families
and job creation are unchanged** — *For any* Labeling_Job configuration using
the `sam` model or a `bedrock:` model, the creation validation outcome, the
model request content, and the generated Pre_Label SHALL equal the pre-feature
behavior for that configuration, with no example image blocks and no few-shot
identification content in any request; and *for any* job submission that omits
the Few_Shot_Option, the creation outcome SHALL equal the pre-feature outcome,
with the option persisted as disabled and the job's example images retained
unchanged in their labeler-instruction role.

**Validates: Requirements 10.1, 10.4, 10.5, 10.6**

Baseline: the expectations below are frozen copies of the pre-change code
paths, not calls into the code under test — `_baseline_bedrock_prompt` is the
pre-change `dda_autolabel_worker._build_prompt`, `_baseline_bedrock_content`
is the pre-change Converse content list, and the expected job records are the
pre-feature `auto_label` documents (no `few_shot` key at all). A drift in
either the extraction (task 2.1/2.2) or the persistence (task 5.1) breaks
these assertions.

Note on Requirement 10.5 (the Portal presents no Prompt_Tuning_Preview and no
Few_Shot_Option control for `sam` / `bedrock:` / no model): that is a frontend
statement, asserted by Property 14 in
`PromptTuningPreview.property.test.tsx`. Its backend half — a `sam` /
`bedrock:` submission never carries few-shot state into the record or into a
model request, whatever the submission contains — is covered here.

Runs against the shared moto stack from conftest.py, reusing the harnesses the
example-based suites use: `CreateJobEnv` from test_dda_labeling_create_job.py
for `create_dda_job`, and `AutolabelEnv` from test_dda_autolabel_worker.py for
the SQS worker with a fake Converse client and a fake SAM worker. Per-example
environments are constructed inside the test bodies (rather than taken from
function-scoped fixtures) so every Hypothesis example gets a fresh Use_Case,
team and job.
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dda_llm_request import (
    FEW_SHOT_HEADER,
    FEW_SHOT_TARGET_INTRO,
    MODEL_IMAGE_LIMIT_DEFAULT,
    select_few_shot_examples,
)
from test_dda_autolabel_worker import (
    AutolabelEnv,
    SAM_FUNCTION,
    jpeg_bytes,
    png_bytes,
)
from test_dda_autolabel_worker import DATASET_BUCKET as AUTOLABEL_BUCKET
from test_dda_labeling_create_job import (
    CreateJobEnv,
    FakeCognitoClient,
    FakeLambdaClient,
    LLM_MODEL,
    LLM_PROMPT,
    POOL_ID,
    REGION,
    llm_auto_label,
    messages,
)
from test_dda_labeling_create_job import DATASET_BUCKET as CREATE_BUCKET

SAM_MODEL = "sam"
BEDROCK_MODEL = "bedrock:anthropic.claude-3-haiku"
BEDROCK_MODEL_ID = "anthropic.claude-3-haiku"

# Pre-feature model/modality matrix (dda-data-labeling Req 8.8): the
# creation outcome this feature must not disturb.
BASELINE_MODALITIES = {
    SAM_MODEL: ("Segmentation", "ObjectDetection"),
    BEDROCK_MODEL: ("Classification", "ObjectDetection"),
    LLM_MODEL: ("Classification", "Segmentation", "ObjectDetection"),
}

# Text that may never appear in a request built for an untouched path.
FEW_SHOT_MARKERS = (FEW_SHOT_HEADER, FEW_SHOT_TARGET_INTRO,
                    "Good example", "Bad example", "reference example")

LABEL_POOL = ["scratch", "dent", "crack", "rust"]


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def worker(aws_stack):
    """The real dda_autolabel_worker imported inside the moto mock, with
    the SAM worker function name bound (test_dda_autolabel_worker's
    convention)."""
    os.environ["SAM_WORKER_FUNCTION_NAME"] = SAM_FUNCTION
    sys.modules.pop("dda_autolabel_worker", None)
    import dda_autolabel_worker

    dda_autolabel_worker.SAM_WORKER_FUNCTION_NAME = SAM_FUNCTION

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=AUTOLABEL_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    return dda_autolabel_worker


@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, with
    fake Cognito and Lambda clients, plus the dataset bucket."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

    fake_cognito = FakeCognitoClient()
    dda_labeling.cognito_client = fake_cognito
    dda_labeling.USER_POOL_ID = POOL_ID

    fake_lambda = FakeLambdaClient()
    dda_labeling.lambda_client = fake_lambda

    boto3.client("s3", region_name=REGION).create_bucket(Bucket=CREATE_BUCKET)

    return SimpleNamespace(module=dda_labeling, cognito=fake_cognito,
                           lambda_client=fake_lambda)


class _Patcher:
    """monkeypatch stand-in for AutolabelEnv, usable inside a Hypothesis
    example (function-scoped fixtures cannot be)."""

    def __init__(self):
        self._saved = []

    def setattr(self, target, name, value):
        self._saved.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self):
        for target, name, original in reversed(self._saved):
            setattr(target, name, original)
        self._saved = []


# --------------------------------------------------- frozen pre-change code

def _baseline_bedrock_prompt(modality, label_set, dimensions,
                            per_label_prompts=None):
    """Frozen copy of the pre-change `dda_autolabel_worker._build_prompt`
    (dda-data-labeling Req 8.2, 9.4) — the `bedrock:` prompt this feature
    must leave byte-identical."""
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
    """Frozen copy of the pre-change `bedrock:` Converse content list:
    the target image block followed by the prompt text block, nothing
    else (Req 10.1)."""
    return [
        {"image": {
            "format": "png" if image_key.lower().endswith(".png") else "jpeg",
            "source": {"bytes": image_bytes},
        }},
        {"text": prompt},
    ]


def _baseline_auto_label_record(model, detection_prompt=None):
    """The pre-feature `auto_label` sub-document: no `few_shot` key at all
    for `sam` / `bedrock:` (Req 10.1) and for a submission that omits the
    option (Req 10.4)."""
    record = {"enabled": True, "model": model}
    if detection_prompt is not None:
        record["detection_prompt"] = detection_prompt
    return record


def _stored_few_shot_examples(job):
    """The Few_Shot_Examples the record carries — none when the option is
    absent, which the resolver's compatibility contract reads as
    disabled (Req 10.3, 10.4)."""
    few_shot = (job.get("auto_label") or {}).get("few_shot") or {}
    return few_shot.get("examples") or []


def _assert_no_few_shot_content(request_repr):
    for marker in FEW_SHOT_MARKERS:
        assert marker not in request_repr, (
            f"request carries few-shot content {marker!r}: {request_repr!r}")


# ------------------------------------------------------------- generators

_example_ref = st.builds(
    lambda ext: f"ex/{uuid.uuid4().hex[:8]}{ext}",
    st.sampled_from([".jpg", ".jpeg", ".png", ".PNG"]),
)


@st.composite
def _example_image_sets(draw):
    """A valid good/bad example image submission (at most 10 of each,
    JPEG/PNG refs), or no `example_images` key at all."""
    if draw(st.booleans()):
        return None
    good = draw(st.lists(_example_ref, max_size=3, unique=True))
    bad = draw(st.lists(_example_ref, max_size=3, unique=True))
    return {"good": good, "bad": bad}


@st.composite
def _creation_cases(draw):
    """(model, task_type, label_set, example_images, submitted_few_shot).

    Covers the `sam` and `bedrock:` families across compatible and
    incompatible modalities with the Few_Shot_Option absent, submitted
    enabled, and submitted disabled (Req 10.1), and the `llm:` family
    with the option omitted entirely (Req 10.4).
    """
    model = draw(st.sampled_from([SAM_MODEL, BEDROCK_MODEL, LLM_MODEL]))
    task_type = draw(st.sampled_from(
        ["Classification", "Segmentation", "ObjectDetection"]))
    label_set = (None if task_type == "Classification"
                 else draw(st.lists(st.sampled_from(LABEL_POOL), min_size=1,
                                    max_size=3, unique=True)))
    example_images = draw(_example_image_sets())
    if model == LLM_MODEL:
        # Req 10.4: the pre-feature submission — no Few_Shot_Option at all.
        submitted_few_shot = None
    else:
        submitted_few_shot = draw(st.sampled_from(
            [None, {"enabled": True}, {"enabled": False}, True, False]))
    top_level = draw(st.booleans())
    return model, task_type, label_set, example_images, submitted_few_shot, \
        top_level


@st.composite
def _bedrock_worker_cases(draw):
    """A `bedrock:` labeling-time case: modality, labels, image, per-label
    prompts and a valid model reply with its baseline Pre_Label."""
    modality = draw(st.sampled_from(["Classification", "ObjectDetection"]))
    label_set = draw(st.lists(st.sampled_from(LABEL_POOL), min_size=1,
                              max_size=3, unique=True))
    width = draw(st.integers(min_value=40, max_value=800))
    height = draw(st.integers(min_value=40, max_value=800))
    extension = draw(st.sampled_from([".png", ".jpg", ".jpeg", ".JPG"]))
    skip_verification = draw(st.booleans())
    per_label_prompts = None
    if skip_verification:
        # The marker scan below (_assert_no_few_shot_content) looks for
        # few-shot text in the request repr, so operator-authored guidance
        # is constrained away from those literals: guidance is echoed into
        # the prompt verbatim by design, and text that happens to equal a
        # marker would be indistinguishable from injected few-shot content.
        # Hypothesis harvests string constants from this repo, so without
        # the filter it draws e.g. "Good example" directly.
        per_label_prompts = {
            label: draw(st.text(min_size=1, max_size=20).filter(
                lambda text: text.strip() and not any(
                    marker in text for marker in FEW_SHOT_MARKERS)))
            for label in label_set
        }

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
        extension=extension, skip_verification=skip_verification,
        per_label_prompts=per_label_prompts, reply=reply, prelabel=prelabel)


@st.composite
def _sam_worker_cases(draw):
    """A `sam` labeling-time case: modality plus the SAM worker payload
    and the baseline class-agnostic Pre_Label it produces."""
    modality = draw(st.sampled_from(["Segmentation", "ObjectDetection"]))
    label_set = draw(st.lists(st.sampled_from(LABEL_POOL), min_size=1,
                              max_size=3, unique=True))
    width = draw(st.integers(min_value=40, max_value=800))
    height = draw(st.integers(min_value=40, max_value=800))
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
    return SimpleNamespace(modality=modality, label_set=label_set,
                           width=width, height=height, payload=payload,
                           prelabel=prelabel)


# =========================================================================== #
# Property 5, creation half: the record a submission produces
# =========================================================================== #

@settings(max_examples=100, deadline=None)
@given(case=_creation_cases())
def test_property_creation_outcome_and_record_unchanged(aws_stack, dda, case):
    """Feature: llm-autolabel-prompt-tuning, Property 5: Untouched model
    families and job creation are unchanged — *For any* Labeling_Job
    configuration using the `sam` model or a `bedrock:` model, the creation
    validation outcome SHALL equal the pre-feature behavior for that
    configuration, with no few-shot state in the record; and *for any* job
    submission that omits the Few_Shot_Option, the creation outcome SHALL
    equal the pre-feature outcome, with the option persisted as disabled and
    the job's example images retained unchanged in their labeler-instruction
    role.

    **Validates: Requirements 10.1, 10.4, 10.5, 10.6**
    """
    (model, task_type, label_set, example_images, submitted_few_shot,
     top_level) = case
    env = CreateJobEnv(aws_stack, dda)
    env.put_images(["a.jpg"])

    auto_label = (llm_auto_label() if model == LLM_MODEL
                  else {"enabled": True, "model": model})
    overrides = {}
    if submitted_few_shot is not None:
        if top_level:
            overrides["few_shot"] = submitted_few_shot
        else:
            auto_label["few_shot"] = submitted_few_shot
    if example_images is not None:
        overrides["example_images"] = example_images

    status, body = env.create(task_type=task_type, label_set=label_set,
                              auto_label=auto_label, **overrides)

    # Pre-feature validation outcome: the model/modality matrix decides,
    # and the Few_Shot_Option never changes it (Req 10.1, 10.4).
    if task_type not in BASELINE_MODALITIES[model]:
        assert status == 400, f"{model}/{task_type} must be rejected: {body!r}"
        assert "does not support" in messages(body)
        env.assert_nothing_persisted()
        return

    assert status == 201, f"{model}/{task_type} must be accepted: {body!r}"
    job = env.get_job(body["job_id"])

    # Req 10.1 / 10.4: the pre-feature auto_label document, exactly — no
    # `few_shot` key for `sam` / `bedrock:` whatever the submission
    # carried, and none for an `llm:` submission that omits the option.
    assert job["auto_label"] == _baseline_auto_label_record(
        model, LLM_PROMPT if model == LLM_MODEL else None), (
        f"auto_label drifted from the pre-feature record for {model}: "
        f"{job['auto_label']!r}")

    # Req 10.3 / 10.4: an absent option attaches nothing, so the request
    # built from this record cannot carry Few_Shot_Examples.
    attached, omitted = select_few_shot_examples(
        _stored_few_shot_examples(job), MODEL_IMAGE_LIMIT_DEFAULT)
    assert (attached, omitted) == ([], [])

    # Req 10.6: example_images keeps its labeler-instruction role,
    # persisted exactly as submitted (empty lists when omitted).
    expected_examples = example_images or {"good": [], "bad": []}
    assert job["example_images"] == expected_examples


# =========================================================================== #
# Property 5, labeling half: `bedrock:` requests and Pre_Labels
# =========================================================================== #

@settings(max_examples=100, deadline=None)
@given(case=_bedrock_worker_cases())
def test_property_bedrock_request_and_prelabel_unchanged(aws_stack, worker,
                                                        case):
    """Feature: llm-autolabel-prompt-tuning, Property 5: Untouched model
    families and job creation are unchanged — *For any* Labeling_Job
    configuration using a `bedrock:` model, the model request content and the
    generated Pre_Label SHALL equal the pre-feature behavior for that
    configuration, with no example image blocks and no few-shot
    identification content in any request.

    **Validates: Requirements 10.1, 10.5**
    """
    patcher = _Patcher()
    try:
        env = AutolabelEnv(aws_stack, worker, patcher)
        job_id = env.make_job(
            task_type=case.modality, label_set=case.label_set,
            model=BEDROCK_MODEL, skip_verification=case.skip_verification,
            autolabel_pending=1 if case.skip_verification else None,
            per_label_prompts=case.per_label_prompts)
        key = f"imgs/{uuid.uuid4()}{case.extension}"
        body = (png_bytes(case.width, case.height)
                if case.extension.lower() == ".png"
                else jpeg_bytes(case.width, case.height))
        image_uri = env.put_image(key, body=body)
        task_id = env.make_task(job_id, image_uri)
        bedrock, recorded = env.use_bedrock(replies=[case.reply])
        sam = env.use_sam(payload={})

        result = env.run([env.record(
            job_id, task_id, image_uri, case.modality, case.label_set,
            BEDROCK_MODEL, per_label_prompts=case.per_label_prompts)])

        assert result == {"batchItemFailures": []}

        # Exactly one Converse call, addressed to the same model id, with
        # the pre-feature content list — the target image block followed
        # by the prompt block and nothing else (Req 10.1).
        assert len(bedrock.calls) == 1
        assert sam.invocations == []
        call = bedrock.calls[0]
        assert call["modelId"] == BEDROCK_MODEL_ID
        dimensions = (None if case.modality == "Classification"
                      else (case.width, case.height))
        expected_prompt = _baseline_bedrock_prompt(
            case.modality, case.label_set, dimensions,
            case.per_label_prompts if case.skip_verification else None)
        assert call["messages"] == [{
            "role": "user",
            "content": _baseline_bedrock_content(body, key, expected_prompt),
        }]
        assert recorded["timeout_seconds"] <= 120
        _assert_no_few_shot_content(repr(call))

        # The pre-feature Pre_Label, byte-for-byte (Req 10.1).
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Available", task.get("prelabel_error")
        assert env.prelabel_json(job_id, task_id) == case.prelabel
    finally:
        patcher.undo()


# =========================================================================== #
# Property 5, labeling half: `sam` requests and Pre_Labels
# =========================================================================== #

@settings(max_examples=100, deadline=None)
@given(case=_sam_worker_cases())
def test_property_sam_request_and_prelabel_unchanged(aws_stack, worker, case):
    """Feature: llm-autolabel-prompt-tuning, Property 5: Untouched model
    families and job creation are unchanged — *For any* Labeling_Job
    configuration using the `sam` model, the model request content and the
    generated Pre_Label SHALL equal the pre-feature behavior for that
    configuration, with no example image blocks and no few-shot
    identification content in any request.

    **Validates: Requirements 10.1, 10.5**
    """
    patcher = _Patcher()
    try:
        env = AutolabelEnv(aws_stack, worker, patcher)
        job_id = env.make_job(task_type=case.modality,
                              label_set=case.label_set, model=SAM_MODEL)
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png",
                                  width=case.width, height=case.height)
        task_id = env.make_task(job_id, image_uri)
        bedrock, _ = env.use_bedrock()
        sam = env.use_sam(payload=case.payload)

        result = env.run([env.record(job_id, task_id, image_uri,
                                     case.modality, case.label_set,
                                     SAM_MODEL)])

        assert result == {"batchItemFailures": []}

        # The SAM path never reaches a model request: one synchronous SAM
        # invocation carrying only the presigned image URL, and no
        # Converse call at all (Req 10.1).
        assert bedrock.calls == []
        assert len(sam.invocations) == 1
        invocation = sam.invocations[0]
        assert invocation["FunctionName"] == SAM_FUNCTION
        assert invocation["InvocationType"] == "RequestResponse"
        payload = json.loads(invocation["Payload"])
        assert set(payload) == {"image_s3_presigned_url"}
        _assert_no_few_shot_content(repr(invocation))

        # The pre-feature class-agnostic Pre_Label, byte-for-byte.
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Available", task.get("prelabel_error")
        assert env.prelabel_json(job_id, task_id) == case.prelabel
    finally:
        patcher.undo()
