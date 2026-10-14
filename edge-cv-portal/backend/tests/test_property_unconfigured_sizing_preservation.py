"""
Unconfigured-sizing preservation property test.

Spec: llm-model-token-and-image-sizing, task 9.3.

**Feature: llm-model-token-and-image-sizing, Property 8: An unconfigured
Downscale_Setting reproduces the pre-feature request** — *For any* `llm:`
job configuration in which the Downscale_Setting is Downscale_Off, absent,
null, or malformed in the job record, and the Few_Shot_Option is disabled,
absent, null, or malformed, the model request content SHALL be exactly the
source image block followed by the text block built from the
Detection_Prompt character-for-character, the Label_Set and the
Source_Dimensions, with no example image blocks and no example
identification content, an omitted Token_Budget_Selection SHALL resolve
through the Model_Token_Limits and the default of 10000, and no failure
SHALL be attributable to the Downscale_Setting or the Token_Budget_Selection
being absent or malformed.

**Validates: Requirements 3.8, 3.10, 5.9, 5.12, 10.1, 10.6, 10.10**

How the property is asserted
----------------------------
The Property 8 statement quantifies over values "in the job record", so the
labeling-time half drives the **real worker SQS handler**
(`dda_autolabel_worker.handler`) against a job record persisted through
moto DynamoDB — numbers arrive back as Decimal exactly as in production —
with one stub Converse client capturing the request. The captured request
is compared against a **pinned pre-feature baseline**: `_baseline_llm_prompt`
and `_baseline_llm_content` below are frozen copies of the
llm-autolabel-prompt-tuning content construction (the last feature to touch
this request), not calls into the code under test, so any drift in the
shared layer breaks the differential.

Three independent tripwires prove the Downscale_Off pass-through:

- a spy on the `downscale_image` binding (both the `dda_llm_image` module
  attribute and `dda_llm_prelabel`'s imported binding) records **zero**
  invocations — Req 10.1's "SHALL invoke the Image_Downscaler for no image
  of that request", which subsumes zero re-encodes;
- the target bytes are **header-only** PNG/JPEG (undecodable beyond the
  IHDR/SOF header), so any wrongly-applied bound that decoded them would
  refuse the image instead of reaching `Available`;
- source dimensions are drawn up to 2600 px, above every Max_Image_Edge
  option, so a malformed value wrongly honored as a bound could not pass
  through unnoticed.

The `maxTokens` oracle is the design's: it must equal
`resolve_token_budget(model, None, mapping)` — the omitted-selection
resolution through the Model_Token_Limits and then the default of 10000 —
and the ambient Bedrock_Configuration carries a poison `max_tokens` far
above the ceiling, so the Global_Max_Tokens can never satisfy the equality.

The job-creation half asserts Requirement 10.6: a submission omitting both
sizing keys is accepted under the pre-feature validation rules and produces
an `auto_label` record byte-identical to the pre-feature record — neither
`downscale_max_edge` nor `token_budget` present.

Harness reuse (Hypothesis cannot consume function-scoped fixtures): the
module-scoped `worker` / `dda` fixtures follow
test_property_llm_autolabel_preservation.py, per-example environments are
built inside the test bodies from `AutolabelEnv` / `CreateJobEnv`, and
`_Patcher` stands in for monkeypatch. The malformed `few_shot` documents
come from the predecessor's generator
(test_property_few_shot_selection._disabled_few_shot_docs), converted to
their DynamoDB-persisted spelling (floats become Decimal).
"""
import json
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import boto3
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import dda_llm_image
from dda_llm_image import MAX_IMAGE_EDGE_OPTIONS
from dda_llm_request import (
    MODEL_TOKEN_LIMIT_CEILING,
    MODEL_TOKEN_LIMIT_DEFAULT,
    resolve_token_budget,
)
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
)
from test_dda_labeling_create_job import DATASET_BUCKET as CREATE_BUCKET
from test_property_llm_autolabel_preservation import _Patcher

MODEL_ID = "us.amazon.nova-pro-v1:0"
MODEL = f"llm:{MODEL_ID}"

MODALITIES = ("Classification", "Segmentation", "ObjectDetection")
# Classification's Label_Set is the fixed binary set (dda-data-labeling
# Req 4.3); the geometry modalities carry job-defined class names.
CLASSIFICATION_LABELS = ["normal", "anomaly"]
LABEL_POOL = ["scratch", "dent", "crack", "rust"]

