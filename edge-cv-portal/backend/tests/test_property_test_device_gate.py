"""Property test for the test-device deployment gate (task 10.6).

**Feature: custom-node-designer, Property 14: Deployment gate restricts test-state plugins to Test_Devices**

For all sets of workflow plugin lifecycle states and target devices
with random test_device flags, deployment submission is permitted if
and only if no plugin is in dev (or unknown — fail closed) state and
every test-state plugin targets only devices flagged as Test_Devices
(prod-state plugins deploy anywhere); every rejection identifies the
Plugin_Component and its Lifecycle_State plus exactly the offending
target devices.

**Validates: Requirements 9.7, 9.8, 9.11**

The gate under test (``evaluate_plugin_lifecycle_gate`` in
functions/deployments.py) is pure over plain dicts, so the property is
exercised directly with no AWS calls. The module is imported through
the shared moto-backed session fixture only so its module-level boto3
clients bind to the mock (same re-import pattern as
test_deployment_plugin_gates.py).
"""

from __future__ import annotations

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
# Reference model: permission restated independently from the
# requirements (9.7, 9.8, 9.11) rather than derived from the
# implementation, so the test cannot silently agree with a wrong gate.
# ---------------------------------------------------------------------------

def pair_permitted_model(state, test_device_flag):
    """May this Plugin_Component reach this device?

    - prod deploys anywhere in the Use_Case (9.11);
    - test only to devices flagged test_device (9.7, 9.8);
    - dev, or any unknown/unresolvable state, never (fail closed).
    """
    if state == "prod":
        return True
    if state == "test":
        return test_device_flag
    return False


def offending_pairs_model(closure_states, device_flags):
    """The exact set of forbidden (component, device) pairs."""
    return {
        (component, device)
        for component, state in closure_states.items()
        for device, flag in device_flags.items()
        if not pair_permitted_model(state, flag)
    }


# ---------------------------------------------------------------------------
# Random closures and device sets
# ---------------------------------------------------------------------------

# The recognized lifecycle states plus unknown / absent values the gate
# must fail closed on.
LIFECYCLE_STATES = ("dev", "test", "prod", "archived", "", None)

_component_names = st.integers(min_value=0, max_value=5).map(
    lambda i: f"dda.plugin.p{i}")
_device_names = st.integers(min_value=0, max_value=5).map(
    lambda i: f"device-{i}")

# Deployments always target at least one device (the handler validates
# target_devices before the gates run), so device sets are nonempty;
# closures may be empty (a workflow without custom plugins).
_closures = st.dictionaries(
    _component_names, st.sampled_from(LIFECYCLE_STATES), max_size=6)
_device_sets = st.dictionaries(
    _device_names, st.booleans(), min_size=1, max_size=6)


# ---------------------------------------------------------------------------
# Property 14
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(closure_states=_closures, device_flags=_device_sets)
def test_gate_restricts_test_state_plugins_to_test_devices(
        deployments, closure_states, device_flags):
    """**Feature: custom-node-designer, Property 14: Deployment gate restricts test-state plugins to Test_Devices**

    For all random closures (components with random lifecycle states)
    against random device sets (random test_device flags), the gate
    passes if and only if every component/device pair is permitted (dev
    or unknown never, test only to flagged devices, prod always), and
    violations identify exactly the offending pairs with each
    component's Lifecycle_State.

    **Validates: Requirements 9.7, 9.8, 9.11**
    """
    violations = deployments.evaluate_plugin_lifecycle_gate(
        closure_states, device_flags)

    expected_pairs = offending_pairs_model(closure_states, device_flags)

    # Submission is permitted iff no pair is forbidden (9.7, 9.11).
    assert (violations == []) == (expected_pairs == set())

    # Violations identify exactly the offending pairs — nothing missing,
    # nothing extra (9.8).
    actual_pairs = {
        (violation["pluginComponent"], device)
        for violation in violations
        for device in violation["devices"]
    }
    assert actual_pairs == expected_pairs

    # Each rejection identifies the Plugin_Component and its
    # Lifecycle_State as recorded in the closure (9.8), lists at least
    # one offending device, and no component is reported twice.
    components = [v["pluginComponent"] for v in violations]
    assert len(components) == len(set(components))
    for violation in violations:
        component = violation["pluginComponent"]
        assert component in closure_states
        assert violation["lifecycleState"] == closure_states[component]
        assert violation["devices"] != []
