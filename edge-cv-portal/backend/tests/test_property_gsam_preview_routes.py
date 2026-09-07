"""
Grounded-SAM Preview_API start-route property tests
(grounded-sam-prompt-tuning-preview, task 1.2).

Two properties, one property-based test each, driving the real
`dda_labeling.handler` with synthetic API Gateway events against the
moto-backed stack from conftest.py (real shared_utils / rbac_middleware,
real DynamoDB tables, fake Cognito and Lambda clients per the
`test_dda_labeling_create_job.py` conventions, the per-example env shape
`test_property_preview_api_guards.py` established):

**Property 4: Start validation accepts iff the grounded-sam predicate
holds, enumerating every violation with nothing persisted** —
Validates: Requirements 2.4, 2.5, 3.2, 3.3, 3.4, 3.5

**Property 9: The in-flight lock TTL takes the family's per-sample
form** — Validates: Requirements 3.6, 9.2

The Property 4 oracle restates the acceptance predicate from the
requirements rather than mirroring the implementation: a request is
accepted (202) exactly when its model is `llm:`-valid under the existing
rules or is exactly `grounded-sam` with a Segmentation/ObjectDetection
modality, a valid Label_Set, creation-rule-valid and guardrail-clean
Prompt_Overrides, no inapplicable llm-only field, in-scope Sample_Images
within 1..5, and the worker configured. Every generated request is run
through that restatement to produce the exact multiset of expected
violations; a rejection must carry exactly that many `validation_errors`
entries, each expected violation matched to an entry by distinctive
substrings, and must persist nothing: no `PREVIEW#` RUN or IMAGE item,
no `PREVIEWLOCK#` claim (the whole tasks table is baselined per
example), and no worker or executor invoke (the fake Lambda client
records every invoke).

The worker-deployed dimension is driven by monkeypatching
`dda_labeling.GROUNDED_SAM_WORKER_FUNCTION_NAME` per example (the
module reads the env var once at import), using the `_Patcher`
monkeypatch stand-in from `test_property_llm_autolabel_preservation.py`
— Hypothesis cannot consume function-scoped fixtures, so the
module-scoped `dda` fixture is combined with a per-example env built
inside the test body, the established pattern.
"""
import json
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError
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

FUNCTION_NAME = "test-dda-labeling-handler"
WORKER_FUNCTION_NAME = "test-grounded-sam-worker"

GSAM_MODEL = "grounded-sam"
LLM_MODEL = "llm:us.amazon.nova-pro-v1:0"
VALID_PROMPT = "Outline every visible surface defect on the part"

# Model strings that are neither `grounded-sam` nor `llm:`-prefixed —
# each must draw the one family rejection naming the accepted families
# (Req 3.2). 'grounded-sam ' and 'GROUNDED-SAM' pin that the family
# match is exact, not trimmed or case-folded.
OTHER_MODELS = ("sam", "bedrock:anthropic.claude-3-haiku", "",
                "grounded-sam ", "GROUNDED-SAM", "gsam")

GSAM_MODALITIES = ("Segmentation", "ObjectDetection")
ALL_MODALITIES = ("Classification", "Segmentation", "ObjectDetection")
JUNK_MODALITY = "Detection"

# Period-free base labels plus one period-bearing label name, whose
# Effective_Prompt falls back to the name itself when no override
# survives — the Prompt_Guardrail's second offense source (Req 2.4).
BASE_LABELS = ("scratch", "dent", "crack")
PERIOD_LABEL = "broken.gap"
ALIGNMENT_CHAR = "."

# One deterministic value per Prompt_Override entry kind, so each
# expected violation matches its validation_errors entry by a
# distinctive substring (Req 2.5: creation rules — string values, raw
# length <= 256, blank-after-trim dropped; Req 2.4: guardrail).
OVERRIDE_VALUES = {
    "valid": "clearly visible surface damage",
    "blank": "   ",
    "period": "gap between broken. cookie pieces",
    "overlength": "x" * 257,
    "nonstring": 12345,
}
OVERRIDE_KINDS = ("absent", "valid", "blank", "period", "overlength",
                  "nonstring")
# Kinds that can never produce a violation under a valid Label_Set with
# a period-free label name.
BENIGN_OVERRIDE_KINDS = ("absent", "valid", "blank")

