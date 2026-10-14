"""
Property test for the additive image-input capability annotation on the
Bedrock model options listing (llm-model-picker-search-and-image-filter,
task 1.2).

**Feature: llm-model-picker-search-and-image-filter, Property 1:
Image-input capability annotation is truthful, join-complete, and total**

*For any* set of inference profile summaries and *any* set of foundation
model summaries (with `inputModalities` drawn from lists containing
'IMAGE', non-empty lists without 'IMAGE', empty lists, non-list values,
and absence; with lifecycle statuses, inference types, and
profile-fronting relationships drawn arbitrarily; and with the
denied-ListFoundationModels branch included), the Model_Catalog_Endpoint
SHALL return a 200 response in which every Foundation_Model_Option
carries `image_input == true` exactly when its own summary's
`inputModalities` is a list containing 'IMAGE', `image_input == false`
exactly when that list is non-empty and lacks 'IMAGE', and no
`image_input` key otherwise; and every Inference_Profile_Option carries
the same resolution computed from the summary whose model id equals the
portion of the profile id after the first '.' - resolved over ALL
summaries of the response, including summaries the option filters
(lifecycle status, on-demand invokability, fronted-model deduplication)
exclude - with no `image_input` key when the profile id has no '.', when
no summary matches, or when the foundation call was denied.

**Validates: Requirements 1.1, 1.2, 1.3, 1.5**

Runs GET /data-accounts/bedrock-configuration/models through the real
handler against the shared moto stack from conftest.py with a stubbed
Bedrock control-plane client, per the FakeBedrockControlClient pattern of
test_bedrock_model_options_image_limit.py (module-scoped here, with the
stubs assigned on the freshly imported module, so the Hypothesis examples
share one environment - the test_property_bedrock_global_config_
preservation.py pattern).
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

SETTINGS_TABLE_NAME = "test-settings-model-options-image-input"
RESOURCE_ID = "bedrock-configuration"


class FakeBedrockControlClient:
    """Stand-in for the bedrock control-plane client (list APIs), with an
    optional denied-ListFoundationModels branch mimicking a missing IAM
    permission (the shape data_accounts._is_access_denied recognizes)."""

    def __init__(self, profiles=None, models=None,
                 deny_foundation_models=False):
        self.profiles = profiles or []
        self.models = models or []
        self.deny_foundation_models = deny_foundation_models

    def list_inference_profiles(self, **kwargs):
        return {"inferenceProfileSummaries": self.profiles}

    def list_foundation_models(self, **kwargs):
        if self.deny_foundation_models:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException",
                           "Message": "not authorized"}},
                "ListFoundationModels",
            )
        return {"modelSummaries": self.models}


@pytest.fixture(scope="module")
def options_env(aws_stack):
    """Settings table + freshly imported data_accounts inside moto, with
    the Bedrock control client and the stored-configuration read stubbed.

    Module-scoped so a single environment serves every Hypothesis example;
    each example swaps `state.client` for one carrying its drawn listings.
    """
    import boto3

    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
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

    state = SimpleNamespace(client=FakeBedrockControlClient())
    data_accounts._get_bedrock_control_client = lambda region: state.client
    # The region resolution is not under test; pin it so no example pays a
    # stored-configuration read.
    data_accounts.read_stored_bedrock_configuration = \
        lambda: {"region": REGION}

    yield SimpleNamespace(data_accounts=data_accounts, state=state)


def make_admin():
    user_id = f"user-{uuid.uuid4()}"
    return {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": "PortalAdmin"}


def invoke_models(options_env):
    """GET /data-accounts/bedrock-configuration/models; (status, body)."""
    user = make_admin()
    event = {
        "httpMethod": "GET",
        "resource": "/data-accounts/{id}/models",
        "path": f"/data-accounts/{RESOURCE_ID}/models",
        "pathParameters": {"id": RESOURCE_ID},
        "queryStringParameters": None,
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
# Generators
# ---------------------------------------------------------------------------

# Dot-free name fragments: model ids and profile prefixes get their dots
# only where the generator places them, so the '<prefix>.<fronted-id>'
# derivation is controlled by construction.
name_fragments = st.text(alphabet="abcdefgh-0123", min_size=1, max_size=8)

# Foundation model ids: provider-prefixed (the realistic '<provider>.<name>'
# shape, so fronted ids keep their own dots) and dotless ones.
foundation_model_ids = st.one_of(
    st.builds(lambda provider, name: f"{provider}.{name}",
              st.sampled_from(["anthropic", "amazon", "meta", "cohere"]),
              name_fragments),
    name_fragments,
)

PROFILE_PREFIXES = ["us", "eu", "apac", "global", "xx"]

NON_IMAGE_MODALITIES = ["TEXT", "EMBEDDING", "SPEECH", "VIDEO", "image"]

# Non-list inputModalities values: none of these is "a list containing
# 'IMAGE'", however IMAGE-flavored they look (a bare string, a tuple, a
# dict, a bool, a number, an explicit null).
NON_LIST_MODALITY_VALUES = [
    "IMAGE", "TEXT,IMAGE", ("IMAGE",), {"modality": "IMAGE"}, 7, True, None,
]


@st.composite
def foundation_summaries(draw, model_id):
    """One list_foundation_models summary: `inputModalities` across the
    five Requirement 1.3 shapes; lifecycle status and inference types
    arbitrary, because the option filters built on them must not affect
    the capability join (Requirement 1.2)."""
    summary = {
        "modelId": model_id,
        "modelName": f"Model {model_id}",
        "modelLifecycle": {
            "status": draw(st.sampled_from(["ACTIVE", "LEGACY"]))},
        "inferenceTypesSupported": draw(st.sampled_from([
            ["ON_DEMAND"], ["ON_DEMAND", "PROVISIONED"],
            ["INFERENCE_PROFILE"], ["PROVISIONED"], [],
        ])),
    }
    shape = draw(st.sampled_from(
        ["with-image", "without-image", "empty-list", "non-list", "absent"]))
    if shape == "with-image":
        others = draw(st.lists(st.sampled_from(NON_IMAGE_MODALITIES),
                               max_size=3))
        position = draw(st.integers(min_value=0, max_value=len(others)))
        summary["inputModalities"] = (
            others[:position] + ["IMAGE"] + others[position:])
    elif shape == "without-image":
        summary["inputModalities"] = draw(
            st.lists(st.sampled_from(NON_IMAGE_MODALITIES),
                     min_size=1, max_size=3))
    elif shape == "empty-list":
        summary["inputModalities"] = []
    elif shape == "non-list":
        summary["inputModalities"] = draw(
            st.sampled_from(NON_LIST_MODALITY_VALUES))
    # "absent": the summary carries no inputModalities key at all.
    return summary


@st.composite
def catalog_listings(draw):
    """(profiles, foundation_models, deny_foundation_models).

    Some profiles front generated foundation models ('<prefix>.<model-id>'
    over a drawn subset - including models that are LEGACY or not
    ON_DEMAND, whose summaries the option filters exclude but the join
    must still see); some front nothing (standalone ids, dotless or
    dotted, whose fronted portion may or may not accidentally match a
    summary - the oracle resolves whatever was drawn). The denied-
    ListFoundationModels branch is a generated boolean.
    """
    model_ids = draw(st.lists(foundation_model_ids, max_size=6, unique=True))
    models = [draw(foundation_summaries(model_id)) for model_id in model_ids]

    profiles = []
    profile_ids_seen = set()

    # Fronting profiles over a drawn subset of the generated models.
    for model_id in model_ids:
        if draw(st.booleans()):
            prefix = draw(st.sampled_from(PROFILE_PREFIXES))
            profile_id = f"{prefix}.{model_id}"
            if profile_id not in profile_ids_seen:
                profile_ids_seen.add(profile_id)
                profiles.append({
                    "inferenceProfileId": profile_id,
                    "inferenceProfileName": f"Profile {profile_id}",
                })

    # Standalone profiles: dotless ids (no derivable Fronted_Model) and
    # dotted ids fronting an arbitrary - usually unmatched - id.
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        profile_id = draw(st.one_of(
            name_fragments,
            st.builds(lambda prefix, name: f"{prefix}.{name}",
                      st.sampled_from(PROFILE_PREFIXES), name_fragments),
        ))
        if profile_id not in profile_ids_seen:
            profile_ids_seen.add(profile_id)
            profiles.append({
                "inferenceProfileId": profile_id,
                "inferenceProfileName": f"Profile {profile_id}",
            })

    deny = draw(st.booleans())
    return profiles, models, deny


# ---------------------------------------------------------------------------
# Oracle: the Requirement 1.1 / 1.2 / 1.3 resolution, restated from the
# requirement text (never from the code under test).
# ---------------------------------------------------------------------------

def expected_image_input(option_id, profile_ids, modalities_by_model_id,
                         denied):
    """True (Image_Capable), False (Text_Only), or None (no key).

    An option whose id is a listed profile id is an
    Inference_Profile_Option (deduplication makes the profile win), and
    resolves through the summary whose model id equals the portion of the
    profile id after the first '.' (Requirement 1.2); any other option is
    a Foundation_Model_Option resolving from its own summary
    (Requirement 1.1). No resolvable Input_Modalities list - absent key,
    non-list value, empty list, dotless profile id, unmatched fronted id,
    or denied foundation call - is Unknown_Capability: no key
    (Requirement 1.3).
    """
    if denied:
        return None
    if option_id in profile_ids:
        if "." not in option_id:
            return None
        modalities = modalities_by_model_id.get(option_id.split(".", 1)[1])
    else:
        modalities = modalities_by_model_id.get(option_id)
    if not isinstance(modalities, list) or not modalities:
        return None
    return "IMAGE" in modalities


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(listings=catalog_listings())
def test_image_input_annotation_is_truthful_join_complete_and_total(
        options_env, listings):
    """Feature: llm-model-picker-search-and-image-filter, Property 1:
    Image-input capability annotation is truthful, join-complete, and
    total.

    Every response is 200; every Foundation_Model_Option carries
    image_input True exactly when its own summary's inputModalities is a
    list containing 'IMAGE', False exactly when that list is non-empty
    without 'IMAGE', and no image_input key otherwise; every
    Inference_Profile_Option carries the resolution computed from the
    summary whose model id equals the profile id portion after the first
    '.', resolved over ALL summaries including ones the option filters
    exclude, with no key when the profile id is dotless, the fronted id
    is unmatched, or the foundation call was denied.

    **Validates: Requirements 1.1, 1.2, 1.3, 1.5**
    """
    profiles, models, deny = listings
    options_env.state.client = FakeBedrockControlClient(
        profiles=profiles, models=models, deny_foundation_models=deny)

    status, payload = invoke_models(options_env)

    # Totality (Requirement 1.5): no combination of summaries - however
    # malformed the inputModalities - fails the request.
    assert status == 200, (
        f"expected 200 for profiles={profiles!r} models={models!r} "
        f"deny={deny!r}, got {status}: {payload!r}")

    profile_ids = {p["inferenceProfileId"] for p in profiles}
    model_ids = {m["modelId"] for m in models}
    # The join source: ALL summaries of the response, captured before any
    # option filter (Requirement 1.2's "including summaries ... exclude").
    modalities_by_model_id = {
        m["modelId"]: m.get("inputModalities") for m in models}

    options = payload["models"]
    returned_ids = {option["id"] for option in options}

    # Oracle applicability: every returned option is one of the generated
    # profiles or foundation models, and - profiles being listed
    # unconditionally - every generated profile is present, so the
    # per-option quantification below is non-vacuous and in particular
    # covers profiles fronting filtered-out (LEGACY / non-ON_DEMAND)
    # summaries.
    assert returned_ids <= profile_ids | model_ids
    assert profile_ids <= returned_ids

    for option in options:
        expected = expected_image_input(
            option["id"], profile_ids, modalities_by_model_id, deny)
        if expected is None:
            assert "image_input" not in option, (
                f"option {option!r} must omit image_input "
                f"(Unknown_Capability; deny={deny!r}, "
                f"models={models!r})")
        else:
            assert "image_input" in option, (
                f"option {option!r} must carry image_input {expected!r} "
                f"(deny={deny!r}, models={models!r})")
            assert option["image_input"] is expected, (
                f"option {option!r} must carry image_input {expected!r} "
                f"(deny={deny!r}, models={models!r})")
