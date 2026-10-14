"""Property test for the additive image_input differential
(llm-model-picker-search-and-image-filter, task 1.3).

**Feature: llm-model-picker-search-and-image-filter, Property 2: The
catalog is byte-identical to the pre-feature catalog once the new field
is removed**

_For any_ set of inference profile summaries and _any_ set of foundation
model summaries (including the partial-denial and full-denial branches
and arbitrary `LLM_MODEL_IMAGE_LIMITS` / persisted token-limit
configurations), removing the `image_input` key from every option of the
Model_Catalog_Endpoint's response SHALL yield exactly the response a
pinned reimplementation of the pre-feature endpoint produces for the
same inputs: the same option membership, the same order (anthropic-first,
then alphabetical), the same `id`, `label`, `image_limit`, and
`token_limit` values per option, the same `region`, and the same
presence and text of the `permissions` hint.

**Validates: Requirements 1.4, 4.4, 4.5**

Differential test: every outcome is compared against a PINNED in-test
reimplementation of the pre-feature endpoint rules (profile listing,
denial degradation, the ACTIVE / ON_DEMAND / fronted-model option
filters, profile-wins deduplication, the anthropic-first sort, the
image_limit / token_limit annotations, region resolution including the
``?region`` override, and the exact permissions hint text), written from
literals and never calling the code under test (repo precedent:
test_property_bedrock_global_config_preservation.py).

Whole-payload equality after stripping only `image_input` is the sharp
assertion: any other additive key, any dropped or reordered option, any
changed pre-feature field value, a moved region, or a reworded
permissions hint would survive the strip and fail the comparison.

Generator space: profile / foundation summary sets with
`inputModalities` across all shapes (lists with IMAGE, non-empty lists
without IMAGE, empty lists, non-list values, absence), lifecycle
statuses, inference types, and profile-fronting relationships drawn
arbitrarily over a small shared id pool (so deduplication, the fronted
join, and configuration-key hits occur frequently); the
denied-ListFoundationModels (partial) and both-denied (full) branches;
arbitrary LLM_MODEL_IMAGE_LIMITS environment values (absent, blank,
malformed, non-object JSON, and objects with valid and invalid entries);
and arbitrary persisted `llm_model_token_limits` settings items (absent,
non-mapping values, and mappings with valid and invalid entries) taken
through a real (moto) DynamoDB put/get round trip so numbers arrive as
Decimal exactly as in production.

Runs against the shared moto stack from conftest.py with a stubbed
Bedrock control-plane client (the FakeBedrockControlClient shape used by
test_bedrock_model_options_image_limit.py / test_bedrock_configuration.py).
"""
import json
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

SETTINGS_TABLE_NAME = "test-settings-model-options-additive"
RESOURCE_ID = "bedrock-configuration"
TOKEN_LIMITS_SETTING_KEY = "llm_model_token_limits"


# ---------------------------------------------------------------------------
# Pinned pre-feature rules (the differential baseline).
# Written from literals only — never from the module under test.
# ---------------------------------------------------------------------------

PINNED_IMAGE_LIMIT_DEFAULT = 20
PINNED_TOKEN_LIMIT_DEFAULT = 10000
PINNED_TOKEN_LIMIT_CEILING = 128000

# The exact pre-feature permissions hint (Requirement 4.4's "pre-feature
# permissions hint on denied list permissions").
PINNED_PERMISSIONS_HINT = (
    'Missing bedrock:ListInferenceProfiles and/or '
    'bedrock:ListFoundationModels permission; enter the model '
    'id manually.'
)


def pinned_region(region_override):
    """Pre-feature region resolution: the stripped non-empty ?region
    override, else the stored Bedrock_Configuration region (no item is
    stored in this module's table, so the default: AWS_REGION)."""
    stripped = (region_override or "").strip()
    return stripped or REGION