# A Few_Shot_Example reference that is valid under the llm: rules
# (JPEG/PNG, good/bad designation, inside the Use_Case data bucket), so
# an enabled few-shot document rejects under grounded-sam for being
# inapplicable to the family — never for its own content (Req 3.4).
FEW_SHOT_EXAMPLE = {"ref": "labeling-examples/good-0.jpg",
                    "designation": "good", "position": 0}

PER_SAMPLE_SECONDS = {"grounded-sam": 240, "llm": 120}
LOCK_SLACK_SECONDS = 60
LOCK_TTL_MAX_SECONDS = 900


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, with
    fake Cognito and Lambda clients (the create-job convention)."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

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

    return SimpleNamespace(module=dda_labeling, cognito=fake_cognito,
                           lambda_client=fake_lambda)


class GsamPreviewEnv:
    """Per-example facade: a fresh Use_Case with a dataset prefix, an
    authorized Job_Creator, the worker-deployed state patched onto the
    module, and baselines for the nothing-persisted assertions."""

    def __init__(self, stack, dda, patcher, worker_deployed):
        self.stack = stack
        self.dda = dda
        self.module = dda.module
        self.tasks = stack.tables.labeling_tasks
        self.context = SimpleNamespace(function_name=FUNCTION_NAME)

        self.usecase_id = f"uc-{uuid.uuid4()}"
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "GSAM Preview Route Property Test",
            "account_id": "123456789012",
            # Single-account: a root role ARN makes
            # get_s3_client_for_bucket take its direct-access fallback.
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        self.prefix = f"datasets/{uuid.uuid4().hex[:8]}/"
        self.creator = self.make_user("DataScientist")

        # Req 6.1 / 3.5 dimension: the start route reads the module
        # constant, bound from the env var once at import time.
        patcher.setattr(self.module, "GROUNDED_SAM_WORKER_FUNCTION_NAME",
                        WORKER_FUNCTION_NAME if worker_deployed else "")

        self.lambda_baseline = len(dda.lambda_client.invocations)
        self.task_baseline = self.task_keys()

    # ----------------------------------------------------------- setup
    @staticmethod
    def make_user(role="DataScientist"):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def sample_key(self, index):
        return f"{self.prefix}img-{index:03d}.jpg"

    # ---------------------------------------------------------- invoke
    def start(self, body, user):
        """`(status, parsed body)` for POST /labeling-preview/runs."""
        event = {
            "httpMethod": "POST",
            "resource": "/labeling-preview/runs",
            "path": "/v1/labeling-preview/runs",
            "pathParameters": None,
            "queryStringParameters": None,
            "body": json.dumps(body),
            "requestContext": {"authorizer": {"claims": {
                "sub": user["user_id"],
                "email": user["email"],
                "cognito:username": user["username"],
                "custom:role": user["role"],
            }}},
        }
        response = self.module.handler(event, self.context)
        return response["statusCode"], json.loads(response["body"])

    # ----------------------------------------------------------- store
    def task_keys(self):
        """Every (job_id, task_id) in the tasks table — covers PREVIEW#
        RUN/IMAGE items and PREVIEWLOCK# claims alike."""
        keys, kwargs = set(), {}
        while True:
            response = self.tasks.scan(**kwargs)
            for item in response.get("Items", []):
                keys.add((item["job_id"], item["task_id"]))
            if not response.get("LastEvaluatedKey"):
                return keys
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def run_item(self, run_id):
        return self.tasks.get_item(
            Key={"job_id": f"PREVIEW#{run_id}",
                 "task_id": "RUN"}).get("Item")

    def lock_item(self, user_sub):
        return self.tasks.get_item(Key={
            "job_id": f"PREVIEWLOCK#{self.usecase_id}",
            "task_id": f"USER#{user_sub}"}).get("Item")

    def new_invocations(self):
        return self.dda.lambda_client.invocations[self.lambda_baseline:]


# ------------------------------------------------------------- generators

