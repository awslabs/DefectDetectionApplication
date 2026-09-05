"""
Preview_API request-guard property tests for POST /labeling-preview/runs.

Spec: llm-autolabel-prompt-tuning, tasks 8.4, 8.5, 8.6.

Three properties, one property-based test each, all driving the real
`dda_labeling.handler` with synthetic API Gateway events against the
moto-backed stack from conftest.py (real shared_utils / rbac_middleware,
real DynamoDB tables, fake Cognito and Lambda clients following the
`test_dda_labeling_create_job.py` conventions):

**Property 6: Rejected Preview_Run requests enumerate every violation and
touch nothing** — Validates Requirements 6.3, 8.3, 8.4, 8.5, 8.7
**Property 7: Authorization precedes and hides everything** — Validates
Requirements 8.2, 8.6
**Property 8: One in-flight Preview_Run per user and Use_Case** — Validates
Requirement 8.8

"Touch nothing" is asserted structurally rather than by inspection: every
S3 entry point the module owns (`s3_client`, `get_s3_client_for_bucket`) and
every Bedrock entry point the `llm:` invocation path owns
(`dda_llm_prelabel.get_bedrock_client`, `bedrock_common.get_bedrock_client`)
is replaced by a spy that records and refuses the call, so a single read or
invocation on a rejection path fails the property. DynamoDB and the async
self-invoke are compared against per-example baselines: no `PREVIEW#` run
item, no `IMAGE#` sample item, no `PREVIEWLOCK#` claim, no jobs-table write
and no executor invoke.

Hypothesis cannot consume function-scoped fixtures, so the module-scoped
`dda` fixture (the pattern `test_property_llm_autolabel_preservation.py`
established) is combined with a per-example `PreviewEnv` built inside the
test body, and that file's `_Patcher` monkeypatch stand-in is reused for the
per-example module patching.
"""
import json
import string
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
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

# A prompt-guided auto-label model identifier: the only family a
# Preview_Run may name (Req 8.5).
LLM_MODEL = "llm:us.amazon.nova-pro-v1:0"
# A bucket that is not any Use_Case's dataset bucket, used to build
# out-of-scope Sample_Image references (Req 8.3). Deliberately never
# created: an out-of-scope reference must be classified without being
# dereferenced.
FOREIGN_BUCKET = "test-preview-foreign-bucket"

MODALITIES = ("Classification", "Segmentation", "ObjectDetection")
VALID_LABEL_SET = ["scratch", "dent"]
VALID_PROMPT = "Outline every scratch on the visible surface of the part."

# Roles that hold no manage_labeling_jobs permission (shared_utils role
# map): the unauthorized callers of Property 7.
UNAUTHORIZED_ROLES = ("Viewer", "Operator", "DataLabeler")

# Rejection material. Every entry violates exactly the rule of its group.
BAD_MODELS = ("sam", "bedrock:anthropic.claude-3-haiku", "nova-pro-v1",
              "", "llm:", "llm:has a space")
BAD_PROMPTS = ("", "   ", "\t\n", "x" * 2001, None)
BAD_MODALITIES = ("Detection", "classification", "", None)
BAD_LABEL_SETS = ([], None, ["", "scratch"], ["dup", "dup"], ["a" * 65],
                  "scratch")
BAD_FEW_SHOT = ({"enabled": True}, {"enabled": True, "examples": []},
                {"enabled": True, "examples": None}, True)


# --------------------------------------------------------------- spies

class _CallSpy:
    """A stand-in AWS client that records and refuses every call.

    Used for the S3 and Bedrock entry points: the start route must reach
    neither on any path, so recording *and* raising makes an accidental
    read or invocation impossible to miss — the recorded call list is
    asserted empty before anything else.
    """

    def __init__(self, name):
        self.name = name
        self.calls = []

    def __getattr__(self, method):
        def _record(*args, **kwargs):
            self.calls.append((method, args, kwargs))
            raise AssertionError(
                f"{self.name}.{method} must not be called by "
                f"POST /labeling-preview/runs")
        return _record


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, with
    fake Cognito and Lambda clients, plus the modules that own the
    Bedrock entry points so they can be spied per example."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling
    import bedrock_common
    import dda_llm_prelabel

    fake_cognito = FakeCognitoClient()
    dda_labeling.cognito_client = fake_cognito
    dda_labeling.USER_POOL_ID = POOL_ID

    fake_lambda = FakeLambdaClient()
    dda_labeling.lambda_client = fake_lambda

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=DATASET_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass

    return SimpleNamespace(module=dda_labeling, cognito=fake_cognito,
                           lambda_client=fake_lambda,
                           prelabel=dda_llm_prelabel,
                           bedrock_common=bedrock_common)


