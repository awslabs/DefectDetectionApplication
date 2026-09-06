"""
Preview / Auto_Labeler fidelity property tests.

Spec: llm-autolabel-prompt-tuning, tasks 9.6 and 9.7.

These are the central fidelity guarantee of the feature: the design's premise
is that a Preview_Run *predicts* labeling-time behavior because both paths run
the same code, not because two implementations were written to agree. Both
properties are therefore asserted by driving the two real entry points —
`dda_labeling._run_preview_sample` (the per-Sample_Image body of
`execute_preview_run`) and `dda_autolabel_worker._generate_llm_prelabel` (the
per-dataset-image body of the SQS worker) — against **one** stub Converse
client, and comparing what each one actually sent and what each one derived:

**Property 1: Preview and Auto_Labeler issue identical model requests** —
Validates Requirements 3.1, 6.6, 7.6
**Property 2: Preview and Auto_Labeler derive identical outcomes from
identical model output** — Validates Requirements 3.2, 3.11, 9.3

Extended for llm-model-token-and-image-sizing (task 9.2) with the sizing
dimension of the same identity claim:

**Property 5: Preview and Auto_Labeler requests stay byte-identical under
downscaling** — Validates Requirements 1.4, 6.1, 6.8, 8.4, 8.6

The sizing property drives the same two entry points over a widened space:
a Downscale_Setting (Downscale_Off plus every Max_Image_Edge option), a
Token_Budget_Selection, a Model_Token_Limits mapping persisted as the
`llm_model_token_limits` settings item both per-invocation loaders read,
and source dimensions widened so a subset of drawn targets and examples
genuinely exceeds each drawn bound — real decode-and-re-encode resizes,
not just pass-throughs. `IdentityEnv` plants the drawn values where the
system persists them (`auto_label.downscale_max_edge` /
`auto_label.token_budget` on the job record for the worker;
`downscale_max_edge` plus the start-route-resolved `token_budget` on the
`RUN` document for the preview), and a spy on the chokepoint's
`downscale_image` binding proves each image block was downscaled exactly
once per path. Everything Properties 1 and 2 assert is untouched: their
generators, environments and assertions run exactly as before, with the
sizing fields inert.

One stub for both paths is what makes the comparison meaningful: each module
rebinds its own `get_bedrock_client` onto `dda_llm_prelabel` immediately
before delegating, so the captured `converse(**kwargs)` list holds the two
requests back to back and can be compared for byte-level equality — model id,
content blocks in order, image bytes and formats, prompt text and inference
configuration. Neither ordering matters, and neither path can accidentally
observe the other's binding.

Generated across: all three modalities, Label_Sets, image dimensions,
`.png` / `.jpg` / `.jpeg` / `.JPG` object keys, the Few_Shot_Option on and off
with varying good/bad counts, and Model_Image_Limit values (including ones
that force omission, and `limit == 1` which attaches nothing).

Known seam difference, recorded rather than hidden
--------------------------------------------------
For a **bare** (non-`s3://`) example reference the two paths resolve
different buckets: `dda_autolabel_worker._few_shot_ref_location` resolves a
bare ref against `PORTAL_ARTIFACTS_BUCKET`, while
`dda_labeling._resolve_sample_reference` resolves it against the Use_Case
dataset bucket. The wizard stores full `s3://…` URIs
(`CreateLabelingJob.tsx` builds `s3://${bucket}/${key}` from the batch upload
response), for which both paths resolve identically — so the identity property
generates full `s3://bucket/key` references, the spelling the system actually
produces. The divergence for the bare spelling is pinned by the single
example-based test at the end of this file
(`test_bare_example_reference_spelling_resolves_to_different_buckets`) so it
is documented and will fail loudly if either side changes.

Harness reuse (Hypothesis cannot consume function-scoped fixtures): the
module-scoped `worker` / `dda` fixtures follow the
`test_property_llm_autolabel_preservation.py` pattern, per-example
environments are built inside the test bodies from `AutolabelEnv`
(test_dda_autolabel_worker.py) plus the preview-side run document, and
`_Patcher` (test_property_llm_autolabel_preservation.py) stands in for
monkeypatch.
"""
import io
import json
import os
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ReadTimeoutError
from hypothesis import given, settings
from hypothesis import strategies as st

from dda_llm_image import MAX_IMAGE_EDGE_OPTIONS, downscale_image
from dda_llm_request import (
    FEW_SHOT_HEADER,
    FEW_SHOT_TARGET_INTRO,
    MODEL_IMAGE_LIMIT_DEFAULT,
    few_shot_identification_text,
    image_format_for_key,
    resolve_token_budget,
    select_few_shot_examples,
)
from test_dda_autolabel_worker import (
    ARTIFACTS_BUCKET,
    AutolabelEnv,
    DATASET_BUCKET,
    SAM_FUNCTION,
    jpeg_bytes,
    png_bytes,
)
from test_dda_llm_prelabel import (
    RecordingConverseClient,
    client_error,
    guidance,
)
from test_property_llm_autolabel_preservation import _Patcher

REGION = "us-east-1"
MODEL_ID = "us.amazon.nova-pro-v1:0"
MODEL = f"llm:{MODEL_ID}"

MODALITIES = ("Classification", "Segmentation", "ObjectDetection")
# Classification's Label_Set is the fixed binary set (dda-data-labeling
# Req 4.3); the geometry modalities carry job-defined class names.
CLASSIFICATION_LABELS = ["normal", "anomaly"]
LABEL_POOL = ["scratch", "dent", "crack", "rust"]

GOOD = "good"
BAD = "bad"

# Deterministic Bedrock_Configuration for both paths: the inferenceConfig is
# derived from it, so pinning it keeps the comparison about the request
# construction rather than about ambient settings. 240 s exercises the
# clamp to the shared 120 s bound (Req 3.3).
BEDROCK_CONFIG = {
    "model_id": MODEL_ID,
    "region": "us-west-2",
    "max_tokens": 4096,
    "temperature": None,
    "top_p": None,
    "timeout_seconds": 240,
}
CLAMPED_TIMEOUT = 120

