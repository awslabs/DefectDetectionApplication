"""
Deployment unit tests for stream cameras — ``create_workflow_deployment``,
``validate_camera_bindings``, and ``check_local_server_compatibility``
(functions/deployments.py), rtsp-rtmp-stream-cameras task 7.4.

The three example-based gaps task 7.3's property test (stream binding
compatibility and override validation) deliberately left open, plus the
backward-compatibility identity that holds them honest:

1. **Stream bindings travel through ``dda-camera-bindings`` unchanged**
   (Requirement 9.6). A stream binding is delivered by the *existing*
   mechanism: ``desired.bindings["{workflowId}/{version}"]`` in each
   target thing's ``dda-camera-bindings`` named shadow, byte-for-byte
   what was submitted, with the packaged artifact and the Greengrass
   component set untouched. Route-level, modelled on the Aravis delivery
   example in test_camera_binding_submission.py (whose mechanism this
   feature reuses without extending): the assertion is the *absence* of a
   stream-specific delivery path, so the stream case is compared against
   the same expected desired document a non-stream binding produces.

2. **Warning ids for other types are unchanged** (Requirements 9.5, 9.6,
   18.1). ``_degraded_source_conditions`` gained ``stream-failed``, and
   that condition is part of the '+'-joined warning **id** an operator
   confirms — so appending it in the wrong place would silently
   invalidate confirmations operators already hold for stale/absent/
   pending sources of every other type. The id formula and the condition
   order are therefore restated here as a frozen oracle (from the
   pre-feature camera-registry-sync behaviour) and pinned over every
   combination of conditions for every non-stream Camera_Source type,
   with the stream case pinned separately to append last.

   The guarantee is conditional in exactly the way the design states it:
   only a stream Camera_Source carries the ``capabilities.stream``
   section the Edge_Sync_Agent reports, so ``_stream_health_state`` is
   None for every other type and its conditions — hence its id — are
   what they were. The realistic non-stream capability shapes below pin
   that; an entry of a non-stream type that *did* report a failed stream
   state would legitimately get the condition, and is not a shape any
   producer emits.

3. **Floor rejections name the required version** (Requirement 9.7). A
   workflow using the new node types must not reach a LocalServer that
   predates them, and the operator must be told which version to reach.
   Both the per-device ``reason`` and the structured
   ``min_local_server_version`` are pinned, including the one case where
   there is no version to name (an architecture with no supporting
   build), and the pre-enablement empty map that rejects everything.

4. **Identity without the new node types** (Requirement 18.1). Every
   assertion above is paired with the corresponding stream-free run: the
   same expected desired document, the same warning ids, and — with a
   9.9.9 feature floor configured — an unaffected 201.

Runs from edge-cv-portal/backend WITH conftest (the moto ``aws_stack``
fixture backs the module import):
    python3 -m pytest tests/test_stream_binding_deployment.py \
        -q -p no:cacheprovider
"""
import itertools
import json
import sys

import boto3
import pytest

from conftest import REGION
from test_camera_binding_context import BindingEnv, camera_node

STREAM_REGISTRY_TABLE = "test-camera-registry-stream-binding"

#: The x86_64 LocalServer variant FakeGreengrass.register_device installs,
#: as ``local_server_component_arch`` resolves it. Every route-level floor
#: case below is keyed on this arch.
FLEET_ARCH = "x86_64"