class PreviewEnv:
    """Per-example facade: a fresh Use_Case with a dataset prefix, spied
    S3 / Bedrock entry points, and DynamoDB baselines for the
    "touch nothing" assertions."""

    def __init__(self, stack, dda, patcher):
        self.stack = stack
        self.dda = dda
        self.module = dda.module
        self.s3 = boto3.client("s3", region_name=REGION)
        self.prefix = f"datasets/{uuid.uuid4().hex[:8]}/"
        self.usecase_id = self.make_usecase()

        # Every S3 and Bedrock entry point the route could possibly reach.
        self.dataset_s3_spy = _CallSpy("dataset s3")
        self.portal_s3_spy = _CallSpy("portal s3")
        self.bedrock_spy = _CallSpy("bedrock")
        patcher.setattr(self.module, "s3_client", self.portal_s3_spy)
        patcher.setattr(self.module, "get_s3_client_for_bucket",
                        self._get_s3_client_for_bucket)
        patcher.setattr(dda.prelabel, "get_bedrock_client",
                        self._get_bedrock_client)
        patcher.setattr(dda.bedrock_common, "get_bedrock_client",
                        self._get_bedrock_client)

        self.lambda_baseline = len(dda.lambda_client.invocations)
        self.task_baseline = self.task_keys()
        self.job_baseline = self.job_ids()

    # ----------------------------------------------------------- spies
    def _get_s3_client_for_bucket(self, *args, **kwargs):
        self.dataset_s3_spy.calls.append(("get_s3_client_for_bucket", args,
                                          kwargs))
        return self.dataset_s3_spy

    def _get_bedrock_client(self, *args, **kwargs):
        self.bedrock_spy.calls.append(("get_bedrock_client", args, kwargs))
        raise AssertionError("no model may be invoked by "
                             "POST /labeling-preview/runs")

    # ----------------------------------------------------------- setup
    def make_usecase(self):
        usecase_id = f"uc-{uuid.uuid4()}"
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": usecase_id,
            "name": "Preview Guard Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        return usecase_id

    def make_user(self, role="DataScientist"):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def sample_key(self, index):
        return f"{self.prefix}img-{index:03d}.jpg"

    def put_samples(self, keys):
        """Seed real objects, so "the object exists" is a real condition
        and not just a naming convention (Property 7)."""
        for key in keys:
            self.s3.put_object(Bucket=DATASET_BUCKET, Key=key,
                               Body=b"fakeimage")

    def valid_body(self, sample_count=2, usecase_id=None, **overrides):
        body = {
            "usecase_id": usecase_id or self.usecase_id,
            "dataset_prefix": self.prefix,
            "model": LLM_MODEL,
            "detection_prompt": VALID_PROMPT,
            "task_type": "ObjectDetection",
            "label_set": list(VALID_LABEL_SET),
            "sample_images": [self.sample_key(i) for i in
                              range(sample_count)],
        }
        body.update(overrides)
        return body

    # ---------------------------------------------------------- invoke
    def event(self, user, raw_body):
        return {
            "httpMethod": "POST",
            "resource": "/labeling-preview/runs",
            "path": "/v1/labeling-preview/runs",
            "pathParameters": None,
            "queryStringParameters": None,
            "body": raw_body,
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": user["user_id"],
                        "email": user["email"],
                        "cognito:username": user["username"],
                        "custom:role": user["role"],
                    }
                }
            },
        }

    def start(self, body, user):
        return self.start_raw(json.dumps(body), user)

    def start_raw(self, raw_body, user):
        """(status, parsed body, raw body string) for one POST."""
        response = self.module.handler(
            self.event(user, raw_body),
            SimpleNamespace(function_name="test-dda-labeling-handler"))
        return (response["statusCode"], json.loads(response["body"]),
                response["body"])

    # ----------------------------------------------------------- store
    def task_keys(self):
        keys = set()
        kwargs = {}
        while True:
            response = self.stack.tables.labeling_tasks.scan(**kwargs)
            for item in response.get("Items", []):
                keys.add((item["job_id"], item["task_id"]))
            if not response.get("LastEvaluatedKey"):
                return keys
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def job_ids(self):
        ids = set()
        kwargs = {}
        while True:
            response = self.stack.tables.labeling_jobs.scan(**kwargs)
            for item in response.get("Items", []):
                ids.add(item["job_id"])
            if not response.get("LastEvaluatedKey"):
                return ids
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def lock_item(self, user_sub, usecase_id=None):
        """The raw claim item, expired or not — a rejection path must
        leave no claim behind at all."""
        return self.stack.tables.labeling_tasks.get_item(Key={
            "job_id": (f"{self.module.PREVIEW_LOCK_PK_PREFIX}"
                       f"{usecase_id or self.usecase_id}"),
            "task_id": f"{self.module.PREVIEW_LOCK_SK_PREFIX}{user_sub}",
        }).get("Item")

    def expire_lock(self, user_sub, usecase_id=None):
        self.stack.tables.labeling_tasks.update_item(
            Key={
                "job_id": (f"{self.module.PREVIEW_LOCK_PK_PREFIX}"
                           f"{usecase_id or self.usecase_id}"),
                "task_id": f"{self.module.PREVIEW_LOCK_SK_PREFIX}{user_sub}",
            },
            UpdateExpression="SET expires_at = :expired",
            ExpressionAttributeValues={
                ":expired": self.module._now_epoch() - 10},
        )

    def run_ids(self, user_sub, usecase_id=None):
        """Run ids of every `PREVIEW#.../RUN` item this user owns in the
        Use_Case."""
        usecase_id = usecase_id or self.usecase_id
        found = set()
        kwargs = {}
        while True:
            response = self.stack.tables.labeling_tasks.scan(**kwargs)
            for item in response.get("Items", []):
                if (item["task_id"] == self.module.PREVIEW_RUN_SK
                        and item["job_id"].startswith(
                            self.module.PREVIEW_RUN_PK_PREFIX)
                        and item.get("created_by") == user_sub
                        and item.get("usecase_id") == usecase_id):
                    found.add(item["job_id"][
                        len(self.module.PREVIEW_RUN_PK_PREFIX):])
            if not response.get("LastEvaluatedKey"):
                return found
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def audit_events(self, user_sub, action):
        items = self.stack.tables.audit_log.scan().get("Items", [])
        return [item for item in items
                if item.get("user_id") == user_sub
                and item.get("action") == action]

    # ------------------------------------------------------ assertions
    def assert_no_io(self):
        """No S3 object read and no model invocation, on any path."""
        assert self.dataset_s3_spy.calls == [], self.dataset_s3_spy.calls
        assert self.portal_s3_spy.calls == [], self.portal_s3_spy.calls
        assert self.bedrock_spy.calls == [], self.bedrock_spy.calls

    def assert_nothing_written(self, user_sub, extra_invocations=0):
        """No run item, no sample item, no lock, no jobs-table write and
        no executor invoke beyond the ones explicitly expected."""
        self.assert_no_io()
        new_invocations = (len(self.dda.lambda_client.invocations)
                           - self.lambda_baseline)
        assert new_invocations == extra_invocations, (
            self.dda.lambda_client.invocations[self.lambda_baseline:])
        assert self.task_keys() == self.task_baseline
        assert self.job_ids() == self.job_baseline
        assert self.lock_item(user_sub) is None


