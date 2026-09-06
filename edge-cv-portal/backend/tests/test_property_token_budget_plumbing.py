"""
Token budget plumbing across both real `llm:` request paths and the
model-options listing.

Spec: llm-model-token-and-image-sizing, task 9.1.

**Feature: llm-model-token-and-image-sizing, Property 2: Every `llm:`
request carries the resolved per-model budget, never the global value**
**Validates: Requirements 1.3, 1.4, 1.6, 1.7, 1.8, 3.7, 3.8**

The property is asserted by driving the two real entry points —
`dda_labeling._run_preview_sample` (the Preview_API executor's
per-sample body) and `dda_autolabel_worker._generate_llm_prelabel` (the
Auto_Labeler's per-image body) — against **one** stub Converse client,
reusing the `IdentityEnv` harness from
test_property_preview_worker_request_identity.py, plus the real
`data_accounts.handler` for `GET /bedrock-configuration/models` (the
budget the job creation flow displays, Req 1.6).

Per the design's test strategy:

- The Global_Max_Tokens is drawn **twice per example** from
  `st.integers(1, 200000)` and both paths run at each value, so the
  same configuration is exercised at two global values (Req 1.7) —
  and both draws are nudged off the resolver's expected outputs, so a
  captured `maxTokens` equal to the global could never be excused as
  coincidence (Req 1.3: derived from no Bedrock_Configuration field).
- The Token_Budget_Selection and the Model_Token_Limits entry are drawn
  from valid budgets plus the invalid persistable sentinels (DynamoDB
  holds no raw float, so the non-integral number is a `Decimal` —
  exactly what a stored 9999.5 reads back as).
- The job record's `token_budget` and the `RUN` item's `token_budget`
  are set from the same draw and persisted **through DynamoDB**, so
  both paths read them back exactly as production does — numbers as
  `Decimal` (the seam `_decimal_to_native` exists for).
- The Model_Token_Limits mapping is persisted as the real
  `llm_model_token_limits` settings item that all three consumers read
  through their per-invocation loaders (Req 1.8), with the
  `LLM_MODEL_TOKEN_LIMITS` environment bootstrap blanked; mappings
  include case-folded and other-model noise keys so only exact string
  matching can resolve the drawn identifier.
- After the four captures, the mapping is **rewritten**: a job record
  carrying a valid budget must not move (Req 3.7), a record carrying
  no valid budget must re-resolve through the new mapping with no
  failure (Req 3.8) — and the request still never carries the global.

The oracle is the shared-layer `resolve_token_budget` itself, whose
tier algebra Property 1 (test_property_token_budget_resolution.py)
pins independently against literals.
"""
import json
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import boto3
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION
from dda_llm_request import resolve_token_budget
from test_dda_autolabel_worker import DATASET_BUCKET, SAM_FUNCTION
from test_dda_llm_prelabel import BOX, RecordingConverseClient, guidance
from test_property_preview_worker_request_identity import (
    IdentityEnv,
    _EnvPatcher,
)

# A settings table of this module's own, so the persisted
# Model_Token_Limits item every consumer reads is exactly the one each
# example writes — never another module's.
SETTINGS_TABLE_NAME = "test-settings-token-budget-plumbing"
TOKEN_LIMITS_SETTING_KEY = "llm_model_token_limits"

# Identifiers the mapping may or may not carry an entry for. All three
# are listed by the control-plane stub, so the model-options payload
# always reports a token_limit for the drawn identifier.
MODEL_POOL = (
    "us.amazon.nova-pro-v1:0",
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "us.meta.llama3-2-90b-instruct-v1:0",
)

LABELS = ["scratch", "dent"]
PROMPT = 'Find every "scratch" and dent on the panel.'
WIDTH, HEIGHT = 100, 80

