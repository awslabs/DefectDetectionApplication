"""ShadowManager's shadow size limit follows the account's IoT quota.

Feature: static-camera-video-loop, task 10 (design Decision 7).

The device derives its camera report cap from ShadowManager's
``shadowDocumentSizeLimitBytes``; ShadowManager's documentation requires
that limit and the account's "Maximum size of a JSON state document" quota
(iotcore L-A295A064) to be raised together. The deployment path therefore
carries the quota into the submitted ShadowManager merge:

- ``account_shadow_document_limit``: GetServiceQuota through the Use_Case
  client path, cached per account and region; an unreadable, non-byte or
  below-default value is None (a deployment never fails on it).
- ``apply_shadow_document_size_limit``: a raised quota sets the limit
  (capped at ShadowManager's 30 KB maximum); a default quota leaves a merge
  without a limit byte-identical; an earlier limit that no longer matches
  the quota is corrected; an unknown quota touches nothing.
- Both call sites: ``create_deployment`` (LocalServer deployments) and the
  workflow revision path (carried-over ShadowManager entry).
"""
import json
import sys

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from test_deployment_shadow_manager import (
    EXPECTED_SYNC_CONFIG, LOCAL_SERVER_COMPONENT, ShadowManagerEnv)
from test_workflow_deploy_subscribe_merge_exploration import (
    LOCAL_SERVER_ARM64JP6, WorkflowDeployEnv)
from test_workflow_packaging_deployment_integration import ACCOUNT_ID

SHADOW_MANAGER = "aws.greengrass.ShadowManager"
LIMIT_KEY = "shadowDocumentSizeLimitBytes"


@pytest.fixture(scope="module")
def deployments(aws_stack):
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments
    return deployments


@pytest.fixture(autouse=True)
def fresh_quota_cache(deployments):
    deployments._shadow_quota_cache.clear()
    yield
    deployments._shadow_quota_cache.clear()


class FakeServiceQuotas:
    """Service Quotas stand-in: a scripted GetServiceQuota response (or a
    ClientError), recording every call."""

    def __init__(self, value=8192.0, unit="Bytes", error=None):
        self.value = value
        self.unit = unit
        self.error = error
        self.calls = []

    def get_service_quota(self, ServiceCode, QuotaCode):
        self.calls.append((ServiceCode, QuotaCode))
        if self.error is not None:
            raise ClientError({"Error": {"Code": self.error,
                                         "Message": "denied"}},
                              "GetServiceQuota")
        return {"Quota": {
            "ServiceCode": ServiceCode, "QuotaCode": QuotaCode,
            "QuotaName": "Maximum size of a JSON state document",
            "Value": self.value, "Unit": self.unit, "Adjustable": True,
        }}


def with_quotas(monkeypatch, deployments, quotas):
    """Route 'service-quotas' clients to ``quotas``; everything else to the
    harness's own client dispatch. Returns the recorded client requests."""
    inner = deployments.get_usecase_client
    requests = []

    def dispatch(service_name, usecase, session_name=None, region=None):
        if service_name == "service-quotas":
            requests.append((usecase.get("account_id"), region, session_name))
            return quotas
        return inner(service_name, usecase, session_name=session_name,
                     region=region)

    monkeypatch.setattr(deployments, "get_usecase_client", dispatch)
    return requests


def shadow_manager_entry(document):
    return {"componentVersion": "2.3.15",
            "configurationUpdate": {"merge": json.dumps(document)}}


def submitted_merge(components):
    return json.loads(components[SHADOW_MANAGER]["configurationUpdate"]["merge"])


# --- reading the quota -----------------------------------------------------------