# Deterministic ambient Bedrock_Configuration. `max_tokens` is a poison
# value above MODEL_TOKEN_LIMIT_CEILING: the resolved budget is always
# within [1, 128000], so a request whose maxTokens came from the
# Global_Max_Tokens could never satisfy the resolver equality below.
POISON_GLOBAL_MAX_TOKENS = 424242
BEDROCK_CONFIG = {
    "model_id": MODEL_ID,
    "region": "us-west-2",
    "max_tokens": POISON_GLOBAL_MAX_TOKENS,
    "temperature": None,
    "top_p": None,
    "timeout_seconds": 60,
}

# A Detection_Prompt that must survive character-for-character: leading and
# trailing whitespace, quotes, braces, a newline and a non-ASCII character.
AWKWARD_PROMPT = '  Find every "scratch" {and dent}\n  on the panel — ✓  '

# "This key is not present in the document at all."
_ABSENT = object()


# --------------------------------------------------- frozen pre-change code

def _baseline_llm_prompt(label_set, detection_prompt, width, height):
    """Frozen copy of the pre-feature prompt construction (the shared
    layer's `build_detection_prompt` with no per-label prompts, as
    established by llm-autolabel-prompt-tuning) — the text block this
    feature must reproduce for an unconfigured Downscale_Setting, built
    from the Detection_Prompt character-for-character, the Label_Set and
    the **Source_Dimensions**."""
    return "\n".join([
        "You are labeling images for a defect-detection dataset.",
        "Locate every object matching the detection request below and "
        "report its location in pixel coordinates.",
        f"The image is {width} pixels wide and {height} pixels tall; "
        f"every coordinate must lie within these bounds.",
        f"Allowed class names: {', '.join(label_set)}.",
        "",
        "Detection request:",
        detection_prompt,
        "",
        'Respond with ONLY a JSON object of the form '
        '{"detections": [{"class": ..., "box": '
        '{"left": ..., "top": ..., "width": ..., "height": ...}}, '
        '{"class": ..., "polygon": [[x, y], ...]}]}',
        'Give each detection exactly one "box" or one "polygon" '
        "(at least 3 vertices).",
        'Use {"detections": []} when nothing matches. '
        "Report at most 100 detections.",
    ])


def _baseline_llm_content(image_bytes, image_key, prompt):
    """Frozen copy of the pre-feature `llm:` Converse content list for a
    request without Few_Shot_Examples: the source image block (bytes
    byte-for-byte, format derived from the object key) followed by the
    prompt text block, nothing else (Req 10.1)."""
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
    """The shared invocation module the worker delegates to — the module
    whose `downscale_image` binding the spy replaces."""
    import dda_llm_prelabel
    return dda_llm_prelabel


@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, with
    fake Cognito and Lambda clients, plus the dataset bucket — the
    test_dda_labeling_create_job convention, for the job-creation half."""
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


class _EnvPatcher(_Patcher):
    """`_Patcher` plus environment variables, restored together —
    `LLM_MODEL_TOKEN_LIMITS` is read from the environment per invocation
    by the worker, so it is the seam through which a generated
    Model_Token_Limits mapping reaches the handler."""

    def __init__(self):
        super().__init__()
        self._env = []

    def setenv(self, name, value):
        self._env.append((name, os.environ.get(name)))
        os.environ[name] = value

    def undo(self):
        for name, original in reversed(self._env):
            if original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original
        self._env = []
        super().undo()


# -------------------------------------------------------------- generators

_prompt_text = st.one_of(
    st.just(AWKWARD_PROMPT),
    st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=0x2FFF,
                               blacklist_categories=("Cs",)),
        min_size=1, max_size=60,
    ).filter(lambda text: text.strip()),
)

# Strings a wrongly-lenient reader might be tempted to convert — plus
# arbitrary text. All are invalid for both sizing values by design
# (Req 2.8 / normalize_downscale_setting's contract).
_malformed_strings = st.one_of(
    st.sampled_from(["1024", "512", "20000", "off", "", "  768  "]),
    st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=0x24F),
            max_size=12),
)

# A number with a genuine fractional part, in its DynamoDB-persisted
# spelling (Decimal) — what a float submitted to the record becomes.
# Always invalid for both values: the worker's Decimal conversion turns
# it into a float, which both resolvers reject with no numeric
# conversion.
_fractional_decimals = st.builds(
    lambda whole, milli: Decimal(whole) + Decimal(milli) / Decimal(1000),
    st.integers(min_value=-(10 ** 5), max_value=10 ** 5),
    st.integers(min_value=1, max_value=999),
)

# The stored `downscale_max_edge` space of Property 8: explicit absence,
# null, booleans, strings, fractional numbers, and integers that are not
# one of the six Max_Image_Edge options (whole numbers survive the
# DynamoDB Decimal round trip as int, so the six options themselves are
# the only integers excluded).
_stored_downscale = st.one_of(
    st.just(_ABSENT),
    st.none(),
    st.booleans(),
    _malformed_strings,
    st.integers(min_value=-(10 ** 6), max_value=10 ** 6).filter(
        lambda value: value not in MAX_IMAGE_EDGE_OPTIONS),
    _fractional_decimals,
)

# The stored `token_budget` space: explicit absence plus every invalid
# shape — null, booleans, strings, fractional numbers, and integers
# outside [1, MODEL_TOKEN_LIMIT_CEILING].
_stored_budget = st.one_of(
    st.just(_ABSENT),
    st.none(),
    st.booleans(),
    _malformed_strings,
    st.integers(min_value=-(10 ** 6), max_value=10 ** 7).filter(
        lambda value: not 1 <= value <= MODEL_TOKEN_LIMIT_CEILING),
    _fractional_decimals,
)


def _dynamo_safe(value):
    """A generated document in its DynamoDB-persisted spelling: floats
    become Decimal (DynamoDB has no float type), containers recursively."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _dynamo_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_dynamo_safe(item) for item in value]
    return value