# A Detection_Prompt that must survive character-for-character: leading and
# trailing whitespace, quotes, braces, a newline and a non-ASCII character.
AWKWARD_PROMPT = '  Find every "scratch" {and dent}\n  on the panel — ✓  '

# A dedicated portal settings table for the `llm_model_token_limits` item
# (llm-model-token-and-image-sizing Req 1.8): the sizing property points
# both modules' SETTINGS_TABLE at it so the Preview_API's and the
# Auto_Labeler's per-invocation loaders read the drawn mapping through the
# same persisted item, exactly as production delivers it.
IDENTITY_SETTINGS_TABLE = "test-settings-request-identity"


def real_image_bytes(width, height, image_format, seed=0):
    """A fully decodable image — unlike `png_bytes` / `jpeg_bytes`'
    header-only bytes — for the sizing cases that drive the
    Image_Downscaler through a real decode-and-re-encode
    (test_dda_llm_prelabel.real_png_bytes's convention). The seed varies
    the fill color so distinct images carry distinct bytes even at equal
    dimensions."""
    from PIL import Image  # lazy, matching the imaging-layer convention

    color = ((37 * seed + 11) % 256, (59 * seed + 23) % 256,
             (83 * seed + 41) % 256)
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(
        buffer, format="PNG" if image_format == "png" else "JPEG")
    return buffer.getvalue()


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
    """The real dda_labeling module imported inside the moto mock.

    Only the preview executor's per-sample body is exercised, so no Cognito
    or Lambda seam is needed — the async self-invoke and the API routes are
    covered by their own suites.
    """
    sys.modules.pop("dda_labeling", None)
    import dda_labeling
    return dda_labeling


@pytest.fixture(scope="module")
def prelabel(aws_stack):
    """The shared invocation module both paths delegate to."""
    import dda_llm_prelabel
    return dda_llm_prelabel


