"""Property test for Plugin_Component deployment gates (task 10.7).

**Feature: custom-node-designer, Property 22: Plugin_Component deployment gates on lifecycle and architecture coverage**

For all deployment submissions over random backing Plugin_Record
lifecycle states, random per-component published-architecture sets, and
random target devices with recorded architectures (including missing)
and test_device flags, the combined pure gates
(``evaluate_plugin_lifecycle_gate`` + ``evaluate_plugin_arch_gate`` in
functions/deployments.py) permit submission if and only if

- no component in the dependency closure is backed by a dev-state (or
  unknown -- fail closed) record,
- every test-state component targets only Test_Devices (prod deploys
  anywhere), and
- every target device's recorded Target_Architecture appears in the
  platform manifests of every depended-on Plugin_Component version,
  matched by exact name -- x86_64 and x86_64_nvidia are distinct with no
  fallback in either direction, and a device with no recorded
  architecture fails closed;

and every rejection identifies exactly the offending tuples: lifecycle
violations as {pluginComponent, lifecycleState, devices} and
architecture misses as {pluginComponent, version, device, deviceArch}.

**Validates: Requirements 16.3, 16.6**
"""
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

ARCHS = ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6")
LIFECYCLE_STATES = ("dev", "test", "prod", None)  # None: unknown, fails closed


@pytest.fixture(scope="module")
def deployments(aws_stack):
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


# ---------------------------------------------------------------------------
# Strategies: published-arch-set x device-arch (design: Property 22)
# ---------------------------------------------------------------------------

components_strategy = st.dictionaries(
    keys=st.sampled_from([f"dda.plugin.p{i}" for i in range(5)]),
    values=st.fixed_dictionaries({
        "lifecycle_state": st.sampled_from(LIFECYCLE_STATES),
        "version": st.integers(min_value=1, max_value=9).map(
            lambda n: f"{n}.0.0"),
        # Published platform manifests: any subset of the five archs
        # (empty = no successful builds recorded on the component).
        "architectures": st.lists(
            st.sampled_from(ARCHS), unique=True).map(sorted),
    }),
    min_size=0, max_size=4,
)

devices_strategy = st.dictionaries(
    keys=st.sampled_from([f"device-{i}" for i in range(5)]),
    values=st.fixed_dictionaries({
        "test_device": st.booleans(),
        # None: no Target_Architecture recorded for the device.
        "arch": st.sampled_from(ARCHS + (None,)),
    }),
    min_size=0, max_size=4,
)


# ---------------------------------------------------------------------------
# Independent oracle over (component, device) pairs
# ---------------------------------------------------------------------------

def lifecycle_permits(state, is_test_device):
    """One (component, device) pair: prod anywhere, test only to
    Test_Devices, dev/unknown nowhere (16.3)."""
    if state == "prod":
        return True
    if state == "test":
        return is_test_device
    return False


@settings(max_examples=25, deadline=None)
@given(components=components_strategy, devices=devices_strategy)
def test_plugin_deployment_gates_pass_iff_lifecycle_and_arch_covered(
        deployments, components, devices):
    """**Feature: custom-node-designer, Property 22: Plugin_Component deployment gates on lifecycle and architecture coverage**

    The combined gates pass iff the lifecycle permits every
    (component, device) pair and every device's recorded architecture is
    covered by every component's published manifests (exact match, no
    x86_64 <-> x86_64_nvidia crossover, unrecorded arch fails closed);
    the violations list exactly the offending tuples.

    **Validates: Requirements 16.3, 16.6**
    """
    closure_states = {name: c["lifecycle_state"]
                      for name, c in components.items()}
    device_flags = {name: d["test_device"] for name, d in devices.items()}
    component_manifests = {
        name: {"version": c["version"], "architectures": c["architectures"]}
        for name, c in components.items()
    }
    device_archs = {name: d["arch"] for name, d in devices.items()}

    lifecycle_violations = deployments.evaluate_plugin_lifecycle_gate(
        closure_states, device_flags)
    arch_offending = deployments.evaluate_plugin_arch_gate(
        component_manifests, device_archs)

    # ------------------------------------------------ the iff (decision)
    # Lifecycle: dev/unknown components fail closed even with no targets;
    # test-state components require every target to be a Test_Device.
    lifecycle_ok = all(
        state == "prod" or (
            state == "test"
            and all(device_flags[d] for d in devices))
        for state in closure_states.values()
    )
    # Architecture: exact-name membership -- x86_64 never matches
    # x86_64_nvidia (and vice versa), None is never a member.
    arch_ok = all(
        d["arch"] in c["architectures"]
        for c in components.values()
        for d in devices.values()
    )

    passes = lifecycle_violations == [] and arch_offending == []
    assert passes == (lifecycle_ok and arch_ok)

    # ------------------------------- lifecycle violations: exact tuples
    expected_lifecycle = []
    for name in sorted(components):
        state = components[name]["lifecycle_state"]
        if state == "prod":
            continue
        if state == "test":
            offending = sorted(d for d in devices if not device_flags[d])
            if not offending:
                continue
        else:  # dev or unknown: every target device, fail closed
            offending = sorted(devices)
        expected_lifecycle.append({
            "pluginComponent": name,
            "lifecycleState": state,
            "devices": offending,
        })

    assert sorted(lifecycle_violations,
                  key=lambda v: v["pluginComponent"]) == \
        [dict(v, devices=sorted(v["devices"])) for v in sorted(
            expected_lifecycle, key=lambda v: v["pluginComponent"])]

    # Sanity: each violating pair is one the oracle rejects.
    for violation in lifecycle_violations:
        state = closure_states[violation["pluginComponent"]]
        for device in violation["devices"]:
            assert not lifecycle_permits(state, device_flags[device])

    # ------------------------------------ arch misses: exact tuples
    expected_arch = [
        {"pluginComponent": name,
         "version": components[name]["version"],
         "device": device,
         "deviceArch": device_archs[device]}
        for name in sorted(components)
        for device in sorted(devices)
        if device_archs[device] not in set(components[name]["architectures"])
    ]

    def arch_key(entry):
        return (entry["pluginComponent"], entry["device"])

    assert sorted(arch_offending, key=arch_key) == \
        sorted(expected_arch, key=arch_key)