# The predecessor's malformed-document generator: every `few_shot`
# sub-document that must resolve to *disabled* — absent, null, non-dict,
# falsy `enabled`, and enabled-but-empty or malformed `examples`
# (llm-autolabel-prompt-tuning Req 10.3), in persisted spelling.
from test_property_few_shot_selection import _disabled_few_shot_docs  # noqa: E402

_stored_few_shot = _disabled_few_shot_docs.map(
    lambda doc: _ABSENT if doc == "__absent__" else _dynamo_safe(doc))

# The Model_Token_Limits mapping the omitted selection must resolve
# through: no configuration at all, the empty mapping, a valid entry for
# the model, an invalid entry for the model, and an entry for a
# different model — all JSON-serializable (the worker's environment
# bootstrap seam).
_token_limit_mappings = st.one_of(
    st.none(),
    st.just({}),
    st.builds(lambda value: {MODEL_ID: value},
              st.integers(min_value=1, max_value=MODEL_TOKEN_LIMIT_CEILING)),
    st.builds(lambda value: {MODEL_ID: value},
              st.sampled_from([0, MODEL_TOKEN_LIMIT_CEILING + 1, True, False,
                               "64000", None])),
    st.builds(lambda value: {"some.other-model": value},
              st.integers(min_value=1, max_value=MODEL_TOKEN_LIMIT_CEILING)),
)


@st.composite
def _unconfigured_cases(draw):
    """One `llm:` job configuration with unconfigured sizing: modality,
    Label_Set, Source_Dimensions, key extension and Detection_Prompt from
    the identity generators' space, plus the three stored documents and
    the Model_Token_Limits mapping.

    Dimensions extend above every Max_Image_Edge option so a malformed
    value wrongly honored as a bound would have to decode the header-only
    bytes — and fail — rather than pass through.
    """
    modality = draw(st.sampled_from(MODALITIES))
    label_set = (list(CLASSIFICATION_LABELS) if modality == "Classification"
                 else draw(st.lists(st.sampled_from(LABEL_POOL), min_size=1,
                                    max_size=3, unique=True)))
    return SimpleNamespace(
        modality=modality,
        label_set=label_set,
        width=draw(st.one_of(st.integers(min_value=40, max_value=500),
                             st.integers(min_value=501, max_value=2600))),
        height=draw(st.one_of(st.integers(min_value=40, max_value=500),
                              st.integers(min_value=501, max_value=2600))),
        extension=draw(st.sampled_from([".png", ".jpg", ".jpeg", ".JPG"])),
        detection_prompt=draw(_prompt_text),
        stored_downscale=draw(_stored_downscale),
        stored_budget=draw(_stored_budget),
        stored_few_shot=draw(_stored_few_shot),
        token_limits=draw(_token_limit_mappings),
    )


@st.composite
def _creation_cases(draw):
    """An `llm:` job submission that omits both sizing keys entirely —
    the pre-feature submission shape (Req 10.6)."""
    modality = draw(st.sampled_from(MODALITIES))
    label_set = (None if modality == "Classification"
                 else draw(st.lists(st.sampled_from(LABEL_POOL), min_size=1,
                                    max_size=3, unique=True)))
    return SimpleNamespace(
        modality=modality,
        label_set=label_set,
        detection_prompt=draw(_prompt_text),
    )