# ------------------------------------------------------------- generators

_name = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6)
# An out-of-scope Sample_Image reference: either inside the Use_Case
# dataset bucket but outside its prefix, or in a foreign bucket entirely
# (Req 8.3).
_out_of_scope_ref = st.tuples(st.sampled_from(["prefix", "bucket"]), _name)


@st.composite
def _rejected_specs(draw):
    """A Preview_Run request violating a non-empty subset of the rules.

    Two rule pairs are kept apart deliberately, because the route cannot
    report both halves and the property is about *enumeration*, not about
    guessing undecidable rules:
    - an invalid Labeling_Modality makes the Label_Set rule undecidable
      (there is no modality to validate it against);
    - a Classification request has a fixed binary Label_Set that can
      never be violated, so the Label_Set rule only applies to the other
      two modalities.
    """
    rules = draw(st.lists(
        st.sampled_from(["model", "prompt", "modality", "label_set",
                         "few_shot"]),
        unique=True, max_size=5))
    violate_samples = draw(st.booleans())
    if not rules and not violate_samples:
        violate_samples = True
    if "modality" in rules:
        rules = [rule for rule in rules if rule != "label_set"]
    modality = draw(st.sampled_from(
        ["Segmentation", "ObjectDetection"] if "label_set" in rules
        else list(MODALITIES)))

    count_kind = (draw(st.sampled_from(["zero", "over", "ok"]))
                  if violate_samples else "ok")
    if count_kind == "zero":
        in_scope_count, out_of_scope = 0, []
    elif count_kind == "over":
        in_scope_count = draw(st.integers(min_value=6, max_value=8))
        out_of_scope = draw(st.lists(_out_of_scope_ref, max_size=2))
    else:
        out_of_scope = draw(st.lists(
            _out_of_scope_ref,
            min_size=1 if violate_samples else 0, max_size=2))
        in_scope_count = draw(st.integers(
            min_value=1, max_value=5 - len(out_of_scope)))

    return SimpleNamespace(
        rules=rules,
        modality=modality,
        in_scope_count=in_scope_count,
        out_of_scope=out_of_scope,
        bad_model=draw(st.sampled_from(BAD_MODELS)),
        bad_prompt=draw(st.sampled_from(BAD_PROMPTS)),
        bad_modality=draw(st.sampled_from(BAD_MODALITIES)),
        bad_label_set=draw(st.sampled_from(BAD_LABEL_SETS)),
        bad_few_shot=draw(st.sampled_from(BAD_FEW_SHOT)),
    )