@pytest.fixture(scope="module")
def settings_table(aws_stack):
    """The moto-backed portal settings table the sizing property writes
    the `llm_model_token_limits` item into."""
    client = boto3.client("dynamodb", region_name=REGION)
    try:
        client.create_table(
            TableName=IDENTITY_SETTINGS_TABLE,
            KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "setting_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    except client.exceptions.ResourceInUseException:
        pass
    return boto3.resource("dynamodb",
                          region_name=REGION).Table(IDENTITY_SETTINGS_TABLE)


class _EnvPatcher(_Patcher):
    """`_Patcher` plus environment variables, restored together.

    `LLM_MODEL_IMAGE_LIMITS` is read from the environment per call by both
    modules, so it is the seam through which a generated Model_Image_Limit
    reaches both paths identically.
    """

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


# ----------------------------------------------------------------- harness

class IdentityEnv:
    """One Use_Case, one dataset object, one example set, one stub Converse
    client — and both code paths pointed at all of them.

    The Auto_Labeler reads its dataset image from the `s3://` URI in its SQS
    message; the Preview_API reads its Sample_Image from `(dataset_bucket,
    sample_key)`. Aiming both at the *same* object is what lets the captured
    requests be compared for byte-level equality: any difference in the image
    bytes would then be a real difference in what the model was sent.
    """

    def __init__(self, stack, worker, dda, prelabel, patcher, stub,
                 image_limit=None, token_limits=None, settings_table=None):
        self.stack = stack
        self.worker = worker
        self.dda = dda
        self.patcher = patcher
        self.stub = stub
        self.worker_env = AutolabelEnv(stack, worker, patcher)
        self.usecase_id = self.worker_env.usecase_id
        self.usecase = dda.get_usecase(self.usecase_id)
        self.s3 = self.worker_env.s3
        self.client_requests = []

        # Both modules rebind their own binding onto the shared module before
        # delegating, so both must be replaced for one stub to serve both.
        # `prelabel.get_bedrock_client` is saved too, so whichever module
        # rebound it last does not leak into a later test.
        self.recorded = []
        patcher.setattr(worker, "get_bedrock_client", self._client_factory)
        patcher.setattr(dda, "get_bedrock_client", self._client_factory)
        patcher.setattr(prelabel, "get_bedrock_client", self._client_factory)
        patcher.setattr(prelabel, "get_bedrock_configuration",
                        lambda: dict(BEDROCK_CONFIG))

        if image_limit is None:
            patcher.setenv("LLM_MODEL_IMAGE_LIMITS", "")
            self.image_limit = MODEL_IMAGE_LIMIT_DEFAULT
        else:
            patcher.setenv("LLM_MODEL_IMAGE_LIMITS",
                           json.dumps({MODEL_ID: image_limit}))
            self.image_limit = image_limit

        # llm-model-token-and-image-sizing: the Model_Token_Limits mapping
        # is persisted as the `llm_model_token_limits` settings item, the
        # one place both per-invocation loaders read (Req 1.8, 6.8). With
        # `settings_table` supplied (the sizing property), the drawn
        # mapping is written through DynamoDB — so both paths read it back
        # through the real Decimal conversion — and both modules'
        # per-invocation memos are cleared so no earlier example's mapping
        # can leak in. A `None` mapping deterministically resolves to the
        # empty mapping (SETTINGS_TABLE off, blank environment bootstrap).
        # The legacy environments (Properties 1 and 2) pass neither
        # argument and are untouched.
        self.sizing = settings_table is not None
        self.token_limits_mapping = {}
        if self.sizing:
            patcher.setattr(worker, "_model_token_limits_cache", None)
            patcher.setattr(dda, "_model_token_limits_cache", None)
            patcher.setenv("LLM_MODEL_TOKEN_LIMITS", "")
            if token_limits is None:
                patcher.setattr(worker, "SETTINGS_TABLE", None)
                patcher.setattr(dda, "SETTINGS_TABLE", None)
            else:
                settings_table.put_item(Item={
                    "setting_key": worker.LLM_MODEL_TOKEN_LIMITS_SETTING_KEY,
                    "value": token_limits,
                })
                patcher.setattr(worker, "SETTINGS_TABLE",
                                IDENTITY_SETTINGS_TABLE)
                patcher.setattr(dda, "SETTINGS_TABLE",
                                IDENTITY_SETTINGS_TABLE)
                self.token_limits_mapping = dict(token_limits)

    # ------------------------------------------------------------- seams
    def _client_factory(self, region, timeout_seconds):
        self.recorded.append((region, timeout_seconds))
        return self.stub

    # ------------------------------------------------------------- setup
    def seed_target(self, extension, width, height, real=False):
        """The one object both paths label.

        `real` seeds fully decodable pixel data instead of the header-only
        bytes, for the sizing cases whose drawn bound makes the
        Image_Downscaler genuinely decode and re-encode the target.
        """
        self.image_key = f"datasets/{uuid.uuid4().hex[:8]}/target{extension}"
        image_format = ("png" if extension.lower() == ".png" else "jpeg")
        if real:
            self.image_bytes = real_image_bytes(width, height, image_format,
                                                seed=7)
        else:
            self.image_bytes = (png_bytes(width, height)
                                if image_format == "png"
                                else jpeg_bytes(width, height))
        self.s3.put_object(Bucket=DATASET_BUCKET, Key=self.image_key,
                           Body=self.image_bytes)
        self.image_uri = f"s3://{DATASET_BUCKET}/{self.image_key}"
        return self.image_key

    def seed_examples(self, good_count, bad_count, bucket=DATASET_BUCKET,
                      bare=False, dimensions=None):
        """Stored Few_Shot_Example references, good then bad in stored order.

        Every example gets distinct bytes (distinct pixel dimensions and,
        for real images, distinct fill colors), so a request that attached
        the same *count* of examples in a different order, or attached a
        different subset, cannot pass the comparison. References are full
        `s3://bucket/key` URIs — the spelling the wizard stores — unless
        `bare` is set, which the seam-difference test uses.

        `dimensions` — a `(width, height)` pair per example in seeded
        order — switches to fully decodable pixel data, for the sizing
        cases whose drawn bound makes the Image_Downscaler genuinely
        decode and re-encode the examples that exceed it.
        """
        examples = []
        self.example_bytes = {}
        base = f"labeling-examples/{uuid.uuid4().hex[:8]}"
        index = 0
        for designation, count in ((GOOD, good_count), (BAD, bad_count)):
            for position in range(count):
                extension = "png" if (position % 2 == 0) else "jpg"
                key = f"{base}/{designation}/{position}.{extension}"
                if dimensions is None:
                    body = (png_bytes(11 + position, 7 + position)
                            if extension == "png"
                            else jpeg_bytes(13 + position, 9 + position))
                else:
                    width, height = dimensions[index]
                    body = real_image_bytes(
                        width, height,
                        "png" if extension == "png" else "jpeg", seed=index)
                index += 1
                self.s3.put_object(Bucket=bucket, Key=key, Body=body)
                ref = key if bare else f"s3://{bucket}/{key}"
                self.example_bytes[ref] = body
                examples.append({"ref": ref, "designation": designation,
                                 "position": position})
        self.examples = examples
        return examples

    # ------------------------------------------------- path entry points
    def _few_shot_document(self, case):
        return {"enabled": case.few_shot_enabled,
                "examples": [dict(example) for example in self.examples]}

    def job_document(self, case):
        """The Labeling_Job record the worker reads few-shot state from.

        `skip_verification` stays False on purpose: Per_Label_Prompts are a
        skip-verification job setting and a Preview_Run has no job, so they
        are outside the identity claim by construction. Carrying a per-label
        prompt map on the record while the flag is off asserts the map cannot
        leak into the request (Req 3.1 names Detection_Prompt, Label_Set and
        dimensions as the prompt inputs).
        """
        auto_label = {
            "enabled": True,
            "model": MODEL,
            "detection_prompt": case.detection_prompt,
            "few_shot": self._few_shot_document(case),
        }
        # llm-model-token-and-image-sizing Req 5.7, 3.6: `create_dda_job`
        # persists `auto_label.downscale_max_edge` / `auto_label.token_budget`
        # only when the submission carried them — Downscale_Off and an empty
        # budget leave the record without the keys, so the drawn `None`s are
        # planted as absence, exactly the record shape production writes.
        downscale_setting = getattr(case, "downscale_setting", None)
        if downscale_setting is not None:
            auto_label["downscale_max_edge"] = downscale_setting
        token_budget_selection = getattr(case, "token_budget_selection", None)
        if token_budget_selection is not None:
            auto_label["token_budget"] = token_budget_selection
        return {
            "job_id": f"labeling-{uuid.uuid4().hex[:8]}",
            "usecase_id": self.usecase_id,
            "task_type": case.modality,
            "label_set": list(case.label_set),
            "skip_verification": False,
            "per_label_prompts": case.per_label_prompts or {},
            "auto_label": auto_label,
        }

    def sqs_message(self, case, job):
        return {
            "job_id": job["job_id"],
            "task_id": f"task-{uuid.uuid4().hex[:8]}",
            "image_s3_uri": self.image_uri,
            "modality": case.modality,
            "label_set": list(case.label_set),
            "model": MODEL,
            "detection_prompt": case.detection_prompt,
        }

    def run_document(self, case):
        """The `PREVIEW#{run_id}` / `RUN` item the executor reads, carrying
        the validated example references `_write_preview_run_item` records
        additively under `few_shot_examples`.

        For the sizing property it also carries what the start route
        records (llm-model-token-and-image-sizing Req 5.3, 1.6): the
        validated `downscale_max_edge` (absent for Downscale_Off) and
        `token_budget` as the Effective_Token_Budget already resolved at
        run start — re-resolution at execution time is the identity, which
        is exactly how the executor reads it back.
        """
        document = {
            "job_id": f"PREVIEW#preview-{uuid.uuid4().hex[:8]}",
            "task_id": "RUN",
            "usecase_id": self.usecase_id,
            "model": MODEL,
            "task_type": case.modality,
            "label_set": list(case.label_set),
            "detection_prompt": case.detection_prompt,
            "few_shot_enabled": case.few_shot_enabled,
            "few_shot_examples": [dict(example) for example in self.examples],
        }
        if self.sizing:
            downscale_setting = getattr(case, "downscale_setting", None)
            if downscale_setting is not None:
                document["downscale_max_edge"] = downscale_setting
            document["token_budget"] = self.expected_token_budget(case)
        return document

    def worker_prelabel(self, case):
        """`dda_autolabel_worker._generate_llm_prelabel` — the labeling-time
        entry point, with the S3 read, the dimension gate, prompt resolution
        and few-shot resolution it owns."""
        job = self.job_document(case)
        return self.worker._generate_llm_prelabel(
            self.sqs_message(case, job), job, MODEL_ID)

    def preview_prelabel(self, case):
        """`dda_labeling._run_preview_sample` — the Preview_API's per-sample
        body, with its own S3 read, dimension gate and few-shot resolution.

        The executor returns the Source_Dimensions and the Sent_Dimensions
        beside the Pre_Label (llm-model-token-and-image-sizing Req 5.10);
        both are stashed for the sizing assertions and the return value
        keeps its meaning as the Pre_Label alone.
        """
        prelabel, source_dimensions, sent_dimensions = (
            self.dda._run_preview_sample(
                self.run_document(case), {}, self.usecase, DATASET_BUCKET,
                self.image_key))
        self.preview_source_dimensions = source_dimensions
        self.preview_sent_dimensions = sent_dimensions
        return prelabel

    # -------------------------------------------------------- assertions
    def expected_token_budget(self, case):
        """The Effective_Token_Budget both paths must send: the shared
        resolver over the drawn selection and the drawn mapping
        (llm-model-token-and-image-sizing Req 1.4)."""
        return resolve_token_budget(
            MODEL_ID, getattr(case, "token_budget_selection", None),
            self.token_limits_mapping)

    def attached_examples(self):
        """What the shared selection says both paths must attach."""
        attached, _omitted = select_few_shot_examples(
            [dict(example) for example in self.examples], self.image_limit)
        return attached

    def expected_attachment_count(self, case):
        if not case.few_shot_enabled:
            return 0
        return len(self.attached_examples())


def image_blocks(call):
    return [block for block in call["messages"][0]["content"]
            if "image" in block]


def text_blocks(call):
    return [block["text"] for block in call["messages"][0]["content"]
            if "text" in block]


# -------------------------------------------------------------- generators

_prompt_text = st.one_of(
    st.just(AWKWARD_PROMPT),
    st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=0x2FFF,
                               blacklist_categories=("Cs",)),
        min_size=1, max_size=60,
    ).filter(lambda text: text.strip()),
)