@st.composite
def _start_specs(draw):
    """A Preview_Run start request spanning the whole generated space:
    model families, modalities including Classification and junk, valid
    and invalid Label_Sets, Prompt_Override entry mixes, in-scope and
    out-of-scope sample lists at counts 0..6, the llm-only fields
    attached or absent, and the worker deployed or not.

    `all_valid` biases half the examples onto the acceptance side of the
    predicate so both directions of the iff are exercised; the oracle is
    computed uniformly from the drawn kinds either way.
    """
    all_valid = draw(st.booleans())
    family = draw(st.sampled_from(
        ("grounded-sam", "llm") if all_valid
        else ("grounded-sam", "llm", "other")))
    is_gsam = family == "grounded-sam"
    model = (GSAM_MODEL if is_gsam
             else LLM_MODEL if family == "llm"
             else draw(st.sampled_from(OTHER_MODELS)))
    gsam_valid = all_valid and is_gsam

    # The worker-deployed rule applies to the grounded-sam family only,
    # so it stays a free draw everywhere except a gsam acceptance.
    worker_deployed = True if gsam_valid else draw(st.booleans())

    labels = list(BASE_LABELS[:draw(st.integers(min_value=1,
                                                max_value=3))])
    include_period_label = draw(st.booleans())
    if include_period_label:
        labels = labels + [PERIOD_LABEL]

    if all_valid:
        modality = draw(st.sampled_from(
            GSAM_MODALITIES if is_gsam else ALL_MODALITIES))
        label_set_kind = "valid"
    else:
        modality = draw(st.sampled_from(ALL_MODALITIES + (JUNK_MODALITY,)))
        label_set_kind = draw(st.sampled_from(("valid", "missing",
                                               "empty")))

    override_kinds = {}
    unknown_keys = ()
    if gsam_valid:
        overrides_kind = draw(st.sampled_from(("absent", "dict")))
        if overrides_kind == "dict":
            override_kinds = {
                label: draw(st.sampled_from(BENIGN_OVERRIDE_KINDS))
                for label in labels}
        if include_period_label:
            # A period-bearing label name is guardrail-clean only when a
            # period-free override survives as its Effective_Prompt.
            overrides_kind = "dict"
            override_kinds[PERIOD_LABEL] = "valid"
    else:
        # llm: and rejected requests draw freely — the llm: family never
        # validates the key, so hostile overrides ride accepted llm runs.
        overrides_kind = draw(st.sampled_from(("absent", "dict",
                                               "nondict")))
        if overrides_kind == "dict":
            override_kinds = {
                label: draw(st.sampled_from(OVERRIDE_KINDS))
                for label in labels}
            unknown_keys = tuple(
                f"ghost-{index}" for index in
                range(draw(st.integers(min_value=0, max_value=2))))

    if all_valid:
        total_samples = draw(st.integers(min_value=1, max_value=5))
        out_of_scope_count = 0
    else:
        total_samples = draw(st.integers(min_value=0, max_value=6))
        out_of_scope_count = draw(st.integers(
            min_value=0, max_value=min(2, total_samples)))

    if gsam_valid:
        few_shot_kind = draw(st.sampled_from(("absent", "disabled")))
        downscale_kind = draw(st.sampled_from(("absent", "null")))
        budget_kind = "absent"
        # detection_prompt is neither required nor recorded under
        # grounded-sam, so attaching one must not disturb acceptance.
        detection_prompt_kind = draw(st.sampled_from(("attached",
                                                      "absent")))
    elif all_valid:
        few_shot_kind = draw(st.sampled_from(("absent", "disabled",
                                              "enabled")))
        downscale_kind = draw(st.sampled_from(("absent", "null",
                                               "valid_option")))
        budget_kind = draw(st.sampled_from(("absent", "valid_int")))
        detection_prompt_kind = "attached"
    else:
        few_shot_kind = draw(st.sampled_from(("absent", "disabled",
                                              "enabled", "enabled_bare")))
        downscale_kind = draw(st.sampled_from(("absent", "null",
                                               "valid_option", "junk")))
        budget_kind = draw(st.sampled_from(("absent", "null", "valid_int",
                                            "junk")))
        detection_prompt_kind = draw(st.sampled_from(("attached",
                                                      "absent")))

    return SimpleNamespace(
        family=family,
        model=model,
        worker_deployed=worker_deployed,
        modality=modality,
        labels=labels,
        label_set_kind=label_set_kind,
        overrides_kind=overrides_kind,
        override_kinds=override_kinds,
        unknown_keys=unknown_keys,
        total_samples=total_samples,
        out_of_scope_count=out_of_scope_count,
        few_shot_kind=few_shot_kind,
        downscale_kind=downscale_kind,
        budget_kind=budget_kind,
        detection_prompt_kind=detection_prompt_kind,
    )


def _out_of_scope_refs(spec):
    """Bare keys inside the dataset bucket but outside the run's dataset
    prefix — classified out of scope without being dereferenced."""
    return [f"outside-scope/img-{index:02d}.jpg"
            for index in range(spec.out_of_scope_count)]