def _build_rejected_request(env, spec):
    """(bare-key body, s3://-URI body, expected parameters, per-sample
    out-of-scope expectation) for one rejection spec.

    The two bodies name the same objects in the two spellings Req 8.7
    calls out, so the property can compare their classifications.
    """
    body = env.valid_body(sample_count=0)
    body["task_type"] = spec.modality
    if spec.modality == "Classification":
        body.pop("label_set", None)
    expected = set()

    if "model" in spec.rules:
        body["model"] = spec.bad_model
        expected.add("model")
    if "prompt" in spec.rules:
        body["detection_prompt"] = spec.bad_prompt
        expected.add("detection_prompt")
    if "modality" in spec.rules:
        body["task_type"] = spec.bad_modality
        expected.add("task_type")
    if "label_set" in spec.rules:
        body["label_set"] = spec.bad_label_set
        expected.add("label_set")
    if "few_shot" in spec.rules:
        body["few_shot"] = spec.bad_few_shot
        expected.add("few_shot")

    in_scope = [env.sample_key(index) for index in
                range(spec.in_scope_count)]
    bare_refs = list(in_scope)
    uri_refs = [f"s3://{DATASET_BUCKET}/{key}" for key in in_scope]
    out_of_scope_expected = [False] * len(in_scope)
    for kind, name in spec.out_of_scope:
        if kind == "prefix":
            # Same bucket, outside the dataset prefix: expressible both
            # ways, so the two spellings must reach the same verdict.
            key = f"outside-prefix/{name}.jpg"
            bare_refs.append(key)
            uri_refs.append(f"s3://{DATASET_BUCKET}/{key}")
        else:
            # A foreign bucket can only be named as a URI.
            reference = f"s3://{FOREIGN_BUCKET}/{env.prefix}{name}.jpg"
            bare_refs.append(reference)
            uri_refs.append(reference)
        out_of_scope_expected.append(True)

    body["sample_images"] = bare_refs
    if (not 1 <= len(bare_refs) <= 5) or spec.out_of_scope:
        expected.add("sample_images")

    uri_body = dict(body)
    uri_body["sample_images"] = uri_refs
    return body, uri_body, expected, out_of_scope_expected


