"""
Property-based test for deploy-time Camera_Binding delivery —
deliver_camera_bindings (functions/deployments.py).

**Feature: camera-registry-sync, Property 19: Binding delivery round trip**

*For any* validated bindings map, the desired document written to each
target device's bindings shadow, decoded back, equals the submitted
bindings for that device and workflow version, and the packaged
Workflow_Component artifact is not modified by the submission.

**Validates: Requirements 8.2, 8.6**

Task 11.9 (spec: camera-registry-sync). Exercised at the pure-function
level against a stateful fake iot-data client (mirroring FakeIotData in
test_camera_binding_context.py, task 11.7): delivery touches nothing
but the `dda-camera-bindings` shadow — the call log admits only
shadow get/update on the delivery targets, which is how the artifact
remains untouched by construction (8.6); the route-level example that
the Greengrass component set carries no binding material is covered by
test_camera_binding_context.py.
"""
import io
import json
import sys

import pytest
from botocore.exceptions import ClientError
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
# Fake iot-data client (mirrors task 11.7's FakeIotData, plus a call log)
# ---------------------------------------------------------------------------

class FakeIotData:
    """Stateful fake of the assumed-role iot-data client: named-shadow
    get/update with standard null-key pruning semantics. Every call is
    logged as (operation, thing_name, shadow_name) so the property can
    assert the delivery path performs no other writes."""

    def __init__(self):
        self.bindings = {}  # thing_name -> {key: bindings}
        self.fail_for = set()
        self.calls = []

    def get_thing_shadow(self, thingName, shadowName):
        self.calls.append(("get", thingName, shadowName))
        if thingName not in self.bindings:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException",
                           "Message": "no shadow"}}, "GetThingShadow")
        payload = json.dumps({"state": {"desired": {
            "bindings": self.bindings[thingName]}}})
        return {"payload": io.BytesIO(payload.encode())}

    def update_thing_shadow(self, thingName, shadowName, payload):
        self.calls.append(("update", thingName, shadowName))
        if thingName in self.fail_for:
            raise ClientError(
                {"Error": {"Code": "InternalFailure", "Message": "boom"}},
                "UpdateThingShadow")
        update = json.loads(payload)["state"]["desired"]["bindings"]
        current = self.bindings.setdefault(thingName, {})
        for key, value in update.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_THING_POOL = ["line-a", "line-b", "line-c", "line-d"]
_NODE_IDS = ["n1", "n2", "n3"]
_WORKFLOW_IDS = ["wf-cam", "wf-other"]
#: Every {workflowId}/{version} key a shadow or deployment may carry.
_KEY_POOL = [f"{wf}/{v}" for wf in _WORKFLOW_IDS for v in (1, 2, 3)]

#: The two bound forms of a per-node Camera_Binding (Req 8.2 / 8.4).
_binding_values = st.one_of(
    st.builds(lambda csid: {"cameraSourceId": csid},
              st.sampled_from(["cfg-1", "cfg-2", "cfg-3"])),
    st.builds(lambda gain: {"override": {"device": "/dev/video1",
                                         "gain": gain}},
              st.integers(0, 100)),
)


@st.composite
def _delivery_cases(draw):
    """Target things, the deployment's binding key, a per-device
    bindings map (devices may carry distinct bindings for the same node,
    an empty entry, or no entry at all), pre-existing shadow keys with
    distinguishable old content (possibly including the binding key
    itself), and the set of keys still deployed to the fleet."""
    things = draw(st.lists(st.sampled_from(_THING_POOL),
                           unique=True, min_size=1, max_size=4))
    binding_key = draw(st.sampled_from(_KEY_POOL))

    camera_bindings = {}
    pre_existing = {}
    for thing in things:
        entry_kind = draw(st.sampled_from(["absent", "empty", "bound"]))
        if entry_kind == "empty":
            camera_bindings[thing] = {}
        elif entry_kind == "bound":
            node_ids = draw(st.lists(st.sampled_from(_NODE_IDS),
                                     unique=True, min_size=1, max_size=3))
            camera_bindings[thing] = {
                node_id: draw(_binding_values) for node_id in node_ids}
        keys = draw(st.lists(st.sampled_from(_KEY_POOL),
                             unique=True, max_size=4))
        if keys:
            pre_existing[thing] = {
                key: {"n1": {"cameraSourceId": f"old-{thing}-{key}"}}
                for key in keys}
    deployed_keys = set(draw(st.lists(st.sampled_from(_KEY_POOL),
                                      unique=True, max_size=4)))
    return things, binding_key, camera_bindings, pre_existing, deployed_keys