def _build_body(env, spec):
    """The request body a spec describes, against this example's
    Use_Case and dataset prefix."""
    in_scope = [env.sample_key(index) for index in
                range(spec.total_samples - spec.out_of_scope_count)]
    body = {
        "usecase_id": env.usecase_id,
        "dataset_prefix": env.prefix,
        "model": spec.model,
        "task_type": spec.modality,
        "sample_images": in_scope + _out_of_scope_refs(spec),
    }
    if spec.label_set_kind == "valid":
        body["label_set"] = list(spec.labels)
    elif spec.label_set_kind == "empty":
        body["label_set"] = []

    if spec.overrides_kind == "dict":
        overrides = {}
        for label, kind in spec.override_kinds.items():
            if kind != "absent":
                overrides[label] = OVERRIDE_VALUES[kind]
        for key in spec.unknown_keys:
            overrides[key] = "anything goes"
        body["prompt_overrides"] = overrides
    elif spec.overrides_kind == "nondict":
        body["prompt_overrides"] = ["not", "an", "object"]

    if spec.few_shot_kind == "disabled":
        body["few_shot"] = False
    elif spec.few_shot_kind == "enabled":
        body["few_shot"] = {"enabled": True,
                            "examples": [dict(FEW_SHOT_EXAMPLE)]}
    elif spec.few_shot_kind == "enabled_bare":
        body["few_shot"] = True

    if spec.downscale_kind == "null":
        body["downscale_max_edge"] = None
    elif spec.downscale_kind == "valid_option":
        body["downscale_max_edge"] = 1024
    elif spec.downscale_kind == "junk":
        body["downscale_max_edge"] = "big"

    if spec.budget_kind == "null":
        body["token_budget"] = None
    elif spec.budget_kind == "valid_int":
        body["token_budget"] = 4096
    elif spec.budget_kind == "junk":
        body["token_budget"] = "lots"

    if spec.detection_prompt_kind == "attached":
        body["detection_prompt"] = VALID_PROMPT
    return body


# ----------------------------------------------------------------- oracle

def _expected_violations(spec):
    """The exact multiset of violations the acceptance predicate expects
    for a spec, each as a tuple of distinctive message substrings.

    Restates Requirements 2.4, 2.5, 3.2-3.5 (and, for the llm: family,
    the pre-feature rules those requirements leave unchanged): the
    request is accepted exactly when this list is empty.
    """
    expected = []
    labels = spec.labels if spec.label_set_kind == "valid" else None

    if spec.family == "grounded-sam":
        # Req 6.1 via 3.5's enumerate-everything posture: the worker
        # must be configured for any grounded-sam start.
        if not spec.worker_deployed:
            expected.append(("Grounded-SAM worker is not deployed",))
        # Req 3.3: the family's modalities are the two geometry ones.
        if spec.modality not in GSAM_MODALITIES:
            expected.append(("grounded-sam family supports Segmentation "
                             "and ObjectDetection",))
        # Shared Label_Set rule, always decidable for the family.
        if labels is None:
            expected.append(("label set with between 1 and 10 class "
                             "names is required",))
        # Req 2.5: the job-creation override rules; Req 2.4: the shared
        # Prompt_Guardrail over each label's Effective_Prompt.
        survivors = {}
        if spec.overrides_kind == "nondict":
            expected.append(("prompt_overrides must be an object",))
        elif spec.overrides_kind == "dict":
            for label, kind in spec.override_kinds.items():
                if kind == "absent":
                    continue
                if labels is None or label not in labels:
                    expected.append((f"'{label}' is not a label",))
                elif kind == "nonstring":
                    expected.append((f"override for label '{label}'",
                                     "must be text"))
                elif kind == "overlength":
                    expected.append((f"override for label '{label}'",
                                     "at most 256 characters"))
                elif kind in ("valid", "period"):
                    survivors[label] = OVERRIDE_VALUES[kind]
                # blank: dropped after trimming, never a violation
            for key in spec.unknown_keys:
                expected.append((f"'{key}' is not a label",))
        for label in (labels or []):
            override = survivors.get(label)
            if override is not None:
                if ALIGNMENT_CHAR in override:
                    expected.append((f"text prompt for label '{label}'",
                                     "contains a period"))
            elif ALIGNMENT_CHAR in label:
                expected.append((f"Label '{label}' contains a period",
                                 "has no text prompt"))
        # Req 3.4: one violation per inapplicable llm-only field.
        if spec.few_shot_kind in ("enabled", "enabled_bare"):
            expected.append(("few_shot does not apply to the grounded-sam "
                             "family",))
        if spec.downscale_kind in ("valid_option", "junk"):
            expected.append(("downscale_max_edge does not apply to the "
                             "grounded-sam family",))
        if spec.budget_kind in ("null", "valid_int", "junk"):
            expected.append(("token_budget does not apply to the "
                             "grounded-sam family",))
    else:
        # Req 3.2: any model neither grounded-sam nor llm:-prefixed
        # draws the one family rejection naming the accepted families.
        if spec.family == "other":
            expected.append(("Preview runs require the 'grounded-sam' "
                             "auto-label family",))
        # The pre-feature llm: rules, unchanged (Req 9.2's posture):
        # a Detection_Prompt is required, the modality must be one of
        # the three, and geometry modalities validate the Label_Set
        # (a junk modality leaves that rule undecidable).
        if spec.detection_prompt_kind == "absent":
            expected.append(("non-empty detection_prompt is required",))
        if spec.modality not in ALL_MODALITIES:
            expected.append(("Labeling modality must be one of",))
        elif (spec.modality != "Classification" and labels is None):
            expected.append(("label set with between 1 and 10 class "
                             "names is required",))
        # The llm-only fields validate under their own (pre-feature)
        # rules; prompt_overrides is never validated outside the
        # grounded-sam family, whatever it carries.
        if spec.few_shot_kind == "enabled_bare":
            expected.append(("At least one example image is required",))
        if spec.downscale_kind == "junk":
            expected.append(("downscale_max_edge must be null for no "
                             "downscaling",))
        if spec.budget_kind in ("null", "junk"):
            expected.append(("token_budget must be a whole number",))

    # Shared Sample_Image rules (Req 3.5): 1..5 references, each
    # resolving inside the Use_Case dataset location.
    if not 1 <= spec.total_samples <= 5:
        expected.append(("Between 1 and 5 sample images must be "
                         "selected",))
    for reference in _out_of_scope_refs(spec):
        expected.append((f"'{reference}'",
                         "outside the use case dataset location"))
    return expected