@st.composite
def _unauthorized_specs(draw):
    """An unauthorized caller plus a Preview_Run request that varies in
    every way the 403 must hide: Use_Case existence, body validity, and
    Sample_Image existence (Req 8.2)."""
    return SimpleNamespace(
        role=draw(st.sampled_from(UNAUTHORIZED_ROLES)),
        usecase=draw(st.sampled_from(
            ["existing", "missing", "absent", "non_string"])),
        body_kind=draw(st.sampled_from(
            ["valid", "empty", "unparseable", "bad_model", "bad_prompt",
             "too_many_samples", "out_of_scope", "few_shot_zero"])),
        samples_exist=draw(st.booleans()),
        sample_count=draw(st.integers(min_value=1, max_value=5)),
    )


def _build_unauthorized_request(env, spec):
    """The raw request body string for one unauthorized-caller spec."""
    if spec.body_kind == "unparseable":
        return "{not json at all"
    if spec.body_kind == "empty":
        return json.dumps({})

    keys = [env.sample_key(index) for index in range(spec.sample_count)]
    if spec.samples_exist:
        env.put_samples(keys)
    body = env.valid_body(sample_count=0)
    body["sample_images"] = keys

    if spec.usecase == "missing":
        body["usecase_id"] = f"uc-{uuid.uuid4()}"
    elif spec.usecase == "absent":
        body.pop("usecase_id")
    elif spec.usecase == "non_string":
        body["usecase_id"] = 12345

    if spec.body_kind == "bad_model":
        body["model"] = "sam"
    elif spec.body_kind == "bad_prompt":
        body["detection_prompt"] = "   "
    elif spec.body_kind == "too_many_samples":
        body["sample_images"] = [env.sample_key(index) for index in range(7)]
    elif spec.body_kind == "out_of_scope":
        body["sample_images"] = [f"s3://{FOREIGN_BUCKET}/secret/x.jpg"]
    elif spec.body_kind == "few_shot_zero":
        body["few_shot"] = {"enabled": True, "examples": []}
    return json.dumps(body)


@st.composite
def _lock_specs(draw):
    """A sequence of Preview_Run requests from one user in one Use_Case:
    an accepted run, one to three requests arriving while it is in
    flight, then the in-flight run reaching a terminal state or its claim
    expiring, then one more request (Req 8.8)."""
    return SimpleNamespace(
        sample_count=draw(st.integers(min_value=1, max_value=5)),
        retry_count=draw(st.integers(min_value=1, max_value=3)),
        retry_sample_count=draw(st.integers(min_value=1, max_value=5)),
        modality=draw(st.sampled_from(list(MODALITIES))),
        few_shot=draw(st.booleans()),
        termination=draw(st.sampled_from(
            ["release", "expire", "complete_then_release"])),
    )


def _lock_body(env, spec, sample_count, usecase_id=None):
    body = env.valid_body(sample_count=sample_count, usecase_id=usecase_id)
    body["task_type"] = spec.modality
    if spec.modality == "Classification":
        body.pop("label_set", None)
    if spec.few_shot:
        body["few_shot"] = {
            "enabled": True,
            "examples": [
                {"ref": "labeling-examples/good-0.jpg",
                 "designation": "good", "position": 0},
                {"ref": f"s3://{DATASET_BUCKET}/labeling-examples/bad-0.png",
                 "designation": "bad", "position": 0},
            ],
        }
    return body


# =========================================================================== #
# Property 6
# =========================================================================== #

