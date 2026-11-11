"""
Default max_tokens per model (bedrock_common.default_max_tokens).

With no max_tokens stored in the Bedrock configuration, a request carries
the maximum output of its model when MODEL_MAX_OUTPUT_TOKENS lists the
model (Claude Opus 5.5, the default model: 128000), and 4096 for every
other model, as before. A stored max_tokens is used as stored. Covers:

1. default_max_tokens over Claude Opus 5.5's ids and profiles, other
   models, and values that are not model ids.
2. bedrock_common.get_bedrock_configuration: the default resolved for the
   configured model, or for the request's model (the auto-labeler's
   `bedrock:` family), and a stored max_tokens returned as stored.
3. The settings API copy in data_accounts: the defaults agree with
   bedrock_common, GET shows the effective default, and with no
   max_tokens stored a PUT whose max_tokens is missing or equals its
   model's default leaves it unstored (null), so each request keeps the
   default of its own model: after the form is saved on a fresh Portal, a
   `bedrock:` labeling job on Nova Pro still sends 4096 while code assist
   and the node generator send 128000. Any other max_tokens, and a stored
   one, is stored as before.

Runs against the shared moto stack from conftest.py.
"""
import json
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from conftest import REGION

SETTINGS_TABLE_NAME = "test-settings-default-max-tokens"
RESOURCE_ID = "bedrock-configuration"
SETTING_KEY = "bedrock_configuration"

OPUS_55 = "global.anthropic.claude-opus-5-5"
OPUS_55_US = "us.anthropic.claude-opus-5-5"
NOVA_PRO = "us.amazon.nova-pro-v1:0"


@pytest.fixture(scope="module")
def env(aws_stack):
    """Settings table + freshly imported bedrock_common and data_accounts
    bound to it inside moto (conftest re-import pattern)."""
    import boto3

    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=SETTINGS_TABLE_NAME,
        KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "setting_key",
                               "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    for module_name in ("data_accounts", "bedrock_common"):
        sys.modules.pop(module_name, None)
    import bedrock_common
    import data_accounts

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        bedrock_common=bedrock_common,
        data_accounts=data_accounts,
        settings_table=resource.Table(SETTINGS_TABLE_NAME),
    )


@pytest.fixture(autouse=True)
def nothing_stored(env):
    """Each test starts and ends with no stored Bedrock configuration."""
    env.settings_table.delete_item(Key={"setting_key": SETTING_KEY})
    yield
    env.settings_table.delete_item(Key={"setting_key": SETTING_KEY})


def store(env, **value):
    """Store a Bedrock configuration value (the settings API's item shape)."""
    env.settings_table.put_item(Item={
        "setting_key": SETTING_KEY,
        "value": {key: Decimal(str(v)) if isinstance(v, float) else v
                  for key, v in value.items()},
    })


def stored_value(env):
    item = env.settings_table.get_item(
        Key={"setting_key": SETTING_KEY}).get("Item")
    return None if item is None else item["value"]


def stored_max_tokens(env):
    """The stored max_tokens as an int, or None when it is null."""
    value = stored_value(env)["max_tokens"]
    return None if value is None else int(value)