# =========================================================================== #
# Property 4
# =========================================================================== #

@settings(max_examples=100, deadline=None)
@given(spec=_start_specs())
def test_property_start_validation_accepts_iff_gsam_predicate_holds(
        aws_stack, dda, spec):
    """Feature: grounded-sam-prompt-tuning-preview, Property 4: Start
    validation accepts iff the grounded-sam predicate holds, enumerating
    every violation with nothing persisted — *For any* generated start
    request (model strings across families; modalities; label sets;
    override maps mixing valid, blank, period-bearing, over-length,
    unknown-key, and non-string entries; sample lists mixing in-scope,
    out-of-scope, and out-of-count; inapplicable llm fields present or
    absent; the worker deployed or not), `POST /labeling-preview/runs`
    SHALL answer 202 exactly when the model is `llm:`-valid (existing
    rules) or is `grounded-sam` with a Segmentation/ObjectDetection
    modality, a valid Label_Set, creation-rule-valid and guardrail-clean
    overrides, no inapplicable field, in-scope samples within 1..5, and
    the worker configured; every rejection SHALL enumerate all violated
    rules in one response and persist no RUN item, no sample item, and
    no lock claim.

    **Validates: Requirements 2.4, 2.5, 3.2, 3.3, 3.4, 3.5**
    """
    patcher = _Patcher()
    try:
        env = GsamPreviewEnv(aws_stack, dda, patcher,
                             worker_deployed=spec.worker_deployed)
        body = _build_body(env, spec)
        expected = _expected_violations(spec)

        status, response = env.start(body, env.creator)

        if not expected:
            # The predicate holds: accepted with the 202 shape, the run
            # recorded, and exactly the one async executor self-invoke.
            assert status == 202, (response, spec)
            assert response["sample_count"] == spec.total_samples
            assert response["status"] == env.module.PREVIEW_STATUS_RUNNING
            run_item = env.run_item(response["run_id"])
            assert run_item is not None, response
            assert run_item["model"] == spec.model
            invocations = env.new_invocations()
            assert len(invocations) == 1, invocations
            payload = json.loads(invocations[0]["Payload"])
            assert payload == {"action": "execute_preview_run",
                               "run_id": response["run_id"]}
        else:
            # The predicate is violated: one 400 enumerating exactly the
            # expected violations, each matched by its distinctive
            # substrings...
            assert status == 400, (status, response, expected)
            assert (response["error"]
                    == env.module.PREVIEW_VALIDATION_FAILED_MESSAGE)
            errors = response["validation_errors"]
            assert len(errors) == len(expected), (errors, expected)
            for fragments in expected:
                assert any(
                    all(fragment in error["message"]
                        for fragment in fragments)
                    for error in errors), (fragments, errors)
            # ... and nothing persisted: no RUN item, no IMAGE item, no
            # lock claim (the tasks table is byte-identical to the
            # baseline), and no worker or executor invoke.
            assert env.task_keys() == env.task_baseline
            assert env.lock_item(env.creator["user_id"]) is None
            assert env.new_invocations() == []
    finally:
        patcher.undo()