@settings(max_examples=100, deadline=None)
@given(spec=_rejected_specs())
def test_property_rejection_enumerates_every_violation(aws_stack, dda, spec):
    """Feature: llm-autolabel-prompt-tuning, Property 6: Rejected
    Preview_Run requests enumerate every violation and touch nothing —
    *For any* Preview_Run request violating a non-empty subset of the
    request rules — non-`llm:` model identifier, empty-after-trim or
    over-2000-character Detection_Prompt, Label_Set invalid for the
    modality, zero or more than five Sample_Images, a Sample_Image
    resolving outside the Use_Case's dataset bucket and prefix, or the
    Few_Shot_Option enabled with zero example references — the Preview_API
    SHALL reject the request with an error naming every violated rule and
    every out-of-scope reference, SHALL read no referenced object, and
    SHALL invoke no model; and equivalent spellings of the same object
    reference (bare key versus `s3://` URI) SHALL receive the identical
    scope classification.

    **Validates: Requirements 6.3, 8.3, 8.4, 8.5, 8.7**
    """
    patcher = _Patcher()
    try:
        env = PreviewEnv(aws_stack, dda, patcher)
        user = env.make_user("DataScientist")
        bare_body, uri_body, expected, out_of_scope = \
            _build_rejected_request(env, spec)

        status, body, _ = env.start(bare_body, user)

        # One 400 carrying every violation, never a short-circuited first
        # failure (Req 8.4).
        assert status == 400, body
        assert body["error"] == env.module.PREVIEW_VALIDATION_FAILED_MESSAGE
        errors = body["validation_errors"]
        assert {error["parameter"] for error in errors} == expected, errors
        assert all(error["message"] for error in errors)

        # Every out-of-scope reference is named individually (Req 8.3).
        named = {error.get("sample_image") for error in errors
                 if error["parameter"] == "sample_images"}
        for reference, is_out_of_scope in zip(bare_body["sample_images"],
                                              out_of_scope):
            assert (reference in named) == is_out_of_scope, (
                f"{reference!r} classification differs from the "
                f"expectation: {errors!r}")

        # Nothing read, nothing invoked, nothing written (Req 8.4, 8.5).
        env.assert_nothing_written(user["user_id"])

        # Req 8.7: resolution precedes comparison, so the bare-key and
        # s3:// spellings of the same objects classify identically.
        uri_status, uri_body_response, _ = env.start(uri_body, user)
        assert uri_status == 400
        uri_errors = uri_body_response["validation_errors"]
        assert {error["parameter"] for error in uri_errors} == expected
        uri_named = {error.get("sample_image") for error in uri_errors
                     if error["parameter"] == "sample_images"}
        for reference, is_out_of_scope in zip(uri_body["sample_images"],
                                              out_of_scope):
            assert (reference in uri_named) == is_out_of_scope, (
                f"{reference!r} classifies differently from its bare-key "
                f"spelling: {uri_errors!r}")
        env.assert_nothing_written(user["user_id"])
    finally:
        patcher.undo()


# =========================================================================== #
# Property 7
# =========================================================================== #

@settings(max_examples=100, deadline=None)
@given(spec=_unauthorized_specs())
def test_property_authorization_precedes_and_hides_everything(aws_stack, dda,
                                                              spec):
    """Feature: llm-autolabel-prompt-tuning, Property 7: Authorization
    precedes and hides everything — *For any* requesting user without
    authorization to create DDA labeling jobs in the target Use_Case, and
    *for any* Preview_Run request from that user — including requests that
    also violate other validation rules and requests naming a non-existent
    Use_Case or non-existent objects — the Preview_API SHALL answer with the
    same authorization error carrying no dataset content and no existence
    information, SHALL never answer with a validation error instead, SHALL
    read no Sample_Image, and SHALL invoke no model.

    **Validates: Requirements 8.2, 8.6**
    """
    patcher = _Patcher()
    try:
        env = PreviewEnv(aws_stack, dda, patcher)
        caller = env.make_user(spec.role)
        raw_body = _build_unauthorized_request(env, spec)

        status, body, raw_response = env.start_raw(raw_body, caller)

        # One fixed 403 body: no dataset content, no existence
        # information, and never a validation error (Req 8.2, 8.6).
        assert status == 403, body
        assert body == {
            "error": env.module.PREVIEW_NOT_AUTHORIZED_MESSAGE}, body

        # The same denial, byte-for-byte, as a fully valid request against
        # an existing Use_Case with existing objects from another
        # unauthorized caller — so the response discloses nothing about
        # what was named.
        control_caller = env.make_user(spec.role)
        control_keys = [env.sample_key(index) for index in range(2)]
        env.put_samples(control_keys)
        control_body = env.valid_body(sample_count=0)
        control_body["sample_images"] = control_keys
        control_status, _, control_raw = env.start(control_body,
                                                   control_caller)
        assert control_status == status
        assert control_raw == raw_response

        # The denial is recorded once per attempt (the @rbac_check audit
        # event), and nothing else happened.
        for user_sub in (caller["user_id"], control_caller["user_id"]):
            denials = env.audit_events(user_sub, "unauthorized_access")
            assert len(denials) == 1, denials
            assert denials[0]["result"] == "denied"
            env.assert_nothing_written(user_sub)
    finally:
        patcher.undo()


