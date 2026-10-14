"""
Component store size limit: prevention + automatic remediation
(functions/deployments.py).

Bugfix: devices retaining two fat LocalServer artifacts exceed the Nucleus
default componentStoreMaxSizeBytes (10 GB), after which EVERY deployment is
rejected during artifact download with FAILED_NO_STATE_CHANGE
"Component store size limit reached" — before the deployment's own
configuration merge is applied, so a single deployment cannot both raise
the limit and ship new artifacts. Users had to SSH in and fix devices by
hand.

The fix is two-part and requires no user intervention:

1. Prevention — every portal deployment's Nucleus entry carries a
   configurationUpdate merge raising componentStoreMaxSizeBytes
   (COMPONENT_STORE_MAX_SIZE_BYTES, default 30 GB), so healthy devices
   receive the raised limit before ever hitting the default.

2. Remediation — a scheduled sweep (remediate_component_store_failures,
   EventBridge action remediate-component-store-limit) advances a
   tag-driven state machine per blocked device:
     FAILED(store limit)   -> submit a config-only revision pinning the
                              device's *installed* ROOT components
                              (nothing to download, passes the pre-merge
                              store check) + the raised limit
                              [tag: dda-portal:store-remediation-for]
     COMPLETED remediation -> resubmit the original blocked deployment
                              [tag: dda-portal:store-remediation-resumed]
   with loop guards so a device that still cannot fit under the raised
   limit is reported rather than remediated forever.
"""
import json
import sys
import uuid

import pytest

from test_workflow_packaging_deployment_integration import (
    ACCOUNT_ID, REGION, FakeGreengrass, FakeIot)


@pytest.fixture(scope="module")
def deployments(aws_stack):
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


LOCAL_SERVER_COMPONENT = {
    "component_name": "aws.edgeml.dda.LocalServer.x86_64",
    "component_version": "1.2.0",
}


def thing_arn(thing_name):
    return f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{thing_name}"


STORE_LIMIT_REASON = (
    "FAILED_NO_STATE_CHANGE: Component store size limit reached: "
    "12652375003 bytes existing, 46432464 bytes needed, "
    "10000000000 bytes maximum allowed total")


class RemediationFakeGreengrass(FakeGreengrass):
    """FakeGreengrass extended with the surface the remediation sweep
    uses: core-device listing, deployment tags, and effective entries
    carrying targetArn."""

    def __init__(self):
        super().__init__()
        self.core_devices = []
        self.tags = {}  # deployment_id -> tags

    def register_core_device(self, thing_name):
        self.core_devices.append(thing_name)

    def _pages_list_core_devices(self, **_):
        return [{"coreDevices": [
            {"coreDeviceThingName": name} for name in self.core_devices]}]

    def seed_deployment(self, target_arn, components, name="pre-existing",
                        tags=None):
        deployment_id = super().seed_deployment(target_arn, components, name)
        self.tags[deployment_id] = dict(tags or {})
        return deployment_id

    def create_deployment(self, **params):
        response = super().create_deployment(**params)
        self.tags[response["deploymentId"]] = dict(params.get("tags", {}))
        return response

    def get_deployment(self, deploymentId=None):
        record = super().get_deployment(deploymentId)
        record["tags"] = dict(self.tags.get(deploymentId, {}))
        return record

    def report_effective(self, thing_name, deployment_id, status, reason="",
                         target_arn=None):
        entry = {
            "deploymentId": deployment_id,
            "coreDeviceExecutionStatus": status,
            "reason": reason,
            "description": "",
        }
        if target_arn:
            entry["targetArn"] = target_arn
        # The sweep reads effectiveDeployments[0] as the latest (matching
        # the real API's newest-first ordering), so prepend.
        self.effective.setdefault(thing_name, []).insert(0, entry)


class StoreLimitEnv:
    """Harness: one Use_Case wired to the remediation-capable fakes, with
    both the create_deployment API path and the scheduled sweep entry."""

    def __init__(self, env, deployments, monkeypatch):
        self.env = env
        self.deployments = deployments

        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Store Limit Test",
            "account_id": ACCOUNT_ID,
        })

        self.gg = RemediationFakeGreengrass()
        self.iot = FakeIot()

        def deployment_client(service_name, usecase, session_name=None,
                              region=None):
            assert usecase["usecase_id"] == self.usecase_id
            if service_name == "greengrassv2":
                return self.gg
            if service_name == "iot":
                return self.iot
            raise AssertionError(f"unexpected client: {service_name}")

        monkeypatch.setattr(deployments, "get_usecase_client",
                            deployment_client)

    def deploy_components(self, components, **body):
        body = {"usecase_id": self.usecase_id, "components": components,
                **body}
        event = self.env.event("POST", "/deployments", self.user, body=body)
        response = self.deployments.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def run_sweep(self):
        return self.deployments.handler(
            {"action": "remediate-component-store-limit"}, None)