# "This record carries no token_budget attribute at all."
_ABSENT = object()


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def settings_table(aws_stack):
    """This module's own portal-settings table inside the moto mock."""
    client = boto3.client("dynamodb", region_name=REGION)
    try:
        client.create_table(
            TableName=SETTINGS_TABLE_NAME,
            KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "setting_key",
                                   "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    except client.exceptions.ResourceInUseException:
        pass
    return boto3.resource("dynamodb",
                          region_name=REGION).Table(SETTINGS_TABLE_NAME)


@pytest.fixture(scope="module")
def accounts(aws_stack, settings_table):
    """The real data_accounts module bound to this module's settings
    table (the test_bedrock_model_options_image_limit.py pattern)."""
    previous = os.environ.get("SETTINGS_TABLE")
    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
    sys.modules.pop("data_accounts", None)
    import data_accounts

    data_accounts.SETTINGS_TABLE = SETTINGS_TABLE_NAME
    yield data_accounts
    if previous is None:
        os.environ.pop("SETTINGS_TABLE", None)
    else:
        os.environ["SETTINGS_TABLE"] = previous


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


# ----------------------------------------------------------------- harness

class _ControlPlaneStub:
    """Stand-in for the bedrock control-plane client: lists every
    MODEL_POOL identifier as an inference profile, so the model-options
    handler always reports the drawn identifier."""

    def list_inference_profiles(self, **kwargs):
        return {"inferenceProfileSummaries": [
            {"inferenceProfileId": model_id,
             "inferenceProfileName": f"Profile {model_id}"}
            for model_id in MODEL_POOL]}

    def list_foundation_models(self, **kwargs):
        return {"modelSummaries": []}


_CONTROL_PLANE = _ControlPlaneStub()


class BudgetEnv(IdentityEnv):
    """IdentityEnv with a drawn model identifier, a mutable
    Global_Max_Tokens, and job / RUN records persisted through DynamoDB
    so both paths read numbers back as Decimal, exactly as production
    does (Req 3.7's "persisted with the Labeling_Job record")."""

    def __init__(self, stack, worker, dda, prelabel, patcher, stub,
                 model_identifier):
        super().__init__(stack, worker, dda, prelabel, patcher, stub)
        self.model_identifier = model_identifier
        self.model = f"llm:{model_identifier}"
        self.global_max_tokens = 4096
        # Re-patch the configuration getter over IdentityEnv's fixed one
        # so each sub-run's drawn Global_Max_Tokens is exactly what
        # build_inference_config sees (Req 1.3, 1.7).
        patcher.setattr(prelabel, "get_bedrock_configuration",
                        self._bedrock_configuration)

    def _bedrock_configuration(self):
        return {
            "model_id": self.model_identifier,
            "region": "us-west-2",
            "max_tokens": self.global_max_tokens,
            "temperature": None,
            "top_p": None,
            "timeout_seconds": 240,
        }

    # ------------------------------------------------- persisted records
    def persisted_job(self, token_budget):
        """A Labeling_Job record carrying the drawn Token_Budget_Selection,
        written through DynamoDB and read back (numbers as Decimal)."""
        job_id = f"labeling-{uuid.uuid4().hex[:8]}"
        item = {
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "task_type": "ObjectDetection",
            "label_set": list(LABELS),
            "skip_verification": False,
            "auto_label": {
                "enabled": True,
                "model": self.model,
                "detection_prompt": PROMPT,
            },
        }
        if token_budget is not _ABSENT:
            item["auto_label"]["token_budget"] = token_budget
        self.stack.tables.labeling_jobs.put_item(Item=item)
        return self.stack.tables.labeling_jobs.get_item(
            Key={"job_id": job_id})["Item"]

    def persisted_run(self, token_budget):
        """The `PREVIEW#{run_id}` / `RUN` item carrying the same draw,
        also round-tripped through DynamoDB."""
        run_key = f"PREVIEW#preview-{uuid.uuid4().hex[:8]}"
        item = {
            "job_id": run_key,
            "task_id": "RUN",
            "usecase_id": self.usecase_id,
            "model": self.model,
            "task_type": "ObjectDetection",
            "label_set": list(LABELS),
            "detection_prompt": PROMPT,
            "few_shot_enabled": False,
            "few_shot_examples": [],
        }
        if token_budget is not _ABSENT:
            item["token_budget"] = token_budget
        self.stack.tables.labeling_tasks.put_item(Item=item)
        return self.stack.tables.labeling_tasks.get_item(
            Key={"job_id": run_key, "task_id": "RUN"})["Item"]

    # ------------------------------------------------- path entry points
    def worker_request(self, job):
        """One Auto_Labeler request. The memo reset models the top of
        the worker's handler(): one Model_Token_Limits read per
        invocation (Req 1.8)."""
        message = {
            "job_id": job["job_id"],
            "task_id": f"task-{uuid.uuid4().hex[:8]}",
            "image_s3_uri": self.image_uri,
            "modality": "ObjectDetection",
            "label_set": list(LABELS),
            "model": self.model,
            "detection_prompt": PROMPT,
        }
        self.worker._reset_model_token_limits_cache()
        return self.worker._generate_llm_prelabel(message, job,
                                                  self.model_identifier)

    def preview_request(self, run):
        """One Preview_API request. The memo reset models the top of
        dda_labeling's handler() for the executor invocation."""
        self.dda._reset_model_token_limits_cache()
        return self.dda._run_preview_sample(run, {}, self.usecase,
                                            DATASET_BUCKET, self.image_key)


# ----------------------------------------------------------------- helpers

def _put_mapping(settings_table, mapping):
    settings_table.put_item(Item={
        "setting_key": TOKEN_LIMITS_SETTING_KEY,
        "value": mapping,
    })


def _clear_mapping(settings_table):
    settings_table.delete_item(
        Key={"setting_key": TOKEN_LIMITS_SETTING_KEY})


def _listed_token_limits(accounts):
    """{model id: token_limit} from the real model-options route — the
    budget the job creation flow displays for each model (Req 1.6)."""
    user_id = f"user-{uuid.uuid4()}"
    event = {
        "httpMethod": "GET",
        "resource": "/data-accounts/{id}/models",
        "path": "/data-accounts/bedrock-configuration/models",
        "pathParameters": {"id": "bedrock-configuration"},
        "queryStringParameters": None,
        "body": None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": user_id,
                    "email": f"{user_id}@example.com",
                    "cognito:username": user_id,
                    "custom:role": "PortalAdmin",
                }
            }
        },
    }
    response = accounts.handler(event, None)
    assert response["statusCode"] == 200, response["body"]
    payload = json.loads(response["body"])
    return {option["id"]: option["token_limit"]
            for option in payload["models"]}


