"""ShadowManager sync entry for the dda-model-status shadow (Task 4.4).

Property 4: Fix Checking — Device-Level Aggregation and Portal Display
(portal leg; design fix-check case 8, model-gpu-fallback-visibility).

Focused checks that ``deployments.py`` carries the ``dda-model-status``
named shadow into the ShadowManager synchronize auto-include:

- the module constant exists with the exact shadow name and stays in sync
  with the ``devices.py`` read-side constant;
- a Portal-created LocalServer deployment's ShadowManager synchronize
  config lists ``dda-model-status`` in ``namedShadows`` ALONGSIDE the two
  camera-registry-sync shadows;
- the ``auto_included`` reason string reported to the caller names it.

Deliberately thin: the full ShadowManager auto-include behavior matrix
(version pinning, caller-supplied override, no-LocalServer skip, the exact
pinned EXPECTED_SYNC_CONFIG) is already covered by
``test_deployment_shadow_manager.py`` — this module reuses that suite's
harness for ONE deployment and asserts only the model-status membership,
without duplicating the rest.

# Validates: Requirements 2.5, 3.4
"""
import json
import sys

import pytest

from test_deployment_shadow_manager import (
    LOCAL_SERVER_COMPONENT, ShadowManagerEnv)

MODEL_STATUS_SHADOW_NAME = "dda-model-status"


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """Import deployments inside the moto mock (the established pattern)."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


@pytest.fixture(scope="module")
def devices(aws_stack):
    """Import devices inside the moto mock for the keep-in-sync check."""
    for module_name in ("devices", "deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import devices

    return devices


@pytest.fixture
def sm_env(env, deployments, monkeypatch):
    return ShadowManagerEnv(env, deployments, monkeypatch)


def test_model_status_shadow_constant(deployments, devices):
    """The deployments.py constant carries the exact shadow name and the
    devices.py read side uses the SAME name (keep-in-sync — a drift here
    would silently break the cloud leg: the shadow would sync under one
    name and be read under another).

    # Validates: Requirements 2.5, 3.4
    """
    assert deployments.MODEL_STATUS_SHADOW_NAME == MODEL_STATUS_SHADOW_NAME
    assert devices.MODEL_STATUS_SHADOW_NAME == MODEL_STATUS_SHADOW_NAME


def test_sync_entry_lists_model_status_alongside_camera_shadows(sm_env):
    """A Portal-created LocalServer deployment auto-includes ShadowManager
    whose synchronize config lists dda-model-status in
    coreThing.namedShadows ALONGSIDE the camera shadows, and the
    auto_included reason string names it.

    # Validates: Requirements 2.5, 3.4
    """
    status, payload = sm_env.deploy_components(
        [LOCAL_SERVER_COMPONENT], target_devices=["line-a-camera-01"])

    assert status == 201, payload
    [call] = sm_env.gg.create_deployment_calls
    shadow_manager = call["components"]["aws.greengrass.ShadowManager"]
    merged = json.loads(shadow_manager["configurationUpdate"]["merge"])
    named_shadows = merged["synchronize"]["coreThing"]["namedShadows"]

    deployments = sm_env.deployments
    assert deployments.MODEL_STATUS_SHADOW_NAME in named_shadows
    # ALONGSIDE the camera shadows — the model-status entry must never
    # displace the camera-registry-sync entries.
    assert deployments.CAMERA_REGISTRY_SHADOW_NAME in named_shadows
    assert deployments.CAMERA_BINDINGS_SHADOW_NAME in named_shadows

    # The auto_included reason reported to the caller names the shadow.
    [entry] = [e for e in payload["auto_included"]
               if e["component_name"] == "aws.greengrass.ShadowManager"]
    assert MODEL_STATUS_SHADOW_NAME in entry["reason"]
