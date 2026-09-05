"""
Preview_Run executor outcome property tests (llm-autolabel-prompt-tuning,
tasks 9.2, 9.3, 9.4, 9.5).

Four properties, one property-based test each, all driving the real
`dda_labeling` start route *and* `dda_labeling.execute_preview_run`
against the moto-backed stack from conftest.py (real shared_utils /
rbac_middleware, real DynamoDB tables, the real portal artifacts bucket,
fake Cognito and Lambda clients per the `test_dda_labeling_create_job.py`
conventions, the `PreviewEnv` shape `test_dda_labeling_preview_routes.py`
and `test_property_preview_api_guards.py` established):

**Property 9: Every Sample_Image yields exactly one categorized outcome,
independently** — Validates Requirements 3.5, 3.7, 3.9, 6.8, 9.1, 9.2,
9.4, 9.5, 9.6
**Property 10: An unreadable example image fails only its own target
image** — Validates Requirements 6.7, 6.8
**Property 11: A Preview_Run produces no labeling-pipeline state** —
Validates Requirements 1.6, 3.5
**Property 12: Model requests carry only image and prompt content** —
Validates Requirement 3.4

Only two seams are stubbed, both of them the boundaries the executor
itself names:

- `dda_labeling.get_s3_client_for_bucket` returns a *scripted wrapper
  around the real moto S3 client*, so every read is a real read and the
  only synthetic behavior is "this one example object is unreadable while
  this one Sample_Image is being processed" — which is what makes
  per-sample example isolation (Property 10) expressible at all, since a
  run's Few_Shot_Example set is shared by every Sample_Image.
- `dda_labeling.get_bedrock_client` returns a stub Converse client. The
  preview rebinds its own binding onto `dda_llm_prelabel`, so patching
  `dda_labeling.get_bedrock_client` is what reaches the invocation, and
  the stub records every request it is handed — which is the capture
  Property 12 inspects.

Everything else is real: DynamoDB run/sample/lock items, the artifacts
bucket payload objects, the audit log, `select_few_shot_examples`,
`build_llm_request`, `parse_guidance` and `guidance_to_prelabel`.

Hypothesis cannot consume function-scoped fixtures, so the module-scoped
`dda` fixture is combined with a per-example `PreviewRunEnv` built inside
the test body, reusing the `_Patcher` monkeypatch stand-in from
`test_property_llm_autolabel_preservation.py`.
"""
import json
import os
import string
import struct
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, ReadTimeoutError
from hypothesis import given, settings
from hypothesis import strategies as st

from test_dda_labeling_create_job import (
    DATASET_BUCKET,
    FakeCognitoClient,
    FakeLambdaClient,
    POOL_ID,
    REGION,
)
from test_property_llm_autolabel_preservation import _Patcher

ARTIFACTS_BUCKET = "test-portal-artifacts"
FUNCTION_NAME = "test-dda-labeling-handler"
WORKER_FUNCTION_NAME = "test-dda-labeling-worker"
LLM_MODEL = "llm:us.amazon.nova-pro-v1:0"
MODEL_IDENTIFIER = "us.amazon.nova-pro-v1:0"

# Sample_Image pixel dimensions. Fixed so the generated guidance geometry
# is always in bounds; the point of the properties is the outcome
# taxonomy, not coordinate validation (which dda_llm_guidance owns).
IMAGE_WIDTH = 64
IMAGE_HEIGHT = 48

MODALITIES = ("Classification", "Segmentation", "ObjectDetection")
GEOMETRY_LABEL_SET = ["scratch", "dent"]
# Raised by the stub Converse client for the `model_error` condition.
MODEL_ERROR_MESSAGE = "ServiceUnavailableException: bedrock is unavailable"
# Returned by the stub Converse client for the `unusable_output`
# condition: no parseable JSON at all, so parse_guidance rejects it and
# the raw text must come back character-for-character (Req 9.3).
UNUSABLE_MODEL_TEXT = "I am not able to label this image."

# One per-sample condition per row of the executor's category table.
# `location_unresolvable` is a whole-run condition and is generated
# separately.
SAMPLE_CONDITIONS = (
    "ok",
    "empty_detections",
    "unreadable_object",
    "undecodable_dimensions",
    "unreadable_example",
    "timeout",
    "model_error",
    "unusable_output",
)
# Conditions that resolve before any Converse request is issued
# (Req 3.9, 6.8, 9.4, 9.5).
PRE_INVOCATION_CONDITIONS = frozenset({
    "unreadable_object", "undecodable_dimensions", "unreadable_example",
})
SUCCESS_CONDITIONS = frozenset({"ok", "empty_detections"})
# The category each condition must be attributed, exactly one each
# (Req 9.6).
EXPECTED_CATEGORY = {
    "unreadable_object": "image_access_failure",
    "undecodable_dimensions": "unsupported_image_content",
    "unreadable_example": "unreadable_example_image",
    "timeout": "timeout",
    "model_error": "model_error",
    "unusable_output": "unusable_model_output",
}