# Sizing dimensions (llm-model-token-and-image-sizing task 9.2): the
# 40-400 px space widened so a subset of drawn targets exceeds *every*
# Max_Image_Edge option — including 2048 — and a subset of drawn examples
# exceeds each bound too, so real decode-and-re-encode resizes happen on
# both kinds of image block rather than only pass-throughs.
_sizing_target_edge = st.one_of(
    st.integers(min_value=40, max_value=400),      # below every bound
    st.integers(min_value=520, max_value=1400),    # above 512/768/1024/1280
    st.integers(min_value=1560, max_value=2600),   # above 1536 and 2048
)
_sizing_example_edge = st.one_of(
    st.integers(min_value=24, max_value=300),      # below every bound
    st.integers(min_value=520, max_value=2200),    # above a drawn subset
)

# Model_Token_Limits mappings as an administrator could persist them:
# absent entirely, empty, carrying the model's entry, and carrying only
# decoys — a whitespace-padded spelling and an unrelated identifier —
# that exact string comparison must not match (Req 1.1).
_token_limits_mappings = st.one_of(
    st.none(),
    st.dictionaries(
        keys=st.sampled_from([
            MODEL_ID,
            f" {MODEL_ID} ",
            "anthropic.claude-3-5-sonnet-20240620-v1:0",
        ]),
        values=st.integers(min_value=1,
                           max_value=128000),
        max_size=3,
    ),
)


@st.composite
def _identity_cases(draw, sizing=False):
    """A full `llm:` job configuration: modality, Label_Set, dimensions, key
    extension, Detection_Prompt, few-shot state and Model_Image_Limit.

    The example counts and limits are drawn together so the generated space
    includes limits that attach everything, limits that force omission, and
    `limit == 1` which attaches nothing at all (Req 7.2, 7.4).

    With `sizing` set (llm-model-token-and-image-sizing task 9.2) the case
    additionally draws a Downscale_Setting, a Token_Budget_Selection, a
    Model_Token_Limits mapping and per-example dimensions, and the source
    dimensions are widened so a subset exceeds each bound. Without it —
    the predecessor properties — those fields are inert (`None`) and every
    draw is exactly what it always was.
    """
    modality = draw(st.sampled_from(MODALITIES))
    label_set = (list(CLASSIFICATION_LABELS) if modality == "Classification"
                 else draw(st.lists(st.sampled_from(LABEL_POOL), min_size=1,
                                    max_size=3, unique=True)))
    per_label_prompts = draw(st.one_of(
        st.none(),
        st.just({label: f"guidance for {label}" for label in label_set})))
    dimension_edge = (_sizing_target_edge if sizing
                      else st.integers(min_value=40, max_value=400))
    width = draw(dimension_edge)
    height = draw(dimension_edge)
    extension = draw(st.sampled_from([".png", ".jpg", ".jpeg", ".JPG"]))
    detection_prompt = draw(_prompt_text)
    few_shot_enabled = draw(st.booleans())
    good_count = draw(st.integers(min_value=0, max_value=4))
    bad_count = draw(st.integers(min_value=0, max_value=4))
    # None means "no configured limit": both paths resolve the shared
    # default of 20.
    image_limit = draw(st.one_of(st.none(),
                                 st.integers(min_value=1, max_value=6)))

    downscale_setting = None
    token_budget_selection = None
    token_limits = None
    example_dimensions = None
    if sizing:
        downscale_setting = draw(
            st.sampled_from((None,) + MAX_IMAGE_EDGE_OPTIONS))
        # Valid-or-absent, the two states the system persists: request
        # validation and job creation reject everything else before a
        # record exists (Req 3.5, 3.6).
        token_budget_selection = draw(st.one_of(
            st.none(), st.integers(min_value=1, max_value=128000)))
        token_limits = draw(_token_limits_mappings)
        total = good_count + bad_count
        example_dimensions = draw(st.lists(
            st.tuples(_sizing_example_edge, _sizing_example_edge),
            min_size=total, max_size=total))

    return SimpleNamespace(
        modality=modality,
        label_set=label_set,
        per_label_prompts=per_label_prompts,
        width=width,
        height=height,
        extension=extension,
        detection_prompt=detection_prompt,
        few_shot_enabled=few_shot_enabled,
        good_count=good_count,
        bad_count=bad_count,
        image_limit=image_limit,
        downscale_setting=downscale_setting,
        token_budget_selection=token_budget_selection,
        token_limits=token_limits,
        example_dimensions=example_dimensions,
    )