@pytest.fixture
def store_env(env, deployments, monkeypatch):
    return StoreLimitEnv(env, deployments, monkeypatch)


def nucleus_merge(call_or_entry):
    entry = (call_or_entry["components"]["aws.greengrass.Nucleus"]
             if "components" in call_or_entry else call_or_entry)
    return json.loads(entry["configurationUpdate"]["merge"])


class TestPreventionMerge:
    def test_pinned_nucleus_carries_store_limit_merge(self, store_env):
        """A LocalServer deployment to a device with a resolvable running
        Nucleus pins Nucleus AND merges the raised store limit."""
        store_env.gg.register_device("dev-1", nucleus_version="2.12.0")
        status, payload = store_env.deploy_components(
            [LOCAL_SERVER_COMPONENT], target_devices=["dev-1"])

        assert status == 201, payload
        [call] = store_env.gg.create_deployment_calls
        nucleus = call["components"]["aws.greengrass.Nucleus"]
        assert nucleus["componentVersion"] == "2.12.0"
        assert nucleus_merge(call) == {
            "componentStoreMaxSizeBytes":
                store_env.deployments.COMPONENT_STORE_MAX_SIZE_BYTES}

    def test_unpinned_nucleus_fallback_carries_store_limit_merge(
            self, store_env):
        """When the running Nucleus cannot be resolved the unpinned
        fallback entry still carries the merge."""
        status, payload = store_env.deploy_components(
            [LOCAL_SERVER_COMPONENT], target_devices=["dev-unknown"])

        assert status == 201, payload
        [call] = store_env.gg.create_deployment_calls
        nucleus = call["components"]["aws.greengrass.Nucleus"]
        assert "componentVersion" not in nucleus
        assert nucleus_merge(call) == {
            "componentStoreMaxSizeBytes":
                store_env.deployments.COMPONENT_STORE_MAX_SIZE_BYTES}

    def test_caller_supplied_nucleus_gains_merge(self, store_env):
        """A caller pinning Nucleus themselves keeps their pin but still
        receives the store-limit merge (they cannot set configurationUpdate
        through the portal API, so nothing of theirs is overwritten)."""
        store_env.gg.register_device("dev-1", nucleus_version="2.12.0")
        status, payload = store_env.deploy_components(
            [LOCAL_SERVER_COMPONENT,
             {"component_name": "aws.greengrass.Nucleus",
              "component_version": "2.12.0"}],
            target_devices=["dev-1"])

        assert status == 201, payload
        [call] = store_env.gg.create_deployment_calls
        nucleus = call["components"]["aws.greengrass.Nucleus"]
        assert nucleus["componentVersion"] == "2.12.0"
        assert nucleus_merge(call)["componentStoreMaxSizeBytes"] == (
            store_env.deployments.COMPONENT_STORE_MAX_SIZE_BYTES)


