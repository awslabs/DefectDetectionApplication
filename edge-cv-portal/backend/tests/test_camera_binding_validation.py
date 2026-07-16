"""
Deploy-time Camera_Binding validation — validate_camera_bindings
(functions/deployments.py).

Task 11.1 (spec: camera-registry-sync). Example-based unit tests for the
pure pre-submit validator:

- Errors (reject): unbound Camera_Input_Node on any target when the
  version has binding points (8.7); referenced cameraSourceId absent from
  the target's registry (9.1, 9.2); Camera_Source type incompatible with
  the node type (9.4); override values violating the node type's declared
  parameter constraints via the workflow_core parameter validator (8.4).
- Warnings (require matching confirmed_warnings ids): bound source
  stale/absent/pending/failed (9.3); never-synced target restricted to
  manual override (8.8); legacy compiled-path check when
  has_binding_points is false — warnings only, never errors (9.5, 11.1).
- Distinct bindings per device for the same node accepted (8.3); versions
  with no Camera_Input_Nodes produce no errors or warnings (8.9).

The property tests over the same function are tasks 11.2-11.6.

_Requirements: 8.3, 8.4, 8.7, 8.8, 8.9, 9.1, 9.2, 9.3, 9.4, 9.5, 11.1_
"""
import sys

import pytest


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """Import deployments inside the moto mock so its module-level boto3
    clients are intercepted."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


# --------------------------------------------------------------------------
# Fixture data builders
# --------------------------------------------------------------------------

def camera_node(node_id="n1", node_type="camera_source", **extra):
    record = {"node_id": node_id, "node_type": node_type,
              "compiled_device_paths": {"x86_64": "/dev/video0"}}
    record.update(extra)
    return record


def version_item(nodes=None, has_binding_points=None):
    nodes = [] if nodes is None else nodes
    if has_binding_points is None:
        has_binding_points = bool(nodes)
    return {"has_binding_points": has_binding_points,
            "camera_input_nodes": nodes}


def registry_entry(source_type="Camera", device_path="/dev/video0",
                   sync_status="synced", absent=False, stale=False):
    return {"type": source_type,
            "params": {"devicePath": device_path},
            "sync_status": sync_status,
            "absent": absent,
            "stale": stale}


def snapshot(cameras, never_synced=False):
    return {"never_synced": never_synced, "cameras": cameras}


# ==========================================================================
# No Camera_Input_Nodes (8.9)
# ==========================================================================

class TestNoCameraNodes:
    def test_version_without_camera_nodes_produces_nothing(self, deployments):
        errors, warnings = deployments.validate_camera_bindings(
            version_item([]), ["line-a"],
            {"line-a": snapshot({})}, {}, [])
        assert errors == []
        assert warnings == []

    def test_pre_feature_version_item_produces_nothing(self, deployments):
        """A version packaged before the feature carries neither
        discriminator attribute (11.1)."""
        errors, warnings = deployments.validate_camera_bindings(
            {}, ["line-a"], {}, {}, [])
        assert errors == []
        assert warnings == []


# ==========================================================================
# Binding completeness (8.3, 8.7)
# ==========================================================================

class TestCompleteness:
    def test_unbound_node_rejected_identifying_node_and_device(self, deployments):
        errors, warnings = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"],
            {"line-a": snapshot({"cfg-1": registry_entry()})}, {}, [])
        [error] = errors
        assert error["code"] == deployments.CAMERA_ERROR_UNBOUND
        assert error["nodeId"] == "n1"
        assert error["device"] == "line-a"
        assert "n1" in error["message"] and "line-a" in error["message"]

    def test_unbound_only_on_the_device_missing_the_binding(self, deployments):
        errors, _ = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a", "line-b"],
            {"line-a": snapshot({"cfg-1": registry_entry()}),
             "line-b": snapshot({"cfg-2": registry_entry()})},
            {"line-a": {"n1": {"cameraSourceId": "cfg-1"}}}, [])
        [error] = errors
        assert error["device"] == "line-b"

    def test_distinct_bindings_per_device_for_the_same_node(self, deployments):
        """One node bound to different sources on different devices (8.3)."""
        errors, warnings = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a", "line-b"],
            {"line-a": snapshot({"cfg-1": registry_entry()}),
             "line-b": snapshot({"cfg-2": registry_entry("NvidiaCSI")})},
            {"line-a": {"n1": {"cameraSourceId": "cfg-1"}},
             "line-b": {"n1": {"cameraSourceId": "cfg-2"}}}, [])
        assert errors == []
        assert warnings == []

    def test_empty_binding_entry_is_unbound(self, deployments):
        errors, _ = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"],
            {"line-a": snapshot({"cfg-1": registry_entry()})},
            {"line-a": {"n1": {}}}, [])
        assert [e["code"] for e in errors] == [deployments.CAMERA_ERROR_UNBOUND]


# ==========================================================================
# Binding existence (9.1, 9.2)
# ==========================================================================

class TestExistence:
    def test_missing_source_rejected_identifying_source_and_device(self, deployments):
        errors, _ = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"],
            {"line-a": snapshot({"cfg-1": registry_entry()})},
            {"line-a": {"n1": {"cameraSourceId": "cfg-gone"}}}, [])
        [error] = errors
        assert error["code"] == deployments.CAMERA_ERROR_SOURCE_MISSING
        assert error["cameraSourceId"] == "cfg-gone"
        assert error["device"] == "line-a"

    def test_present_source_accepted(self, deployments):
        errors, warnings = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"],
            {"line-a": snapshot({"cfg-1": registry_entry()})},
            {"line-a": {"n1": {"cameraSourceId": "cfg-1"}}}, [])
        assert errors == []
        assert warnings == []


# ==========================================================================
# Type compatibility (9.4) and override constraints (8.4)
# ==========================================================================

class TestTypeAndOverride:
    def test_folder_source_bound_to_camera_source_rejected(self, deployments):
        errors, _ = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"],
            {"line-a": snapshot({"cfg-1": registry_entry("Folder")})},
            {"line-a": {"n1": {"cameraSourceId": "cfg-1"}}}, [])
        [error] = errors
        assert error["code"] == deployments.CAMERA_ERROR_TYPE_INCOMPATIBLE
        assert error["sourceType"] == "Folder"
        assert error["nodeType"] == "camera_source"

    def test_rtsp_stream_cannot_back_camera_source(self, deployments):
        """camera_source captures from a device camera; a network stream
        cannot feed it (9.4)."""
        errors, _ = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"],
            {"line-a": snapshot({"cfg-1": registry_entry("RTSP")})},
            {"line-a": {"n1": {"cameraSourceId": "cfg-1"}}}, [])
        assert [e["code"] for e in errors] == \
            [deployments.CAMERA_ERROR_TYPE_INCOMPATIBLE]

    def test_discovered_v4l2_source_compatible(self, deployments):
        errors, _ = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"],
            {"line-a": snapshot({"disc-1": registry_entry("V4L2Discovered")})},
            {"line-a": {"n1": {"cameraSourceId": "disc-1"}}}, [])
        assert errors == []

    def test_custom_camera_backed_type_rejects_only_folder(self, deployments):
        registry = {"line-a": snapshot({
            "cfg-r": registry_entry("RTSP"),
            "cfg-f": registry_entry("Folder")})}
        nodes = [camera_node("n1", "acme.rtsp_input")]
        errors, _ = deployments.validate_camera_bindings(
            version_item(nodes), ["line-a"], registry,
            {"line-a": {"n1": {"cameraSourceId": "cfg-r"}}}, [])
        assert errors == []
        errors, _ = deployments.validate_camera_bindings(
            version_item(nodes), ["line-a"], registry,
            {"line-a": {"n1": {"cameraSourceId": "cfg-f"}}}, [])
        assert [e["code"] for e in errors] == \
            [deployments.CAMERA_ERROR_TYPE_INCOMPATIBLE]

    def test_valid_override_accepted(self, deployments):
        errors, warnings = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"],
            {"line-a": snapshot({})},
            {"line-a": {"n1": {"override": {"device": "/dev/video2",
                                            "gain": 8}}}}, [])
        assert errors == []
        assert warnings == []

    def test_override_violating_declared_constraints_rejected(self, deployments):
        """camera_source declares gain 0-100 in the workflow_core catalog."""
        errors, _ = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"],
            {"line-a": snapshot({})},
            {"line-a": {"n1": {"override": {"gain": 500}}}}, [])
        [error] = errors
        assert error["code"] == deployments.CAMERA_ERROR_OVERRIDE_INVALID
        assert error["parameter"] == "gain"
        assert error["violation"] == "PARAM_MAX"

    def test_override_with_undeclared_parameter_rejected(self, deployments):
        errors, _ = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"],
            {"line-a": snapshot({})},
            {"line-a": {"n1": {"override": {"bogus": 1}}}}, [])
        [error] = errors
        assert error["code"] == deployments.CAMERA_ERROR_OVERRIDE_INVALID
        assert error["parameter"] == "bogus"

    def test_override_for_unresolvable_node_type_fails_closed(self, deployments):
        errors, _ = deployments.validate_camera_bindings(
            version_item([camera_node("n1", "acme.unknown")]), ["line-a"],
            {"line-a": snapshot({})},
            {"line-a": {"n1": {"override": {"device": "/dev/video1"}}}}, [])
        assert [e["code"] for e in errors] == \
            [deployments.CAMERA_ERROR_OVERRIDE_INVALID]

    def test_caller_supplied_descriptor_validates_custom_type(self, deployments):
        from workflow_core.catalog import get_node_type
        descriptor = get_node_type("camera_source")
        errors, _ = deployments.validate_camera_bindings(
            version_item([camera_node("n1", "acme.cam")]), ["line-a"],
            {"line-a": snapshot({})},
            {"line-a": {"n1": {"override": {"gain": 8}}}}, [],
            descriptors={"acme.cam": descriptor})
        assert errors == []


# ==========================================================================
# Degraded-source and never-synced warnings (9.3, 8.8)
# ==========================================================================

class TestWarnings:
    @pytest.mark.parametrize("entry_kwargs,condition", [
        ({"stale": True}, "stale"),
        ({"absent": True}, "absent"),
        ({"sync_status": "pending"}, "pending"),
        ({"sync_status": "failed"}, "failed"),
    ])
    def test_degraded_source_warns_identifying_condition(
            self, deployments, entry_kwargs, condition):
        errors, warnings = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"],
            {"line-a": snapshot({"cfg-1": registry_entry(**entry_kwargs)})},
            {"line-a": {"n1": {"cameraSourceId": "cfg-1"}}}, [])
        assert errors == []
        [warning] = warnings
        assert warning["code"] == deployments.CAMERA_WARNING_SOURCE_DEGRADED
        assert warning["conditions"] == [condition]
        assert warning["confirmed"] is False

    def test_warning_confirmed_when_id_is_submitted(self, deployments):
        _, [warning] = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"],
            {"line-a": snapshot({"cfg-1": registry_entry(stale=True)})},
            {"line-a": {"n1": {"cameraSourceId": "cfg-1"}}}, [])
        _, [confirmed] = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"],
            {"line-a": snapshot({"cfg-1": registry_entry(stale=True)})},
            {"line-a": {"n1": {"cameraSourceId": "cfg-1"}}}, [warning["id"]])
        assert confirmed["id"] == warning["id"]
        assert confirmed["confirmed"] is True

    def test_never_synced_target_warns_and_permits_only_override(self, deployments):
        """8.8: the warning fires, and a registered-source binding on the
        never-synced device fails the existence check (manual-override
        restriction)."""
        errors, warnings = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"],
            {"line-a": snapshot({}, never_synced=True)},
            {"line-a": {"n1": {"cameraSourceId": "cfg-1"}}}, [])
        codes = {w["code"] for w in warnings}
        assert deployments.CAMERA_WARNING_NEVER_SYNCED in codes
        assert [e["code"] for e in errors] == \
            [deployments.CAMERA_ERROR_SOURCE_MISSING]

    def test_never_synced_target_with_override_only_needs_confirmation(
            self, deployments):
        errors, warnings = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"],
            {"line-a": snapshot({}, never_synced=True)},
            {"line-a": {"n1": {"override": {"device": "/dev/video1"}}}}, [])
        assert errors == []
        [warning] = warnings
        assert warning["code"] == deployments.CAMERA_WARNING_NEVER_SYNCED
        assert warning["confirmed"] is False

    def test_device_missing_from_snapshot_treated_as_never_synced(self, deployments):
        errors, warnings = deployments.validate_camera_bindings(
            version_item([camera_node()]), ["line-a"], {},
            {"line-a": {"n1": {"override": {"device": "/dev/video1"}}}}, [])
        assert errors == []
        assert [w["code"] for w in warnings] == \
            [deployments.CAMERA_WARNING_NEVER_SYNCED]


# ==========================================================================
# Legacy compiled-path check (9.5, 11.1)
# ==========================================================================

class TestLegacyPathCheck:
    def test_unmatched_compiled_path_warns_never_errors(self, deployments):
        errors, warnings = deployments.validate_camera_bindings(
            version_item([camera_node()], has_binding_points=False),
            ["line-a"],
            {"line-a": snapshot({"cfg-1": registry_entry(
                device_path="/dev/video9")})},
            {}, [])
        assert errors == []
        [warning] = warnings
        assert warning["code"] == deployments.CAMERA_WARNING_LEGACY_PATH
        assert warning["path"] == "/dev/video0"
        assert warning["device"] == "line-a"
        assert warning["confirmed"] is False

    def test_matched_compiled_path_produces_no_warning(self, deployments):
        errors, warnings = deployments.validate_camera_bindings(
            version_item([camera_node()], has_binding_points=False),
            ["line-a"],
            {"line-a": snapshot({"cfg-1": registry_entry(
                device_path="/dev/video0")})},
            {}, [])
        assert errors == []
        assert warnings == []

    def test_legacy_version_never_requires_bindings(self, deployments):
        """No unbound errors in the legacy regime even with no bindings
        submitted (11.1)."""
        errors, _ = deployments.validate_camera_bindings(
            version_item([camera_node()], has_binding_points=False),
            ["line-a", "line-b"], {}, {}, [])
        assert errors == []