def _is_valid_budget(value):
    """An in-range non-boolean integer — the requirement's own words
    (Req 2.2), used only to pick which tier the example lands on."""
    return (isinstance(value, int) and not isinstance(value, bool)
            and 1 <= value <= 128000)


def _global_avoiding(seed, forbidden):
    """The drawn Global_Max_Tokens nudged off the resolver's expected
    outputs, so budget and global are provably distinct and an equality
    could never hide a leak (Req 1.3)."""
    value = seed
    while value in forbidden:
        value += 1
    return value


# -------------------------------------------------------------- generators

_valid_budgets = st.integers(min_value=1, max_value=128000)

# Invalid-but-persistable values (Property 1's invalid sentinels
# restricted to what DynamoDB can hold: no raw floats, so the
# non-integral number is the Decimal a stored 9999.5 reads back as).
_invalid_stored_values = st.sampled_from(
    (None, True, False, 0, -1, 128001, 500000, "20000", "off",
     Decimal("9999.5")))


@st.composite
def _plumbing_cases(draw):
    """A model identifier, a Token_Budget_Selection draw shared by the
    job record and the RUN item, a Model_Token_Limits configuration
    shape, and the two Global_Max_Tokens seeds."""
    model_identifier = draw(st.sampled_from(MODEL_POOL))

    selection_kind = draw(st.sampled_from(("absent", "invalid", "valid")))
    if selection_kind == "absent":
        selection = _ABSENT
    elif selection_kind == "invalid":
        selection = draw(_invalid_stored_values)
    else:
        selection = draw(_valid_budgets)

    mapping_kind = draw(st.sampled_from(
        ("no_item", "empty", "no_entry", "valid_entry", "invalid_entry")))
    if mapping_kind == "no_item":
        mapping = None
    else:
        mapping = {}
        if mapping_kind != "empty":
            if draw(st.booleans()):
                mapping["some-other-model"] = draw(_valid_budgets)
            if draw(st.booleans()):
                # Exact string matching (Req 1.1): a case-folded key
                # holding a valid budget must never resolve for the
                # drawn identifier.
                mapping[model_identifier.upper()] = draw(_valid_budgets)
        if mapping_kind == "valid_entry":
            mapping[model_identifier] = draw(_valid_budgets)
        elif mapping_kind == "invalid_entry":
            mapping[model_identifier] = draw(_invalid_stored_values)

    return SimpleNamespace(
        model_identifier=model_identifier,
        selection=selection,
        mapping=mapping,
        global_seed_1=draw(st.integers(min_value=1, max_value=200000)),
        global_seed_2=draw(st.integers(min_value=1, max_value=200000)),
    )


