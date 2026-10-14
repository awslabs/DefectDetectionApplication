"""
Property-based test for manual-override constraint validation of
aravis_camera_source nodes — validate_camera_bindings
(functions/deployments.py).

**Feature: aravis-camera-input, Property 12: Aravis override constraint validation**

*For any* manual override submitted for an `aravis_camera_source` node,
validation SHALL accept the override exactly when every value satisfies
the descriptor's declared constraints (non-empty string `camera_id`,
`gain` within 0-100, `exposure` non-negative, no undeclared parameter
names).

**Validates: Requirements 5.4**

Task 8.3 (spec: aravis-camera-input). The example-based override unit
tests live in test_camera_binding_validation.py (task 8.1); the generic
override property over camera_source (camera-registry-sync Property 16)
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
# Generators: per declared parameter, a valid pool and a violating pool,
# so the expected verdict is decidable by the requirement's own words
# (never by the implementation's validator).
# ---------------------------------------------------------------------------

#: camera_id: string, min_length 1 — valid iff a non-empty string.
_CAMERA_ID_VALID = st.one_of(
    st.sampled_from(["Aravis-Fake-GV01", "Basler-12345678", "caméra ☂"]),
    st.text(min_size=1, max_size=30),
)
#: Violations: emptiness (the emptiness case named by the task) and
#: non-string values.
_CAMERA_ID_INVALID = st.one_of(
    st.just(""),
    st.integers(min_value=-10, max_value=10),
    st.booleans(),
)

#: gain: int, 0-100 — valid iff an int (bools are not ints to the
#: validator) within bounds.
_GAIN_VALID = st.integers(min_value=0, max_value=100)
_GAIN_INVALID = st.one_of(
    st.integers(min_value=-1000, max_value=-1),
    st.integers(min_value=101, max_value=1_000_000),
    st.sampled_from(["4", "high"]),
    st.booleans(),
    st.floats(allow_nan=False, allow_infinity=False),
)

#: exposure: int, min 0 (no max) — valid iff a non-negative int.
_EXPOSURE_VALID = st.integers(min_value=0, max_value=10**12)
_EXPOSURE_INVALID = st.one_of(
    st.integers(min_value=-10**9, max_value=-1),
    st.sampled_from(["5000000"]),
    st.booleans(),
    st.floats(allow_nan=False, allow_infinity=False),
)

_POOLS = {
    "camera_id": (_CAMERA_ID_VALID, _CAMERA_ID_INVALID),
    "gain": (_GAIN_VALID, _GAIN_INVALID),
    "exposure": (_EXPOSURE_VALID, _EXPOSURE_INVALID),
}

#: Undeclared parameter names: violations whatever the value.
_UNDECLARED_NAMES = ["device", "bogus", "camera-id"]


@st.composite
def _override_cases(draw):
    """One aravis_camera_source node per target with a manual-override
    binding drawing, per declared parameter, from the valid or the
    violating pool (recording which), plus optionally an undeclared key.
    Returns the expected per-value verdicts alongside the inputs."""
    targets = draw(st.lists(st.sampled_from(["line-a", "line-b"]),
                            unique=True, min_size=1, max_size=2))

    bindings = {}
    # (device, name) -> expected_valid
    verdicts = {}
    for device in targets:
        override = {}
        names = draw(st.lists(st.sampled_from(sorted(_POOLS)),
                              unique=True, max_size=3))
        for name in names:
            valid_pool, invalid_pool = _POOLS[name]
            valid = draw(st.booleans())
            override[name] = draw(valid_pool if valid else invalid_pool)
            verdicts[(device, name)] = valid
        if draw(st.booleans()):
            name = draw(st.sampled_from(_UNDECLARED_NAMES))
            override[name] = draw(st.one_of(
                st.text(max_size=10), st.integers(-5, 5)))
            verdicts[(device, name)] = False
        bindings[device] = {"n1": {"override": override}}

    version = {
        "has_binding_points": True,
        "camera_input_nodes": [
            {"node_id": "n1", "node_type": "aravis_camera_source"}
        ],
    }
    registry_snapshot = {device: {"never_synced": False, "cameras": {}}
                         for device in targets}
    return version, targets, registry_snapshot, bindings, verdicts


# ---------------------------------------------------------------------------
# Property 12
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_override_cases())
def test_aravis_override_accepted_exactly_when_constraints_hold(
        deployments, case):
    """**Feature: aravis-camera-input, Property 12: Aravis override
    constraint validation**

    **Validates: Requirements 5.4**

    An aravis_camera_source override produces one
    CAMERA_OVERRIDE_INVALID error per value violating the descriptor's
    declared constraints (empty or non-string camera_id, gain outside
    0-100 or not an int, negative or non-int exposure) and per
    undeclared parameter name — and is accepted (no error at all)
    exactly when every supplied value satisfies its declared constraint.
    """
    version, targets, registry_snapshot, bindings, verdicts = case

    errors, warnings = deployments.validate_camera_bindings(
        version, targets, registry_snapshot, bindings, [])

    override_errors = [
        e for e in errors
        if e["code"] == deployments.CAMERA_ERROR_OVERRIDE_INVALID]

    expected_rejections = {(device, name)
                           for (device, name), valid in verdicts.items()
                           if not valid}

    # One error per violating value / undeclared name, none for
    # satisfying values: acceptance exactly when every value holds.
    assert {(e["device"], e["parameter"]) for e in override_errors} \
        == expected_rejections
    assert len(override_errors) == len(expected_rejections)

    for error in override_errors:
        assert error["nodeId"] == "n1"
        # The message names the offending parameter.
        assert error["parameter"] in error["message"]

    # Override constraint checking is the only error signal here, and a
    # bound (overridden) node on a synced target raises no warnings.
    assert len(errors) == len(override_errors)
    assert warnings == []
