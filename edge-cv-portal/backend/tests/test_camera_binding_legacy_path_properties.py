"""
Property-based test for the deploy-time legacy compiled-path check —
validate_camera_bindings (functions/deployments.py).

**Feature: camera-registry-sync, Property 17: Legacy compiled-path warning**

*For any* workflow version without binding points carrying compiled-in
device paths, and any target device registry, validation emits a warning
for exactly those compiled-in paths that match no registered
Camera_Source's device path on that device, and never emits an error for
them.

**Validates: Requirements 9.5, 11.1**

Task 11.6 (spec: camera-registry-sync). The example-based unit tests over
the same function live in test_camera_binding_validation.py (task 11.1);
the sibling properties live in
test_camera_binding_completeness_properties.py (13),
test_camera_binding_existence_properties.py (14),
test_camera_binding_degraded_warning_properties.py (15), and
test_camera_binding_type_override_properties.py (16).
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
_ARCHES = ["aarch64", "armv7l", "x86_64"]

#: Small device-path pool so compiled paths and registered paths overlap
#: often — both matched (silent) and unmatched (warning) paths occur.
_PATHS = ["/dev/video0", "/dev/video1", "/dev/video2", "/dev/video3"]

#: Registry-entry param keys the validator accepts for the source's
#: device path (edge reports write devicePath; device is the node
#: parameter name).
_PATH_KEYS = ["devicePath", "device"]


@st.composite
def _legacy_cases(draw):
    """A legacy workflow version (has_binding_points false) with
    compiled-in device paths, targets, per-device registries, and the
    expected unmatched-path warnings.

    Each Camera_Input_Node compiles a path for a drawn subset of
    architectures; distinct architectures may share one path (the
    warning groups them). Each target device is either absent from the
    registry snapshot entirely (fail-safe: empty registry) or carries
    Camera_Source entries whose device path sits under ``devicePath`` or
    ``device`` — or carries no path at all — with degraded flags drawn
    freely, since the legacy regime warns on unmatched paths and nothing
    else. Also draws the warning subset to confirm in the
    confirmation pass, and an arbitrary bindings map that the legacy
    regime must ignore (11.1: bindings are never required).
    """
    node_ids = draw(st.lists(st.sampled_from(_NODE_IDS),
                             unique=True, min_size=1, max_size=3))
    camera_nodes = []
    for node_id in node_ids:
        arches = draw(st.lists(st.sampled_from(_ARCHES),
                               unique=True, min_size=1, max_size=3))
        compiled = {arch: draw(st.sampled_from(_PATHS)) for arch in arches}
        if draw(st.booleans()):
            # The packager records only rendered non-empty strings, but
            # the validator tolerates blanks: they never warn.
            compiled[draw(st.sampled_from(_ARCHES))] = ""
        camera_nodes.append({
            "node_id": node_id,
            "node_type": draw(st.sampled_from(
                ["camera_source", "acme.cam_input"])),
            "compiled_device_paths": compiled,
        })

    targets = draw(st.lists(st.sampled_from(_DEVICES),
                            unique=True, min_size=1, max_size=3))

    registry_snapshot = {}
    registered_paths = {}
    for device in targets:
        if draw(st.booleans()) and draw(st.booleans()):
            # Absent from the snapshot: treated as an empty registry, so
            # every non-empty compiled path is unmatched.
            registered_paths[device] = set()
            continue
        cameras = {}
        paths = set()
        for index in range(draw(st.integers(0, 3))):
            params = {}
            if draw(st.booleans()):
                key = draw(st.sampled_from(_PATH_KEYS))
                path = draw(st.sampled_from(_PATHS))
                params[key] = path
                paths.add(path)
            cameras[f"cfg-{device}-{index}"] = {
                "type": draw(st.sampled_from(["Camera", "Folder"])),
                "params": params,
                "sync_status": draw(st.sampled_from(
                    ["synced", "pending", "failed"])),
                "absent": draw(st.booleans()),
                "stale": draw(st.booleans()),
            }
        registry_snapshot[device] = {
            "never_synced": draw(st.booleans()) and not cameras,
            "cameras": cameras,
        }
        registered_paths[device] = paths

    # (device, node_id, path) -> sorted architectures compiled to it
    expected = {}
    for device in targets:
        for node in camera_nodes:
            for arch in sorted(node["compiled_device_paths"]):
                path = node["compiled_device_paths"][arch]
                if path and path not in registered_paths[device]:
                    key = (device, node["node_id"], path)
                    expected.setdefault(key, []).append(arch)

    confirm_keys = {key for key in expected if draw(st.booleans())}

    # Arbitrary bindings the legacy regime must ignore entirely.
    bindings = {
        device: {node_id: draw(st.sampled_from([
            {"cameraSourceId": "cfg-anything"},
            {"override": {"device": "/dev/video9"}},
        ])) for node_id in node_ids if draw(st.booleans())}
        for device in targets if draw(st.booleans())
    }

    version = {"has_binding_points": False,
               "camera_input_nodes": camera_nodes}
    return version, targets, registry_snapshot, bindings, expected, \
        confirm_keys


# ---------------------------------------------------------------------------
# Property 17
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_legacy_cases())
def test_legacy_compiled_path_warning(deployments, case):
    """**Feature: camera-registry-sync, Property 17: Legacy compiled-path
    warning**

    **Validates: Requirements 9.5, 11.1**

    In the legacy regime a warning identifying the node, device, path,
    and architectures is emitted for exactly the compiled-in paths that
    match no registered Camera_Source device path on the target; matched
    paths are silent, no errors are ever produced, and no bindings are
    required (submitted bindings change nothing).
    """
    (version, targets, registry_snapshot, bindings, expected,
     confirm_keys) = case

    # ---------------------------------------- no-bindings, unconfirmed pass
    errors, warnings = deployments.validate_camera_bindings(
        version, targets, registry_snapshot, {}, [])

    # Never an error, and no bindings required (9.5, 11.1).
    assert errors == []

    # A warning for exactly each unmatched (device, node, path),
    # identifying the architectures compiled to it (9.5).
    assert all(w["code"] == deployments.CAMERA_WARNING_LEGACY_PATH
               for w in warnings)
    assert {(w["device"], w["nodeId"], w["path"]): w["architectures"]
            for w in warnings} == expected
    assert len(warnings) == len(expected)
    for warning in warnings:
        assert warning["path"] in warning["message"]
        assert warning["nodeId"] in warning["message"]
        assert warning["device"] in warning["message"]

    # Warning ids are distinct, and nothing is confirmed when nothing
    # was submitted.
    ids = [w["id"] for w in warnings]
    assert len(set(ids)) == len(ids)
    assert all(not w["confirmed"] for w in warnings)

    # ------------------------------------------------- confirmation pass
    submitted = [w["id"] for w in warnings
                 if (w["device"], w["nodeId"], w["path"]) in confirm_keys]
    confirmed_errors, confirmed_warnings = \
        deployments.validate_camera_bindings(
            version, targets, registry_snapshot, {},
            submitted + ["not-a-real-warning-id"])

    assert confirmed_errors == []
    assert [w["id"] for w in confirmed_warnings] == ids
    for warning in confirmed_warnings:
        assert warning["confirmed"] == (
            (warning["device"], warning["nodeId"], warning["path"])
            in confirm_keys)

    # ------------------------------------------------ bindings-ignored pass
    # 11.1: the legacy regime never requires nor inspects bindings —
    # submitting an arbitrary bindings map changes nothing.
    bound_errors, bound_warnings = deployments.validate_camera_bindings(
        version, targets, registry_snapshot, bindings, [])

    assert bound_errors == []
    assert bound_warnings == warnings