# =========================================================================== #
# Property 9
# =========================================================================== #

@st.composite
def _ttl_specs(draw):
    """An accepted start per family and sample count: grounded-sam over
    its two modalities, llm: over all three (Classification omits the
    Label_Set — the route substitutes the fixed binary one).

    The remaining draws vary benign request content the families accept
    (a surviving Prompt_Override and an ignored Detection_Prompt under
    grounded-sam; few-shot, a Downscale_Setting, and a Token_Budget
    under llm:) — none of which may move the claim's TTL off the
    family's per-sample form.
    """
    family = draw(st.sampled_from(("grounded-sam", "llm")))
    is_gsam = family == "grounded-sam"
    return SimpleNamespace(
        family=family,
        modality=draw(st.sampled_from(
            GSAM_MODALITIES if is_gsam else ALL_MODALITIES)),
        sample_count=draw(st.integers(min_value=1, max_value=5)),
        with_overrides=draw(st.booleans()) if is_gsam else False,
        with_prompt=draw(st.booleans()) if is_gsam else True,
        with_few_shot=False if is_gsam else draw(st.booleans()),
        with_downscale=False if is_gsam else draw(st.booleans()),
        with_budget=False if is_gsam else draw(st.booleans()),
    )


def _valid_family_body(env, spec):
    """A request the family's predicate accepts, carrying the spec's
    benign extras."""
    body = {
        "usecase_id": env.usecase_id,
        "dataset_prefix": env.prefix,
        "model": GSAM_MODEL if spec.family == "grounded-sam" else LLM_MODEL,
        "task_type": spec.modality,
        "sample_images": [env.sample_key(index)
                          for index in range(spec.sample_count)],
    }
    if spec.family == "grounded-sam":
        body["label_set"] = ["scratch", "dent"]
        if spec.with_overrides:
            body["prompt_overrides"] = {
                "scratch": OVERRIDE_VALUES["valid"]}
    elif spec.modality != "Classification":
        body["label_set"] = ["scratch", "dent"]
    if spec.with_prompt:
        body["detection_prompt"] = VALID_PROMPT
    if spec.with_few_shot:
        body["few_shot"] = {"enabled": True,
                            "examples": [dict(FEW_SHOT_EXAMPLE)]}
    if spec.with_downscale:
        body["downscale_max_edge"] = 1024
    if spec.with_budget:
        body["token_budget"] = 4096
    return body


@settings(max_examples=100, deadline=None)
@given(spec=_ttl_specs())
def test_property_lock_ttl_takes_the_family_per_sample_form(aws_stack, dda,
                                                            spec):
    """Feature: grounded-sam-prompt-tuning-preview, Property 9: The
    in-flight lock TTL takes the family's per-sample form — *For any*
    sample count 1..5, a grounded-sam start's lock claim SHALL carry
    `expires_at - claimed_at = min(count × 240 + 60, 900)` seconds, and
    an `llm:` start's SHALL carry `min(count × 120 + 60, 900)` — the
    pre-feature form.

    **Validates: Requirements 3.6, 9.2**
    """
    patcher = _Patcher()
    try:
        env = GsamPreviewEnv(aws_stack, dda, patcher, worker_deployed=True)
        body = _valid_family_body(env, spec)

        status, response = env.start(body, env.creator)
        assert status == 202, response

        lock = env.lock_item(env.creator["user_id"])
        assert lock is not None
        assert lock["run_id"] == response["run_id"]
        expected_ttl = min(
            spec.sample_count * PER_SAMPLE_SECONDS[spec.family]
            + LOCK_SLACK_SECONDS,
            LOCK_TTL_MAX_SECONDS)
        assert (int(lock["expires_at"]) - int(lock["claimed_at"])
                == expected_ttl), (lock, expected_ttl)
    finally:
        patcher.undo()
