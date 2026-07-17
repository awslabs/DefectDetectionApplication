"""
Property-based test for the Aravis type-compatibility rule of
deploy-time Camera_Binding validation — validate_camera_bindings
(functions/deployments.py).

**Feature: aravis-camera-input, Property 11: Aravis type-compatibility validation**

*For any* workflow version with Camera_Input_Nodes, target registry
snapshot, and binding set, `validate_camera_bindings` SHALL produce a
type-incompatibility error for a binding exactly when the bound
Camera_Source's type is outside the node type's declared compatible set
— `{Camera, AravisDiscovered}` for `aravis_camera_source`, the existing
set plus `AravisDiscovered` for `camera_source`.

**Validates: Requirements 5.2, 5.3**

Task 8.2 (spec: aravis-camera-input). The example-based unit tests over
the same rule live in test_camera_binding_validation.py (task 8.1); the
pre-feature type/override property (camera-registry-sync Property 16)
lives in test_camera_binding_type_override_properties.py.
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
# Compatibility-map oracle (restated from the requirements, independent
# of the implementation's _CAMERA_COMPATIBLE_SOURCE_TYPES)
# ---------------------------------------------------------------------------

#: camera_source keeps its pre-feature camera-backed set plus
#: AravisDiscovered (Requirement 5.3); aravis_camera_source must bind to
#: an Aravis-backed source (Requirement 5.2).
_COMPATIBLE = {
    "camera_source": frozenset({"Camera", "ICam", "NvidiaCSI",
                                "V4L2Discovered", "AravisDiscovered"}),
    "aravis_camera_source": frozenset({"Camera", "AravisDiscovered"}),
}

_NODE_TYPES = sorted(_COMPATIBLE)

#: Arbitrary registry source types: every type in either compatible set,
#: plus types outside both (Folder is Requirement 9.4's categorical
#: mismatch; RTSP/HTTPPull are network-stream types) and an unknown one.
_SOURCE_TYPES = ["Camera", "ICam", "NvidiaCSI", "V4L2Discovered",
                 "AravisDiscovered", "Folder", "RTSP", "HTTPPull",
                 "SomethingElse"]

_NODE_IDS = ["n1", "n2", "n3", "n4"]
_DEVICES = ["line-a", "line-b", "line-c"]


@st.composite
def _type_cases(draw):
    """A version item mixing aravis_camera_source and camera_source
    nodes, per-device registries whose entries carry arbitrary source
    types, and a binding set referencing them. Every referenced source
    exists and is healthy (synced, present, fresh), so type
    compatibility is the only possible error signal."""
    node_ids = draw(st.lists(st.sampled_from(_NODE_IDS),
                             unique=True, min_size=1, max_size=4))
    node_types = {node_id: draw(st.sampled_from(_NODE_TYPES))
                  for node_id in node_ids}
    # At least one Aravis node so every case exercises the new rule.
    node_types[node_ids[0]] = "aravis_camera_source"
    targets = draw(st.lists(st.sampled_from(_DEVICES),
                            unique=True, min_size=1, max_size=3))

    registry_snapshot = {}
    bindings = {}
    # (device, node_id) -> (camera_source_id, source_type)
    source_refs = {}

    for device in targets:
        cameras = {}
        device_bindings = {}
        for node_id in node_ids:
            source_type = draw(st.sampled_from(_SOURCE_TYPES))
            camera_source_id = f"src-{device}-{node_id}"
            cameras[camera_source_id] = {
                "type": source_type,
                "params": {"cameraId": "Aravis-Fake-GV01"},
                "sync_status": "synced",
                "absent": False,
                "stale": False,
            }
            device_bindings[node_id] = {"cameraSourceId": camera_source_id}
            source_refs[(device, node_id)] = (camera_source_id, source_type)
        registry_snapshot[device] = {"never_synced": False,
                                     "cameras": cameras}
        bindings[device] = device_bindings

    version = {
        "has_binding_points": True,
        "camera_input_nodes": [
            {"node_id": node_id, "node_type": node_types[node_id]}
            for node_id in node_ids
        ],
    }
    return version, targets, registry_snapshot, bindings, node_types, \
        source_refs


# ---------------------------------------------------------------------------
# Property 11
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_type_cases())
def test_aravis_type_incompatibility_is_flagged_exactly(deployments, case):
    """**Feature: aravis-camera-input, Property 11: Aravis
    type-compatibility validation**

    **Validates: Requirements 5.2, 5.3**

    A CAMERA_TYPE_INCOMPATIBLE error is produced exactly for the
    bindings whose Camera_Source type is outside the node type's
    compatible set per the compatibility-map oracle: an
    aravis_camera_source node accepts only {Camera, AravisDiscovered}
    (5.2), a camera_source node accepts its pre-feature set plus
    AravisDiscovered (5.3) — and no other error is produced for these
    healthy, fully bound cases.
    """
    (version, targets, registry_snapshot, bindings, node_types,
     source_refs) = case

    errors, warnings = deployments.validate_camera_bindings(
        version, targets, registry_snapshot, bindings, [])

    expected_mismatches = {
        (device, node_id, camera_source_id, source_type)
        for (device, node_id), (camera_source_id, source_type)
        in source_refs.items()
        if source_type not in _COMPATIBLE[node_types[node_id]]
    }

    type_errors = [
        e for e in errors
        if e["code"] == deployments.CAMERA_ERROR_TYPE_INCOMPATIBLE]

    # Exactly one type error per incompatible binding; every compatible
    # binding passes the type check.
    assert {(e["device"], e["nodeId"], e["cameraSourceId"], e["sourceType"])
            for e in type_errors} == expected_mismatches
    assert len(type_errors) == len(expected_mismatches)

    # Each error identifies both sides of the mismatch.
    for error in type_errors:
        assert error["nodeType"] == node_types[error["nodeId"]]
        assert error["sourceType"] in error["message"]
        assert error["nodeType"] in error["message"]

    # Healthy, present, fully bound sources: type compatibility is the
    # only signal — no other errors, no warnings.
    assert len(errors) == len(type_errors)
    assert warnings == []