def _valid_reply(case):
    """A Coordinate_Guidance document valid for this case's modality, labels
    and dimensions."""
    return guidance([{
        "class": case.label_set[0],
        "box": {"left": 0, "top": 0,
                "width": max(2, case.width // 2),
                "height": max(2, case.height // 2)},
    }])


# The model outputs Property 2 quantifies over. Each entry maps a case to the
# stub's behavior; the same behavior is served to both paths.
OUTCOME_VALID = "valid_guidance"
OUTCOME_EMPTY = "empty_detections"
OUTCOME_UNPARSEABLE = "unparseable_text"
OUTCOME_UNKNOWN_CLASS = "out_of_label_set_class"
OUTCOME_BAD_GEOMETRY = "malformed_geometry"
OUTCOME_OVER_LIMIT = "detection_count_exceeded"
OUTCOME_TEXTLESS = "textless_response"
OUTCOME_TIMEOUT = "read_timeout"
OUTCOME_MODEL_ERROR = "client_error"

OUTCOMES = (OUTCOME_VALID, OUTCOME_EMPTY, OUTCOME_UNPARSEABLE,
            OUTCOME_UNKNOWN_CLASS, OUTCOME_BAD_GEOMETRY, OUTCOME_OVER_LIMIT,
            OUTCOME_TEXTLESS, OUTCOME_TIMEOUT, OUTCOME_MODEL_ERROR)

SUCCESS_OUTCOMES = (OUTCOME_VALID, OUTCOME_EMPTY)


def _stub_for_outcome(outcome, case):
    """One stub Converse client configured for the generated outcome."""
    if outcome == OUTCOME_VALID:
        return RecordingConverseClient(reply=_valid_reply(case))
    if outcome == OUTCOME_EMPTY:
        return RecordingConverseClient(reply=guidance([]))
    if outcome == OUTCOME_UNPARSEABLE:
        return RecordingConverseClient(
            reply="I could not find anything of note in this image.")
    if outcome == OUTCOME_UNKNOWN_CLASS:
        return RecordingConverseClient(reply=guidance([{
            "class": "definitely-not-in-the-label-set",
            "box": {"left": 1, "top": 1, "width": 4, "height": 4}}]))
    if outcome == OUTCOME_BAD_GEOMETRY:
        return RecordingConverseClient(reply=guidance([{
            "class": case.label_set[0],
            "box": {"left": 1, "top": 1, "width": 0, "height": -3}}]))
    if outcome == OUTCOME_OVER_LIMIT:
        return RecordingConverseClient(reply=guidance([
            {"class": case.label_set[0],
             "box": {"left": 0, "top": 0, "width": 2, "height": 2}}
            for _ in range(101)]))
    if outcome == OUTCOME_TEXTLESS:
        return RecordingConverseClient(
            response={"output": {"message": {"content": []}}})
    if outcome == OUTCOME_TIMEOUT:
        return RecordingConverseClient(
            error=ReadTimeoutError(endpoint_url="https://bedrock.test"))
    return RecordingConverseClient(
        error=client_error("ThrottlingException", "slow down"))


@st.composite
def _outcome_cases(draw):
    """An `llm:` configuration paired with one canned model outcome."""
    case = draw(_identity_cases())
    case.outcome = draw(st.sampled_from(OUTCOMES))
    return case


# =========================================================================== #
# Property 1
# =========================================================================== #

@settings(max_examples=100, deadline=None)
@given(case=_identity_cases())
def test_property_preview_and_worker_issue_identical_requests(
        aws_stack, worker, dda, prelabel, case):
    """Feature: llm-autolabel-prompt-tuning, Property 1: Preview and
    Auto_Labeler issue identical model requests — *For any* Labeling_Modality,
    Label_Set, Detection_Prompt, per-label prompt map, image bytes, image
    dimensions, few-shot configuration and `llm:` model identifier, the
    Converse request the Preview_API issues for a Sample_Image and the
    Converse request the Auto_Labeler issues for a dataset image with the same
    configuration SHALL be equal in every element — model id, message content
    blocks in order, image bytes and formats, prompt text, and inference
    configuration — and exactly one invocation SHALL be issued per image.

    **Validates: Requirements 3.1, 6.6, 7.6**
    """
    patcher = _EnvPatcher()
    try:
        stub = RecordingConverseClient(reply=_valid_reply(case))
        env = IdentityEnv(aws_stack, worker, dda, prelabel, patcher, stub,
                          image_limit=case.image_limit)
        env.seed_target(case.extension, case.width, case.height)
        env.seed_examples(case.good_count, case.bad_count)

        worker_prelabel = env.worker_prelabel(case)
        preview_prelabel = env.preview_prelabel(case)

        # Exactly one invocation per image, from each path (Req 3.1).
        assert len(stub.calls) == 2, (
            f"expected one Converse call per path, got {len(stub.calls)}")
        worker_call, preview_call = stub.calls

        # The whole request, element for element: model id, the ordered
        # content blocks with their image bytes and formats, the prompt text,
        # and the inference configuration (Req 3.1, 6.6, 7.6).
        assert preview_call == worker_call, (
            "preview and Auto_Labeler requests differ\n"
            f"worker : {worker_call!r}\npreview: {preview_call!r}")
        assert set(worker_call) == {"modelId", "messages", "inferenceConfig"}
        assert worker_call["modelId"] == MODEL_ID
        # Both clients were asked for the same region and the same bound.
        assert env.recorded == [(BEDROCK_CONFIG["region"], CLAMPED_TIMEOUT)] * 2

        # The request really is the one this configuration calls for, so the
        # equality above cannot pass by both paths attaching nothing.
        attached = env.attached_examples() if case.few_shot_enabled else []
        blocks = image_blocks(worker_call)
        assert len(blocks) == len(attached) + 1
        assert len(blocks) <= env.image_limit
        # Target image last, byte-identical to the seeded object, with the
        # key-derived format.
        assert blocks[-1]["image"]["source"]["bytes"] == env.image_bytes
        assert blocks[-1]["image"]["format"] == (
            "png" if case.extension.lower() == ".png" else "jpeg")
        # Attached examples in selection order, each carrying its own bytes.
        assert [block["image"]["source"]["bytes"] for block in blocks[:-1]] == [
            env.example_bytes[example["ref"]] for example in attached]

        texts = text_blocks(worker_call)
        # The Detection_Prompt reaches the model character-for-character, and
        # the skip-verification-only per-label prompts never do (Req 3.1).
        assert case.detection_prompt in texts[-1]
        if case.per_label_prompts:
            for label, prompt in case.per_label_prompts.items():
                assert prompt not in texts[-1]
        # Few-shot framing appears exactly when examples are attached.
        if attached:
            assert texts[0] == FEW_SHOT_HEADER
            assert FEW_SHOT_TARGET_INTRO in texts
        else:
            assert FEW_SHOT_HEADER not in texts
            assert FEW_SHOT_TARGET_INTRO not in texts

        # Same request, same shared conversion: the same Pre_Label.
        assert preview_prelabel == worker_prelabel
    finally:
        patcher.undo()


# =========================================================================== #
# Property 2
# =========================================================================== #

@settings(max_examples=100, deadline=None)
@given(case=_outcome_cases())
def test_property_preview_and_worker_derive_identical_outcomes(
        aws_stack, worker, dda, prelabel, case):
    """Feature: llm-autolabel-prompt-tuning, Property 2: Preview and
    Auto_Labeler derive identical outcomes from identical model output — *For
    any* model response text (valid Coordinate_Guidance, malformed JSON,
    unknown class, out-of-bounds or degenerate geometry, over-limit detection
    counts, empty detections) and any modality, Label_Set and image
    dimensions, the Preview_API's outcome SHALL equal the Auto_Labeler's
    outcome for that response: the same converted Pre_Label on success, or the
    same failure reason string on rejection.

    **Validates: Requirements 3.2, 3.11, 9.3**
    """
    patcher = _EnvPatcher()
    try:
        stub = _stub_for_outcome(case.outcome, case)
        env = IdentityEnv(aws_stack, worker, dda, prelabel, patcher, stub,
                          image_limit=case.image_limit)
        env.seed_target(case.extension, case.width, case.height)
        env.seed_examples(case.good_count, case.bad_count)

        worker_outcome = _worker_outcome(env, worker, case)
        preview_outcome = _preview_outcome(env, dda, case)

        # One invocation per path whatever the outcome (Req 3.1).
        assert len(stub.calls) == 2

        if case.outcome in SUCCESS_OUTCOMES:
            assert worker_outcome.failed is False, worker_outcome.reason
            assert preview_outcome.failed is False, preview_outcome.reason
            # Req 3.2: the same parsing, validation and conversion rules, so
            # the same Pre_Label document.
            assert preview_outcome.prelabel == worker_outcome.prelabel
            return

        assert worker_outcome.failed is True, (
            f"{case.outcome} must fail at labeling time, got "
            f"{worker_outcome.prelabel!r}")
        assert preview_outcome.failed is True, (
            f"{case.outcome} must fail in the preview, got "
            f"{preview_outcome.prelabel!r}")

        # Req 3.11: the reason the Auto_Labeler records — the string that
        # reaches `prelabel_error` — is the reason the preview reports.
        assert preview_outcome.reason == worker_outcome.reason
        # And it is the shared module's reason on both sides, so neither path
        # rewrote it on the way out (Req 3.10, 3.11).
        assert worker_outcome.category is not None, (
            "the worker's GenerationFailure must carry the shared "
            "LlmPrelabelError as its cause")
        assert preview_outcome.category == worker_outcome.category
        # Req 9.3: the raw model text is carried verbatim, and only when a
        # response was actually received.
        assert preview_outcome.raw_model_output == worker_outcome.raw_text
        if case.outcome in (OUTCOME_TIMEOUT, OUTCOME_MODEL_ERROR,
                            OUTCOME_TEXTLESS):
            assert worker_outcome.raw_text is None
        else:
            assert worker_outcome.raw_text is not None
    finally:
        patcher.undo()


def _worker_outcome(env, worker, case):
    """The Auto_Labeler's outcome: the Pre_Label, or the `GenerationFailure`
    reason together with the shared `LlmPrelabelError` it was translated
    from."""
    try:
        return SimpleNamespace(failed=False, prelabel=env.worker_prelabel(case),
                               reason=None, category=None, raw_text=None)
    except worker.GenerationFailure as failure:
        cause = failure.__cause__
        return SimpleNamespace(
            failed=True, prelabel=None, reason=str(failure),
            category=getattr(cause, "category", None),
            raw_text=getattr(cause, "raw_text", None))


def _preview_outcome(env, dda, case):
    """The Preview_API's outcome: the Pre_Label, or the categorized failure
    exactly as the executor writes it into the result payload."""
    try:
        return SimpleNamespace(failed=False,
                               prelabel=env.preview_prelabel(case),
                               reason=None, category=None,
                               raw_model_output=None)
    except dda.PreviewSampleFailure as failure:
        payload = dda._preview_failure_payload(
            env.image_key, failure.category, failure.reason,
            raw_model_output=failure.raw_model_output)
        return SimpleNamespace(
            failed=True, prelabel=None, reason=payload["failure_reason"],
            category=payload["failure_category"],
            raw_model_output=payload.get("raw_model_output"))


# =========================================================================== #
# Property 5 (llm-model-token-and-image-sizing)
# =========================================================================== #

def declared(image_bytes):
    """(width, height) parsed from the container header, for assertion
    diagnostics."""
    from dda_llm_image import declared_dimensions
    return declared_dimensions(image_bytes)


def summarize(downscale_records):
    """One diagnostic tuple per recorded `downscale_image` call."""
    return [(record.format, record.setting, record.source_dimensions,
             declared(record.bytes)) for record in downscale_records]


def _spy_on_downscale(patcher, prelabel):
    """Wrap the chokepoint's `downscale_image` binding with a recorder that
    delegates to the real Image_Downscaler (test_dda_llm_prelabel's
    `spy_downscale` convention, patched through `_Patcher` because
    Hypothesis cannot consume the function-scoped monkeypatch)."""
    real = prelabel.downscale_image
    calls = []

    def recording(image_bytes, image_format, downscale_setting, **kwargs):
        calls.append(SimpleNamespace(
            bytes=image_bytes, format=image_format,
            setting=downscale_setting,
            source_dimensions=kwargs.get("source_dimensions")))
        return real(image_bytes, image_format, downscale_setting, **kwargs)

    patcher.setattr(prelabel, "downscale_image", recording)
    return calls


def _sizing_reply(case):
    """A Coordinate_Guidance document valid in **Sent** space for every
    drawn dimension pair: the smallest sent edge the sizing generators can
    produce is 7 px (a 40x2600 source at the 512 bound), so a 2x2 box at
    the origin is always within bounds."""
    return guidance([{
        "class": case.label_set[0],
        "box": {"left": 0, "top": 0, "width": 2, "height": 2},
    }])


@settings(max_examples=100, deadline=None)
@given(case=_identity_cases(sizing=True))
def test_property_preview_and_worker_stay_byte_identical_under_downscaling(
        aws_stack, worker, dda, prelabel, settings_table, case):
    """Feature: llm-model-token-and-image-sizing, Property 5: Preview and
    Auto_Labeler requests stay byte-identical under downscaling — *For any*
    Labeling_Modality, Label_Set, Detection_Prompt, per-label prompt map,
    source image bytes, Few_Shot_Example set, Downscale_Setting,
    Token_Budget_Selection and `llm:` model identifier, the Converse request
    the Preview_API issues and the Converse request the Auto_Labeler issues
    SHALL be equal in every element — model id, ordered content blocks in
    the order of Requirement 8.6, image bytes and formats, prompt text, and
    inference configuration — and exactly one invocation SHALL be issued
    per image.

    **Validates: Requirements 1.4, 6.1, 6.8, 8.4, 8.6**
    """
    patcher = _EnvPatcher()
    try:
        stub = RecordingConverseClient(reply=_sizing_reply(case))
        env = IdentityEnv(aws_stack, worker, dda, prelabel, patcher, stub,
                          image_limit=case.image_limit,
                          token_limits=case.token_limits,
                          settings_table=settings_table)
        env.seed_target(case.extension, case.width, case.height, real=True)
        env.seed_examples(case.good_count, case.bad_count,
                          dimensions=case.example_dimensions)
        downscale_calls = _spy_on_downscale(patcher, prelabel)

        worker_prelabel = env.worker_prelabel(case)
        worker_downscales = list(downscale_calls)
        del downscale_calls[:]
        preview_prelabel = env.preview_prelabel(case)
        preview_downscales = list(downscale_calls)

        # Exactly one invocation per image, from each path.
        assert len(stub.calls) == 2, (
            f"expected one Converse call per path, got {len(stub.calls)}")
        worker_call, preview_call = stub.calls

        # The central claim: the whole request, element for element, under
        # the drawn Downscale_Setting and Token_Budget_Selection (Req 1.4,
        # 6.8).
        assert preview_call == worker_call, (
            "preview and Auto_Labeler requests differ\n"
            f"worker : {worker_call!r}\npreview: {preview_call!r}")
        assert set(worker_call) == {"modelId", "messages", "inferenceConfig"}
        assert worker_call["modelId"] == MODEL_ID
        assert env.recorded == [(BEDROCK_CONFIG["region"],
                                 CLAMPED_TIMEOUT)] * 2

        # Req 1.4: both requests carry the Token_Budget_Resolver's output
        # for the drawn selection and the persisted mapping — the worker
        # resolving the job record's selection, the preview re-resolving
        # the budget the start route recorded — never the
        # Global_Max_Tokens.
        expected_budget = env.expected_token_budget(case)
        assert worker_call["inferenceConfig"]["maxTokens"] == expected_budget
        assert preview_call["inferenceConfig"]["maxTokens"] == expected_budget

        # The request really is the one this configuration calls for.
        attached = env.attached_examples() if case.few_shot_enabled else []
        blocks = image_blocks(worker_call)
        assert len(blocks) == len(attached) + 1
        assert len(blocks) <= env.image_limit

        # Target image last, carrying the *downscaled* bytes of the seeded
        # object in the key-derived format — recomputed here through the
        # deterministic Image_Downscaler (Property 4), so a pass-through
        # setting must yield the seeded bytes exactly (Req 6.1, 8.6).
        target_format = ("png" if case.extension.lower() == ".png"
                         else "jpeg")
        expected_target, sent_width, sent_height = downscale_image(
            env.image_bytes, target_format, case.downscale_setting,
            source_dimensions=(case.width, case.height))
        assert blocks[-1]["image"]["source"]["bytes"] == expected_target
        assert blocks[-1]["image"]["format"] == target_format
        if (case.downscale_setting is None
                or max(case.width, case.height) <= case.downscale_setting):
            assert blocks[-1]["image"]["source"]["bytes"] == env.image_bytes

        # Each attached example carries the downscaled bytes of its seeded
        # object, in selection order and its own key-derived format
        # (Req 8.4, 8.6).
        expected_example_bytes = []
        for example in attached:
            seeded = env.example_bytes[example["ref"]]
            if case.downscale_setting is None:
                expected_example_bytes.append(seeded)
            else:
                resized, _w, _h = downscale_image(
                    seeded, image_format_for_key(example["ref"]),
                    case.downscale_setting)
                expected_example_bytes.append(resized)
        actual_example_bytes = [block["image"]["source"]["bytes"]
                                for block in blocks[:-1]]
        assert actual_example_bytes == expected_example_bytes, (
            f"attached example bytes differ from the downscaled seeds\n"
            f"actual   dims: {[declared(b) for b in actual_example_bytes]}\n"
            f"expected dims: {[declared(b) for b in expected_example_bytes]}\n"
            f"worker downscales : {summarize(worker_downscales)}\n"
            f"preview downscales: {summarize(preview_downscales)}")
        assert [block["image"]["format"] for block in blocks[:-1]] == [
            image_format_for_key(example["ref"]) for example in attached]

        # The block sequence is the Requirement 8.6 order for every
        # setting: header, each example identified then attached, the
        # target intro, the target image, then the prompt.
        content = worker_call["messages"][0]["content"]
        texts = text_blocks(worker_call)
        assert case.detection_prompt in texts[-1]
        # The prompt names the dimensions of the image actually sent.
        assert (f"The image is {sent_width} pixels wide and "
                f"{sent_height} pixels tall") in texts[-1]
        if attached:
            assert len(content) == 2 * len(attached) + 4
            ordinals = {}
            expected_labels = []
            for example in attached:
                designation = example["designation"]
                ordinals[designation] = ordinals.get(designation, 0) + 1
                expected_labels.append(few_shot_identification_text(
                    designation, ordinals[designation]))
            assert texts[0] == FEW_SHOT_HEADER
            assert texts[1:-2] == expected_labels
            assert texts[-2] == FEW_SHOT_TARGET_INTRO
            for index in range(len(attached)):
                assert content[2 * index + 1] == {
                    "text": expected_labels[index]}
                assert "image" in content[2 * index + 2]
        else:
            assert len(content) == 2
            assert FEW_SHOT_HEADER not in texts
            assert FEW_SHOT_TARGET_INTRO not in texts
        assert "image" in content[-2]
        assert content[-2] == blocks[-1]

        # The chokepoint's downscaler binding ran exactly once per image
        # block on each path — the target first (with the caller's
        # Source_Dimensions), then each attached example — and never at
        # Downscale_Off (Req 6.1, 8.1).
        for path_name, path_downscales in (("worker", worker_downscales),
                                           ("preview", preview_downscales)):
            if case.downscale_setting is None:
                assert path_downscales == [], path_name
                continue
            assert len(path_downscales) == len(blocks), (
                f"{path_name} path: expected one downscale per image block\n"
                f"calls: {summarize(path_downscales)}")
            assert all(call.setting == case.downscale_setting
                       for call in path_downscales)
            assert path_downscales[0].bytes == env.image_bytes
            assert path_downscales[0].source_dimensions == (case.width,
                                                            case.height)
            assert [call.bytes for call in path_downscales[1:]] == [
                env.example_bytes[example["ref"]] for example in attached]

        # The executor reports the same Sent_Dimensions the request
        # embodies, and the same shared conversion yields the same
        # Pre_Label on both paths.
        assert env.preview_sent_dimensions == (sent_width, sent_height)
        assert preview_prelabel == worker_prelabel
    finally:
        patcher.undo()


# =========================================================================== #
# The known seam difference, pinned by example
# =========================================================================== #

def test_bare_example_reference_spelling_resolves_to_different_buckets(
        aws_stack, worker, dda, prelabel):
    """A **bare** example reference is resolved against different buckets by
    the two paths — recorded here rather than hidden.

    `dda_autolabel_worker._few_shot_ref_location` resolves a non-`s3://` ref
    against `PORTAL_ARTIFACTS_BUCKET`; `dda_labeling._resolve_sample_reference`
    resolves it against the Use_Case dataset bucket. The two namespaces are
    disjoint by design — the preview's request validation *requires* every
    example ref to resolve inside the Use_Case data bucket, while a bare ref
    on a job record is an artifacts-bucket key — and the wizard stores full
    `s3://…` URIs for both paths, so no configuration the system produces
    reaches this divergence. The `s3://` half of this test is the spelling
    that is actually used, and it agrees.

    Requirements 6.6, 7.6 (documented boundary of the identity claim).
    """
    case = SimpleNamespace(
        modality="ObjectDetection", label_set=["scratch"],
        per_label_prompts=None, width=100, height=80, extension=".png",
        detection_prompt=AWKWARD_PROMPT, few_shot_enabled=True,
        good_count=1, bad_count=0, image_limit=None)

    # --- bare spelling: the same key in two buckets, different bytes.
    patcher = _EnvPatcher()
    try:
        stub = RecordingConverseClient(reply=_valid_reply(case))
        env = IdentityEnv(aws_stack, worker, dda, prelabel, patcher, stub)
        env.seed_target(case.extension, case.width, case.height)
        env.seed_examples(1, 0, bucket=ARTIFACTS_BUCKET, bare=True)
        artifacts_bytes = dict(env.example_bytes)
        bare_ref = env.examples[0]["ref"]
        # The same bare key in the dataset bucket, with different content.
        dataset_example = jpeg_bytes(31, 29)
        env.s3.put_object(Bucket=DATASET_BUCKET, Key=bare_ref,
                          Body=dataset_example)

        env.worker_prelabel(case)
        env.preview_prelabel(case)

        worker_call, preview_call = stub.calls
        assert preview_call != worker_call
        # The worker read the artifacts-bucket object...
        assert (image_blocks(worker_call)[0]["image"]["source"]["bytes"]
                == artifacts_bytes[bare_ref])
        # ...the preview read the dataset-bucket object of the same key.
        assert (image_blocks(preview_call)[0]["image"]["source"]["bytes"]
                == dataset_example)
        # Nothing else differs: same model id, same inference config, same
        # prompt, same target image.
        assert worker_call["modelId"] == preview_call["modelId"]
        assert worker_call["inferenceConfig"] == preview_call["inferenceConfig"]
        assert text_blocks(worker_call) == text_blocks(preview_call)
        assert (image_blocks(worker_call)[-1] == image_blocks(preview_call)[-1])
    finally:
        patcher.undo()

    # --- s3:// spelling of the very same object: the two paths agree.
    patcher = _EnvPatcher()
    try:
        stub = RecordingConverseClient(reply=_valid_reply(case))
        env = IdentityEnv(aws_stack, worker, dda, prelabel, patcher, stub)
        env.seed_target(case.extension, case.width, case.height)
        env.seed_examples(1, 0, bucket=ARTIFACTS_BUCKET, bare=False)

        env.worker_prelabel(case)
        env.preview_prelabel(case)

        worker_call, preview_call = stub.calls
        assert preview_call == worker_call
    finally:
        patcher.undo()