def pinned_image_limits_config(raw):
    """Pre-feature LLM_MODEL_IMAGE_LIMITS parse: absent, blank, malformed,
    or non-object values resolve to an empty mapping."""
    if raw is None:
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def pinned_image_limit(model_id, limits):
    """Pre-feature Model_Image_Limit resolution: the configured value only
    when it is a genuine (non-bool) integer of at least 1, else 20."""
    configured = limits.get(model_id)
    if isinstance(configured, bool) or not isinstance(configured, int):
        return PINNED_IMAGE_LIMIT_DEFAULT
    if configured < 1:
        return PINNED_IMAGE_LIMIT_DEFAULT
    return configured


def pinned_token_budget(model_id, limits):
    """Pre-feature Effective_Token_Budget with no selection: the mapping
    entry only when it is a non-bool integer in [1, 128000], else 10000."""
    configured = limits.get(model_id)
    if (not isinstance(configured, bool) and isinstance(configured, int)
            and 1 <= configured <= PINNED_TOKEN_LIMIT_CEILING):
        return configured
    return PINNED_TOKEN_LIMIT_DEFAULT


def pinned_sort_key(option):
    """Pre-feature ordering: anthropic-first (by the id or the portion
    after the first '.'), then case-insensitive label, then id."""
    model_id = option["id"]
    base_id = model_id.split(".", 1)[1] if "." in model_id else model_id
    is_anthropic = (base_id.startswith("anthropic.")
                    or model_id.startswith("anthropic."))
    return (0 if is_anthropic else 1, option["label"].lower(), model_id)


def pinned_pre_feature_payload(profiles, models, profiles_denied,
                               models_denied, region, image_limits_raw,
                               token_limits):
    """The whole pre-feature 200 payload for one set of inputs: option
    membership, order, per-option id / label / image_limit / token_limit,
    region, and the permissions hint — and nothing else."""
    access_denied = False

    profile_options = []
    if profiles_denied:
        access_denied = True
    else:
        for profile in profiles:
            profile_id = profile.get("inferenceProfileId")
            if profile_id:
                profile_options.append({
                    "id": profile_id,
                    "label": profile.get("inferenceProfileName") or profile_id,
                })

    # Foundation model ids fronted by a listed profile are dropped;
    # profile ids are '<prefix>.<model-id>'.
    profile_base_ids = {
        option["id"].split(".", 1)[1]
        for option in profile_options if "." in option["id"]
    }

    model_options = []
    if models_denied:
        access_denied = True
    else:
        for summary in models:
            model_id = summary.get("modelId")
            if not model_id:
                continue
            if summary.get("modelLifecycle", {}).get("status") != "ACTIVE":
                continue
            if "ON_DEMAND" not in (summary.get("inferenceTypesSupported")
                                   or []):
                continue
            if model_id in profile_base_ids:
                continue
            model_options.append({
                "id": model_id,
                "label": summary.get("modelName") or model_id,
            })

    # Deduplicate by id (profiles first so they win), then sort.
    seen, options = set(), []
    for option in profile_options + model_options:
        if option["id"] not in seen:
            seen.add(option["id"])
            options.append(option)
    options.sort(key=pinned_sort_key)

    limits = pinned_image_limits_config(image_limits_raw)
    for option in options:
        option["image_limit"] = pinned_image_limit(option["id"], limits)
        option["token_limit"] = pinned_token_budget(option["id"],
                                                    token_limits)

    payload = {"models": options, "region": region}
    if access_denied:
        payload["permissions"] = PINNED_PERMISSIONS_HINT
    return payload


# ---------------------------------------------------------------------------
# DynamoDB round-trip helpers (as in
# test_property_bedrock_global_config_preservation.py)
# ---------------------------------------------------------------------------

def to_ddb(value):
    """Native Python -> DynamoDB-storable (floats become Decimal)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [to_ddb(v) for v in value]
    if isinstance(value, dict):
        return {k: to_ddb(v) for k, v in value.items()}
    return value


def expected_native(value):
    """A stored native value after the DynamoDB Decimal round trip and the
    module's Decimal-to-native conversion: whole numbers come back as int,
    fractional ones as float, everything else unchanged."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        d = Decimal(str(value))
        return float(d) if d % 1 else int(d)
    if isinstance(value, list):
        return [expected_native(v) for v in value]
    return value


