"""
Property-based test for deploy-time Camera_Binding completeness
validation — validate_camera_bindings (functions/deployments.py).

**Feature: camera-registry-sync, Property 13: Binding completeness validation**

*For any* workflow version with binding points, any set of target
devices, and any bindings map, deployment validation reports an unbound
error identifying exactly the (Camera_Input_Node, Edge_Device) pairs
missing from the map — including maps that bind the same node to
different sources on different devices without error — and reports
nothing for workflow versions containing no Camera_Input_Nodes.

**Validates: Requirements 8.3, 8.7, 8.9**

Task 11.2 (spec: camera-registry-sync). The example-based unit tests over
the same function live in test_camera_binding_validation.py (task 11.1).
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

#: Per-(device, node) binding shapes. "missing" and "empty" are the two
#: unbound forms (no entry at all / an entry carrying neither a
#: cameraSourceId nor an override); "source" and "override" are the two
#: bound forms of Requirement 8.7.
_BINDING_KINDS = st.sampled_from(["missing", "empty", "source", "override"])


def _clean_registry_entry():
    """A synced, present, non-stale Camera-type entry: bound sources are
    kept healthy and compatible so completeness is the only signal."""
    return {"type": "Camera",
            "params": {"devicePath": "/dev/video0"},
            "sync_status": "synced",
            "absent": False,
            "stale": False}


@st.composite
def _completeness_cases(draw):
    """A version with binding points, targets, per-device registries, and
    a bindings map with a known set of unbound (device, node) pairs.

    Bound registered-source bindings use a source id unique to the
    device (``cfg-{device}-{node}``), so any case where the same node is
    bound on several devices exercises the distinct-bindings-per-device
    acceptance of Requirement 8.3 by construction.
    """
    node_ids = draw(st.lists(st.sampled_from(_NODE_IDS),
                             unique=True, min_size=1, max_size=3))
    node_types = {
        node_id: draw(st.sampled_from(["icam_source", "acme.cam_input"]))
        for node_id in node_ids
    }
    targets = draw(st.lists(st.sampled_from(_DEVICES),
                            unique=True, min_size=1, max_size=3))

    registry_snapshot = {}
    bindings = {}
    unbound_pairs = set()
    for device in targets:
        cameras = {}
        device_bindings = {}
        for node_id in node_ids:
            kind = draw(_BINDING_KINDS)
            if kind == "override" and node_types[node_id] != "icam_source":
                # Overrides for custom types need caller-supplied
                # descriptors; out of scope for completeness.
                kind = "source"
            if kind == "missing":
                unbound_pairs.add((device, node_id))
            elif kind == "empty":
                device_bindings[node_id] = {}
                unbound_pairs.add((device, node_id))
            elif kind == "source":
                camera_source_id = f"cfg-{device}-{node_id}"
                cameras[camera_source_id] = _clean_registry_entry()
                device_bindings[node_id] = {
                    "cameraSourceId": camera_source_id}
            else:
                device_bindings[node_id] = {
                    "override": {"device": "/dev/video1"}}
        # A device with no bound nodes may be missing from the map
        # entirely or present with an empty entry — both mean unbound.
        if device_bindings or draw(st.booleans()):
            bindings[device] = device_bindings
        registry_snapshot[device] = {"never_synced": False,
                                     "cameras": cameras}

    version = {
        "has_binding_points": True,
        "camera_input_nodes": [
            {"node_id": node_id,
             "node_type": node_types[node_id],
             "compiled_device_paths": {"x86_64": "/dev/video0"}}
            for node_id in node_ids
        ],
    }
    return version, targets, registry_snapshot, bindings, unbound_pairs


@st.composite
def _no_camera_node_cases(draw):
    """A version without Camera_Input_Nodes plus arbitrary targets,
    registries (including never-synced and degraded entries), and
    bindings maps — none of which may produce output (Req 8.9)."""
    targets = draw(st.lists(st.sampled_from(_DEVICES),
                            unique=True, max_size=3))
    registry_snapshot = {}
    bindings = {}
    for device in targets:
        if draw(st.booleans()):
            registry_snapshot[device] = {
                "never_synced": draw(st.booleans()),
                "cameras": {
                    "cfg-1": {
                        "type": draw(st.sampled_from(
                            ["Camera", "Folder", "RTSP"])),
                        "params": {"devicePath": "/dev/video0"},
                        "sync_status": draw(st.sampled_from(
                            ["synced", "pending", "failed"])),
                        "absent": draw(st.booleans()),
                        "stale": draw(st.booleans()),
                    },
                },
            }
        if draw(st.booleans()):
            bindings[device] = {
                "n1": draw(st.sampled_from(
                    [{}, {"cameraSourceId": "cfg-1"},
                     {"override": {"gain": 5}}])),
            }
    version = {"has_binding_points": draw(st.booleans()),
               "camera_input_nodes": []}
    confirmed = draw(st.lists(st.sampled_from(["w-1", "w-2"]), max_size=2))
    return version, targets, registry_snapshot, bindings, confirmed


# ---------------------------------------------------------------------------
# Property 13
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_completeness_cases())
def test_unbound_errors_identify_exactly_the_missing_pairs(deployments, case):
    """**Feature: camera-registry-sync, Property 13: Binding completeness
    validation**

    **Validates: Requirements 8.3, 8.7**

    Unbound errors are produced exactly for the (node, device) pairs
    missing a binding, each identifying its node and device; distinct
    per-device bindings for the same node are accepted without error.
    """
    version, targets, registry_snapshot, bindings, unbound_pairs = case

    errors, warnings = deployments.validate_camera_bindings(
        version, targets, registry_snapshot, bindings, [])

    unbound_errors = [e for e in errors
                      if e["code"] == deployments.CAMERA_ERROR_UNBOUND]

    # Exactly one unbound error per missing (device, node) pair — no
    # pair reported twice, no bound pair reported (8.7).
    assert {(e["device"], e["nodeId"]) for e in unbound_errors} \
        == unbound_pairs
    assert len(unbound_errors) == len(unbound_pairs)

    # Each error message identifies the node and the device (8.7).
    for error in unbound_errors:
        assert error["nodeId"] in error["message"]
        assert error["device"] in error["message"]

    # Every bound pair is a healthy, compatible source or a valid
    # override — including the same node bound to different sources on
    # different devices — so completeness is the only error and there
    # are no warnings (8.3).
    assert errors == unbound_errors
    assert warnings == []


@settings(deadline=None)
@given(_no_camera_node_cases())
def test_version_without_camera_nodes_reports_nothing(deployments, case):
    """**Feature: camera-registry-sync, Property 13: Binding completeness
    validation**

    **Validates: Requirements 8.9**

    A version containing no Camera_Input_Nodes produces no errors and no
    warnings for any targets, registry snapshots, or bindings maps.
    """
    version, targets, registry_snapshot, bindings, confirmed = case

    errors, warnings = deployments.validate_camera_bindings(
        version, targets, registry_snapshot, bindings, confirmed)

    assert errors == []
    assert warnings == []
