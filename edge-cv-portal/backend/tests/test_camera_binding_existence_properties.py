"""
Property-based test for deploy-time Camera_Binding existence
validation — validate_camera_bindings (functions/deployments.py).

**Feature: camera-registry-sync, Property 14: Binding existence validation**

*For any* bindings map and any per-device registry snapshots, validation
reports a missing-source error identifying the Camera_Source and
Edge_Device exactly for those bindings whose referenced `cameraSourceId`
is not present in that device's registry snapshot.

**Validates: Requirements 9.1, 9.2**

Task 11.3 (spec: camera-registry-sync). The example-based unit tests over
the same function live in test_camera_binding_validation.py (task 11.1);
the completeness property (task 11.2) lives in
test_camera_binding_completeness_properties.py.
"""
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """Import deployments inside the moto mock so its module-level boto3
    clients are intercepted."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_NODE_IDS = ["n1", "n2", "n3"]
_DEVICES = ["line-a", "line-b", "line-c"]


@st.composite
def _registry_entries(draw):
    """A registry entry in an arbitrary state: any type (compatible or
    not), any sync status, any stale/absent flags. Existence is judged
    purely by presence in the camera map, whatever other errors or
    warnings the entry's state may produce."""
    params = {}
    if draw(st.booleans()):
        params["devicePath"] = draw(st.sampled_from(
            ["/dev/video0", "/dev/video1"]))
    return {
        "type": draw(st.sampled_from(
            ["Camera", "ICam", "NvidiaCSI", "Folder", "RTSP"])),
        "params": params,
        "sync_status": draw(st.sampled_from(["synced", "pending", "failed"])),
        "absent": draw(st.booleans()),
        "stale": draw(st.booleans()),
    }


@st.composite
def _existence_cases(draw):
    """A version with binding points, targets, per-device registries in
    arbitrary states, and a bindings map where every (device, node) pair
    references a cameraSourceId — some present in that device's registry,
    some absent from it, some on devices missing from the snapshot
    entirely (an empty registry by the fail-safe rule).

    Each referenced id is unique to its (device, node) pair
    (``cfg-{device}-{node}``), so presence on one device never masks
    absence on another — the per-device scoping of Requirement 9.1 is
    exercised by construction.
    """
    node_ids = draw(st.lists(st.sampled_from(_NODE_IDS),
                             unique=True, min_size=1, max_size=3))
    node_types = {
        node_id: draw(st.sampled_from(["camera_source", "acme.cam_input"]))
        for node_id in node_ids
    }
    targets = draw(st.lists(st.sampled_from(_DEVICES),
                            unique=True, min_size=1, max_size=3))

    registry_snapshot = {}
    bindings = {}
    missing_refs = set()
    for device in targets:
        # A device may be missing from the snapshot entirely: treated as
        # never synced with an empty registry, so every reference on it
        # is a missing source.
        snapshot_missing = draw(st.booleans())
        cameras = {}
        device_bindings = {}
        for node_id in node_ids:
            camera_source_id = f"cfg-{device}-{node_id}"
            present = draw(st.booleans()) and not snapshot_missing
            if present:
                cameras[camera_source_id] = draw(_registry_entries())
            else:
                missing_refs.add((device, node_id, camera_source_id))
            device_bindings[node_id] = {"cameraSourceId": camera_source_id}
        bindings[device] = device_bindings
        if not snapshot_missing:
            # Unreferenced entries must not affect the outcome.
            if draw(st.booleans()):
                cameras[f"extra-{device}"] = draw(_registry_entries())
            registry_snapshot[device] = {
                "never_synced": draw(st.booleans()),
                "cameras": cameras,
            }

    version = {
        "has_binding_points": True,
        "camera_input_nodes": [
            {"node_id": node_id,
             "node_type": node_types[node_id],
             "compiled_device_paths": {"x86_64": "/dev/video0"}}
            for node_id in node_ids
        ],
    }
    return version, targets, registry_snapshot, bindings, missing_refs


# ---------------------------------------------------------------------------
# Property 14
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_existence_cases())
def test_missing_source_errors_identify_exactly_the_absent_references(
        deployments, case):
    """**Feature: camera-registry-sync, Property 14: Binding existence
    validation**

    **Validates: Requirements 9.1, 9.2**

    A missing-source error is produced exactly for the (device, node)
    pairs whose referenced cameraSourceId is absent from that device's
    registry snapshot, each identifying the Camera_Source and the
    Edge_Device; references present in the registry pass the existence
    check regardless of the entry's type, sync status, or stale/absent
    flags.
    """
    version, targets, registry_snapshot, bindings, missing_refs = case

    errors, warnings = deployments.validate_camera_bindings(
        version, targets, registry_snapshot, bindings, [])

    missing_errors = [
        e for e in errors
        if e["code"] == deployments.CAMERA_ERROR_SOURCE_MISSING]

    # Exactly one missing-source error per absent reference — nothing
    # reported for ids present in the target's registry (9.1).
    assert {(e["device"], e["nodeId"], e["cameraSourceId"])
            for e in missing_errors} == missing_refs
    assert len(missing_errors) == len(missing_refs)

    # Each error identifies the missing Camera_Source and the
    # Edge_Device (9.2).
    for error in missing_errors:
        assert error["cameraSourceId"] in error["message"]
        assert error["device"] in error["message"]