# --------------------------------------------------------------------------
# Module stack: own registry table name so this module can coexist with the
# other camera-binding modules in one moto session
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def stream_stack(aws_stack):
    import os

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=STREAM_REGISTRY_TABLE,
        KeySchema=[
            {"AttributeName": "device_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "device_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    os.environ["CAMERA_REGISTRY_TABLE"] = STREAM_REGISTRY_TABLE

    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield {
        "deployments": deployments,
        "registry": resource.Table(STREAM_REGISTRY_TABLE),
    }


@pytest.fixture
def fleet(env, stream_stack, monkeypatch):
    return BindingEnv(env, stream_stack, monkeypatch)


@pytest.fixture(scope="module")
def deployments(stream_stack):
    """The module under test, for the pure-function cases."""
    return stream_stack["deployments"]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def stream_node(node_id="s1", node_type="rtsp_camera_source", hint_csid=None):
    """A packager-recorded ``camera_input_nodes`` entry for a
    Stream_Camera_Source_Node (task 6.1's shape: no
    ``compiled_device_paths`` — a stream node has no device path)."""
    node = {"node_id": node_id, "node_type": node_type}
    if hint_csid:
        node["binding_hint"] = {"cameraSourceId": hint_csid}
    return node


def stream_registry_entry(source_type="RTSP",
                          url="rtsp://cam.local:554/Streaming/Channels/101",
                          state="streaming"):
    """A healthy stream Camera_Source as the sync reducer stores it: the
    credential-free URL in ``params`` and the last reported Stream_Health
    under ``capabilities.stream``."""
    entry = {
        "type": source_type,
        "params": {"url": url},
        "capabilities": {"stream": {"state": state,
                                    "last_connected_at": 1_730_000_000_000}},
    }
    return entry


def mark_stream_features(fleet, enabled=True):
    """Set the packager-recorded ``has_stream_features`` discriminator on
    the seeded version item — what activates the Requirement 9.7 floor
    gate in the deployment handler."""
    fleet.env.stack.tables.versions.update_item(
        Key={"workflow_id": fleet.workflow_id, "version": 1},
        UpdateExpression="SET has_stream_features = :v",
        ExpressionAttributeValues={":v": enabled})


def set_feature_floor(fleet, monkeypatch, floor_map):
    """Stand in for the deployed
    WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS env map (empty until
    spec tasks 9.2/26.3 configure it)."""
    monkeypatch.setattr(fleet.deployments,
                        "WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
                        dict(floor_map))


def expected_desired_document(fleet, device_bindings):
    """The desired.bindings document the *existing* delivery mechanism
    writes for one thing: the submitted per-node bindings under the
    deployment's {workflowId}/{version} key, and nothing else."""
    return {f"{fleet.workflow_id}/1": device_bindings}


# --------------------------------------------------------------------------
# 1. Stream bindings travel through dda-camera-bindings unchanged (9.6)
# --------------------------------------------------------------------------

class TestStreamBindingDeliveryUnchanged:
    """The shadow mechanism is reused verbatim: same shadow, same key,
    same document shape, artifact untouched (Requirement 9.6)."""

    def test_shadow_name_is_the_pre_feature_one(self, deployments):
        """No stream-specific shadow was introduced."""
        assert deployments.CAMERA_BINDINGS_SHADOW_NAME == "dda-camera-bindings"

    def test_rtsp_selection_delivers_and_leaves_artifact_untouched(
            self, fleet, monkeypatch):
        """Binding an ``rtsp_camera_source`` node to a registered RTSP
        Camera_Source writes exactly the pre-feature desired document into
        the target's ``dda-camera-bindings`` shadow, keyed
        {workflowId}/{version}; the Greengrass deployment carries only the
        component map, the staged artifact bytes are byte-identical after
        submission, and the delivered bindings are recorded (9.6)."""
        fleet.seed_workflow([stream_node()])
        mark_stream_features(fleet)
        set_feature_floor(fleet, monkeypatch, {FLEET_ARCH: "1.0.0"})
        fleet.seed_registry("line-a", {"rtsp-1": stream_registry_entry()})
        fleet.gg.register_device("line-a")

        artifact_key = (f"workflows/{fleet.workflow_id}/1/{FLEET_ARCH}/"
                        f"component.zip")
        artifact_bytes = b"compiled-stream-workflow-artifact-bytes"
        fleet.env.s3.put_object(Bucket=fleet.env.bucket, Key=artifact_key,
                                Body=artifact_bytes)

        bindings = {"line-a": {"s1": {"cameraSourceId": "rtsp-1"}}}
        status, payload = fleet.deploy(["line-a"], camera_bindings=bindings)

        assert status == 201, payload
        assert payload["camera_bindings_delivered"] is True
        assert fleet.iot_data.bindings["line-a"] == \
            expected_desired_document(fleet, bindings["line-a"])
        # No binding material rides the component set or the artifact.
        [call] = fleet.gg.create_deployment_calls
        assert call["components"] == {
            f"dda.workflow.{fleet.workflow_id}": {"componentVersion": "1.0.0"}}
        stored_artifact = fleet.env.s3.get_object(
            Bucket=fleet.env.bucket, Key=artifact_key)["Body"].read()
        assert stored_artifact == artifact_bytes
        record = fleet.env.stack.tables.deployments.get_item(
            Key={"deployment_id": payload["deployment_id"]})["Item"]
        assert record["camera_bindings"] == bindings

    def test_rtmp_override_url_travels_verbatim(self, fleet, monkeypatch):
        """A manual ``{"override": {"url": ...}}`` on an
        ``rtmp_stream_source`` node passes validation and reaches the
        shadow and the deployment record with the URL string unchanged —
        the deploy-time half of the device-side resolution of
        Requirement 10.6 (9.6)."""
        fleet.seed_workflow([stream_node(node_type="rtmp_stream_source")])
        mark_stream_features(fleet)
        set_feature_floor(fleet, monkeypatch, {FLEET_ARCH: "1.0.0"})
        fleet.seed_registry("line-a", {"rtmp-1": stream_registry_entry(
            source_type="RTMP", url="rtmp://media.local/live/line1")})
        fleet.gg.register_device("line-a")

        url = "rtmps://media.plant.example.com:1936/live/line-1?x=1"
        bindings = {"line-a": {"s1": {"override": {"url": url}}}}

        status, payload = fleet.deploy(["line-a"], camera_bindings=bindings)

        assert status == 201, payload
        assert fleet.iot_data.bindings["line-a"] == \
            expected_desired_document(fleet, bindings["line-a"])
        record = fleet.env.stack.tables.deployments.get_item(
            Key={"deployment_id": payload["deployment_id"]})["Item"]
        assert record["camera_bindings"]["line-a"]["s1"]["override"]["url"] \
            == url

    def test_mixed_stream_and_legacy_nodes_share_one_shadow_key(
            self, fleet, monkeypatch):
        """A workflow with a stream node beside an ``icam_source`` node
        delivers both nodes' bindings in the same {workflowId}/{version}
        key, per device, with distinct bindings per device — the stream
        node adds an entry to the existing document rather than a second
        delivery (9.6, 18.1)."""
        fleet.seed_workflow([stream_node(), camera_node("n1")])
        mark_stream_features(fleet)
        set_feature_floor(fleet, monkeypatch, {FLEET_ARCH: "1.0.0"})
        for thing, csid in (("line-a", "rtsp-a"), ("line-b", "rtsp-b")):
            fleet.seed_registry(thing, {
                csid: stream_registry_entry(
                    url=f"rtsp://{thing}.local/live"),
                "cfg-1": {},
            })
            fleet.gg.register_device(thing)
        bindings = {
            "line-a": {"s1": {"cameraSourceId": "rtsp-a"},
                       "n1": {"cameraSourceId": "cfg-1"}},
            "line-b": {"s1": {"cameraSourceId": "rtsp-b"},
                       "n1": {"cameraSourceId": "cfg-1"}},
        }

        status, payload = fleet.deploy(["line-a", "line-b"],
                                       camera_bindings=bindings)

        assert status == 201, payload
        for thing in ("line-a", "line-b"):
            assert fleet.iot_data.bindings[thing] == \
                expected_desired_document(fleet, bindings[thing])

    def test_stream_free_deployment_produces_the_same_document_shape(
            self, fleet):
        """The identity half (18.1): a workflow with only the pre-feature
        ``icam_source`` node — no ``has_stream_features``, no feature
        floor configured — delivers the identical document shape through
        the identical shadow."""
        fleet.seed_workflow([camera_node("n1")])
        fleet.seed_registry("line-a", {"cfg-1": {}})
        fleet.gg.register_device("line-a")
        bindings = {"line-a": {"n1": {"cameraSourceId": "cfg-1"}}}

        status, payload = fleet.deploy(["line-a"], camera_bindings=bindings)

        assert status == 201, payload
        assert fleet.iot_data.bindings["line-a"] == \
            expected_desired_document(fleet, bindings["line-a"])

    def test_crossed_protocol_rejects_before_any_shadow_write(self, fleet,
                                                             monkeypatch):
        """Route-level teeth for Requirement 9.3's rejection: an RTSP node
        bound to the target's RTMP Camera_Source is rejected with
        CAMERA_TYPE_INCOMPATIBLE and nothing is delivered."""
        fleet.seed_workflow([stream_node()])
        mark_stream_features(fleet)
        set_feature_floor(fleet, monkeypatch, {FLEET_ARCH: "1.0.0"})
        fleet.seed_registry("line-a", {"rtmp-1": stream_registry_entry(
            source_type="RTMP", url="rtmp://media.local/live/line1")})
        fleet.gg.register_device("line-a")

        status, payload = fleet.deploy(
            ["line-a"],
            camera_bindings={"line-a": {"s1": {"cameraSourceId": "rtmp-1"}}})

        assert status == 409
        assert payload["error"]["code"] == "CAMERA_BINDINGS_INVALID"
        codes = {e["code"] for e in payload["error"]["details"]["errors"]}
        assert codes == {"CAMERA_TYPE_INCOMPATIBLE"}
        assert fleet.iot_data.bindings == {}
        assert fleet.gg.create_deployment_calls == []


# --------------------------------------------------------------------------
# 2. Warning ids for other types are unchanged (9.5, 9.6, 18.1)
# --------------------------------------------------------------------------

#: The pre-feature degraded-condition order and warning-id formula,
#: restated from camera-registry-sync Requirement 9.3 rather than imported
#: from the implementation: absent, then stale, then the pending/failed
#: sync status. ``stream-failed`` is appended AFTER all three, which is
#: what keeps every other type's id byte-identical.
_PRE_FEATURE_CONDITION_ORDER = ("absent", "stale", "pending")


def _pre_feature_warning_id(thing, node_id, csid, conditions):
    return f"camera-degraded:{thing}:{node_id}:{csid}:{'+'.join(conditions)}"


#: Camera_Source types of every other family, each paired with a
#: Camera_Input_Node type it is compatible with (so the only finding is the
#: degraded-source warning) and with the capability shape its producer
#: actually reports — none of which carries a ``stream`` section (design
#: component 6).
_OTHER_TYPE_ENTRIES = {
    "Camera": ("icam_source",
               {"formats": ["YUYV"], "resolutions": ["1920x1080"]}),
    "ICam": ("icam_source", {"formats": ["MJPG"]}),
    "V4L2Discovered": ("icam_source",
                       {"formats": ["YUYV"], "driver": "uvcvideo"}),
    "NvidiaCSI": ("csi_camera_source",
                  {"sensorModes": [{"width": 1920, "height": 1080}]}),
    "AravisDiscovered": ("aravis_camera_source",
                         {"pixelFormats": ["Mono8"]}),
    "StaticImage": ("aravis_camera_source", {}),
}


def _degraded_entry(source_type, capabilities, conditions,
                    stream_state=None):
    """A registry entry in exactly the given pre-feature conditions."""
    entry = {
        "type": source_type,
        "params": {"devicePath": "/dev/video0"},
        "capabilities": dict(capabilities),
        "absent": "absent" in conditions,
        "stale": "stale" in conditions,
        "sync_status": "pending" if "pending" in conditions else "synced",
    }
    if stream_state is not None:
        entry["capabilities"]["stream"] = {"state": stream_state}
    return entry


def _version_item(node_type):
    return {"has_binding_points": True,
            "camera_input_nodes": [{"node_id": "n1",
                                    "node_type": node_type}]}


def _validate_one(deployments, node_type, entry, confirmed=None):
    """``validate_camera_bindings`` over a single device/node/entry."""
    return deployments.validate_camera_bindings(
        _version_item(node_type), ["line-a"],
        {"line-a": {"never_synced": False, "cameras": {"cs-1": entry}}},
        {"line-a": {"n1": {"cameraSourceId": "cs-1"}}}, confirmed or [])


#: Every subset of the pre-feature conditions, in canonical order.
_CONDITION_SUBSETS = [
    tuple(c for c in _PRE_FEATURE_CONDITION_ORDER if c in subset)
    for size in range(len(_PRE_FEATURE_CONDITION_ORDER) + 1)
    for subset in itertools.combinations(_PRE_FEATURE_CONDITION_ORDER, size)
]


class TestOtherTypeWarningIdsUnchanged:
    @pytest.mark.parametrize("source_type", sorted(_OTHER_TYPE_ENTRIES))
    @pytest.mark.parametrize("conditions", _CONDITION_SUBSETS,
                             ids=lambda c: "+".join(c) or "healthy")
    def test_every_other_type_keeps_its_pre_feature_warning_id(
            self, deployments, source_type, conditions):
        """For every non-stream Camera_Source type and every combination
        of the pre-feature degraded conditions, the emitted warning id
        (and its ``conditions`` list) is exactly the pre-feature one — no
        ``stream-failed`` member, no reordering (9.6, 18.1)."""
        node_type, capabilities = _OTHER_TYPE_ENTRIES[source_type]
        entry = _degraded_entry(source_type, capabilities, conditions)

        errors, warnings = _validate_one(deployments, node_type, entry)

        assert errors == []
        if not conditions:
            assert warnings == []
            return
        [warning] = warnings
        assert warning["conditions"] == list(conditions)
        assert warning["id"] == _pre_feature_warning_id(
            "line-a", "n1", "cs-1", conditions)
        assert deployments.CAMERA_CONDITION_STREAM_FAILED not in warning["id"]

    def test_a_confirmation_held_from_before_the_feature_still_confirms(
            self, deployments):
        """The operational meaning of "unchanged": a warning id an
        operator confirmed before this feature shipped — a literal string
        here, not one the implementation computed — still matches, so the
        submission is still accepted (9.6, 18.1)."""
        held_id = "camera-degraded:line-a:n1:cs-1:stale+pending"
        entry = _degraded_entry("Camera", _OTHER_TYPE_ENTRIES["Camera"][1],
                                ("stale", "pending"))

        errors, warnings = _validate_one(deployments, "icam_source", entry,
                                         confirmed=[held_id])

        assert errors == []
        [warning] = warnings
        assert warning["id"] == held_id
        assert warning["confirmed"] is True

    def test_stream_failed_is_appended_after_the_pre_feature_conditions(
            self, deployments):
        """A stream Camera_Source reporting ``state: failed`` raises the
        existing degraded-source warning with ``stream-failed`` appended
        LAST, so the id is the pre-feature id plus a suffix rather than a
        different string (9.5)."""
        entry = _degraded_entry("RTSP", {}, ("stale", "pending"),
                                stream_state="failed")

        errors, warnings = _validate_one(deployments, "rtsp_camera_source",
                                         entry)

        assert errors == []
        [warning] = warnings
        assert warning["conditions"] == ["stale", "pending", "stream-failed"]
        assert warning["id"] == \
            "camera-degraded:line-a:n1:cs-1:stale+pending+stream-failed"
        assert warning["code"] == deployments.CAMERA_WARNING_SOURCE_DEGRADED
        assert warning["confirmed"] is False

    def test_failed_stream_alone_requires_confirmation(self, deployments):
        """An otherwise healthy stream source whose last reported
        Stream_Health state is ``failed`` is degraded on that condition
        alone, and submitting the id confirms it (9.5)."""
        entry = _degraded_entry("RTMP", {}, (), stream_state="failed")
        warning_id = "camera-degraded:line-a:n1:cs-1:stream-failed"

        errors, warnings = _validate_one(deployments, "rtmp_stream_source",
                                         entry)
        [warning] = warnings
        assert errors == []
        assert warning["id"] == warning_id
        assert warning["confirmed"] is False

        _, confirmed_warnings = _validate_one(
            deployments, "rtmp_stream_source", entry, confirmed=[warning_id])
        assert confirmed_warnings[0]["confirmed"] is True

    @pytest.mark.parametrize("state", ["streaming", "reconnecting", "idle",
                                       "FAILED", "Failed", None])
    def test_non_failed_stream_states_raise_no_warning(self, deployments,
                                                       state):
        """Only the exact lowercase ``failed`` degrades: a streaming,
        reconnecting, idle, or differently-cased state leaves a healthy
        stream source warning-free (9.5)."""
        entry = _degraded_entry("RTSP", {}, (), stream_state=state)
        errors, warnings = _validate_one(deployments, "rtsp_camera_source",
                                         entry)
        assert errors == []
        assert warnings == []

    @pytest.mark.parametrize("capabilities", [
        None, [], "stream", {"stream": None}, {"stream": []},
        {"stream": "failed"}, {"stream": {"state": None}},
        {"stream": {"state": 7}}, {"stream": {}},
    ])
    def test_malformed_capabilities_read_as_no_state_reported(
            self, deployments, capabilities):
        """``_stream_health_state`` is total: a malformed capabilities or
        stream section is "no state reported", never an exception and
        never a warning."""
        entry = {"type": "RTSP", "params": {"url": "rtsp://cam.local/live"},
                 "capabilities": capabilities, "absent": False,
                 "stale": False, "sync_status": "synced"}
        errors, warnings = _validate_one(deployments, "rtsp_camera_source",
                                         entry)
        assert errors == []
        assert warnings == []


# --------------------------------------------------------------------------
# 3. Floor rejections name the required version (9.7) + identity (18.1)
# --------------------------------------------------------------------------

class TestFloorRejectionNamesRequiredVersion:
    """The pre-submit feature-floor gate: rejected devices are told which
    LocalServer version they must reach (Requirement 9.7)."""

    def deploy_stream_workflow(self, fleet, monkeypatch, floor_map,
                               installed="1.0.5"):
        fleet.seed_workflow([stream_node()])
        mark_stream_features(fleet)
        set_feature_floor(fleet, monkeypatch, floor_map)
        fleet.seed_registry("line-a", {"rtsp-1": stream_registry_entry()})
        fleet.gg.register_device("line-a", local_server_version=installed)
        return fleet.deploy(
            ["line-a"],
            camera_bindings={"line-a": {"s1": {"cameraSourceId": "rtsp-1"}}})

    def test_below_floor_device_is_rejected_with_the_version_named(
            self, fleet, monkeypatch):
        """A device below the feature floor is rejected with the existing
        409 INCOMPATIBLE_LOCAL_SERVER, and both the structured
        ``min_local_server_version`` and the human ``reason`` name the
        required version — nothing is delivered and no Greengrass
        deployment is created (9.7)."""
        status, payload = self.deploy_stream_workflow(
            fleet, monkeypatch, {FLEET_ARCH: "1.4.0"}, installed="1.0.5")

        assert status == 409, payload
        error = payload["error"]
        assert error["code"] == "INCOMPATIBLE_LOCAL_SERVER"
        [device] = error["details"]["incompatible_devices"]
        assert device["device"] == "line-a"
        assert device["local_server_version"] == "1.0.5"
        assert device["min_local_server_version"] == "1.4.0"
        assert "1.4.0" in device["reason"]
        # The configured floor that produced the per-device minimums is
        # echoed so an operator can see which build to publish.
        assert error["details"]["stream_features"] is True
        assert error["details"]["stream_camera_min_local_server_versions"] \
            == {FLEET_ARCH: "1.4.0"}
        assert fleet.iot_data.bindings == {}
        assert fleet.gg.create_deployment_calls == []

    def test_device_exactly_at_the_floor_is_accepted(self, fleet,
                                                     monkeypatch):
        """The floor is inclusive: a device running exactly the required
        version deploys, and the bindings are delivered (9.7)."""
        status, payload = self.deploy_stream_workflow(
            fleet, monkeypatch, {FLEET_ARCH: "1.4.0"}, installed="1.4.0")

        assert status == 201, payload
        assert fleet.iot_data.bindings["line-a"] == \
            expected_desired_document(
                fleet, {"s1": {"cameraSourceId": "rtsp-1"}})

    def test_comparison_is_numeric_not_lexicographic(self, fleet,
                                                     monkeypatch):
        """``1.0.10`` satisfies a ``1.0.9`` floor: the gate orders
        versions numerically, so a device is not rejected by a string
        comparison (9.7)."""
        status, payload = self.deploy_stream_workflow(
            fleet, monkeypatch, {FLEET_ARCH: "1.0.9"}, installed="1.0.10")
        assert status == 201, payload

    def test_unsupported_architecture_is_rejected_naming_the_arch(
            self, fleet, monkeypatch):
        """An architecture with no feature-floor entry has no supporting
        build, so there is no version to name: the gate fails closed with
        ``min_local_server_version: None`` and a reason naming the
        architecture and the feature (9.7)."""
        status, payload = self.deploy_stream_workflow(
            fleet, monkeypatch, {"arm64_jp7": "1.4.0"}, installed="99.0.0")

        assert status == 409, payload
        [device] = payload["error"]["details"]["incompatible_devices"]
        assert device["local_server_version"] == "99.0.0"
        assert device["min_local_server_version"] is None
        assert FLEET_ARCH in device["reason"]
        assert "stream camera" in device["reason"]

    def test_deployed_empty_map_rejects_every_device(self, fleet,
                                                     monkeypatch):
        """The shipped pre-enablement state (the map is empty until spec
        tasks 9.2/26.3 configure it) rejects every device rather than
        resolving a floor every device satisfies: fail closed (9.7)."""
        status, payload = self.deploy_stream_workflow(
            fleet, monkeypatch, {}, installed="99.0.0")

        assert status == 409, payload
        assert payload["error"]["code"] == "INCOMPATIBLE_LOCAL_SERVER"
        [device] = payload["error"]["details"]["incompatible_devices"]
        assert device["min_local_server_version"] is None
        assert fleet.gg.create_deployment_calls == []

    def test_per_version_pin_does_not_license_an_unsupported_device(
            self, fleet, monkeypatch):
        """A per-version ``min_local_server_version`` override is a floor
        pin, not a licence: it cannot lower a stream workflow below the
        feature floor (9.7)."""
        fleet.seed_workflow([stream_node()])
        mark_stream_features(fleet)
        fleet.env.stack.tables.versions.update_item(
            Key={"workflow_id": fleet.workflow_id, "version": 1},
            UpdateExpression="SET min_local_server_version = :v",
            ExpressionAttributeValues={":v": "1.0.0"})
        set_feature_floor(fleet, monkeypatch, {FLEET_ARCH: "1.4.0"})
        fleet.seed_registry("line-a", {"rtsp-1": stream_registry_entry()})
        fleet.gg.register_device("line-a", local_server_version="1.0.5")

        status, payload = fleet.deploy(
            ["line-a"],
            camera_bindings={"line-a": {"s1": {"cameraSourceId": "rtsp-1"}}})

        assert status == 409, payload
        [device] = payload["error"]["details"]["incompatible_devices"]
        assert device["min_local_server_version"] == "1.4.0"
        assert "1.4.0" in device["reason"]

    def test_stream_free_workflow_is_unaffected_by_the_feature_floor(
            self, fleet, monkeypatch):
        """Identity (18.1): with a 9.9.9 feature floor configured, a
        workflow that uses none of the new node types deploys exactly as
        before — the gate is activated by ``has_stream_features`` only."""
        fleet.seed_workflow([camera_node("n1")])
        set_feature_floor(fleet, monkeypatch, {FLEET_ARCH: "9.9.9"})
        fleet.seed_registry("line-a", {"cfg-1": {}})
        fleet.gg.register_device("line-a", local_server_version="1.0.5")

        status, payload = fleet.deploy(
            ["line-a"],
            camera_bindings={"line-a": {"n1": {"cameraSourceId": "cfg-1"}}})

        assert status == 201, payload

    def test_stream_free_rejection_payload_gains_no_stream_keys(
            self, fleet, monkeypatch):
        """Identity (18.1): a pre-feature workflow rejected by the
        ordinary architecture floor gets the pre-feature payload — no
        ``stream_features`` and no feature-floor map."""
        fleet.seed_workflow([camera_node("n1")])
        set_feature_floor(fleet, monkeypatch, {FLEET_ARCH: "9.9.9"})
        monkeypatch.setattr(fleet.deployments,
                            "WORKFLOW_MIN_LOCAL_SERVER_VERSION", "2.0.0")
        fleet.seed_registry("line-a", {"cfg-1": {}})
        fleet.gg.register_device("line-a", local_server_version="1.0.5")

        status, payload = fleet.deploy(
            ["line-a"],
            camera_bindings={"line-a": {"n1": {"cameraSourceId": "cfg-1"}}})

        assert status == 409, payload
        details = payload["error"]["details"]
        assert "stream_features" not in details
        assert "stream_camera_min_local_server_versions" not in details
        [device] = details["incompatible_devices"]
        assert device["min_local_server_version"] == "2.0.0"
        assert "2.0.0" in device["reason"]


class TestFloorGateFunctionLevel:
    """``check_local_server_compatibility`` directly, for the wording and
    the defaults the route depends on."""

    class _Greengrass:
        def __init__(self, installed):
            self.installed = installed

        def get_paginator(self, operation):
            assert operation == "list_installed_components"
            installed = self.installed

            class _Paginator:
                def paginate(self, coreDeviceThingName=None, **_):
                    version = installed.get(coreDeviceThingName)
                    components = ([] if version is None else [{
                        "componentName": "aws.edgeml.dda.LocalServer.arm64JP6",
                        "componentVersion": version}])
                    return iter([{"installedComponents": components}])
            return _Paginator()

    def test_reason_names_the_installed_and_the_required_version(
            self, deployments, monkeypatch):
        monkeypatch.setattr(
            deployments, "WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.2.3"})
        incompatible = deployments.check_local_server_compatibility(
            self._Greengrass({"jp6-a": "1.0.40"}), ["jp6-a"], "1.0.0", {},
            stream_features=True)

        [device] = incompatible
        assert device["min_local_server_version"] == "1.2.3"
        assert "1.2.3" in device["reason"]
        assert "1.0.40" in device["reason"]

    def test_feature_floor_raises_but_never_lowers_the_resolved_floor(
            self, deployments, monkeypatch):
        """The effective minimum is the maximum of the architecture floor
        and the feature floor, in both directions (9.7)."""
        monkeypatch.setattr(
            deployments, "WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.1.0"})
        gg = self._Greengrass({"jp6-a": "1.0.40"})

        # Architecture floor below the feature floor: the feature wins.
        [raised] = deployments.check_local_server_compatibility(
            gg, ["jp6-a"], "1.0.0", {"arm64_jp6": "1.0.1"},
            stream_features=True)
        assert raised["min_local_server_version"] == "1.1.0"

        # Architecture floor above it: the architecture floor stands.
        [kept] = deployments.check_local_server_compatibility(
            gg, ["jp6-a"], "1.0.0", {"arm64_jp6": "1.3.0"},
            stream_features=True)
        assert kept["min_local_server_version"] == "1.3.0"

    def test_stream_features_defaults_to_false(self, deployments,
                                               monkeypatch):
        """Identity (18.1): the parameter defaults off, so every existing
        call site — and every pre-feature workflow — resolves exactly as
        before even with a 9.9.9 feature floor configured."""
        import inspect

        signature = inspect.signature(
            deployments.check_local_server_compatibility)
        assert signature.parameters["stream_features"].default is False

        monkeypatch.setattr(
            deployments, "WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "9.9.9"})
        assert deployments.check_local_server_compatibility(
            self._Greengrass({"jp6-a": "1.0.40"}), ["jp6-a"], "1.0.0",
            {"arm64_jp6": "1.0.0"}) == []

    def test_no_local_server_installed_keeps_its_pre_feature_reason(
            self, deployments, monkeypatch):
        """A device with no LocalServer at all is rejected by the
        pre-existing branch, with its pre-feature reason, whether or not
        the workflow uses the new node types (18.1)."""
        monkeypatch.setattr(
            deployments, "WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.2.3"})
        [device] = deployments.check_local_server_compatibility(
            self._Greengrass({"jp6-a": None}), ["jp6-a"], "1.0.0", {},
            stream_features=True)
        assert device["local_server_version"] is None
        assert device["reason"] == \
            "No LocalServer component is installed on this device"

    def test_configured_feature_floor_is_never_completed_with_a_safe_floor(
            self, aws_stack, monkeypatch):
        """The module-level derivation of the feature floor must NOT pass
        the parsed map through ``_fill_missing_arch_floors``: filling a
        missing architecture with SAFE_LINEAGE_FLOOR ('1.0.0', which every
        field build satisfies) would turn "no supporting build exists for
        this lineage" into "every device passes" — failing OPEN on exactly
        the architectures Requirement 9.7 exists to protect.

        Re-imports the module with a PARTIAL floor map configured (the
        realistic state while the first supporting builds roll out, spec
        task 26.3) so the derivation itself is under test rather than the
        monkeypatched attribute the cases above use.
        """
        monkeypatch.setenv("WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
                           '{"arm64_jp7": "1.4.0"}')
        saved = {name: sys.modules.get(name)
                 for name in ("deployments", "workflow_guards")}
        for name in saved:
            sys.modules.pop(name, None)
        try:
            import deployments as fresh

            assert fresh.WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS == \
                {"arm64_jp7": "1.4.0"}
            assert fresh.stream_feature_floor_for("arm64_jp7") == "1.4.0"
            for arch in ("x86_64", "arm64_cpu", "arm64_jp5", "arm64_jp6"):
                assert fresh.stream_feature_floor_for(arch) is None, arch
            # ... while the per-lineage ARCHITECTURE floor is completed,
            # which is the contrast that makes the omission deliberate.
            assert fresh.SAFE_LINEAGE_FLOOR not in \
                fresh.WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS.values()
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


