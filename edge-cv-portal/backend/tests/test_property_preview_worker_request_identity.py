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

from dda_llm_request import (
    FEW_SHOT_HEADER,
    FEW_SHOT_TARGET_INTRO,
    MODEL_IMAGE_LIMIT_DEFAULT,
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
                 image_limit=None):
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

    # ------------------------------------------------------------- seams
    def _client_factory(self, region, timeout_seconds):
        self.recorded.append((region, timeout_seconds))
        return self.stub

    # ------------------------------------------------------------- setup
    def seed_target(self, extension, width, height):
        """The one object both paths label."""
        self.image_key = f"datasets/{uuid.uuid4().hex[:8]}/target{extension}"
        self.image_bytes = (png_bytes(width, height)
                            if extension.lower() == ".png"
                            else jpeg_bytes(width, height))
        self.s3.put_object(Bucket=DATASET_BUCKET, Key=self.image_key,
                           Body=self.image_bytes)
        self.image_uri = f"s3://{DATASET_BUCKET}/{self.image_key}"
        return self.image_key

    def seed_examples(self, good_count, bad_count, bucket=DATASET_BUCKET,
                      bare=False):
        """Stored Few_Shot_Example references, good then bad in stored order.

        Every example gets distinct bytes (distinct pixel dimensions), so a
        request that attached the same *count* of examples in a different
        order, or attached a different subset, cannot pass the comparison.
        References are full `s3://bucket/key` URIs — the spelling the wizard
        stores — unless `bare` is set, which the seam-difference test uses.
        """
        examples = []
        self.example_bytes = {}
        base = f"labeling-examples/{uuid.uuid4().hex[:8]}"
        for designation, count in ((GOOD, good_count), (BAD, bad_count)):
            for position in range(count):
                extension = "png" if (position % 2 == 0) else "jpg"
                key = f"{base}/{designation}/{position}.{extension}"
                body = (png_bytes(11 + position, 7 + position)
                        if extension == "png"
                        else jpeg_bytes(13 + position, 9 + position))
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
        return {
            "job_id": f"labeling-{uuid.uuid4().hex[:8]}",
            "usecase_id": self.usecase_id,
            "task_type": case.modality,
            "label_set": list(case.label_set),
            "skip_verification": False,
            "per_label_prompts": case.per_label_prompts or {},
            "auto_label": {
                "enabled": True,
                "model": MODEL,
                "detection_prompt": case.detection_prompt,
                "few_shot": self._few_shot_document(case),
            },
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
        additively under `few_shot_examples`."""
        return {
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

    def worker_prelabel(self, case):
        """`dda_autolabel_worker._generate_llm_prelabel` — the labeling-time
        entry point, with the S3 read, the dimension gate, prompt resolution
        and few-shot resolution it owns."""
        job = self.job_document(case)
        return self.worker._generate_llm_prelabel(
            self.sqs_message(case, job), job, MODEL_ID)

    def preview_prelabel(self, case):
        """`dda_labeling._run_preview_sample` — the Preview_API's per-sample
        body, with its own S3 read, dimension gate and few-shot resolution."""
        prelabel, _width, _height = self.dda._run_preview_sample(
            self.run_document(case), {}, self.usecase, DATASET_BUCKET,
            self.image_key)
        return prelabel

    # -------------------------------------------------------- assertions
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


@st.composite
def _identity_cases(draw):
    """A full `llm:` job configuration: modality, Label_Set, dimensions, key
    extension, Detection_Prompt, few-shot state and Model_Image_Limit.

    The example counts and limits are drawn together so the generated space
    includes limits that attach everything, limits that force omission, and
    `limit == 1` which attaches nothing at all (Req 7.2, 7.4).
    """
    modality = draw(st.sampled_from(MODALITIES))
    label_set = (list(CLASSIFICATION_LABELS) if modality == "Classification"
                 else draw(st.lists(st.sampled_from(LABEL_POOL), min_size=1,
                                    max_size=3, unique=True)))
    per_label_prompts = draw(st.one_of(
        st.none(),
        st.just({label: f"guidance for {label}" for label in label_set})))
    return SimpleNamespace(
        modality=modality,
        label_set=label_set,
        per_label_prompts=per_label_prompts,
        width=draw(st.integers(min_value=40, max_value=400)),
        height=draw(st.integers(min_value=40, max_value=400)),
        extension=draw(st.sampled_from([".png", ".jpg", ".jpeg", ".JPG"])),
        detection_prompt=draw(_prompt_text),
        few_shot_enabled=draw(st.booleans()),
        good_count=draw(st.integers(min_value=0, max_value=4)),
        bad_count=draw(st.integers(min_value=0, max_value=4)),
        # None means "no configured limit": both paths resolve the shared
        # default of 20.
        image_limit=draw(st.one_of(st.none(),
                                   st.integers(min_value=1, max_value=6))),
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