# Nothing in a model request may name infrastructure or carry a secret
# (Req 3.4). The credential values are read from the environment the
# moto stack runs under, so a request that leaked the caller's own
# credentials would be caught by value, not by guesswork.
_CREDENTIAL_VALUES = tuple(sorted({
    value for value in (os.environ.get(name) for name in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN")) if value
}))
FORBIDDEN_REQUEST_SUBSTRINGS = (
    "arn:aws:",       # role ARNs
    "s3://",          # object references
    "https://",       # presigned URLs
    "http://",
    "X-Amz-",         # SigV4 query parameters
    "Signature=",
    DATASET_BUCKET,   # bucket names
    ARTIFACTS_BUCKET,
    "123456789012",   # account id
) + _CREDENTIAL_VALUES


# ------------------------------------------------------------- image bytes

def _png_bytes(width=IMAGE_WIDTH, height=IMAGE_HEIGHT, tag=0):
    """A 24-byte PNG header whose IHDR carries `width`/`height`.

    `_preview_image_dimensions` reads the dimensions straight out of the
    IHDR, so this is a genuine decodable image as far as the executor is
    concerned. `tag` varies the bytes per sample, which lets the captured
    Converse requests be traced back to the object they were built from.
    """
    return (b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\rIHDR'
            + struct.pack('>II', width, height) + bytes([tag % 256]))


# Bytes with no PNG signature and no JPEG SOI: the header decode yields
# no dimensions, so the sample fails before any invocation (Req 9.5).
UNDECODABLE_BYTES = b'GIF89a-not-an-image-the-executor-can-measure'


# ----------------------------------------------------------------- stubs

class _ScriptedS3:
    """The real moto S3 client, plus a per-sample example-read failure
    script.

    Every read is delegated, so Sample_Images and Few_Shot_Examples are
    fetched from real objects and a missing object fails exactly the way
    it fails in production. The only scripted behavior is example-read
    failure attributed to *one* Sample_Image: a Preview_Run's example set
    is shared by every sample, so this is the only way to express
    "an attached example was unreadable while this target image was being
    built" — the condition Property 10 is about.

    `state.current` tracks which Sample_Image the executor is working on.
    The executor reads the target image first and the examples after, so
    observing a target-key read is enough to attribute the reads that
    follow.
    """

    def __init__(self, real, state):
        self._real = real
        self.state = state
        self.reads = []

    def get_object(self, Bucket=None, Key=None, **kwargs):
        self.reads.append((Bucket, Key))
        if Key in self.state.sample_index:
            self.state.current = self.state.sample_index[Key]
        elif Key in self.state.example_failures.get(self.state.current, ()):
            raise ClientError(
                {"Error": {"Code": "AccessDenied",
                           "Message": "Access Denied"}},
                "GetObject")
        return self._real.get_object(Bucket=Bucket, Key=Key, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _StubBedrock:
    """A stub Converse client that records every request and answers
    according to the current Sample_Image's condition.

    Recording is keyed by sample index (from the same `state.current` the
    scripted S3 client maintains), which is what lets the properties
    assert *per sample* that exactly one invocation happened, or none.
    """

    def __init__(self, state):
        self.state = state
        self.calls = []

    def converse(self, **kwargs):
        index = self.state.current
        self.calls.append((index, kwargs))
        condition = self.state.conditions.get(index, "ok")
        if condition == "timeout":
            raise ReadTimeoutError(endpoint_url="https://bedrock-runtime")
        if condition == "model_error":
            raise RuntimeError(MODEL_ERROR_MESSAGE)
        return {"output": {"message": {"content": [
            {"text": self.state.model_text[index]}]}}}

    def calls_for(self, index):
        return [kwargs for called_index, kwargs in self.calls
                if called_index == index]


# -------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, with
    fake Cognito and Lambda clients.

    `DDA_LABELING_WORKER_FUNCTION_NAME` is set for the life of the
    module so that a labeler notification invoke, if the executor ever
    attempted one, would actually be issued and recorded — otherwise
    Property 11's "no notification" assertion would pass vacuously
    through `_invoke_labeling_worker`'s unset-env guard.
    """
    sys.modules.pop("dda_labeling", None)
    import dda_labeling
    import dda_llm_prelabel

    fake_cognito = FakeCognitoClient()
    dda_labeling.cognito_client = fake_cognito
    dda_labeling.USER_POOL_ID = POOL_ID

    fake_lambda = FakeLambdaClient()
    dda_labeling.lambda_client = fake_lambda

    try:
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=DATASET_BUCKET)
    except ClientError:
        pass  # a sibling module already created the shared dataset bucket

    saved_worker = os.environ.get("DDA_LABELING_WORKER_FUNCTION_NAME")
    os.environ["DDA_LABELING_WORKER_FUNCTION_NAME"] = WORKER_FUNCTION_NAME
    try:
        yield SimpleNamespace(module=dda_labeling, cognito=fake_cognito,
                              lambda_client=fake_lambda,
                              prelabel=dda_llm_prelabel)
    finally:
        if saved_worker is None:
            os.environ.pop("DDA_LABELING_WORKER_FUNCTION_NAME", None)
        else:
            os.environ["DDA_LABELING_WORKER_FUNCTION_NAME"] = saved_worker


class PreviewRunEnv:
    """Per-example facade: a fresh Use_Case with a dataset prefix and an
    authorized Job_Creator, real seeded objects, the scripted S3 client
    and the stub Converse client, plus readers for every piece of state a
    run touches."""

    def __init__(self, stack, dda, patcher):
        self.stack = stack
        self.dda = dda
        self.module = dda.module
        self.tasks = stack.tables.labeling_tasks
        self.real_s3 = boto3.client("s3", region_name=REGION)
        self.context = SimpleNamespace(function_name=FUNCTION_NAME)

        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.usecase_item = {
            "usecase_id": self.usecase_id,
            "name": "Preview Outcome Test",
            "account_id": "123456789012",
            # Single-account: a root role ARN makes
            # get_s3_client_for_bucket take its direct-access fallback.
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        }
        stack.tables.usecases.put_item(Item=self.usecase_item)
        self.creator = self.make_user("DataScientist")
        self.prefix = f"datasets/{uuid.uuid4().hex[:8]}/"
        self.example_prefix = f"labeling-examples/{uuid.uuid4().hex[:8]}/"

        self.state = SimpleNamespace(
            sample_index={},       # sample object key -> request index
            current=None,          # index of the sample being processed
            example_failures={},   # request index -> unreadable example keys
            conditions={},         # request index -> generated condition
            model_text={},         # request index -> model response text
        )
        self.s3 = _ScriptedS3(self.real_s3, self.state)
        self.bedrock = _StubBedrock(self.state)
        self.image_bytes = {}      # object key -> the bytes seeded there

        patcher.setattr(self.module, "get_s3_client_for_bucket",
                        self._client_for_bucket)
        patcher.setattr(self.module, "get_bedrock_client",
                        self._bedrock_client)
        # The executor rebinds this on every sample; restoring it keeps
        # the shared module clean for other test files in the session.
        patcher.setattr(dda.prelabel, "get_bedrock_client",
                        self._bedrock_client)

        self.dda.lambda_client.invocations.clear()

    # -------------------------------------------------------- the seams
    def _client_for_bucket(self, usecase, bucket, session_name=None):
        return self.s3

    def _bedrock_client(self, region=None, timeout_seconds=None):
        return self.bedrock

    # ----------------------------------------------------------- setup
    def make_user(self, role="DataScientist"):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def sample_key(self, index):
        return f"{self.prefix}sample-{index:02d}.png"

    def seed_sample(self, index, condition):
        """Seed the Sample_Image object its condition calls for and
        register the model response its condition calls for."""
        key = self.sample_key(index)
        self.state.sample_index[key] = index
        self.state.conditions[index] = condition
        if condition == "unreadable_object":
            # Deliberately never created: the read must fail (Req 9.4).
            return key
        body = (UNDECODABLE_BYTES if condition == "undecodable_dimensions"
                else _png_bytes(tag=index))
        self.real_s3.put_object(Bucket=DATASET_BUCKET, Key=key, Body=body)
        self.image_bytes[key] = body
        return key

    def example_ref(self, position, designation):
        suffix = "png" if designation == "good" else "jpg"
        return f"{self.example_prefix}{designation}-{position}.{suffix}"

    def seed_examples(self, good=1, bad=0):
        """Seed the Few_Shot_Example objects and return the request's
        `few_shot` document in stored order (good first, then bad)."""
        examples = []
        for position in range(good):
            examples.append({"ref": self.example_ref(position, "good"),
                             "designation": "good", "position": position})
        for position in range(bad):
            examples.append({"ref": self.example_ref(position, "bad"),
                             "designation": "bad", "position": position})
        for offset, example in enumerate(examples):
            body = _png_bytes(tag=200 + offset)
            self.real_s3.put_object(Bucket=DATASET_BUCKET,
                                    Key=example["ref"], Body=body)
            self.image_bytes[example["ref"]] = body
        return {"enabled": True, "examples": examples}

    def fail_example_for(self, index, ref):
        """Make one attached example unreadable while the Sample_Image at
        `index` is being processed."""
        self.state.example_failures.setdefault(index, set()).add(ref)

    # ---------------------------------------------------------- invoke
    def _claims(self, user):
        return {"requestContext": {"authorizer": {"claims": {
            "sub": user["user_id"],
            "email": user["email"],
            "cognito:username": user["username"],
            "custom:role": user["role"],
        }}}}

    def start(self, sample_keys, modality="ObjectDetection",
              label_set=None, detection_prompt="Find every scratch.",
              few_shot=None, user=None):
        """`(status, body)` for POST /labeling-preview/runs."""
        body = {
            "usecase_id": self.usecase_id,
            "dataset_prefix": self.prefix,
            "model": LLM_MODEL,
            "detection_prompt": detection_prompt,
            "task_type": modality,
            "sample_images": list(sample_keys),
        }
        if modality != "Classification":
            body["label_set"] = list(label_set or GEOMETRY_LABEL_SET)
        if few_shot is not None:
            body["few_shot"] = few_shot
        event = {
            "httpMethod": "POST",
            "resource": "/labeling-preview/runs",
            "path": "/v1/labeling-preview/runs",
            "pathParameters": None,
            "queryStringParameters": None,
            "body": json.dumps(body),
        }
        event.update(self._claims(user or self.creator))
        response = self.module.handler(event, self.context)
        return response["statusCode"], json.loads(response["body"])

    def execute(self, run_id):
        self.state.current = None
        return self.module.execute_preview_run(run_id)

    def status(self, run_id, user=None):
        """`(status, body)` for GET /labeling-preview/runs/{runId}."""
        event = {
            "httpMethod": "GET",
            "resource": "/labeling-preview/runs/{runId}",
            "path": f"/v1/labeling-preview/runs/{run_id}",
            "pathParameters": {"runId": run_id},
            "queryStringParameters": None,
            "body": None,
        }
        event.update(self._claims(user or self.creator))
        response = self.module.handler(event, self.context)
        return response["statusCode"], json.loads(response["body"])

    # ----------------------------------------------------------- store
    def run_item(self, run_id):
        return self.tasks.get_item(
            Key={"job_id": f"PREVIEW#{run_id}", "task_id": "RUN"}).get("Item")

    def sample_items(self, run_id):
        return self.module._read_preview_sample_items(run_id)

    def lock_item(self, user_sub=None):
        return self.tasks.get_item(Key={
            "job_id": f"PREVIEWLOCK#{self.usecase_id}",
            "task_id": f"USER#{user_sub or self.creator['user_id']}",
        }).get("Item")

    def payload(self, run_id, index):
        key = f"labeling-previews/{self.usecase_id}/{run_id}/{index}.json"
        body = self.real_s3.get_object(Bucket=ARTIFACTS_BUCKET,
                                       Key=key)["Body"].read()
        return json.loads(body.decode("utf-8"))

    def artifact_keys(self, prefix=""):
        keys, token = set(), None
        while True:
            kwargs = {"Bucket": ARTIFACTS_BUCKET, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = self.real_s3.list_objects_v2(**kwargs)
            keys.update(item["Key"] for item in response.get("Contents", []))
            if not response.get("IsTruncated"):
                return keys
            token = response.get("NextContinuationToken")

    def task_keys(self):
        keys, kwargs = set(), {}
        while True:
            response = self.tasks.scan(**kwargs)
            for item in response.get("Items", []):
                keys.add((item["job_id"], item["task_id"]))
            if not response.get("LastEvaluatedKey"):
                return keys
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def job_ids(self):
        ids, kwargs = set(), {}
        while True:
            response = self.stack.tables.labeling_jobs.scan(**kwargs)
            for item in response.get("Items", []):
                ids.add(item["job_id"])
            if not response.get("LastEvaluatedKey"):
                return ids
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def assignee_index_items(self, user_sub):
        return self.tasks.query(
            IndexName="assignee-index",
            KeyConditionExpression=Key("assignee_user_id").eq(user_sub),
        ).get("Items", [])

    def drop_usecase(self):
        self.stack.tables.usecases.delete_item(
            Key={"usecase_id": self.usecase_id})

    def restore_usecase(self):
        self.stack.tables.usecases.put_item(Item=self.usecase_item)


# ------------------------------------------------------------ guidance

def _guidance_text(modality, label_set, empty=False):
    """Coordinate_Guidance the shared parser accepts: one in-bounds box,
    or an explicitly empty detection list (a valid empty result)."""
    if empty:
        return json.dumps({"detections": []})
    return json.dumps({"detections": [{
        "class": label_set[0],
        "box": {"left": 1, "top": 1, "width": 10, "height": 10},
    }]})


def _label_set_for(modality):
    # Classification carries the fixed binary Label_Set the route
    # substitutes, so the generated guidance must use its class names.
    return (["normal", "anomaly"] if modality == "Classification"
            else list(GEOMETRY_LABEL_SET))


def _register_model_text(env, index, condition, modality, label_set):
    if condition == "unusable_output":
        env.state.model_text[index] = UNUSABLE_MODEL_TEXT
    else:
        env.state.model_text[index] = _guidance_text(
            modality, label_set, empty=(condition == "empty_detections"))


# ---------------------------------------------------------- generators

# Prompt / label text from a restricted alphabet: Property 12 asserts no
# infrastructure identifier appears anywhere in a request, so the
# generated Detection_Prompt must not be able to spell one by accident.
_prompt_text = st.text(
    alphabet=string.ascii_letters + string.digits + " .,;-",
    min_size=1, max_size=120).filter(lambda text: text.strip())
_class_name = st.text(alphabet=string.ascii_lowercase + "-",
                      min_size=1, max_size=12).filter(
    lambda name: name.strip())


@st.composite
def _outcome_specs(draw):
    """A Preview_Run over 1 to 5 Sample_Images with an arbitrary mix of
    per-sample conditions, plus the whole-run condition that makes the
    dataset location unresolvable."""
    sample_count = draw(st.integers(min_value=1, max_value=5))
    conditions = draw(st.lists(st.sampled_from(SAMPLE_CONDITIONS),
                               min_size=sample_count,
                               max_size=sample_count))
    location_unresolvable = draw(st.booleans())
    # Few-shot is required for `unreadable_example` to be reachable, and
    # is otherwise free.
    few_shot = (True if "unreadable_example" in conditions
                else draw(st.booleans()))
    return SimpleNamespace(
        conditions=conditions,
        modality=draw(st.sampled_from(MODALITIES)),
        few_shot=few_shot,
        good=draw(st.integers(min_value=0, max_value=2)),
        bad=draw(st.integers(min_value=0, max_value=2)),
        location_unresolvable=location_unresolvable,
    )


@st.composite
def _example_isolation_specs(draw):
    """A few-shot Preview_Run over 2 to 4 Sample_Images in which a
    non-empty strict subset of the samples has an unreadable attached
    example."""
    sample_count = draw(st.integers(min_value=2, max_value=4))
    affected = draw(st.lists(st.integers(min_value=0,
                                         max_value=sample_count - 1),
                             min_size=1, max_size=sample_count - 1,
                             unique=True))
    good = draw(st.integers(min_value=0, max_value=2))
    bad = draw(st.integers(min_value=0, max_value=2))
    if good + bad == 0:
        good = 1
    return SimpleNamespace(
        sample_count=sample_count,
        affected=sorted(affected),
        modality=draw(st.sampled_from(MODALITIES)),
        good=good,
        bad=bad,
        # Which of the attached examples becomes unreadable.
        failing_offset=draw(st.integers(min_value=0, max_value=3)),
    )


@st.composite
def _pipeline_state_specs(draw):
    """A Preview_Run whose per-sample outcomes span success and every
    failure category, so "no pipeline state" is asserted whatever the
    outcomes are."""
    sample_count = draw(st.integers(min_value=1, max_value=4))
    return SimpleNamespace(
        conditions=draw(st.lists(st.sampled_from(SAMPLE_CONDITIONS),
                                 min_size=sample_count,
                                 max_size=sample_count)),
        modality=draw(st.sampled_from(MODALITIES)),
        few_shot=draw(st.booleans()),
    )


@st.composite
def _request_content_specs(draw):
    """A successful Preview_Run whose prompt, Label_Set, modality and
    Few_Shot_Example set vary, so the captured requests cover the whole
    content layout."""
    modality = draw(st.sampled_from(MODALITIES))
    return SimpleNamespace(
        sample_count=draw(st.integers(min_value=1, max_value=3)),
        modality=modality,
        detection_prompt=draw(_prompt_text),
        label_set=(["normal", "anomaly"] if modality == "Classification"
                   else draw(st.lists(_class_name, min_size=1, max_size=3,
                                      unique=True))),
        few_shot=draw(st.booleans()),
        good=draw(st.integers(min_value=0, max_value=2)),
        bad=draw(st.integers(min_value=0, max_value=2)),
    )


# =========================================================================== #
# Property 9
# =========================================================================== #

@settings(max_examples=100, deadline=None)
@given(spec=_outcome_specs())
def test_property_every_sample_yields_one_categorized_outcome(aws_stack, dda,
                                                              spec):
    """Feature: llm-autolabel-prompt-tuning, Property 9: Every
    Sample_Image yields exactly one categorized outcome, independently —
    *For any* Preview_Run over 1 to 5 Sample_Images and *any* mix of
    per-sample conditions (unreadable object, undecodable dimensions,
    unreadable attached example, invocation timeout, model error, unusable
    output, valid guidance, empty detections), the run SHALL return
    exactly one Preview_Result per requested Sample_Image paired with that
    Sample_Image, each result SHALL be either a converted Pre_Label or a
    failure carrying exactly one category from the defined category set
    with its reason, a failure for one Sample_Image SHALL not change the
    outcome of any other Sample_Image, and samples failing before
    invocation (unreadable object, undecodable dimensions, unreadable
    example image) SHALL have had no model invoked.

    **Validates: Requirements 3.5, 3.7, 3.9, 6.8, 9.1, 9.2, 9.4, 9.5,
    9.6**
    """
    patcher = _Patcher()
    try:
        env = PreviewRunEnv(aws_stack, dda, patcher)
        label_set = _label_set_for(spec.modality)

        few_shot = None
        attached_refs = []
        if spec.few_shot:
            few_shot = env.seed_examples(good=spec.good, bad=spec.bad)
            if not few_shot["examples"]:
                few_shot = env.seed_examples(good=1, bad=0)
            # Every reference is attached: the default Model_Image_Limit
            # of 20 leaves 19 slots for at most 4 examples.
            attached_refs = [example["ref"]
                             for example in few_shot["examples"]]

        sample_keys = []
        for index, condition in enumerate(spec.conditions):
            if condition == "unreadable_example" and not attached_refs:
                # Unreachable without an attached example; fall back to a
                # plain success so the mix stays well defined.
                condition = "ok"
                spec.conditions[index] = condition
            sample_keys.append(env.seed_sample(index, condition))
            _register_model_text(env, index, condition, spec.modality,
                                 label_set)
            if condition == "unreadable_example":
                env.fail_example_for(index, attached_refs[0])

        status, body = env.start(sample_keys, modality=spec.modality,
                                 label_set=label_set, few_shot=few_shot)
        assert status == 202, body
        run_id = body["run_id"]

        # The whole-run condition: the dataset location cannot be
        # resolved, so every sample must still resolve to its own
        # categorized outcome rather than the run collapsing.
        if spec.location_unresolvable:
            env.drop_usecase()

        result = env.execute(run_id)

        # The run itself completes once every sample has an outcome, even
        # when every one of them failed (Req 3.7).
        assert result["status"] == env.module.PREVIEW_STATUS_COMPLETED, result
        assert result["sample_count"] == len(sample_keys)
        assert env.run_item(run_id)["status"] == \
            env.module.PREVIEW_STATUS_COMPLETED

        # Exactly one outcome per requested Sample_Image, in request
        # order, each paired with the Sample_Image it came from (Req 3.5).
        items = env.sample_items(run_id)
        assert [item["sample_key"] for item in items] == sample_keys

        expected_succeeded = 0
        for index, (item, condition) in enumerate(zip(items,
                                                      spec.conditions)):
            sample_key = sample_keys[index]
            payload = env.payload(run_id, index)
            assert payload["sample_key"] == sample_key
            assert item["result_s3_key"] == \
                f"labeling-previews/{env.usecase_id}/{run_id}/{index}.json"
            assert item["resolved_at"] is not None

            # An unresolvable dataset location makes every sample an
            # image access failure naming that sample, with no invocation.
            if spec.location_unresolvable:
                assert item["state"] == env.module.PREVIEW_SAMPLE_FAILED
                assert item["failure_category"] == "image_access_failure"
                assert item["failure_reason"].startswith(
                    f"image {sample_key} is not accessible: ")
                assert env.bedrock.calls_for(index) == []
                continue

            if condition in SUCCESS_CONDITIONS:
                expected_succeeded += 1
                assert item["state"] == env.module.PREVIEW_SAMPLE_SUCCEEDED
                assert "failure_category" not in item
                assert "failure_reason" not in item
                # A success payload carries the Pre_Label and the pixel
                # dimensions the prompt was built from, and nothing else.
                assert set(payload) == {"sample_key", "state", "prelabel",
                                        "image_width", "image_height"}
                assert payload["state"] == \
                    env.module.PREVIEW_SAMPLE_SUCCEEDED
                assert payload["image_width"] == IMAGE_WIDTH
                assert payload["image_height"] == IMAGE_HEIGHT
                assert payload["prelabel"]["modality"] == spec.modality
                if condition == "empty_detections":
                    # A zero-detection Pre_Label is a success, not a
                    # failure.
                    if spec.modality == "Classification":
                        assert payload["prelabel"]["label"] == "normal"
                    elif spec.modality == "Segmentation":
                        assert payload["prelabel"]["regions"] == []
                    else:
                        assert payload["prelabel"]["boxes"] == []
                assert len(env.bedrock.calls_for(index)) == 1
                continue

            # Exactly one category, from the defined set, with a reason
            # (Req 9.6).
            category = EXPECTED_CATEGORY[condition]
            assert item["state"] == env.module.PREVIEW_SAMPLE_FAILED
            assert item["failure_category"] == category
            assert category in env.module.PREVIEW_FAILURE_CATEGORIES
            assert item["failure_reason"]
            expected_keys = {"sample_key", "state", "failure_category",
                             "failure_reason"}
            if condition == "unusable_output":
                # The model's raw text comes back character-for-character,
                # and only this category can carry it (Req 9.3).
                expected_keys.add("raw_model_output")
                assert payload["raw_model_output"] == UNUSABLE_MODEL_TEXT
            assert set(payload) == expected_keys
            assert payload["failure_category"] == category
            assert payload["failure_reason"] == item["failure_reason"]

            # The reason identifies the cause, per the executor's category
            # table (Req 9.1, 9.2, 9.4, 9.5, 6.8).
            if condition == "unreadable_object":
                assert item["failure_reason"].startswith(
                    f"image s3://{DATASET_BUCKET}/{sample_key} is not "
                    f"accessible: ")
            elif condition == "undecodable_dimensions":
                assert item["failure_reason"] == \
                    env.module.PREVIEW_UNSUPPORTED_IMAGE_REASON
            elif condition == "unreadable_example":
                assert item["failure_reason"].startswith(
                    f"few-shot example image {attached_refs[0]} is not "
                    f"accessible: ")
            elif condition == "timeout":
                assert item["failure_reason"] == \
                    "model invocation timed out after 120s"
            elif condition == "model_error":
                assert item["failure_reason"] == \
                    f"model error: {MODEL_ERROR_MESSAGE}"

            # Pre-invocation failures invoked no model; invocation
            # failures invoked it exactly once, never twice (Req 3.9,
            # 6.8, 9.4, 9.5).
            expected_calls = 0 if condition in PRE_INVOCATION_CONDITIONS else 1
            assert len(env.bedrock.calls_for(index)) == expected_calls

        # The tallies agree with the per-sample outcomes, so no sample was
        # counted twice and none was skipped.
        assert result["succeeded"] == expected_succeeded
        assert result["failed"] == len(sample_keys) - expected_succeeded

        # No sample was invoked for more than its own condition allows.
        assert len(env.bedrock.calls) == sum(
            0 if (spec.location_unresolvable
                  or condition in PRE_INVOCATION_CONDITIONS) else 1
            for condition in spec.conditions)

        # The claim is released on the terminal path, whatever the
        # outcomes (Req 8.8).
        assert env.lock_item() is None

        # The status route reports the same one-entry-per-Sample_Image
        # set, in request order (Req 3.5, 9.6).
        if spec.location_unresolvable:
            env.restore_usecase()
        status_code, status_body = env.status(run_id)
        assert status_code == 200, status_body
        assert status_body["status"] == env.module.PREVIEW_STATUS_COMPLETED
        assert [entry["sample_key"] for entry in status_body["results"]] == \
            sample_keys
        for entry, item in zip(status_body["results"], items):
            assert entry["state"] == item["state"]
            assert entry.get("failure_category") == \
                item.get("failure_category")
    finally:
        patcher.undo()


# =========================================================================== #
# Property 10
# =========================================================================== #

@settings(max_examples=100, deadline=None)
@given(spec=_example_isolation_specs())
def test_property_unreadable_example_fails_only_its_own_target(aws_stack, dda,
                                                              spec):
    """Feature: llm-autolabel-prompt-tuning, Property 10: An unreadable
    example image fails only its own target image — *For any* Labeling_Job
    or Preview_Run with the Few_Shot_Option enabled in which an attached
    example image is unreadable, the affected target image (dataset image
    or Sample_Image) SHALL fail with a reason identifying that example
    image, and every other image of the job or run SHALL be processed and
    resolved as it would have been.

    "As it would have been" is asserted against a control run: the same
    Use_Case, samples, prompt, modality and example set with no example
    read failing at all. Every unaffected sample's payload and captured
    model request must match the control's for the same Sample_Image,
    which is a stronger statement than "it succeeded".

    **Validates: Requirements 6.7, 6.8**
    """
    patcher = _Patcher()
    try:
        env = PreviewRunEnv(aws_stack, dda, patcher)
        label_set = _label_set_for(spec.modality)
        few_shot = env.seed_examples(good=spec.good, bad=spec.bad)
        attached_refs = [example["ref"] for example in few_shot["examples"]]
        failing_ref = attached_refs[spec.failing_offset % len(attached_refs)]

        sample_keys = []
        for index in range(spec.sample_count):
            sample_keys.append(env.seed_sample(index, "ok"))
            _register_model_text(env, index, "ok", spec.modality, label_set)

        # The control run first, with every example readable: it defines
        # "as it would have been" for every sample.
        status, body = env.start(sample_keys, modality=spec.modality,
                                 label_set=label_set, few_shot=few_shot)
        assert status == 202, body
        control_run_id = body["run_id"]
        control_result = env.execute(control_run_id)
        assert control_result["status"] == \
            env.module.PREVIEW_STATUS_COMPLETED, control_result
        assert control_result["failed"] == 0
        control_payloads = {
            item["sample_key"]: env.payload(control_run_id, index)
            for index, item in enumerate(env.sample_items(control_run_id))}
        control_requests = {
            index: env.bedrock.calls_for(index)
            for index in range(spec.sample_count)}
        env.bedrock.calls.clear()

        # The same run again, with one attached example unreadable while
        # the affected samples are being built.
        for index in spec.affected:
            env.fail_example_for(index, failing_ref)
        status, body = env.start(sample_keys, modality=spec.modality,
                                 label_set=label_set, few_shot=few_shot)
        assert status == 202, body
        run_id = body["run_id"]
        result = env.execute(run_id)

        # The run still completes: an unreadable example is a per-sample
        # failure, never a run failure (Req 6.8).
        assert result["status"] == env.module.PREVIEW_STATUS_COMPLETED, result
        items = env.sample_items(run_id)
        assert [item["sample_key"] for item in items] == sample_keys
        assert result["failed"] == len(spec.affected)
        assert result["succeeded"] == spec.sample_count - len(spec.affected)

        for index, item in enumerate(items):
            sample_key = sample_keys[index]
            payload = env.payload(run_id, index)
            if index in spec.affected:
                # Fails with a reason naming that example image, and with
                # no model invoked for it.
                assert item["state"] == env.module.PREVIEW_SAMPLE_FAILED
                assert item["failure_category"] == "unreadable_example_image"
                assert item["failure_reason"].startswith(
                    f"few-shot example image {failing_ref} is not "
                    f"accessible: ")
                assert failing_ref in item["failure_reason"]
                assert set(payload) == {"sample_key", "state",
                                        "failure_category", "failure_reason"}
                assert env.bedrock.calls_for(index) == []
            else:
                # Processed and resolved exactly as the control run
                # resolved it, request included.
                assert item["state"] == env.module.PREVIEW_SAMPLE_SUCCEEDED
                assert "failure_category" not in item
                assert payload == control_payloads[sample_key]
                assert env.bedrock.calls_for(index) == \
                    control_requests[index]

        assert env.lock_item() is None
    finally:
        patcher.undo()


# =========================================================================== #
# Property 11
# =========================================================================== #

@settings(max_examples=100, deadline=None)
@given(spec=_pipeline_state_specs())
def test_property_preview_run_produces_no_pipeline_state(aws_stack, dda,
                                                         spec):
    """Feature: llm-autolabel-prompt-tuning, Property 11: A Preview_Run
    produces no labeling-pipeline state — *For any* Preview_Run, whatever
    its per-sample outcomes, the set of Labeling_Job records,
    Task_Assignment items and Pre_Label artifacts under
    `labeling/{usecase_id}/` SHALL be unchanged from before the run, and
    no labeler notification SHALL be sent.

    Task_Assignment absence is asserted two ways: no tasks-table item
    outside the run's own `PREVIEW#` / `PREVIEWLOCK#` namespace, and no
    preview item carrying `assignee_user_id` — so the items are not
    projected into the `assignee-index` GSI and are invisible to
    `_query_caller_tasks` and therefore to every labeler API.

    **Validates: Requirements 1.6, 3.5**
    """
    patcher = _Patcher()
    try:
        env = PreviewRunEnv(aws_stack, dda, patcher)
        label_set = _label_set_for(spec.modality)
        few_shot = env.seed_examples(good=1, bad=1) if spec.few_shot else None
        attached_refs = ([example["ref"] for example in few_shot["examples"]]
                         if few_shot else [])

        sample_keys = []
        for index, condition in enumerate(spec.conditions):
            if condition == "unreadable_example" and not attached_refs:
                condition = "ok"
                spec.conditions[index] = condition
            sample_keys.append(env.seed_sample(index, condition))
            _register_model_text(env, index, condition, spec.modality,
                                 label_set)
            if condition == "unreadable_example":
                env.fail_example_for(index, attached_refs[0])

        job_baseline = env.job_ids()
        task_baseline = env.task_keys()
        pipeline_baseline = env.artifact_keys(f"labeling/{env.usecase_id}/")

        status, body = env.start(sample_keys, modality=spec.modality,
                                 label_set=label_set, few_shot=few_shot)
        assert status == 202, body
        run_id = body["run_id"]
        result = env.execute(run_id)
        assert result["status"] == env.module.PREVIEW_STATUS_COMPLETED, result

        # No Labeling_Job record (Req 1.6).
        assert env.job_ids() == job_baseline

        # No Task_Assignment item: every new tasks-table item belongs to
        # this run's own preview namespace.
        run_pk = f"PREVIEW#{run_id}"
        lock_pk = f"PREVIEWLOCK#{env.usecase_id}"
        for job_id, task_id in env.task_keys() - task_baseline:
            assert job_id in (run_pk, lock_pk), (job_id, task_id)

        # No preview item projects into assignee-index, so no labeler API
        # can ever see one.
        for item in env.sample_items(run_id) + [env.run_item(run_id)]:
            assert "assignee_user_id" not in item
        creator_sub = env.creator["user_id"]
        assert env.assignee_index_items(creator_sub) == []
        assert env.module._query_caller_tasks(creator_sub) == []

        # No pipeline Pre_Label artifact: the run's payloads live only
        # under its own ephemeral preview prefix (Req 3.5).
        assert env.artifact_keys(f"labeling/{env.usecase_id}/") == \
            pipeline_baseline
        preview_keys = env.artifact_keys(
            f"labeling-previews/{env.usecase_id}/{run_id}/")
        assert len(preview_keys) == len(sample_keys)
        assert all(key.startswith(
            f"labeling-previews/{env.usecase_id}/{run_id}/")
            for key in preview_keys)

        # No labeler notification: the only invoke the whole flow makes is
        # the executor self-invoke, and the labeling worker's function
        # name is configured, so a notification would have been recorded.
        invocations = env.dda.lambda_client.invocations
        assert len(invocations) == 1, invocations
        assert invocations[0]["FunctionName"] == FUNCTION_NAME
        assert json.loads(invocations[0]["Payload"]) == {
            "action": "execute_preview_run", "run_id": run_id}
        assert all(call["FunctionName"] != WORKER_FUNCTION_NAME
                   for call in invocations)

        assert env.lock_item() is None
    finally:
        patcher.undo()


# =========================================================================== #
# Property 12
# =========================================================================== #

def _walk_request(value, path="request"):
    """Every (path, leaf) pair of a captured Converse request."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_request(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _walk_request(child, f"{path}[{index}]")
    else:
        yield path, value


@settings(max_examples=100, deadline=None)
@given(spec=_request_content_specs())
def test_property_model_requests_carry_only_image_and_prompt_content(
        aws_stack, dda, spec):
    """Feature: llm-autolabel-prompt-tuning, Property 12: Model requests
    carry only image and prompt content — *For any* Preview_Run or
    Auto_Labeler request, every content block of the request SHALL be
    either an image block or a text block derived from the
    Detection_Prompt, Label_Set, dimensions, per-label prompts or few-shot
    identification text, and no request field SHALL contain dataset
    credentials, presigned URLs, role ARNs or portal configuration
    secrets.

    Derivation is asserted by construction rather than by pattern
    matching: every text block must be either the few-shot header, the
    target intro, one of the per-example identification strings, or the
    prompt `build_detection_prompt` produces from the Detection_Prompt,
    Label_Set and pixel dimensions — character-for-character. Every image
    block's bytes must be bytes of an object the run was actually given.

    **Validates: Requirement 3.4**
    """
    from dda_llm_guidance import build_detection_prompt
    from dda_llm_request import (
        FEW_SHOT_HEADER,
        FEW_SHOT_TARGET_INTRO,
        few_shot_identification_text,
    )

    patcher = _Patcher()
    try:
        env = PreviewRunEnv(aws_stack, dda, patcher)
        label_set = list(spec.label_set)
        few_shot = None
        if spec.few_shot and (spec.good + spec.bad) > 0:
            few_shot = env.seed_examples(good=spec.good, bad=spec.bad)

        sample_keys = []
        for index in range(spec.sample_count):
            sample_keys.append(env.seed_sample(index, "ok"))
            _register_model_text(env, index, "ok", spec.modality, label_set)

        status, body = env.start(sample_keys, modality=spec.modality,
                                 label_set=label_set,
                                 detection_prompt=spec.detection_prompt,
                                 few_shot=few_shot)
        assert status == 202, body
        run_id = body["run_id"]
        result = env.execute(run_id)
        assert result["status"] == env.module.PREVIEW_STATUS_COMPLETED, result
        assert result["succeeded"] == spec.sample_count

        # Classification's Label_Set is fixed by the route, so the prompt
        # is built from the set the run actually carries.
        run_label_set = [str(label) for label in env.run_item(run_id)
                         ["label_set"]]
        expected_prompt = build_detection_prompt(
            spec.modality, run_label_set, spec.detection_prompt,
            IMAGE_WIDTH, IMAGE_HEIGHT, None)

        # The identification strings the attached examples may carry.
        allowed_identifications = {
            few_shot_identification_text(designation, ordinal)
            for designation in ("good", "bad")
            for ordinal in range(1, max(spec.good, spec.bad) + 1)
        }
        allowed_texts = ({expected_prompt, FEW_SHOT_HEADER,
                          FEW_SHOT_TARGET_INTRO} | allowed_identifications)
        allowed_bytes = set(env.image_bytes.values())

        assert len(env.bedrock.calls) == spec.sample_count
        for index, request in env.bedrock.calls:
            # Only the three Converse fields the shared module sends.
            assert set(request) == {"modelId", "messages", "inferenceConfig"}
            assert request["modelId"] == MODEL_IDENTIFIER
            assert set(request["inferenceConfig"]) <= {
                "maxTokens", "temperature", "topP"}
            assert len(request["messages"]) == 1
            message = request["messages"][0]
            assert set(message) == {"role", "content"}
            assert message["role"] == "user"

            for block in message["content"]:
                # Exactly one kind per block: an image or a text.
                assert set(block) in ({"image"}, {"text"}), block
                if "image" in block:
                    image = block["image"]
                    assert set(image) == {"format", "source"}
                    assert image["format"] in ("png", "jpeg")
                    assert set(image["source"]) == {"bytes"}
                    # Nothing but bytes of an object the run was given.
                    assert image["source"]["bytes"] in allowed_bytes
                else:
                    assert block["text"] in allowed_texts, block["text"]

            # The request ends with the target image then the prompt, so
            # the Detection_Prompt reaches the model verbatim.
            assert message["content"][-1] == {"text": expected_prompt}
            assert set(message["content"][-2]) == {"image"}

            # Nothing anywhere in the request names infrastructure or
            # carries a credential.
            for path, leaf in _walk_request(request):
                if isinstance(leaf, str):
                    for forbidden in FORBIDDEN_REQUEST_SUBSTRINGS:
                        assert forbidden not in leaf, (path, forbidden, leaf)
                elif isinstance(leaf, (bytes, bytearray)):
                    assert bytes(leaf) in allowed_bytes, path
    finally:
        patcher.undo()