def invoke(env, method, body=None):
    """GET/PUT /data-accounts/bedrock-configuration as a PortalAdmin."""
    user_id = f"user-{uuid.uuid4()}"
    event = {
        "httpMethod": method,
        "resource": "/data-accounts/{id}",
        "path": f"/data-accounts/{RESOURCE_ID}",
        "pathParameters": {"id": RESOURCE_ID},
        "body": json.dumps(body) if body is not None else None,
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
    response = env.data_accounts.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


class RecordingClient:
    """bedrock-runtime stand-in: records each Converse request and answers
    with one text block."""

    def __init__(self, text):
        self.text = text
        self.requests = []

    def converse(self, **request):
        self.requests.append(request)
        return {"output": {"message": {"role": "assistant",
                                       "content": [{"text": self.text}]}},
                "stopReason": "end_turn"}


@pytest.fixture
def callers(env, monkeypatch):
    """The request code of code assist, the node generator and the
    auto-labeler's `bedrock:` family, reading this module's settings table
    and sending to a recording client. Each returns (modelId, maxTokens,
    client timeout) of the one request it sends; the timeout comes from
    the stored configuration, so it shows the caller read it."""
    import boto3
    import code_assist
    import dda_autolabel_worker
    import node_generator

    resource = boto3.resource("dynamodb", region_name=REGION)
    for module in (code_assist, node_generator, dda_autolabel_worker):
        # The settings read of the caller's own get_bedrock_configuration.
        settings = module.get_bedrock_configuration.__globals__
        monkeypatch.setitem(settings, "SETTINGS_TABLE", SETTINGS_TABLE_NAME)
        monkeypatch.setitem(settings, "dynamodb", resource)
    monkeypatch.setattr(dda_autolabel_worker, "_read_image_bytes",
                        lambda job, uri: (b"\x89PNG\r\n\x1a\n", "frame.png"))

    def sent(module, reply, call):
        client = RecordingClient(reply)
        timeouts = []

        def get_client(region, timeout_seconds):
            timeouts.append(timeout_seconds)
            return client

        monkeypatch.setattr(module, "get_bedrock_client", get_client)
        call()
        assert len(client.requests) == 1
        request = client.requests[0]
        return (request["modelId"], request["inferenceConfig"]["maxTokens"],
                timeouts[0])

    return SimpleNamespace(
        code_assist=lambda: sent(
            code_assist, "", lambda: code_assist.generate_code(
                "process_frame", "System prompt.",
                "Convert each frame to grayscale.")),
        node_generator=lambda: sent(
            node_generator, "", lambda: node_generator.invoke_generation(
                node_generator.get_bedrock_configuration(), "System prompt.",
                node_generator.converse_messages([], "Blur each frame."),
                {"tools": []})),
        bedrock_job=lambda model_id: sent(
            dda_autolabel_worker, '{"label": "normal"}',
            lambda: dda_autolabel_worker._generate_bedrock_prelabel(
                {"modality": "Classification",
                 "label_set": ["normal", "anomaly"],
                 "image_s3_uri": "s3://images/frame.png"},
                {"skip_verification": False}, model_id)),
    )


# ===========================================================================
# 1. default_max_tokens
# ===========================================================================

@pytest.mark.parametrize("model_id", [
    "anthropic.claude-opus-5-5",
    OPUS_55,
    OPUS_55_US,
    "eu.anthropic.claude-opus-5-5",
    "au.anthropic.claude-opus-5-5",
    "jp.anthropic.claude-opus-5-5",
    "us-gov.anthropic.claude-opus-5-5",
    f"  {OPUS_55}  ",
])
def test_claude_opus_5_5_defaults_to_its_maximum_output(env, model_id):
    """The model and every inference profile fronting it: 128000, the
    maximum output on its Amazon Bedrock model card."""
    assert env.bedrock_common.default_max_tokens(model_id) == 128000


@pytest.mark.parametrize("model_id", [
    NOVA_PRO,
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "global.anthropic.claude-opus-5",
    "global.anthropic.claude-opus-5-5-v1:0",
    "claude-opus-5-5",
    "amazon.nova-pro-v1:0",
    "",
    None,
    128000,
    ["anthropic.claude-opus-5-5"],
])
def test_any_other_value_keeps_4096(env, model_id):
    """A model the table does not list, and any value that is not a model
    id, gets 4096, the default every model had before."""
    assert env.bedrock_common.default_max_tokens(model_id) == 4096


def test_listed_maxima_and_the_default_model_agree(env):
    bc = env.bedrock_common
    assert bc.MODEL_MAX_OUTPUT_TOKENS == {"anthropic.claude-opus-5-5": 128000}
    assert bc.UNLISTED_MODEL_MAX_TOKENS == 4096
    assert bc.DEFAULT_BEDROCK_CONFIG["max_tokens"] == bc.default_max_tokens(
        bc.DEFAULT_BEDROCK_CONFIG["model_id"]) == 128000


# ===========================================================================
# 2. bedrock_common.get_bedrock_configuration
# ===========================================================================

def test_nothing_stored_resolves_the_default_model_and_its_maximum(env):
    config = env.bedrock_common.get_bedrock_configuration()
    assert config["model_id"] == OPUS_55
    assert config["max_tokens"] == 128000
    assert env.bedrock_common.build_inference_config(config) == {
        "maxTokens": 128000}


@pytest.mark.parametrize("value, expected", [
    ({"model_id": NOVA_PRO}, 4096),
    ({"model_id": OPUS_55_US}, 128000),
    ({"model_id": NOVA_PRO, "max_tokens": None}, 4096),
    ({"temperature": None, "top_p": 0.9}, 128000),
])
def test_unstored_max_tokens_is_the_configured_model_default(env, value,
                                                            expected):
    store(env, **value)
    assert env.bedrock_common.get_bedrock_configuration()[
        "max_tokens"] == expected


@pytest.mark.parametrize("value", [
    {"model_id": OPUS_55, "max_tokens": 4096},
    {"model_id": OPUS_55, "max_tokens": 128000},
    {"model_id": NOVA_PRO, "max_tokens": 128000},
    {"model_id": NOVA_PRO, "max_tokens": 2048},
])
def test_stored_max_tokens_is_returned_as_stored(env, value):
    store(env, **value)
    config = env.bedrock_common.get_bedrock_configuration()
    assert config["max_tokens"] == value["max_tokens"]
    # The request's model never replaces a stored value.
    assert env.bedrock_common.get_bedrock_configuration(
        request_model_id=OPUS_55_US)["max_tokens"] == value["max_tokens"]


@pytest.mark.parametrize("request_model_id, expected", [
    (NOVA_PRO, 4096),
    (OPUS_55_US, 128000),
    ("test-model-id", 4096),
])
def test_request_model_sets_the_unstored_default(env, request_model_id,
                                                 expected):
    """The auto-labeler's `bedrock:` family sends the job's model: with no
    max_tokens stored, the default is that model's, whatever the
    configured model."""
    assert env.bedrock_common.get_bedrock_configuration(
        request_model_id=request_model_id)["max_tokens"] == expected
    store(env, model_id=OPUS_55)
    config = env.bedrock_common.get_bedrock_configuration(
        request_model_id=request_model_id)
    assert config["max_tokens"] == expected
    assert config["model_id"] == OPUS_55


# ===========================================================================
# 3. The settings API copy (data_accounts)
# ===========================================================================

def test_settings_api_defaults_agree_with_bedrock_common(env):
    assert (env.data_accounts.DEFAULT_BEDROCK_CONFIG
            == env.bedrock_common.DEFAULT_BEDROCK_CONFIG)


def test_get_shows_the_effective_default(env):
    status, payload = invoke(env, "GET")
    assert status == 200
    assert payload["bedrock_configuration"]["model_id"] == OPUS_55
    assert payload["bedrock_configuration"]["max_tokens"] == 128000
    assert payload["defaults"]["max_tokens"] == 128000

    store(env, model_id=NOVA_PRO)
    status, payload = invoke(env, "GET")
    assert status == 200
    assert payload["bedrock_configuration"]["max_tokens"] == 4096


def test_saving_the_form_unchanged_keeps_the_default_unstored(env):
    """The settings form sends every field it shows; with nothing stored it
    shows Claude Opus 5.5 with 128000. Saving that writes max_tokens as
    null, so each request keeps the default of its own model; the
    response, which the form reloads, shows 128000, and saving the
    reloaded form again changes nothing."""
    _, shown = invoke(env, "GET")
    status, payload = invoke(env, "PUT", shown["bedrock_configuration"])
    assert status == 200
    assert payload["bedrock_configuration"] == shown["bedrock_configuration"]
    assert set(stored_value(env)) == {"model_id", "region", "max_tokens",
                                      "temperature", "top_p",
                                      "timeout_seconds"}
    assert stored_value(env)["model_id"] == OPUS_55
    assert stored_max_tokens(env) is None
    assert env.bedrock_common.get_bedrock_configuration()[
        "max_tokens"] == 128000
    assert env.bedrock_common.get_bedrock_configuration(
        request_model_id=NOVA_PRO)["max_tokens"] == 4096

    status, again = invoke(env, "PUT", payload["bedrock_configuration"])
    assert status == 200
    assert again["bedrock_configuration"]["max_tokens"] == 128000
    assert stored_max_tokens(env) is None
    _, shown = invoke(env, "GET")
    assert shown["bedrock_configuration"]["max_tokens"] == 128000


def test_form_save_on_a_fresh_portal_keeps_each_model_default(env, callers):
    """A PortalAdmin saves the form on a Portal with nothing stored,
    changing only the timeout. A `bedrock:` labeling job on Nova Pro (whose
    maximum is 10000) then still sends its own default, 4096, while code
    assist and the node generator send Claude Opus 5.5's 128000. Every
    caller uses the saved timeout, 60 s."""
    _, shown = invoke(env, "GET")
    form = dict(shown["bedrock_configuration"], timeout_seconds=60)
    assert (form["model_id"], form["max_tokens"]) == (OPUS_55, 128000)
    status, payload = invoke(env, "PUT", form)
    assert status == 200
    assert payload["bedrock_configuration"] == form
    assert stored_max_tokens(env) is None
    assert int(stored_value(env)["timeout_seconds"]) == 60

    assert callers.bedrock_job(NOVA_PRO) == (NOVA_PRO, 4096, 60)
    assert callers.bedrock_job(OPUS_55_US) == (OPUS_55_US, 128000, 60)
    assert callers.code_assist() == (OPUS_55, 128000, 60)
    assert callers.node_generator() == (OPUS_55, 128000, 60)


@pytest.mark.parametrize("change, expected, stored", [
    ({"model_id": NOVA_PRO}, 4096, None),
    ({"model_id": f" {OPUS_55_US} "}, 128000, None),
    ({"temperature": None}, 128000, None),
    ({"model_id": NOVA_PRO, "max_tokens": 4096}, 4096, None),
    ({"model_id": NOVA_PRO, "max_tokens": 9000}, 9000, 9000),
    ({"max_tokens": 4096}, 4096, 4096),
])
def test_put_with_nothing_stored(env, change, expected, stored):
    """With no max_tokens stored, a change whose max_tokens is missing or
    equals its model's default leaves max_tokens unstored, and the
    response, like a later GET, shows that default; any other max_tokens
    is stored."""
    status, payload = invoke(env, "PUT", change)
    assert status == 200
    assert payload["bedrock_configuration"]["max_tokens"] == expected
    assert stored_max_tokens(env) == stored
    _, shown = invoke(env, "GET")
    assert shown["bedrock_configuration"]["max_tokens"] == expected


@pytest.mark.parametrize("change", [
    {"model_id": NOVA_PRO},
    {"model_id": OPUS_55, "max_tokens": 128000, "timeout_seconds": 120},
])
def test_put_keeps_a_stored_max_tokens(env, change):
    """A stored max_tokens is a saved value, even when it equals the
    model's default: a change that leaves it out keeps it, and a change
    that sends it again stores it again, exactly as before."""
    store(env, model_id=OPUS_55, max_tokens=128000)
    status, payload = invoke(env, "PUT", change)
    assert status == 200
    assert payload["bedrock_configuration"]["max_tokens"] == 128000
    assert stored_max_tokens(env) == 128000