def native_token_limits(token_config):
    """The Model_Token_Limits mapping the pre-feature endpoint reads for
    one generated persisted state: the stored mapping after the Decimal
    round trip when the item's value is a mapping, else the environment
    bootstrap — an empty mapping, since this module pops
    LLM_MODEL_TOKEN_LIMITS (absent item, non-mapping value)."""
    if isinstance(token_config, dict):
        return {key: expected_native(value)
                for key, value in token_config.items()}
    return {}


# ---------------------------------------------------------------------------
# Fixture: settings table + freshly imported data_accounts inside moto,
# with a swappable stubbed Bedrock control-plane client.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def options_env(aws_stack):
    """Settings table + freshly imported data_accounts module inside moto.

    Module-scoped (Hypothesis-compatible); all per-example state — the
    stubbed client, the LLM_MODEL_IMAGE_LIMITS variable, and the persisted
    token-limits item — is applied inside the test body per example.
    """
    import boto3

    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
    # The environment bootstraps must not shadow the per-example
    # configurations this test plants (their content is under test).
    os.environ.pop("LLM_MODEL_TOKEN_LIMITS", None)
    os.environ.pop("LLM_MODEL_IMAGE_LIMITS", None)

    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=SETTINGS_TABLE_NAME,
        KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "setting_key",
                               "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    # Re-import so the module binds SETTINGS_TABLE and a moto-intercepted
    # boto3 resource (conftest pattern).
    sys.modules.pop("data_accounts", None)
    import data_accounts

    holder = SimpleNamespace(client=None)
    data_accounts._get_bedrock_control_client = lambda region: holder.client

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        data_accounts=data_accounts,
        holder=holder,
        settings_table=resource.Table(SETTINGS_TABLE_NAME),
    )

    # Nobody inherits this module's patched copy.
    os.environ.pop("LLM_MODEL_IMAGE_LIMITS", None)
    sys.modules.pop("data_accounts", None)


class FakeBedrockControlClient:
    """Stand-in for the bedrock control-plane client (list APIs)."""

    def __init__(self, profiles=None, models=None,
                 profiles_error=None, models_error=None):
        self.profiles = profiles or []
        self.models = models or []
        self.profiles_error = profiles_error
        self.models_error = models_error

    def list_inference_profiles(self, **kwargs):
        if self.profiles_error:
            raise self.profiles_error
        return {"inferenceProfileSummaries": self.profiles}

    def list_foundation_models(self, **kwargs):
        if self.models_error:
            raise self.models_error
        return {"modelSummaries": self.models}


def access_denied_error(operation):
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        operation,
    )