class TestRemediationSweep:
    def _seed_blocked_device(self, store_env, thing_name="edge-1"):
        """A core device whose latest (thing-targeted) deployment failed on
        the store limit, with a LocalServer root already installed."""
        gg = store_env.gg
        gg.register_core_device(thing_name)
        gg.register_device(thing_name, local_server_version="1.0.29",
                           nucleus_version="2.12.0")
        blocked_id = gg.seed_deployment(
            thing_arn(thing_name),
            {"aws.edgeml.dda.LocalServer.x86_64":
                 {"componentVersion": "1.0.30"},
             "aws.greengrass.Nucleus": {"componentVersion": "2.12.0"}},
            name="portal-deployment-blocked")
        gg.report_effective(thing_name, blocked_id, "FAILED",
                            STORE_LIMIT_REASON,
                            target_arn=thing_arn(thing_name))
        return blocked_id

    def test_store_limit_failure_submits_config_only_remediation(
            self, store_env):
        """State 1: the sweep answers a store-limit failure with a revision
        pinning the installed ROOT set (no downloads) plus the raised limit,
        tagged back to the blocked deployment."""
        blocked_id = self._seed_blocked_device(store_env)

        result = store_env.run_sweep()

        [action] = result["actions"]
        assert action["action"] == "remediation_submitted"
        assert action["blocked_deployment_id"] == blocked_id

        [call] = store_env.gg.create_deployment_calls
        # Installed root pinned exactly as installed — nothing new to fetch.
        assert call["components"]["aws.edgeml.dda.LocalServer.x86_64"] == {
            "componentVersion": "1.0.29"}
        nucleus = call["components"]["aws.greengrass.Nucleus"]
        assert nucleus["componentVersion"] == "2.12.0"
        assert nucleus_merge(call)["componentStoreMaxSizeBytes"] == (
            store_env.deployments.COMPONENT_STORE_MAX_SIZE_BYTES)
        assert call["tags"][
            store_env.deployments.TAG_REMEDIATION_FOR] == blocked_id

    def test_completed_remediation_resumes_original_deployment(
            self, store_env):
        """State 2: once the remediation revision completes, the sweep
        resubmits the original blocked component set, with the store merge
        injected so pre-fix documents can't reintroduce the default."""
        gg = store_env.gg
        thing_name = "edge-2"
        gg.register_core_device(thing_name)
        gg.register_device(thing_name, local_server_version="1.0.29",
                           nucleus_version="2.12.0")

        original_id = gg.seed_deployment(
            thing_arn(thing_name),
            {"aws.edgeml.dda.LocalServer.x86_64":
                 {"componentVersion": "1.0.30"},
             "aws.greengrass.Nucleus": {"componentVersion": "2.12.0"}},
            name="portal-deployment-blocked")
        remediation_id = gg.seed_deployment(
            thing_arn(thing_name),
            {"aws.edgeml.dda.LocalServer.x86_64":
                 {"componentVersion": "1.0.29"}},
            name="portal-deployment-blocked",
            tags={store_env.deployments.TAG_REMEDIATION_FOR: original_id})
        gg.report_effective(thing_name, remediation_id, "COMPLETED",
                            target_arn=thing_arn(thing_name))

        result = store_env.run_sweep()

        [action] = result["actions"]
        assert action["action"] == "original_resumed"
        assert action["original_deployment_id"] == original_id

        [call] = store_env.gg.create_deployment_calls
        assert call["components"]["aws.edgeml.dda.LocalServer.x86_64"] == {
            "componentVersion": "1.0.30"}
        assert nucleus_merge(call)["componentStoreMaxSizeBytes"] == (
            store_env.deployments.COMPONENT_STORE_MAX_SIZE_BYTES)
        assert call["tags"][
            store_env.deployments.TAG_RESUMED_FROM] == original_id

    def test_resumed_deployment_failing_again_is_not_looped(self, store_env):
        """Loop guard: a resumed deployment that STILL hits the store limit
        (device genuinely cannot fit under the raised cap) is reported, not
        remediated again."""
        gg = store_env.gg
        thing_name = "edge-3"
        gg.register_core_device(thing_name)
        gg.register_device(thing_name, local_server_version="1.0.29",
                           nucleus_version="2.12.0")
        resumed_id = gg.seed_deployment(
            thing_arn(thing_name),
            {"aws.edgeml.dda.LocalServer.x86_64":
                 {"componentVersion": "1.0.30"}},
            tags={store_env.deployments.TAG_RESUMED_FROM: "dep-original"})
        gg.report_effective(thing_name, resumed_id, "FAILED",
                            STORE_LIMIT_REASON,
                            target_arn=thing_arn(thing_name))

        result = store_env.run_sweep()

        assert result["actions"] == []
        assert store_env.gg.create_deployment_calls == []

    def test_unrelated_failures_are_untouched(self, store_env):
        """Deployments failing for any other reason are outside the state
        machine: no remediation is submitted."""
        gg = store_env.gg
        thing_name = "edge-4"
        gg.register_core_device(thing_name)
        gg.register_device(thing_name, local_server_version="1.0.29",
                           nucleus_version="2.12.0")
        failed_id = gg.seed_deployment(
            thing_arn(thing_name),
            {"aws.edgeml.dda.LocalServer.x86_64":
                 {"componentVersion": "1.0.30"}})
        gg.report_effective(
            thing_name, failed_id, "FAILED",
            "FAILED_ROLLBACK_COMPLETE: Service model-x in broken state",
            target_arn=thing_arn(thing_name))

        result = store_env.run_sweep()

        assert result["actions"] == []
        assert store_env.gg.create_deployment_calls == []

    def test_healthy_devices_are_untouched(self, store_env):
        """Devices whose latest deployment completed are outside the state
        machine (a completed NON-remediation deployment must not trigger
        the resume branch)."""
        gg = store_env.gg
        thing_name = "edge-5"
        gg.register_core_device(thing_name)
        gg.register_device(thing_name, local_server_version="1.0.30",
                           nucleus_version="2.12.0")
        completed_id = gg.seed_deployment(
            thing_arn(thing_name),
            {"aws.edgeml.dda.LocalServer.x86_64":
                 {"componentVersion": "1.0.30"}})
        gg.report_effective(thing_name, completed_id, "COMPLETED",
                            target_arn=thing_arn(thing_name))

        result = store_env.run_sweep()

        assert result["actions"] == []
        assert store_env.gg.create_deployment_calls == []