class TestAccountShadowDocumentLimit:
    USECASE = {"usecase_id": "uc-1", "account_id": ACCOUNT_ID}

    def test_reads_the_iot_state_document_quota(self, deployments, monkeypatch):
        quotas = FakeServiceQuotas(value=16384.0)
        requests = with_quotas(monkeypatch, deployments, quotas)
        limit = deployments.account_shadow_document_limit(
            self.USECASE, "us-east-1", session_name="sq-test")
        assert limit == 16384
        assert quotas.calls == [("iotcore", "L-A295A064")]
        assert requests == [(ACCOUNT_ID, "us-east-1", "sq-test")]

    def test_default_quota_reads_as_8_kb(self, deployments, monkeypatch):
        with_quotas(monkeypatch, deployments, FakeServiceQuotas(8192.0))
        assert deployments.account_shadow_document_limit(
            self.USECASE, "us-east-1") == 8192

    def test_kilobyte_unit_is_converted(self, deployments, monkeypatch):
        with_quotas(monkeypatch, deployments,
                    FakeServiceQuotas(16.0, unit="Kilobytes"))
        assert deployments.account_shadow_document_limit(
            self.USECASE, "us-east-1") == 16384

    @pytest.mark.parametrize("quotas", [
        FakeServiceQuotas(error="AccessDeniedException"),
        FakeServiceQuotas(error="NoSuchResourceException"),
        FakeServiceQuotas(value=16384.0, unit="Count"),
        FakeServiceQuotas(value=4096.0),
        FakeServiceQuotas(value=None),
        FakeServiceQuotas(value="16384"),
        FakeServiceQuotas(value=True),
    ])
    def test_unreadable_quota_is_none(self, deployments, monkeypatch, quotas):
        with_quotas(monkeypatch, deployments, quotas)
        assert deployments.account_shadow_document_limit(
            self.USECASE, "us-east-1") is None

    def test_client_construction_failure_is_none(self, deployments,
                                                 monkeypatch):
        def failing(service_name, usecase, session_name=None, region=None):
            raise ClientError({"Error": {"Code": "AccessDenied",
                                         "Message": "no AssumeRole"}},
                              "AssumeRole")

        monkeypatch.setattr(deployments, "get_usecase_client", failing)
        assert deployments.account_shadow_document_limit(
            self.USECASE, "us-east-1") is None

    def test_cached_per_account_and_region(self, deployments, monkeypatch):
        quotas = FakeServiceQuotas(value=16384.0)
        with_quotas(monkeypatch, deployments, quotas)
        now = [1_000.0]
        read = lambda usecase, region: deployments.account_shadow_document_limit(  # noqa: E731
            usecase, region, clock=lambda: now[0])

        assert read(self.USECASE, "us-east-1") == 16384
        assert read(self.USECASE, "us-east-1") == 16384
        assert len(quotas.calls) == 1
        # Another region and another account are separate entries.
        read(self.USECASE, "eu-west-1")
        read({"usecase_id": "uc-2", "account_id": "210987654321"}, "us-east-1")
        assert len(quotas.calls) == 3
        # The entry expires: a raised quota is picked up.
        quotas.value = 30720.0
        now[0] += deployments.SHADOW_QUOTA_CACHE_SECONDS
        assert read(self.USECASE, "us-east-1") == 30720
        assert len(quotas.calls) == 4

    def test_failures_are_not_cached(self, deployments, monkeypatch):
        quotas = FakeServiceQuotas(error="AccessDeniedException")
        with_quotas(monkeypatch, deployments, quotas)
        assert deployments.account_shadow_document_limit(
            self.USECASE, "us-east-1") is None
        quotas.error, quotas.value = None, 16384.0
        assert deployments.account_shadow_document_limit(
            self.USECASE, "us-east-1") == 16384
        assert len(quotas.calls) == 2


# --- applying it to the ShadowManager entry ----------------------------------------------