# =========================================================================== #
# Property 8
# =========================================================================== #

@settings(max_examples=100, deadline=None)
@given(spec=_lock_specs())
def test_property_one_in_flight_run_per_user_and_usecase(aws_stack, dda,
                                                         spec):
    """Feature: llm-autolabel-prompt-tuning, Property 8: One in-flight
    Preview_Run per user and Use_Case — *For any* sequence of Preview_Run
    requests from one user in one Use_Case, at most one run SHALL be
    executing at any time: a request arriving while that user's run is in
    flight in that Use_Case SHALL be rejected with an already-in-progress
    error and SHALL invoke no model, and after the in-flight run reaches a
    terminal state or its claim expires, a subsequent request from the same
    user SHALL be accepted.

    **Validates: Requirement 8.8**
    """
    patcher = _Patcher()
    try:
        env = PreviewEnv(aws_stack, dda, patcher)
        user = env.make_user("DataScientist")
        user_sub = user["user_id"]
        accepted = 0

        # The first request is accepted and holds the claim.
        status, body, _ = env.start(
            _lock_body(env, spec, spec.sample_count), user)
        assert status == 202, body
        assert body["status"] == env.module.PREVIEW_STATUS_RUNNING
        assert body["sample_count"] == spec.sample_count
        first_run_id = body["run_id"]
        accepted += 1
        claim = env.lock_item(user_sub)
        assert claim is not None and claim["run_id"] == first_run_id

        # Every request arriving while it is in flight is rejected with
        # the already-in-progress error, invokes no model, starts no
        # executor, and leaves the existing claim untouched.
        for _ in range(spec.retry_count):
            retry_status, retry_body, _ = env.start(
                _lock_body(env, spec, spec.retry_sample_count), user)
            assert retry_status == 409, retry_body
            assert retry_body == {
                "error": env.module.PREVIEW_IN_PROGRESS_MESSAGE}
            assert env.run_ids(user_sub) == {first_run_id}
            claim = env.lock_item(user_sub)
            assert claim["run_id"] == first_run_id
            env.assert_no_io()
            assert (len(dda.lambda_client.invocations)
                    - env.lambda_baseline) == accepted

        # The guard is per user and per Use_Case: another user in the same
        # Use_Case, and the same user in another Use_Case, are unaffected.
        other_user = env.make_user("DataScientist")
        other_status, other_body, _ = env.start(
            _lock_body(env, spec, spec.sample_count), other_user)
        assert other_status == 202, other_body
        accepted += 1
        assert env.lock_item(other_user["user_id"])["run_id"] == \
            other_body["run_id"]

        other_usecase_id = env.make_usecase()
        cross_status, cross_body, _ = env.start(
            _lock_body(env, spec, spec.sample_count,
                       usecase_id=other_usecase_id), user)
        assert cross_status == 202, cross_body
        accepted += 1
        assert env.lock_item(user_sub, other_usecase_id)["run_id"] == \
            cross_body["run_id"]

        # The in-flight run reaches a terminal state, or its claim
        # expires — either way the next request from the same user is
        # accepted and takes the claim.
        if spec.termination == "release":
            env.module._release_preview_lock(env.usecase_id, user_sub)
        elif spec.termination == "expire":
            env.expire_lock(user_sub)
        else:
            env.module._update_preview_run_status(
                first_run_id, env.module.PREVIEW_STATUS_COMPLETED)
            env.module._release_preview_lock(env.usecase_id, user_sub)

        final_status, final_body, _ = env.start(
            _lock_body(env, spec, spec.sample_count), user)
        assert final_status == 202, final_body
        accepted += 1
        assert final_body["run_id"] != first_run_id
        assert env.run_ids(user_sub) == {first_run_id, final_body["run_id"]}
        assert env.lock_item(user_sub)["run_id"] == final_body["run_id"]

        # No request on any path — accepted or rejected — read an object or
        # invoked a model; the executor was started exactly once per
        # accepted run.
        env.assert_no_io()
        assert (len(dda.lambda_client.invocations)
                - env.lambda_baseline) == accepted
    finally:
        patcher.undo()