def make_admin():
    user_id = f"user-{uuid.uuid4()}"
    return {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": "PortalAdmin"}


def invoke_models(options_env, region_override=None):
    """GET /data-accounts/bedrock-configuration/models; (status, body)."""
    user = make_admin()
    query = ({"region": region_override}
             if region_override is not None else None)
    event = {
        "httpMethod": "GET",
        "resource": "/data-accounts/{id}/models",
        "path": f"/data-accounts/{RESOURCE_ID}/models",
        "pathParameters": {"id": RESOURCE_ID},
        "queryStringParameters": query,
        "body": None,
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
    response = options_env.data_accounts.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


# ---------------------------------------------------------------------------
# Generators. A small shared id pool makes deduplication, the
# profile-fronting join, and configuration-key hits frequent.
# ---------------------------------------------------------------------------

MODEL_NAMES = ("m1", "claude-x", "titan-y", "nova-z:1", "Embed-G1")
VENDORS = ("anthropic", "amazon", "meta", "ai21")
PROFILE_PREFIXES = ("us", "eu", "global")

# '<vendor>.<name>' plus dotless ids (a dotless profile id has no
# Fronted_Model; a dotless foundation id exercises the sort key's
# no-dot branch).
foundation_ids = st.one_of(
    st.builds("{}.{}".format, st.sampled_from(VENDORS),
              st.sampled_from(MODEL_NAMES)),
    st.sampled_from(MODEL_NAMES),
)

# Fronting-shaped profile ids ('<prefix>.<foundation-id>', which sometimes
# hits a generated summary id) plus dotless profile ids.
profile_ids = st.one_of(
    st.builds("{}.{}".format, st.sampled_from(PROFILE_PREFIXES),
              foundation_ids),
    st.sampled_from(MODEL_NAMES),
)

# Labels including case-fold ties (broken by id in the sort) and "" (falsy:
# the option label falls back to the id).
labels = st.sampled_from(("US Anthropic Claude", "Titan Text Express",
                          "nova PRO", "Aa", "aA", ""))

# inputModalities across every shape of Requirement 1.3: lists with IMAGE,
# non-empty lists without IMAGE, empty lists, non-list values, absence
# (drawn as key-presence below), plus arbitrary lists.
modality_values = st.one_of(
    st.sampled_from((("TEXT", "IMAGE"), ("IMAGE",), ("TEXT",),
                     ("TEXT", "EMBEDDING"), ())).map(list),
    st.sampled_from(("TEXT", 7)),  # non-list values
    st.lists(st.sampled_from(("TEXT", "IMAGE", "EMBEDDING", "VIDEO")),
             max_size=3),
)


@st.composite
def foundation_summaries(draw):
    """One list_foundation_models summary: id present / absent / empty,
    name optional, lifecycle and inference types arbitrary (weighted so
    returned options are frequent), modalities across all shapes."""
    summary = {}
    id_state = draw(st.sampled_from(("present",) * 8 + ("absent", "empty")))
    if id_state == "present":
        summary["modelId"] = draw(foundation_ids)
    elif id_state == "empty":
        summary["modelId"] = ""
    if draw(st.booleans()):
        summary["modelName"] = draw(labels)
    lifecycle = draw(st.sampled_from(("ACTIVE", "ACTIVE", "ACTIVE",
                                      "LEGACY", None)))
    if lifecycle is not None:
        summary["modelLifecycle"] = {"status": lifecycle}
    types = draw(st.sampled_from((("ON_DEMAND",),
                                  ("ON_DEMAND",),
                                  ("ON_DEMAND", "INFERENCE_PROFILE"),
                                  ("INFERENCE_PROFILE",),
                                  (),
                                  None,
                                  "key-absent")))
    if types is None:
        summary["inferenceTypesSupported"] = None
    elif types != "key-absent":
        summary["inferenceTypesSupported"] = list(types)
    if draw(st.booleans()):
        summary["inputModalities"] = draw(modality_values)
    return summary


@st.composite
def profile_summaries(draw):
    """One list_inference_profiles summary: id present / absent / empty,
    name optional."""
    summary = {}
    id_state = draw(st.sampled_from(("present",) * 8 + ("absent", "empty")))
    if id_state == "present":
        summary["inferenceProfileId"] = draw(profile_ids)
    elif id_state == "empty":
        summary["inferenceProfileId"] = ""
    if draw(st.booleans()):
        summary["inferenceProfileName"] = draw(labels)
    return summary


# Configuration keys drawn from the same pool as the option ids (so entries
# hit generated options), plus a never-matching key.
config_keys = st.one_of(foundation_ids, profile_ids,
                        st.just("unrelated-model"))

# LLM_MODEL_IMAGE_LIMITS: absent, blank, malformed, non-object JSON, and
# objects whose entries span valid integers and every invalid shape
# (strings, floats — JSON has no int/float distinction in text, so 20.0
# parses back as float — bools, nulls, below-1 integers).
image_limit_entry_values = st.one_of(
    st.integers(min_value=1, max_value=50),
    st.integers(min_value=-5, max_value=0),
    st.booleans(),
    st.sampled_from(("20", 2.5, 20.0, None)),
)
image_limits_env = st.one_of(
    st.none(),  # variable not set at all
    st.sampled_from(("", "   ", "{not json", "[1, 2]", "null", '"x"')),
    st.dictionaries(config_keys, image_limit_entry_values,
                    max_size=5).map(json.dumps),
)

# Persisted llm_model_token_limits states: no item, a non-mapping stored
# value (falls back to the empty environment bootstrap), or a mapping whose
# entries span valid integers, out-of-range integers, bools, strings,
# nulls, and floats — including whole-valued floats, which the DynamoDB
# Decimal round trip turns into valid ints on BOTH sides of the
# differential.
token_entry_values = st.one_of(
    st.integers(min_value=1, max_value=128000),
    st.integers(min_value=128001, max_value=200000),
    st.integers(min_value=-10, max_value=0),
    st.booleans(),
    st.sampled_from(("5000", None)),
    st.integers(min_value=1, max_value=200).map(float),  # whole floats
    st.floats(min_value=1, max_value=50000,
              allow_nan=False, allow_infinity=False)
    .map(lambda x: round(x, 3)),
)
token_configs = st.one_of(
    st.none(),  # no item stored
    st.sampled_from((17, "not-a-mapping")),  # item with a non-mapping value
    st.dictionaries(config_keys, token_entry_values, max_size=5),
)

# ?region override: absent, plain, whitespace-padded (the endpoint strips),
# and empty (falls back to the stored configuration's region).
region_overrides = st.one_of(
    st.none(),
    st.sampled_from(("us-west-2", "eu-central-1", " ap-south-1 ", "")),
)


# ---------------------------------------------------------------------------
# Property 2: strip image_input, compare the whole payload against the
# pinned pre-feature endpoint.
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(
    profiles=st.lists(profile_summaries(), max_size=6),
    models=st.lists(foundation_summaries(), max_size=6),
    profiles_denied=st.booleans(),
    models_denied=st.booleans(),
    image_limits_raw=image_limits_env,
    token_config=token_configs,
    region_override=region_overrides,
)
def test_catalog_equals_pre_feature_catalog_once_image_input_is_removed(
        options_env, profiles, models, profiles_denied, models_denied,
        image_limits_raw, token_config, region_override):
    """For any profile / foundation summary sets (modalities across all
    shapes, fronting and lifecycle arbitrary), any LLM_MODEL_IMAGE_LIMITS
    environment value, any persisted token-limit configuration, and the
    partial- and full-denial branches, the endpoint returns 200 and —
    once `image_input` is stripped from every option — the WHOLE payload
    equals the pinned pre-feature reimplementation's: same option
    membership, same order, same id / label / image_limit / token_limit
    per option, same region, same presence and exact text of the
    permissions hint (Requirements 1.4, 4.4, 4.5)."""
    env = options_env

    # Stubbed control-plane listings, with the denial branches: partial
    # denial = foundation denied while profiles succeed (Requirement 4.5),
    # full denial = both denied.
    env.holder.client = FakeBedrockControlClient(
        profiles=profiles,
        models=models,
        profiles_error=(access_denied_error("ListInferenceProfiles")
                        if profiles_denied else None),
        models_error=(access_denied_error("ListFoundationModels")
                      if models_denied else None),
    )

    # LLM_MODEL_IMAGE_LIMITS environment configuration for this example.
    if image_limits_raw is None:
        os.environ.pop("LLM_MODEL_IMAGE_LIMITS", None)
    else:
        os.environ["LLM_MODEL_IMAGE_LIMITS"] = image_limits_raw

    # Persisted Model_Token_Limits settings item for this example (real
    # moto put, so numbers round-trip through Decimal as in production).
    env.settings_table.delete_item(
        Key={"setting_key": TOKEN_LIMITS_SETTING_KEY})
    if token_config is not None:
        env.settings_table.put_item(Item={
            "setting_key": TOKEN_LIMITS_SETTING_KEY,
            "value": to_ddb(token_config),
        })

    status, payload = invoke_models(env, region_override)
    assert status == 200, (
        f"the annotated endpoint must keep returning 200, got {status} "
        f"with {payload!r}")

    stripped = {
        **payload,
        "models": [{key: value for key, value in option.items()
                    if key != "image_input"}
                   for option in payload["models"]],
    }
    expected = pinned_pre_feature_payload(
        profiles=profiles,
        models=models,
        profiles_denied=profiles_denied,
        models_denied=models_denied,
        region=pinned_region(region_override),
        image_limits_raw=image_limits_raw,
        token_limits=native_token_limits(token_config),
    )
    assert stripped == expected, (
        "stripping image_input from every option must yield the exact "
        f"pre-feature payload.\nstripped: {stripped!r}\n"
        f"expected: {expected!r}")
