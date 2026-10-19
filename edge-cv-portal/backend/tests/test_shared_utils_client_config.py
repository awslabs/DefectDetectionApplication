"""The optional client ``config`` of get_usecase_client / create_boto3_client.

Spec triton-model-cleanup-wedge, D11 (task 6.1, Requirement 3.8): both
factories gain an optional keyword ``config=None``. When it is set, every
``boto3.client(...)`` call they make passes ``config=config``; when it is
None, the calls are exactly today's, with no ``config`` argument.

Harness: the real shared layer, imported inside the moto-backed
``aws_stack`` session fixture; the module's ``boto3`` binding is replaced
by a recorder so the exact ``boto3.client`` calls can be asserted.
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from botocore.config import Config

ACCOUNT_ID = "123456789012"
REGION = "us-east-1"

SINGLE_ACCOUNT_USECASE = {
    "usecase_id": "uc-single",
    "account_id": ACCOUNT_ID,
    "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:root",
}
CROSS_ACCOUNT_USECASE = {
    "usecase_id": "uc-cross",
    "account_id": ACCOUNT_ID,
    "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:role/portal-cross-account",
    "external_id": "ext-1",
}
ASSUMED = {
    "AccessKeyId": "AKIAEXAMPLE",
    "SecretAccessKey": "secret-example",
    "SessionToken": "token-example",
}
DEFAULT_CREDENTIALS = {
    "AccessKeyId": None,
    "SecretAccessKey": None,
    "SessionToken": None,
    "is_default_credentials": True,
}


@pytest.fixture
def su(aws_stack, monkeypatch):
    """The live shared_utils with a recording boto3.client."""
    module = importlib.import_module("shared_utils")
    assert hasattr(module, "get_usecase_client"), "a fake shared_utils is installed"
    calls = []

    def _client(service_name, *args, **kwargs):
        calls.append((service_name, args, kwargs))
        return SimpleNamespace(service=service_name)

    monkeypatch.setattr(module, "boto3", SimpleNamespace(client=_client))
    monkeypatch.setattr(module, "assume_usecase_role", lambda *a, **k: dict(ASSUMED))
    module._recorded_calls = calls
    yield module
    del module._recorded_calls


# ---------------------------------------------------------------------------
# config set: every boto3.client call carries it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("region", [REGION, None])
def test_single_account_branch_passes_config(su, region):
    cfg = Config(connect_timeout=2)
    su.get_usecase_client("iot-data", SINGLE_ACCOUNT_USECASE, session_name="s", region=region,
                          config=cfg)
    expected = {"config": cfg}
    if region:
        expected["region_name"] = region
    assert su._recorded_calls == [("iot-data", (), expected)]


@pytest.mark.parametrize("region", [REGION, None])
def test_cross_account_branch_passes_config(su, region):
    cfg = Config(read_timeout=2)
    su.get_usecase_client("iot-data", CROSS_ACCOUNT_USECASE, session_name="s", region=region,
                          config=cfg)
    expected = {
        "aws_access_key_id": ASSUMED["AccessKeyId"],
        "aws_secret_access_key": ASSUMED["SecretAccessKey"],
        "aws_session_token": ASSUMED["SessionToken"],
        "config": cfg,
    }
    if region:
        expected["region_name"] = region
    assert su._recorded_calls == [("iot-data", (), expected)]


@pytest.mark.parametrize("region", [REGION, None])
def test_create_boto3_client_default_credentials_passes_config(su, region):
    cfg = Config(retries={"total_max_attempts": 1})
    su.create_boto3_client("iot-data", dict(DEFAULT_CREDENTIALS), region, config=cfg)
    expected = {"config": cfg}
    if region:
        expected["region_name"] = region
    assert su._recorded_calls == [("iot-data", (), expected)]


def test_create_boto3_client_explicit_credentials_passes_config(su):
    cfg = Config(connect_timeout=2)
    su.create_boto3_client("iot-data", dict(ASSUMED), REGION, config=cfg)
    assert su._recorded_calls == [("iot-data", (), {
        "aws_access_key_id": ASSUMED["AccessKeyId"],
        "aws_secret_access_key": ASSUMED["SecretAccessKey"],
        "aws_session_token": ASSUMED["SessionToken"],
        "region_name": REGION,
        "config": cfg,
    })]


# ---------------------------------------------------------------------------
# config absent: the calls are exactly today's, with no config argument
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("region", [REGION, None])
def test_single_account_branch_without_config_is_unchanged(su, region):
    su.get_usecase_client("greengrassv2", SINGLE_ACCOUNT_USECASE, session_name="s", region=region)
    expected = {"region_name": region} if region else {}
    assert su._recorded_calls == [("greengrassv2", (), expected)]


@pytest.mark.parametrize("region", [REGION, None])
def test_cross_account_branch_without_config_is_unchanged(su, region):
    su.get_usecase_client("greengrassv2", CROSS_ACCOUNT_USECASE, session_name="s", region=region)
    expected = {
        "aws_access_key_id": ASSUMED["AccessKeyId"],
        "aws_secret_access_key": ASSUMED["SecretAccessKey"],
        "aws_session_token": ASSUMED["SessionToken"],
    }
    if region:
        expected["region_name"] = region
    assert su._recorded_calls == [("greengrassv2", (), expected)]


@pytest.mark.parametrize("region", [REGION, None])
def test_create_boto3_client_default_credentials_without_config_is_unchanged(su, region):
    su.create_boto3_client("greengrassv2", dict(DEFAULT_CREDENTIALS), region)
    expected = {"region_name": region} if region else {}
    assert su._recorded_calls == [("greengrassv2", (), expected)]


def test_cross_account_branch_without_config_calls_factory_as_today(su, monkeypatch):
    """Without config, get_usecase_client calls create_boto3_client with
    today's three positional arguments, so callers that replace it with a
    three-argument fake keep working."""
    seen = []
    monkeypatch.setattr(su, "create_boto3_client",
                        lambda *args, **kwargs: seen.append((args, kwargs)))
    su.get_usecase_client("greengrassv2", CROSS_ACCOUNT_USECASE, session_name="s", region=REGION)
    assert seen == [(("greengrassv2", ASSUMED, REGION), {})]
    for _service, _args, kwargs in su._recorded_calls:
        assert "config" not in kwargs