class TestApplyShadowDocumentSizeLimit:

    def apply(self, deployments, document, quota):
        components = {SHADOW_MANAGER: shadow_manager_entry(document)}
        before = components[SHADOW_MANAGER]["configurationUpdate"]["merge"]
        result = deployments.apply_shadow_document_size_limit(components, quota)
        after = components[SHADOW_MANAGER]["configurationUpdate"]["merge"]
        return result, before, after

    def test_raised_quota_sets_the_limit(self, deployments):
        result, _, after = self.apply(deployments, EXPECTED_SYNC_CONFIG, 16384)
        assert result == "set"
        assert json.loads(after) == dict(EXPECTED_SYNC_CONFIG, **{LIMIT_KEY: 16384})

    def test_quota_above_the_shadow_manager_maximum_is_capped(self, deployments):
        result, _, after = self.apply(deployments, EXPECTED_SYNC_CONFIG, 65536)
        assert result == "set"
        assert json.loads(after)[LIMIT_KEY] == 30720

    def test_default_quota_leaves_the_merge_byte_identical(self, deployments):
        result, before, after = self.apply(deployments, EXPECTED_SYNC_CONFIG,
                                           8192)
        assert result == "unchanged"
        assert after == before

    def test_matching_limit_is_left_byte_identical(self, deployments):
        document = dict(EXPECTED_SYNC_CONFIG, **{LIMIT_KEY: 16384})
        result, before, after = self.apply(deployments, document, 16384)
        assert result == "unchanged"
        assert after == before

    @pytest.mark.parametrize("current, quota, expected", [
        (30720, 16384, 16384),  # above the quota: the cloud would reject
        (12288, 16384, 16384),  # the quota was raised again
        (16384, 8192, 8192),    # a limit the quota no longer backs
        ("16384", 16384, 16384),  # not a number
    ])
    def test_stale_limit_follows_the_quota(self, deployments, current, quota,
                                           expected):
        document = dict(EXPECTED_SYNC_CONFIG, **{LIMIT_KEY: current})
        result, _, after = self.apply(deployments, document, quota)
        assert result == "set"
        assert json.loads(after) == dict(EXPECTED_SYNC_CONFIG,
                                         **{LIMIT_KEY: expected})

    def test_unknown_quota_touches_nothing(self, deployments):
        document = dict(EXPECTED_SYNC_CONFIG, **{LIMIT_KEY: 30720})
        result, before, after = self.apply(deployments, document, None)
        assert result == "skipped"
        assert after == before

    @pytest.mark.parametrize("components", [
        {},
        {SHADOW_MANAGER: {"componentVersion": "2.3.15"}},
        {SHADOW_MANAGER: {"componentVersion": "2.3.15",
                          "configurationUpdate": {"merge": "{not json"}}},
        {SHADOW_MANAGER: {"componentVersion": "2.3.15",
                          "configurationUpdate": {"merge": "[1, 2]"}}},
    ])
    def test_entries_without_a_usable_merge_are_skipped(self, deployments,
                                                         components):
        snapshot = json.dumps(components, sort_keys=True)
        assert deployments.apply_shadow_document_size_limit(
            components, 16384) == "skipped"
        assert json.dumps(components, sort_keys=True) == snapshot

    def test_sibling_configuration_update_keys_survive(self, deployments):
        components = {SHADOW_MANAGER: {
            "componentVersion": "2.3.15",
            "configurationUpdate": {"merge": json.dumps(EXPECTED_SYNC_CONFIG),
                                    "reset": ["/rateLimits"]},
        }}
        deployments.apply_shadow_document_size_limit(components, 16384)
        assert components[SHADOW_MANAGER]["configurationUpdate"]["reset"] == [
            "/rateLimits"]

    @settings(max_examples=200, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(quota=st.integers(min_value=8192, max_value=131072),
           current=st.one_of(st.none(), st.integers(min_value=1024,
                                                    max_value=65536)),
           extras=st.dictionaries(
               st.sampled_from(["rateLimits", "strategy", "shadowDocuments"]),
               st.dictionaries(st.text(min_size=1, max_size=8),
                               st.integers(), max_size=3),
               max_size=3))
    def test_property_limit_follows_the_quota(self, deployments, quota,
                                              current, extras):
        document = dict(EXPECTED_SYNC_CONFIG, **extras)
        if current is not None:
            document[LIMIT_KEY] = current
        result, before, after = self.apply(deployments, document, quota)
        applied = json.loads(after)
        target = min(quota, 30720)
        if current is None and quota == 8192:
            # The default quota and no limit: nothing written.
            assert result == "unchanged" and after == before
            assert LIMIT_KEY not in applied
        else:
            assert applied[LIMIT_KEY] == target
            assert result == ("unchanged" if current == target else "set")
        # Everything else in the merge is preserved exactly.
        applied.pop(LIMIT_KEY, None)
        document.pop(LIMIT_KEY, None)
        assert applied == document


# --- the call sites ---------------------------------------------------------------------


class TestCreateDeploymentCarriesTheQuota:

    @pytest.fixture
    def sm_env(self, env, deployments, monkeypatch):
        return ShadowManagerEnv(env, deployments, monkeypatch)

    def deploy(self, sm_env, monkeypatch, quotas):
        with_quotas(monkeypatch, sm_env.deployments, quotas)
        status, payload = sm_env.deploy_components(
            [LOCAL_SERVER_COMPONENT], target_devices=["line-a-camera-01"])
        assert status == 201, payload
        [call] = sm_env.gg.create_deployment_calls
        return call["components"]

    def test_raised_quota_raises_the_shadow_manager_limit(self, sm_env,
                                                          monkeypatch):
        quotas = FakeServiceQuotas(value=16384.0)
        components = self.deploy(sm_env, monkeypatch, quotas)
        assert submitted_merge(components) == dict(
            EXPECTED_SYNC_CONFIG, **{LIMIT_KEY: 16384})
        assert quotas.calls == [("iotcore", "L-A295A064")]

    def test_default_quota_submits_the_unchanged_sync_config(self, sm_env,
                                                             monkeypatch):
        components = self.deploy(sm_env, monkeypatch, FakeServiceQuotas(8192.0))
        assert submitted_merge(components) == EXPECTED_SYNC_CONFIG

    def test_unreadable_quota_never_fails_the_deployment(self, sm_env,
                                                         monkeypatch):
        components = self.deploy(
            sm_env, monkeypatch,
            FakeServiceQuotas(error="AccessDeniedException"))
        assert submitted_merge(components) == EXPECTED_SYNC_CONFIG

    def test_non_local_server_deployment_reads_no_quota(self, sm_env,
                                                        monkeypatch):
        quotas = FakeServiceQuotas(value=16384.0)
        with_quotas(monkeypatch, sm_env.deployments, quotas)
        status, payload = sm_env.deploy_components(
            [{"component_name": "com.example.CustomComponent",
              "component_version": "1.0.0"}],
            target_devices=["line-a-camera-01"])
        assert status == 201, payload
        [call] = sm_env.gg.create_deployment_calls
        assert SHADOW_MANAGER not in call["components"]
        assert quotas.calls == []


class TestWorkflowRevisionCarriesTheQuota:

    @pytest.fixture
    def wf_env(self, env, deployments, monkeypatch):
        return WorkflowDeployEnv(env, deployments, monkeypatch)

    def revise(self, wf_env, monkeypatch, quotas, shadow_manager):
        with_quotas(monkeypatch, wf_env.deployments, quotas)
        workflow_id = wf_env.seed_subscribing_workflow(topics=None)
        wf_env.register_device()
        wf_env.gg.seed_deployment(wf_env.thing_arn(), {
            LOCAL_SERVER_ARM64JP6: {"componentVersion": "1.0.51"},
            SHADOW_MANAGER: shadow_manager,
        })
        status, payload = wf_env.deploy(workflow_id)
        assert status == 201, payload
        assert payload["is_revision"] is True
        return wf_env.submitted_components()

    def test_carried_over_entry_gets_the_raised_limit(self, wf_env,
                                                      monkeypatch):
        components = self.revise(
            wf_env, monkeypatch, FakeServiceQuotas(value=16384.0),
            shadow_manager_entry(EXPECTED_SYNC_CONFIG))
        assert submitted_merge(components) == dict(
            EXPECTED_SYNC_CONFIG, **{LIMIT_KEY: 16384})

    def test_default_quota_keeps_the_carried_merge_byte_identical(
            self, wf_env, monkeypatch):
        entry = shadow_manager_entry(EXPECTED_SYNC_CONFIG)
        components = self.revise(wf_env, monkeypatch,
                                 FakeServiceQuotas(8192.0), dict(entry))
        assert (components[SHADOW_MANAGER]["configurationUpdate"]["merge"]
                == entry["configurationUpdate"]["merge"])

    def test_unreadable_quota_keeps_the_carried_limit(self, wf_env,
                                                      monkeypatch):
        entry = shadow_manager_entry(dict(EXPECTED_SYNC_CONFIG,
                                          **{LIMIT_KEY: 16384}))
        components = self.revise(
            wf_env, monkeypatch,
            FakeServiceQuotas(error="AccessDeniedException"), dict(entry))
        assert submitted_merge(components)[LIMIT_KEY] == 16384

    def test_fresh_workflow_deployment_reads_no_quota(self, wf_env,
                                                      monkeypatch):
        quotas = FakeServiceQuotas(value=16384.0)
        with_quotas(monkeypatch, wf_env.deployments, quotas)
        workflow_id = wf_env.seed_subscribing_workflow(topics=None)
        wf_env.register_device()
        status, payload = wf_env.deploy(workflow_id)
        assert status == 201, payload
        assert SHADOW_MANAGER not in wf_env.submitted_components()
        assert quotas.calls == []