# ---------------------------------------------------------------------------
# Reqs 5.7, 6.1: the binding context serves params as the registry does
# ---------------------------------------------------------------------------

class TestBindingContextRedaction:
    """The binding matrix shows each option's URL, so the binding context
    must serve ``params`` exactly as ``GET /devices/{id}/cameras`` does:
    URL user information redacted, no Credential_Reference, and a legacy
    stream row's credential-like keys masked. Other types are served as
    before (Req 18.3)."""

    LEGACY_PASSWORD = "LEGACY-PWD-51c0"
    LEGACY_TOKEN = "LEGACY-TOKEN-8d2e"
    REFERENCE_SENTINEL = "REF-SENTINEL-6b90"

    def test_stream_options_are_redacted_like_the_camera_view(self, fleet):
        fleet.seed_workflow([{"node_id": "dock_cam",
                              "node_type": "rtsp_camera_source"}])
        fleet.seed_registry("line-a", {
            "legacy-rtsp": {"type": "RTSP", "params": {
                "url": (f"rtsp://viewer:{self.LEGACY_PASSWORD}@10.0.9.9"
                        f"/live?token={self.LEGACY_TOKEN}"),
                "password": self.LEGACY_PASSWORD, "transport": "tcp"}},
            "rtsp-dock": {"type": "RTSP", "params": {
                "url": "rtsp://10.0.4.21:554/Streaming/Channels/101",
                "credentialRef": {
                    "secretArn": ("arn:aws:secretsmanager:us-east-1:"
                                  "111122223333:secret:"
                                  f"{self.REFERENCE_SENTINEL}"),
                    "versionId": "v1"},
                "credentialsConfigured": True}},
            "usb": {"type": "Camera", "params": {
                "devicePath": "/dev/video0", "password": "camera-param"}},
        })

        status, payload = fleet.binding_context(["line-a"])

        assert status == 200, payload
        raw = json.dumps(payload, default=str)
        for value in (self.LEGACY_PASSWORD, self.LEGACY_TOKEN,
                      self.REFERENCE_SENTINEL):
            assert value not in raw
        options = {camera["camera_source_id"]: camera["params"]
                   for camera in payload["targets"]["line-a"]["cameras"]}
        legacy = options["legacy-rtsp"]
        assert legacy["password"] == "***"
        assert legacy["url"].startswith("rtsp://***@10.0.9.9/live")
        assert "token=***" in legacy["url"]
        assert legacy["transport"] == "tcp"
        assert options["rtsp-dock"] == {
            "url": "rtsp://10.0.4.21:554/Streaming/Channels/101",
            "credentialsConfigured": True}
        assert options["usb"] == {"devicePath": "/dev/video0",
                                  "password": "camera-param"}
