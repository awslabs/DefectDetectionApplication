"""
ShadowManager auto-include in Portal-created deployments
(functions/deployments.py create_deployment).

Bugfix: Portal-created deployments never configured
aws.greengrass.ShadowManager, so it ran with its default config (no
``synchronize`` section) and the camera-registry-sync named shadows —
``dda-camera-registry`` (written on the edge by
src/backend/camera_sync/agent.py, device -> cloud) and
``dda-camera-bindings`` (read on the edge by
src/backend/workflow_engine/camera_binding_store.py, cloud -> device) —
never mirrored to IoT Core. The Portal then reported "Device has no
camera registry shadow to refresh from" and devices stayed
"Never synced".

The fix auto-includes aws.greengrass.ShadowManager (unpinned, like the
unpinned-Nucleus fallback — it is already an implicit dependency of the
LocalServer component with VersionRequirement >=2.2.0) with a
``synchronize`` configurationUpdate merge whenever a deployment carries a
LocalServer component (``needs_nucleus``), unless the caller already
supplies ShadowManager themselves.
"""
import json
import sys
import uuid

import pytest

from test_workflow_packaging_deployment_integration import (
    ACCOUNT_ID, FakeGreengrass, FakeIot)


@pytest.fixture(scope="module")
def deployments(aws_stack):
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


EXPECTED_SYNC_CONFIG = {
    "synchronize": {
        "direction": "betweenDeviceAndCloud",
        "coreThing": {
            "classic": True,
            "namedShadows": ["dda-camera-registry", "dda-camera-bindings"],
        },
    }
}

LOCAL_SERVER_COMPONENT = {
    "component_name": "aws.edgeml.dda.LocalServer.x86_64",
    "component_version": "1.2.0",
}


class ShadowManagerEnv:
    """Minimal create_deployment harness: a Use_Case with the stateful
    Greengrass/IoT fakes wired in as the Use_Case-account clients."""

    def __init__(self, env, deployments, monkeypatch):
        self.env = env
        self.deployments = deployments

        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "ShadowManager Sync Test",
            "account_id": ACCOUNT_ID,
        })

        self.gg = FakeGreengrass()
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


@pytest.fixture
def sm_env(env, deployments, monkeypatch):
    return ShadowManagerEnv(env, deployments, monkeypatch)


class TestShadowManagerAutoInclude:
    def test_local_server_deployment_auto_includes_shadow_manager(self, sm_env):
        """A deployment carrying a LocalServer component auto-includes
        aws.greengrass.ShadowManager (unpinned) with the synchronize merge
        config for the two camera-registry-sync named shadows."""
        status, payload = sm_env.deploy_components(
            [LOCAL_SERVER_COMPONENT], target_devices=["line-a-camera-01"])

        assert status == 201, payload
        [call] = sm_env.gg.create_deployment_calls
        assert "aws.greengrass.ShadowManager" in call["components"]

        shadow_manager = call["components"]["aws.greengrass.ShadowManager"]
        # Unpinned: ShadowManager is an implicit dependency of LocalServer
        # (VersionRequirement >=2.2.0); Greengrass resolves the version.
        assert "componentVersion" not in shadow_manager

        merged = json.loads(shadow_manager["configurationUpdate"]["merge"])
        assert merged == EXPECTED_SYNC_CONFIG

        # Reported to the caller alongside the other auto-included entries.
        [entry] = [e for e in payload["auto_included"]
                   if e["component_name"] == "aws.greengrass.ShadowManager"]
        assert entry["component_version"] == "auto"
        assert "dda-camera-registry" in entry["reason"]
        assert "dda-camera-bindings" in entry["reason"]

    def test_caller_supplied_shadow_manager_is_not_overridden(self, sm_env):
        """When the caller already includes aws.greengrass.ShadowManager the
        auto-include is skipped: their pinned version/config is submitted
        untouched and no auto_included entry is reported."""
        status, payload = sm_env.deploy_components(
            [LOCAL_SERVER_COMPONENT,
             {"component_name": "aws.greengrass.ShadowManager",
              "component_version": "2.3.5"}],
            target_devices=["line-a-camera-01"])

        assert status == 201, payload
        [call] = sm_env.gg.create_deployment_calls
        assert call["components"]["aws.greengrass.ShadowManager"] == {
            "componentVersion": "2.3.5"}
        assert [e for e in payload["auto_included"]
                if e["component_name"] == "aws.greengrass.ShadowManager"] == []

    def test_not_included_without_local_server_component(self, sm_env):
        """Deployments with no DDA/LocalServer component (needs_nucleus
        false) don't pull in ShadowManager (nor the other DDA-driven
        auto-includes)."""
        status, payload = sm_env.deploy_components(
            [{"component_name": "com.example.CustomComponent",
              "component_version": "1.0.0"}],
            target_devices=["line-a-camera-01"])

        assert status == 201, payload
        [call] = sm_env.gg.create_deployment_calls
        assert "aws.greengrass.ShadowManager" not in call["components"]
        assert payload["auto_included"] == []