def _guidance_reply(case):
    """A Coordinate_Guidance document valid for the case's modality,
    Label_Set and Source_Dimensions, so the outcome is a successful
    Pre_Label."""
    return json.dumps({"detections": [{
        "class": case.label_set[0],
        "box": {"left": 0, "top": 0,
                "width": max(2, case.width // 2),
                "height": max(2, case.height // 2)},
    }]})


def _put_sized_job(env, case):
    """A persisted `llm:` job whose auto_label document carries the drawn
    `downscale_max_edge` / `token_budget` / `few_shot` values (absent
    keys genuinely absent), written through DynamoDB so the worker reads
    them back exactly as production does — numbers as Decimal."""
    job_id = env.make_job(task_type=case.modality, label_set=case.label_set,
                          model=MODEL)
    auto_label = {"enabled": True, "model": MODEL}
    for name, value in (("downscale_max_edge", case.stored_downscale),
                        ("token_budget", case.stored_budget),
                        ("few_shot", case.stored_few_shot)):
        if value is not _ABSENT:
            auto_label[name] = value
    env.stack.tables.labeling_jobs.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET auto_label = :al",
        ExpressionAttributeValues={":al": auto_label})
    return job_id


# =========================================================================== #
# Property 8, labeling-time half: the request the worker issues
# =========================================================================== #

@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(case=_unconfigured_cases())
def test_property_unconfigured_sizing_reproduces_pre_feature_request(
        aws_stack, worker, prelabel, case):
    """Feature: llm-model-token-and-image-sizing, Property 8: An
    unconfigured Downscale_Setting reproduces the pre-feature request —
    *For any* `llm:` job configuration in which the Downscale_Setting is
    Downscale_Off, absent, null, or malformed in the job record, and the
    Few_Shot_Option is disabled, absent, null, or malformed, the model
    request content SHALL be exactly the source image block followed by the
    text block built from the Detection_Prompt character-for-character, the
    Label_Set and the Source_Dimensions, with no example image blocks and no
    example identification content, an omitted Token_Budget_Selection SHALL
    resolve through the Model_Token_Limits and the default of 10000, and no
    failure SHALL be attributable to the Downscale_Setting or the
    Token_Budget_Selection being absent or malformed.

    **Validates: Requirements 3.8, 3.10, 5.9, 5.12, 10.1, 10.10**
    """
    patcher = _EnvPatcher()
    try:
        env = AutolabelEnv(aws_stack, worker, patcher)

        # Hermetic Model_Token_Limits: no settings-table read (other test
        # modules leave SETTINGS_TABLE set in os.environ) and exactly the
        # generated mapping in the environment bootstrap.
        patcher.setattr(worker, "SETTINGS_TABLE", None)
        patcher.setenv("LLM_MODEL_TOKEN_LIMITS",
                       json.dumps(case.token_limits)
                       if case.token_limits is not None else "")
        patcher.setenv("LLM_MODEL_IMAGE_LIMITS", "")
        patcher.setattr(prelabel, "get_bedrock_configuration",
                        lambda: dict(BEDROCK_CONFIG))
        # The worker rebinds prelabel.get_bedrock_client while running;
        # registering the current value restores it afterwards.
        patcher.setattr(prelabel, "get_bedrock_client",
                        prelabel.get_bedrock_client)

        # The spy on the Image_Downscaler seam: both the module attribute
        # and the chokepoint's imported binding, so any invocation is
        # recorded whichever spelling reaches it (Req 10.1: zero calls,
        # which subsumes zero re-encodes and zero decodes).
        downscale_calls = []
        real_downscale = prelabel.downscale_image

        def spying_downscale(*args, **kwargs):
            downscale_calls.append((args, kwargs))
            return real_downscale(*args, **kwargs)

        patcher.setattr(prelabel, "downscale_image", spying_downscale)
        patcher.setattr(dda_llm_image, "downscale_image", spying_downscale)

        # One header-only dataset object: parseable dimensions, but
        # undecodable pixel data — any wrongly-applied bound would refuse
        # it instead of reaching a Pre_Label.
        image_key = f"imgs/{uuid.uuid4()}{case.extension}"
        body = (png_bytes(case.width, case.height)
                if case.extension.lower() == ".png"
                else jpeg_bytes(case.width, case.height))
        image_uri = env.put_image(image_key, body=body)

        job_id = _put_sized_job(env, case)
        task_id = env.make_task(job_id, image_uri)
        bedrock, _recorded = env.use_bedrock(replies=[_guidance_reply(case)])

        result = env.run([env.record(
            job_id, task_id, image_uri, case.modality, case.label_set,
            MODEL, detection_prompt=case.detection_prompt)])

        # No failure attributable to either value: the record processes
        # to a successful Pre_Label (Req 3.8, 5.9, 5.12, 10.10).
        assert result == {"batchItemFailures": []}
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Available", (
            f"unconfigured sizing must never fail a job "
            f"(downscale={case.stored_downscale!r}, "
            f"budget={case.stored_budget!r}): {task.get('prelabel_error')!r}")
        assert task.get("prelabel_error") is None
        assert env.prelabel_exists(job_id, task_id)

        # Exactly one invocation, addressed to the message's model id.
        assert len(bedrock.calls) == 1
        call = bedrock.calls[0]
        assert call["modelId"] == MODEL_ID

        # The pinned pre-feature request, exactly: the source image block
        # (bytes byte-for-byte, key-derived format) followed by the text
        # block built from the Detection_Prompt character-for-character,
        # the Label_Set and the Source_Dimensions — no example image
        # blocks, no example identification content (Req 10.1).
        expected_prompt = _baseline_llm_prompt(
            case.label_set, case.detection_prompt, case.width, case.height)
        assert call["messages"] == [{
            "role": "user",
            "content": _baseline_llm_content(body, image_key,
                                             expected_prompt),
        }], "request content drifted from the pre-feature baseline"

        # The omitted (or malformed, which is the same thing) selection
        # resolves through the Model_Token_Limits and then the default of
        # 10000 (Req 3.8, 3.10) — the design's oracle, spelled both ways.
        mapping = case.token_limits if case.token_limits is not None else {}
        expected_budget = resolve_token_budget(MODEL_ID, None, mapping)
        entry = mapping.get(MODEL_ID)
        assert expected_budget == (
            entry if (isinstance(entry, int) and not isinstance(entry, bool)
                      and 1 <= entry <= MODEL_TOKEN_LIMIT_CEILING)
            else MODEL_TOKEN_LIMIT_DEFAULT)
        # The whole inference configuration: the resolved budget and
        # nothing else (temperature and top_p are unset), never the
        # poison Global_Max_Tokens.
        assert call["inferenceConfig"] == {"maxTokens": expected_budget}

        # Zero Image_Downscaler invocations: the source bytes passed
        # through untouched, nothing was re-encoded and nothing was
        # decoded (Req 10.1).
        assert downscale_calls == [], (
            f"downscale_image must not run for an unconfigured "
            f"Downscale_Setting: {downscale_calls!r}")
    finally:
        patcher.undo()


# =========================================================================== #
# Property 8, creation half: a submission omitting both keys
# =========================================================================== #

@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(case=_creation_cases())
def test_property_submission_omitting_both_keys_yields_pre_feature_record(
        aws_stack, dda, case):
    """Feature: llm-model-token-and-image-sizing, Property 8: An
    unconfigured Downscale_Setting reproduces the pre-feature request —
    job-creation half: *for any* `llm:` submission that omits both the
    Token_Budget_Selection and the Downscale_Setting, the creation SHALL be
    accepted under the pre-feature validation rules with no message
    referring to either value, and the persisted record SHALL carry neither
    `downscale_max_edge` nor `token_budget` — byte-identical to the
    pre-feature auto_label record.

    **Validates: Requirements 10.6, 3.10**
    """
    env = CreateJobEnv(aws_stack, dda)
    env.put_images(["a.jpg"])

    status, response_body = env.create(
        task_type=case.modality, label_set=case.label_set,
        auto_label={"enabled": True, "model": MODEL,
                    "detection_prompt": case.detection_prompt})

    # Accepted under the pre-feature rules: no rejection, and therefore
    # no validation message, on account of either omitted value.
    assert status == 201, (
        f"a submission omitting both sizing keys must be accepted: "
        f"{response_body!r}")
    assert "validation_errors" not in response_body

    # The pre-feature auto_label record, exactly — neither key present.
    job = env.get_job(response_body["job_id"])
    assert "downscale_max_edge" not in job["auto_label"]
    assert "token_budget" not in job["auto_label"]
    assert job["auto_label"] == {
        "enabled": True,
        "model": MODEL,
        "detection_prompt": case.detection_prompt,
    }, (f"auto_label drifted from the pre-feature record: "
        f"{job['auto_label']!r}")