# =========================================================================== #
# Property 2
# =========================================================================== #

# Feature: llm-model-token-and-image-sizing, Property 2: Every `llm:`
# request carries the resolved per-model budget, never the global value
@settings(max_examples=100, deadline=None)
@given(case=_plumbing_cases())
def test_property_llm_requests_carry_the_resolved_per_model_budget(
        aws_stack, worker, dda, prelabel, accounts, settings_table, case):
    """
    **Feature: llm-model-token-and-image-sizing, Property 2: Every
    `llm:` request carries the resolved per-model budget, never the
    global value**

    *For any* Global_Max_Tokens value (including values above every
    model's cap), *any* model identifier, *any* Token_Budget_Selection
    and *any* Model_Token_Limits configuration, the `maxTokens` of the
    Converse request the Preview_API issues and of the request the
    Auto_Labeler issues SHALL both equal the Effective_Token_Budget the
    Token_Budget_Resolver returns for the same persisted
    Model_Token_Limits, SHALL equal the budget the Portal displays for
    that model in the job creation flow, and SHALL be independent of
    the Global_Max_Tokens.

    And per Requirements 3.7 / 3.8: a Model_Token_Limits mapping
    rewritten after the Labeling_Job record was persisted does not move
    the Auto_Labeler's `maxTokens` when the record carries a valid
    Token_Budget_Selection, and re-resolves it through the new mapping
    — with no failure — when it does not.

    **Validates: Requirements 1.3, 1.4, 1.6, 1.7, 1.8, 3.7, 3.8**
    """
    # ----------------------------------------------------------- oracle
    oracle_selection = None if case.selection is _ABSENT else case.selection
    oracle_mapping = {} if case.mapping is None else case.mapping
    expected = resolve_token_budget(
        case.model_identifier, oracle_selection, oracle_mapping)
    # What every consumer resolves with no selection — the model-options
    # token_limit for this persisted mapping (Req 1.6).
    no_selection_budget = resolve_token_budget(
        case.model_identifier, None, oracle_mapping)
    selection_wins = _is_valid_budget(oracle_selection)

    # The rewritten mapping's entry: always a valid budget that differs
    # from both pre-rewrite resolutions, so an unmoved worker budget
    # proves immutability (Req 3.7) and a moved one proves mapping-tier
    # re-resolution (Req 3.8) — never a coincidence.
    rewrite_value = next(value for value in (64000, 32000, 48000)
                         if value not in (expected, no_selection_budget))
    expected_after = oracle_selection if selection_wins else rewrite_value

    # Two different Global_Max_Tokens per example (Req 1.7), each
    # provably distinct from every budget the resolver can produce here.
    forbidden = {expected, expected_after}
    global_1 = _global_avoiding(case.global_seed_1, forbidden)
    global_2 = _global_avoiding(case.global_seed_2,
                                forbidden | {global_1})

    patcher = _EnvPatcher()
    try:
        stub = RecordingConverseClient(reply=guidance([BOX]))
        env = BudgetEnv(aws_stack, worker, dda, prelabel, patcher, stub,
                        case.model_identifier)

        # One persisted Model_Token_Limits item, read by the worker, the
        # preview executor and the model-options handler through the
        # same per-model configuration delivery (Req 1.8): pin every
        # module to this module's settings table and blank the
        # environment bootstrap so nothing else can leak in.
        patcher.setattr(worker, "SETTINGS_TABLE", SETTINGS_TABLE_NAME)
        patcher.setattr(dda, "SETTINGS_TABLE", SETTINGS_TABLE_NAME)
        patcher.setenv("LLM_MODEL_TOKEN_LIMITS", "")
        patcher.setattr(accounts, "_get_bedrock_control_client",
                        lambda region: _CONTROL_PLANE)

        if case.mapping is None:
            _clear_mapping(settings_table)
        else:
            _put_mapping(settings_table, case.mapping)

        env.seed_target(".png", WIDTH, HEIGHT)
        job = env.persisted_job(case.selection)
        run = env.persisted_run(case.selection)

        # --- both real paths, at two different Global_Max_Tokens -------
        for global_value in (global_1, global_2):
            env.global_max_tokens = global_value
            env.worker_request(job)
            env.preview_request(run)

        assert len(stub.calls) == 4
        budgets = [call["inferenceConfig"]["maxTokens"]
                   for call in stub.calls]

        # Req 1.3, 1.4: every request's maxTokens is the resolver's
        # output for the same persisted mapping, equal across the two
        # paths.
        assert budgets == [expected] * 4, (
            f"expected maxTokens {expected} in every request, got "
            f"{budgets} (selection={case.selection!r}, "
            f"mapping={case.mapping!r})")

        # Req 1.3, 1.7: independent of the Global_Max_Tokens — the
        # budget equals neither draw, and changing the global between
        # the first pair and the second moved nothing.
        assert budgets[0] not in (global_1, global_2)
        assert budgets[:2] == budgets[2:]

        # Req 1.6: the token_limit the model-options route reports for
        # this identifier is the no-selection resolution of the same
        # persisted mapping — and is exactly the maxTokens sent whenever
        # no valid selection is present.
        listed = _listed_token_limits(accounts)
        assert listed[case.model_identifier] == no_selection_budget
        if not selection_wins:
            assert listed[case.model_identifier] == budgets[0]

        # --- the mapping is rewritten AFTER the records were persisted -
        _put_mapping(settings_table, {case.model_identifier: rewrite_value})

        env.worker_request(job)
        assert len(stub.calls) == 5
        after = stub.calls[-1]["inferenceConfig"]["maxTokens"]

        if selection_wins:
            # Req 3.7: the persisted budget is immutable for the life of
            # the job — the rewritten mapping moved nothing.
            assert after == oracle_selection
            assert after != rewrite_value
        else:
            # Req 3.8: no valid persisted budget, so the worker
            # re-resolves through the rewritten mapping, with no failure.
            assert after == rewrite_value
        assert after == expected_after
        assert after not in (global_1, global_2)

        # And the displayed budget follows the rewritten mapping too, so
        # display and requests stay in agreement (Req 1.6).
        listed_after = _listed_token_limits(accounts)
        assert listed_after[case.model_identifier] == rewrite_value
    finally:
        patcher.undo()
