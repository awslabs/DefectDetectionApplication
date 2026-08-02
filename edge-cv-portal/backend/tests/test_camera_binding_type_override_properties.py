"""
Property-based test for deploy-time Camera_Binding type compatibility and
manual-override constraint validation — validate_camera_bindings
(functions/deployments.py).

**Feature: camera-registry-sync, Property 16: Type compatibility and override constraint validation**

*For any* Camera_Binding, validation rejects it identifying a type
mismatch exactly when the bound Camera_Source's type is outside the node
type's compatible set, and accepts a manual override exactly when every
override value satisfies the node type's declared parameter constraints
(as judged by the existing `workflow_core` parameter validator).

**Validates: Requirements 8.4, 9.4**

Task 11.5 (spec: camera-registry-sync). The example-based unit tests over
the same function live in test_camera_binding_validation.py (task 11.1);
the completeness property (task 11.2) lives in
test_camera_binding_completeness_properties.py, the existence property
(task 11.3) in test_camera_binding_existence_properties.py, and the
degraded-warning property (task 11.4) in
test_camera_binding_degraded_warning_properties.py.
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

#: Camera_Source types seen in registries: the four camera-backed types
#: compatible with camera_source, plus Folder (Requirement 9.4's example
#: of a categorical mismatch) and network-stream types a camera-backed
#: Custom_Node_Type may accept.
_SOURCE_TYPES = ["Camera", "ICam", "NvidiaCSI", "V4L2Discovered",
                 "Folder", "RTSP", "HTTPPull"]

#: Requirement 9.4 compatibility rule, restated from the design: the
#: built-in camera_source node captures from a device camera, so only
#: camera-backed source types may back it; a camera-backed
#: Custom_Node_Type declares no transport, so only the categorically
#: incompatible Folder type is rejected for it.
_CAMERA_SOURCE_COMPATIBLE = frozenset(
    {"ICam", "V4L2Discovered", "Camera"})

#: Node types under test: the built-in (descriptor resolved from the
#: workflow_core catalog), a camera-backed Custom_Node_Type with a
#: caller-supplied descriptor, and a Custom_Node_Type whose descriptor is
#: unresolvable, for which override validation must fail closed.
_BUILTIN = "icam_source"
_CUSTOM = "acme.cam_input"
_UNRESOLVABLE = "acme.unresolvable"
_NODE_TYPES = [_BUILTIN, _CUSTOM, _UNRESOLVABLE]


def _type_compatible(node_type, source_type):
    """The Requirement 9.4 expectation, independent of the implementation."""
    if node_type == _BUILTIN:
        return source_type in _CAMERA_SOURCE_COMPATIBLE
    return source_type != "Folder"


#: Arbitrary override values: wrong-typed values (bools are rejected for
#: int parameters, floats for int and string ones), out-of-range ints,
#: empty and non-empty strings — validity is judged by the
#: check_parameter_value oracle in the test, never assumed here.
_WILD_VALUES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-1000, max_value=1_000_000),
    st.floats(allow_nan=False, allow_infinity=False),
    st.sampled_from(["", "/dev/video0", "10", "caméra ☂"]),
)

#: Values satisfying each declared camera_source constraint (device:
#: string min_length 1; gain: int 0-100; exposure: int >= 0), mixed with
#: the wild pool so fully valid overrides occur often.
_VALID_VALUES = {
    "device": st.sampled_from(["/dev/video0", "/dev/video7", "caméra ☂"]),
    "gain": st.integers(min_value=0, max_value=100),
    "exposure": st.integers(min_value=0, max_value=10**9),
}

#: The declared camera_source parameters plus an undeclared name, which
#: must be rejected whatever its value.
_OVERRIDE_NAMES = ["device", "gain", "exposure", "bogus"]


def _value_for(name):
    valid = _VALID_VALUES.get(name)
    if valid is None:
        return _WILD_VALUES
    return st.one_of(valid, _WILD_VALUES)


@st.composite
def _type_override_cases(draw):
    """A version with binding points and, per (device, node), either a
    registered-source binding to an entry of arbitrary source type
    (exercising the type-compatibility half) or a manual override with
    arbitrary values over declared and undeclared parameters (exercising
    the constraint half, including the unresolvable-descriptor fail-closed
    rule). Every referenced source is present and healthy so type
    compatibility and override validity are the only error signals."""
    node_ids = draw(st.lists(st.sampled_from(_NODE_IDS),
                             unique=True, min_size=1, max_size=3))
    node_types = {node_id: draw(st.sampled_from(_NODE_TYPES))
                  for node_id in node_ids}
    targets = draw(st.lists(st.sampled_from(_DEVICES),
                            unique=True, min_size=1, max_size=3))

    registry_snapshot = {}
    bindings = {}
    # (device, node_id) -> (camera_source_id, source_type)
    source_refs = {}
    # (device, node_id) -> override dict
    overrides = {}

    for device in targets:
        cameras = {}
        device_bindings = {}
        for node_id in node_ids:
            if draw(st.booleans()):
                source_type = draw(st.sampled_from(_SOURCE_TYPES))
                camera_source_id = f"cfg-{device}-{node_id}"
                cameras[camera_source_id] = {
                    "type": source_type,
                    "params": {"devicePath": "/dev/video0"},
                    "sync_status": "synced",
                    "absent": False,
                    "stale": False,
                }
                device_bindings[node_id] = {
                    "cameraSourceId": camera_source_id}
                source_refs[(device, node_id)] = (camera_source_id,
                                                  source_type)
            else:
                names = draw(st.lists(st.sampled_from(_OVERRIDE_NAMES),
                                      unique=True, max_size=3))
                override = {name: draw(_value_for(name)) for name in names}
                device_bindings[node_id] = {"override": override}
                overrides[(device, node_id)] = override
        registry_snapshot[device] = {"never_synced": False,
                                     "cameras": cameras}
        bindings[device] = device_bindings

    version = {
        "has_binding_points": True,
        "camera_input_nodes": [
            {"node_id": node_id,
             "node_type": node_types[node_id],
             "compiled_device_paths": {"x86_64": "/dev/video0"}}
            for node_id in node_ids
        ],
    }
    return version, targets, registry_snapshot, bindings, node_types, \
        source_refs, overrides


# ---------------------------------------------------------------------------
# Property 16
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_type_override_cases())
def test_type_mismatch_and_override_violations_are_rejected_exactly(
        deployments, case):
    """**Feature: camera-registry-sync, Property 16: Type compatibility
    and override constraint validation**

    **Validates: Requirements 8.4, 9.4**

    A type-mismatch error is produced exactly for the bindings whose
    Camera_Source type is outside the node type's compatible set,
    identifying the mismatch (9.4); an override error is produced exactly
    for each override value the workflow_core parameter validator rejects
    against the node type's declared constraints, for each undeclared
    parameter name, and (fail-closed) for overrides on a node type with
    no resolvable parameter declaration — so a binding is accepted
    exactly when its source type is compatible or every override value
    satisfies the declared constraints (8.4).
    """
    from workflow_core.catalog import get_node_type
    from workflow_core.validator import check_parameter_value

    (version, targets, registry_snapshot, bindings, node_types,
     source_refs, overrides) = case

    descriptor = get_node_type(_BUILTIN)
    parameters = {p.name: p for p in descriptor.parameters}

    errors, _ = deployments.validate_camera_bindings(
        version, targets, registry_snapshot, bindings, [],
        descriptors={_CUSTOM: descriptor})

    # ------------------------------------------- type compatibility (9.4)
    expected_mismatches = {
        (device, node_id, camera_source_id, source_type)
        for (device, node_id), (camera_source_id, source_type)
        in source_refs.items()
        if not _type_compatible(node_types[node_id], source_type)
    }
    type_errors = [
        e for e in errors
        if e["code"] == deployments.CAMERA_ERROR_TYPE_INCOMPATIBLE]

    # Exactly one type error per incompatible binding; compatible
    # bindings pass the type check.
    assert {(e["device"], e["nodeId"], e["cameraSourceId"], e["sourceType"])
            for e in type_errors} == expected_mismatches
    assert len(type_errors) == len(expected_mismatches)

    # Each error identifies the mismatch: both sides of it (9.4).
    for error in type_errors:
        assert error["nodeType"] == node_types[error["nodeId"]]
        assert error["sourceType"] in error["message"]
        assert error["nodeType"] in error["message"]

    # ------------------------------------------ override constraints (8.4)
    # Expected rejections, judged by the existing workflow_core parameter
    # validator: (device, node, parameter, violation code); undeclared
    # parameters carry no violation code, and the unresolvable-descriptor
    # fail-closed rejection carries neither parameter nor code.
    expected_violations = set()
    for (device, node_id), override in overrides.items():
        if node_types[node_id] == _UNRESOLVABLE:
            expected_violations.add((device, node_id, None, None))
            continue
        for name, value in override.items():
            parameter = parameters.get(name)
            if parameter is None:
                expected_violations.add((device, node_id, name, None))
                continue
            violation = check_parameter_value(parameter, value)
            if violation is not None:
                expected_violations.add(
                    (device, node_id, name, violation.code))

    override_errors = [
        e for e in errors
        if e["code"] == deployments.CAMERA_ERROR_OVERRIDE_INVALID]

    # An override is accepted exactly when every value satisfies the
    # declared constraints: one error per rejected value / undeclared
    # name / unresolvable declaration, and none otherwise.
    assert {(e["device"], e["nodeId"], e.get("parameter"),
             e.get("violation")) for e in override_errors} \
        == expected_violations
    assert len(override_errors) == len(expected_violations)

    for error in override_errors:
        if error.get("parameter") is not None:
            # The message names the offending parameter.
            assert error["parameter"] in error["message"]
        else:
            # Fail-closed: the message names the undeclarable node type.
            assert node_types[error["nodeId"]] in error["message"]

    # Type compatibility and override constraints are the only error
    # signals in these cases.
    assert len(errors) == len(type_errors) + len(override_errors)