@st.composite
def _failure_cases(draw):
    """A delivery case plus the index of the thing whose shadow write
    fails mid-list (possibly the first or the last)."""
    case = draw(_delivery_cases())
    things = case[0]
    fail_index = draw(st.integers(0, len(things) - 1))
    return case, fail_index


def _seed(pre_existing):
    iot_data = FakeIotData()
    for thing, keys in pre_existing.items():
        iot_data.bindings[thing] = dict(keys)
    # A thing outside the deployment's target list: delivery must never
    # touch it.
    iot_data.bindings["bystander"] = {
        _KEY_POOL[0]: {"n1": {"cameraSourceId": "bystander-cam"}}}
    return iot_data


def _expected_shadow(pre_existing, thing, binding_key, camera_bindings,
                     deployed_keys):
    """The post-delivery desired.bindings for one thing: pre-existing
    keys survive exactly when still deployed, and the binding key holds
    exactly the submitted bindings (empty when the device has none)."""
    expected = {key: value
                for key, value in pre_existing.get(thing, {}).items()
                if key != binding_key and key in deployed_keys}
    expected[binding_key] = camera_bindings.get(thing) or {}
    return expected


# ---------------------------------------------------------------------------
# Property 19
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_delivery_cases())
def test_delivery_round_trips_and_prunes_exactly(deployments, case):
    """**Feature: camera-registry-sync, Property 19: Binding delivery
    round trip**

    **Validates: Requirements 8.2, 8.6**

    Decoding each target thing's bindings shadow after delivery yields
    exactly the submitted per-device bindings under the deployment's
    {workflowId}/{version} key (8.2); pre-existing keys are pruned
    exactly when their version is no longer deployed (8.6); and the
    delivery path performs no operation besides get/update on the
    targets' dda-camera-bindings shadows — the packaged artifact and
    everything else are untouched by construction.
    """
    things, binding_key, camera_bindings, pre_existing, deployed_keys = case
    iot_data = _seed(pre_existing)

    written, failure = deployments.deliver_camera_bindings(
        iot_data, things, binding_key, camera_bindings, deployed_keys)

    assert failure is None
    assert written == list(things)

    # Round trip: the decoded shadow state is exactly the submitted
    # bindings plus the still-deployed survivors — nothing else (8.2, 8.6).
    for thing in things:
        assert iot_data.bindings[thing] == _expected_shadow(
            pre_existing, thing, binding_key, camera_bindings, deployed_keys)

    # Non-target things are never touched.
    assert iot_data.bindings["bystander"] == {
        _KEY_POOL[0]: {"n1": {"cameraSourceId": "bystander-cam"}}}

    # Artifact untouched: delivery consists solely of shadow get/update
    # against the delivery targets' dda-camera-bindings shadows (8.6).
    assert {operation for operation, _, _ in iot_data.calls} \
        <= {"get", "update"}
    assert all(shadow == deployments.CAMERA_BINDINGS_SHADOW_NAME
               for _, _, shadow in iot_data.calls)
    assert {thing for _, thing, _ in iot_data.calls} == set(things)


@settings(deadline=None)
@given(_failure_cases())
def test_mid_list_failure_aborts_and_prunes_written_targets(deployments,
                                                            case):
    """**Feature: camera-registry-sync, Property 19: Binding delivery
    round trip**

    **Validates: Requirements 8.2, 8.6**

    A shadow write failing for any thing mid-list aborts delivery with a
    failure record naming that thing, returns exactly the things written
    before it, best-effort prunes the deployment's binding key from
    those already-written shadows, and leaves the failing and remaining
    things' shadows unchanged.
    """
    (things, binding_key, camera_bindings, pre_existing,
     deployed_keys), fail_index = case
    iot_data = _seed(pre_existing)
    iot_data.fail_for.add(things[fail_index])

    written, failure = deployments.deliver_camera_bindings(
        iot_data, things, binding_key, camera_bindings, deployed_keys)

    assert failure is not None
    assert failure["device"] == things[fail_index]
    assert failure["error"]
    assert written == things[:fail_index]

    # Already-written targets: the aborted deployment's key is pruned;
    # still-deployed pre-existing keys survive the rollback.
    for thing in written:
        state = iot_data.bindings.get(thing, {})
        assert binding_key not in state
        assert state == {key: value
                         for key, value in pre_existing.get(thing, {}).items()
                         if key != binding_key and key in deployed_keys}

    # The failing thing and every thing after it are untouched.
    for thing in things[fail_index:]:
        assert iot_data.bindings.get(thing, {}) == pre_existing.get(thing, {})
    assert iot_data.bindings["bystander"] == {
        _KEY_POOL[0]: {"n1": {"cameraSourceId": "bystander-cam"}}}
