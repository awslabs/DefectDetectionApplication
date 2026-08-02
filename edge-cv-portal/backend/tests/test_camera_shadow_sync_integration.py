"""
Shadow sync integration tests (camera-registry-sync task 15.1).

The single integration example over the AWS-owned transports: everything
else in the feature is fake-tested, so these two tests wire the REAL
Edge_Sync_Agent (src/backend/camera_sync) to the REAL Portal_Sync_Service
ingest handler and Camera_Registry API (functions/camera_sync.py,
functions/camera_registry.py) across an emulated ``dda-camera-registry``
named shadow.

moto does not emulate IoT named shadows or IoT rules, so the shadow is a
:class:`NamedShadowEmulator` — one in-memory shadow document implementing
both sides' accessor contracts (the edge ``IoTShadowAccessor``'s
``get/update_thing_shadow_state_request`` and the portal iot-data client's
``get_thing_shadow``/``update_thing_shadow``) with AWS shadow semantics:
recursive state merge, ``null`` deletes a key, a delta computed as desired
minus reported, and one ``/update/documents`` event per accepted update.

Direction 1 (edge -> portal, Reqs 3.3, 12.4): the real agent, driven with
a fake clock, publishes the device inventory to the shadow — first while
disconnected (writes fail, the report is retained), then after reconnect
(the first successful write is the complete current state, Req 3.3). The
resulting documents event is wrapped exactly like the IoT topic rule
(``SELECT *, topic(3) AS thing_name``) and delivered as an SQS record to
the real ingest handler against the moto DynamoDB registry.

Direction 2 (portal -> edge, Reqs 5.5, 12.4): the real mutation route
writes ``desired.changes`` (with the emulator standing in for the
assumed-role iot-data client) and marks the registry entry pending; the
delta is delivered to the real agent's ``on_delta``, applied through the
image-source accessor, acknowledged in the resulting report, and that
report travels back through the rule/SQS ingest path until the registry
entry is synced. The disconnected-then-reconnect case delivers the pending
change through the reconnect-time ``apply_desired_changes`` pass over the
shadow's current desired document, exactly as ``server_setup`` does.

The device's IoT identity (Req 12.4) is represented structurally: the
shadow is scoped to one thing name (the emulator rejects access under any
other identity — on real AWS the device's IoT policy does the same), and
the ingest attribution comes from ``topic(3)`` of the thing-scoped shadow
topic, never from anything the payload claims.

The real-LocalServer apply path (SQLite, accessor schema validation) is
already covered by test/backend-test/camera_sync/
test_property_portal_change_round_trip.py; here a dict-backed accessor
keeps the focus on the transport wiring.

Requirements: 3.3, 5.5, 12.4
"""
import contextlib
import copy
import io
import json
import os
import pathlib
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION, TEST_ENV

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-shadow-sync"
SETTINGS_TABLE_NAME = "test-settings-shadow-sync"
DLQ_NAME = "test-camera-shadow-sync-dlq"

SHADOW_NAME = "dda-camera-registry"

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SRC_BACKEND = _REPO_ROOT / "src" / "backend"


# --- real edge modules (loaded by path) ----------------------------------------


def _load_edge_modules():
    """Load the REAL edge packages from src/backend.

    The edge package and the portal Lambda module are both named
    ``camera_sync``, so the edge package (whose submodules import each
    other absolutely) is imported under its own name first and then
    detached from sys.modules — the already-executed module objects keep
    their internal references, and the portal module can own the name for
    the rest of the suite (conftest re-import pattern).
    """
    saved = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == "camera_sync" or name.startswith("camera_sync.")
    }
    sys.path.insert(0, str(_SRC_BACKEND))
    try:
        import camera_discovery
        import camera_sync as edge_camera_sync
    finally:
        for name in list(sys.modules):
            if name == "camera_sync" or name.startswith("camera_sync."):
                del sys.modules[name]
        sys.modules.update(saved)
        sys.path.remove(str(_SRC_BACKEND))
    return camera_discovery, edge_camera_sync


_camera_discovery, _edge_camera_sync = _load_edge_modules()

DiscoveredCamera = _camera_discovery.DiscoveredCamera
DiscoveryResult = _camera_discovery.DiscoveryResult
EdgeSyncAgent = _edge_camera_sync.EdgeSyncAgent
CameraSyncStateStore = _edge_camera_sync.CameraSyncStateStore
build_inventory = _edge_camera_sync.build_inventory


# --- the emulated named shadow --------------------------------------------------


class NamedShadowEmulator:
    """One in-memory ``dda-camera-registry`` named shadow serving both
    sides' accessor contracts.

    Edge side (``IoTShadowAccessor`` contract):
      - ``get_thing_shadow_state_request(thing_name, shadow_name)``
      - ``update_thing_shadow_state_request(thing_name, shadow_name, state)``
      Both honor :attr:`edge_online` — a disconnected device's shadow I/O
      fails (Reqs 3.3, 5.5).

    Portal side (assumed-role iot-data client contract):
      - ``update_thing_shadow(thingName, shadowName, payload)``
      - ``get_thing_shadow(thingName, shadowName)``
      Cloud-side access works regardless of device connectivity — the
      shadow itself is the retention buffer for pending desired changes.

    Shadow document semantics follow AWS: state sections merge
    recursively, an explicit ``null`` deletes a key, the delta is desired
    minus reported, and every accepted update emits an
    ``/update/documents`` event (recorded with which sections the update
    touched, so tests can select the reported-state events the ingest
    direction consumes).

    Req 12.4: the emulator is scoped to a single thing name — access
    under any other identity raises, standing in for the device's IoT
    policy restricting shadow access to the thing's own identity.
    """

    def __init__(self, thing_name):
        self.thing_name = thing_name
        self.desired = {}
        self.reported = {}
        self.version = 0
        self.edge_online = True
        self.documents_events = []  # [(touched_sections, documents_payload)]

    # --- shared shadow-document core -----------------------------------

    def _check_identity(self, thing_name, shadow_name):
        assert thing_name == self.thing_name, (
            "shadow access under a foreign IoT identity: {!r} != {!r} "
            "(Req 12.4)".format(thing_name, self.thing_name)
        )
        assert shadow_name == SHADOW_NAME

    @staticmethod
    def _merge(target, patch):
        """AWS shadow merge: dicts merge recursively, null deletes."""
        for key, value in patch.items():
            if value is None:
                target.pop(key, None)
            elif isinstance(value, dict):
                node = target.get(key)
                if not isinstance(node, dict):
                    node = {}
                    target[key] = node
                NamedShadowEmulator._merge(node, value)
            else:
                target[key] = value

    def _accept_update(self, state):
        touched = set()
        for section in ("desired", "reported"):
            if section in state and state[section] is not None:
                self._merge(getattr(self, section), state[section])
                touched.add(section)
        self.version += 1
        self.documents_events.append((
            frozenset(touched),
            {
                "current": {
                    "state": {
                        "desired": copy.deepcopy(self.desired),
                        "reported": copy.deepcopy(self.reported),
                    },
                    "version": self.version,
                },
            },
        ))

    @staticmethod
    def _diff(desired, reported):
        out = {}
        for key, value in desired.items():
            current = reported.get(key) if isinstance(reported, dict) else None
            if isinstance(value, dict):
                sub = NamedShadowEmulator._diff(
                    value, current if isinstance(current, dict) else {}
                )
                if sub:
                    out[key] = sub
            elif value != current:
                out[key] = value
        return out

    def delta(self):
        """The shadow delta message (desired minus reported), or ``None``
        when nothing is pending — what the shadow service would publish on
        ``.../update/delta`` to the connected device."""
        state = self._diff(self.desired, self.reported)
        if not state:
            return None
        return {"state": copy.deepcopy(state), "version": self.version}

    def drain_reported_events(self):
        """Documents events from updates that touched reported state,
        drained — the events the edge->portal ingest direction forwards
        (desired-only updates carry no new reported state to ingest)."""
        events = [
            payload for touched, payload in self.documents_events
            if "reported" in touched
        ]
        self.documents_events = []
        return events

    # --- edge side: IoTShadowAccessor contract ---------------------------

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        self._check_identity(thing_name, shadow_name)
        if not self.edge_online:
            raise ConnectionError("device has no AWS IoT connectivity")
        if not self.desired and not self.reported:
            return None
        return {
            "desired": copy.deepcopy(self.desired),
            "reported": copy.deepcopy(self.reported),
        }

    def update_thing_shadow_state_request(self, thing_name, shadow_name, state):
        self._check_identity(thing_name, shadow_name)
        if not self.edge_online:
            raise ConnectionError("device has no AWS IoT connectivity")
        self._accept_update(state)

    # --- portal side: iot-data client contract ---------------------------

    def update_thing_shadow(self, thingName, shadowName, payload):
        self._check_identity(thingName, shadowName)
        self._accept_update(json.loads(payload).get("state") or {})
        return {}

    def get_thing_shadow(self, thingName, shadowName):
        self._check_identity(thingName, shadowName)
        document = {
            "state": {
                "desired": copy.deepcopy(self.desired),
                "reported": copy.deepcopy(self.reported),
            },
            "version": self.version,
        }
        return {"payload": io.BytesIO(json.dumps(document).encode())}


def iot_rule_record(thing_name, documents_event):
    """One SQS record exactly as the IoT topic rule produces it.

    The rule is ``SELECT *, topic(3) AS thing_name FROM
    '$aws/things/+/shadow/name/dda-camera-registry/update/documents'``:
    the body is the documents payload plus the thing name parsed from the
    topic. Req 12.4: the topic is thing-scoped and only the device's own
    IoT identity may publish to its shadow, so ``topic(3)`` attributes the
    report to the authenticated device — never to anything the payload
    itself claims.
    """
    body = dict(documents_event)
    body["thing_name"] = thing_name  # topic(3)
    return {"messageId": str(uuid.uuid4()), "body": json.dumps(body)}


# --- edge-side fakes (same patterns as the sibling camera_sync suites) ----------


class FakeClock:
    def __init__(self, start=1_730_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeDiscovery:
    def __init__(self, snapshot):
        self.latest_snapshot = snapshot


class FakeImageSourceAccessor:
    """Dict-backed stand-in for the LocalServer ``ImageSourceAccessor``.

    The real-accessor apply path (SQLite, schema validation, verbatim
    error messages) is exercised by the Property 7 suite in
    test/backend-test/camera_sync/; this integration example is about the
    transports, so a minimal accessor implementing the agent's contract
    (list/create/update/delete) suffices.
    """

    def __init__(self):
        self.sources = {}
        self._next_id = 1

    def list_image_sources(self, request, session):
        return [copy.deepcopy(source) for source in self.sources.values()]

    def create_image_source(self, data, session):
        image_source_id = str(self._next_id)
        self._next_id += 1
        self.sources[image_source_id] = {
            "imageSourceId": image_source_id,
            "name": data.get("name"),
            "type": data.get("type"),
            "cameraId": data.get("cameraId"),
            "location": data.get("location"),
            "imageSourceConfiguration": {},
        }
        return {"imageSourceId": image_source_id}

    def update_image_source(self, image_source_id, data, session):
        source = self.sources[image_source_id]
        for key, value in data.items():
            if key == "imageSourceConfiguration":
                source["imageSourceConfiguration"].update(value)
            else:
                source[key] = value

    def delete_image_source(self, image_source_id, session):
        del self.sources[image_source_id]


def _flush(agent, clock, max_iterations=20):
    """Pump the agent past debounce/backoff waits until it goes idle."""
    for _ in range(max_iterations):
        delay = agent.pump()
        if delay is None:
            return
        clock.advance(delay + 0.001)
    raise AssertionError("agent never went idle")


def _pump_offline(agent, clock, attempts=3):
    """Let a disconnected agent burn a few failed write attempts; the
    pending report stays retained (dirty) for the reconnect catch-up."""
    for _ in range(attempts):
        delay = agent.pump()
        assert delay is not None, "agent went idle while offline"
        clock.advance(delay + 0.001)


# --- portal-side fixtures and helpers -------------------------------------------


@pytest.fixture(scope="module")
def sync_env(aws_stack):
    """Registry + settings tables, the shadow-report DLQ, and freshly
    bound portal camera_sync / camera_registry modules (conftest pattern)."""
    import boto3

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=CAMERA_REGISTRY_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "device_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "device_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-index",
            "KeySchema": [{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=SETTINGS_TABLE_NAME,
        KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "setting_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    sqs = boto3.client("sqs", region_name=REGION)
    dlq_url = sqs.create_queue(QueueName=DLQ_NAME)["QueueUrl"]

    os.environ["CAMERA_REGISTRY_TABLE"] = CAMERA_REGISTRY_TABLE_NAME
    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
    os.environ["CAMERA_SHADOW_REPORT_DLQ_URL"] = dlq_url

    sys.modules.pop("camera_sync", None)
    sys.modules.pop("camera_registry", None)
    import camera_sync
    import camera_registry

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        camera_sync=camera_sync,
        camera_registry=camera_registry,
        registry=resource.Table(CAMERA_REGISTRY_TABLE_NAME),
        devices=resource.Table(TEST_ENV["DEVICES_TABLE"]),
    )


def ingest(sync_env, thing_name, emulator):
    """Deliver the emulator's new reported-state documents events through
    the IoT-rule wrapper and the real SQS ingest handler."""
    events = emulator.drain_reported_events()
    assert events, "no reported-state shadow events to ingest"
    records = [iot_rule_record(thing_name, event) for event in events]
    result = sync_env.camera_sync.handler({"Records": records}, None)
    assert result == {"batchItemFailures": []}
    return events


def device_items(sync_env, thing_name):
    from boto3.dynamodb.conditions import Key

    response = sync_env.registry.query(
        KeyConditionExpression=Key("device_id").eq(thing_name))
    return {item["sk"]: item for item in response["Items"]}


def make_event(method, device_id, user, sub_path="", query=None, body=None):
    path_parameters = {"id": device_id}
    if sub_path and sub_path != "/refresh":
        path_parameters["csid"] = sub_path.lstrip("/")
    return {
        "httpMethod": method,
        "path": f"/devices/{device_id}/cameras{sub_path}",
        "pathParameters": path_parameters,
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": user["user_id"],
                    "email": user["email"],
                    "cognito:username": user["username"],
                    "custom:role": user["role"],
                }
            }
        },
    }


def invoke_registry_api(sync_env, method, device_id, user, sub_path="",
                        body=None):
    response = sync_env.camera_registry.handler(
        make_event(method, device_id, user, sub_path, body=body), None)
    return response["statusCode"], json.loads(response["body"])


def make_edge_device(sync_env, env, tmp_path, snapshot=None):
    """A registered device with a real EdgeSyncAgent over the emulated
    shadow: fake clock, dict-backed accessor, real build_inventory."""
    usecase_id = env.create_usecase()
    thing_name = f"thing-{uuid.uuid4()}"
    sync_env.devices.put_item(Item={
        "device_id": thing_name, "usecase_id": usecase_id,
    })

    emulator = NamedShadowEmulator(thing_name)
    accessor = FakeImageSourceAccessor()
    discovery = FakeDiscovery(snapshot if snapshot is not None
                              else DiscoveryResult())
    clock = FakeClock()
    agent = EdgeSyncAgent(
        iot_shadow_accessor=emulator,
        image_source_accessor=accessor,
        camera_discovery=discovery,
        db_session_factory=lambda: contextlib.nullcontext(),
        state_store=CameraSyncStateStore(
            str(tmp_path / f"camera_sync_state_{thing_name}.json")),
        thing_name=thing_name,
        clock=clock,
        wall_clock=clock,
    )
    return SimpleNamespace(
        usecase_id=usecase_id, thing_name=thing_name, emulator=emulator,
        accessor=accessor, discovery=discovery, agent=agent, clock=clock,
    )


def add_configured_camera(device, name="Line camera", device_path="/dev/video0",
                          gain=4, exposure=900):
    """One configured Image_Source on the device; returns its cfg- csid."""
    source_id = device.accessor.create_image_source(
        {"name": name, "type": "Camera", "cameraId": f"cam-{source_suffix()}"},
        None)["imageSourceId"]
    device.accessor.update_image_source(
        source_id,
        {"imageSourceConfiguration": {
            "device": device_path, "gain": gain, "exposure": exposure}},
        None)
    return source_id, f"cfg-{source_id}"


_suffix_counter = iter(range(10_000))


def source_suffix():
    return next(_suffix_counter)


def establish_synced_registry(sync_env, env, tmp_path):
    """Direction-2 preamble: seed the registry through the real
    edge->portal path (agent report -> rule -> SQS -> ingest handler)."""
    device = make_edge_device(sync_env, env, tmp_path)
    source_id, csid = add_configured_camera(device)
    device.agent.report_inventory()
    _flush(device.agent, device.clock)
    ingest(sync_env, device.thing_name, device.emulator)

    items = device_items(sync_env, device.thing_name)
    entry = items[f"CAMERA#{csid}"]
    assert entry["sync_status"] == "synced"
    return device, source_id, csid


@pytest.fixture
def portal_shadow_client(sync_env, monkeypatch):
    """Route the mutation routes' assumed-role iot-data client to a
    per-test emulator (bound by the test via ``bind``)."""
    holder = {}
    monkeypatch.setattr(
        sync_env.camera_registry, "iot_data_client",
        lambda usecase_id: holder["emulator"])
    return SimpleNamespace(bind=lambda emulator: holder.update(
        emulator=emulator))


# --- direction 1: edge -> portal over the rule/SQS path -------------------------


class TestEdgeToPortal:
    def test_edge_report_reaches_registry_through_rule_and_sqs(
            self, sync_env, env, tmp_path):
        """A disconnected device's inventory report is retained, published
        as the complete current state on reconnect (Req 3.3), forwarded by
        the thing-scoped IoT rule to SQS under the device's IoT identity
        (Req 12.4), and ingested into the registry scoped to the device's
        use case."""
        snapshot = DiscoveryResult(cameras=[
            DiscoveredCamera(
                stable_id="disc-int000000001", device_path="/dev/video0",
                card_name="Line sensor", bus_info="usb-0000:00:14.0-1",
                driver="uvcvideo", kind="v4l2",
                formats=[{"pixel_format": "YUYV",
                          "resolutions": [[1920, 1080], [1280, 720]]}]),
            DiscoveredCamera(
                stable_id="disc-int000000002", device_path="/dev/video1",
                card_name="USB spare cam", bus_info="usb-0000:00:14.0-2",
                driver="uvcvideo", kind="v4l2",
                formats=[{"pixel_format": "MJPG",
                          "resolutions": [[640, 480]]}]),
        ])
        device = make_edge_device(sync_env, env, tmp_path, snapshot=snapshot)
        source_id, cfg_csid = add_configured_camera(
            device, device_path="/dev/video0")

        # Disconnected: the write fails and the report is retained (3.3).
        device.emulator.edge_online = False
        device.agent.report_inventory()
        _pump_offline(device.agent, device.clock)
        assert device.emulator.reported == {}

        # Reconnect: the first successful write is the complete current
        # state — one report, not a replay of queued deltas (3.3).
        device.emulator.edge_online = True
        _flush(device.agent, device.clock)
        events = ingest(sync_env, device.thing_name, device.emulator)
        assert len(events) == 1

        # The registry mirrors the device inventory (build_inventory oracle).
        expected = build_inventory(
            device.accessor.list_image_sources(None, None), snapshot)
        items = device_items(sync_env, device.thing_name)
        camera_items = {
            sk[len("CAMERA#"):]: item for sk, item in items.items()
            if sk.startswith("CAMERA#")
        }
        assert set(camera_items) == {
            entry.camera_source_id for entry in expected}

        # Configured source merged with its discovered hardware.
        cfg = camera_items[cfg_csid]
        assert cfg["origin"] == "edge-configured"
        assert cfg["name"] == "Line camera"
        assert cfg["type"] == "Camera"
        assert cfg["sync_status"] == "synced"
        assert cfg["params"]["devicePath"] == "/dev/video0"
        assert cfg["params"]["gain"] == 4
        assert cfg["params"]["exposure"] == 900
        assert cfg["capabilities"]["driver"] == "uvcvideo"
        assert cfg["capabilities"]["formats"][0]["pixelFormat"] == "YUYV"

        # Discovered-only hardware reports under its discovery stable id.
        disc = camera_items["disc-int000000002"]
        assert disc["origin"] == "edge-discovered"
        assert disc["params"]["devicePath"] == "/dev/video1"

        # Ingest attribution and scoping come from the device's IoT
        # identity: the thing name from the shadow topic keyed the
        # registry partition and resolved the use case (Req 12.4).
        reported_at = events[0]["current"]["state"]["reported"]["reportedAt"]
        meta = items["META"]
        assert meta["usecase_id"] == device.usecase_id
        assert meta["last_report_at"] == reported_at
        assert meta["never_synced"] is False
        assert all(item["usecase_id"] == device.usecase_id
                   for item in camera_items.values())


# --- direction 2: portal -> edge over the shadow delta --------------------------


class TestPortalToEdge:
    def test_portal_change_delta_applied_acked_and_synced(
            self, sync_env, env, tmp_path, portal_shadow_client):
        """A portal desired change is delivered as a shadow delta, applied
        by the real agent, acknowledged in its report, and marked synced
        by the real ingest handler (Reqs 5.1-5.3 wiring over the emulated
        Sync_Channel)."""
        device, source_id, csid = establish_synced_registry(
            sync_env, env, tmp_path)
        portal_shadow_client.bind(device.emulator)
        operator = env.make_user(role="Operator")

        status, body = invoke_registry_api(
            sync_env, "PUT", device.thing_name, operator, sub_path=f"/{csid}",
            body={"name": "Portal name", "type": "Camera",
                  "params": {"devicePath": "/dev/video2", "gain": 8,
                             "exposure": 1200}})
        assert status == 200
        change_id = body["portal_change_id"]

        # Registry entry pending; the desired change sits in the shadow.
        entry = device_items(sync_env, device.thing_name)[f"CAMERA#{csid}"]
        assert entry["sync_status"] == "pending"
        assert entry["portal_change_id"] == change_id

        # The shadow service publishes the delta (desired minus reported)
        # to the connected device; the agent applies and reports.
        delta = device.emulator.delta()
        assert set(delta["state"]["changes"]) == {csid}
        device.agent.on_delta(delta)
        _flush(device.agent, device.clock)

        # Applied on the device through the accessor.
        source = device.accessor.sources[source_id]
        assert source["name"] == "Portal name"
        assert source["imageSourceConfiguration"]["device"] == "/dev/video2"
        assert source["imageSourceConfiguration"]["gain"] == 8

        # The processed desired entry was cleared (null write), so the
        # delta does not re-fire.
        assert device.emulator.desired.get("changes", {}) == {}
        assert device.emulator.delta() is None

        # The resulting report carries the ack and travels the same
        # rule/SQS path back into the registry.
        events = ingest(sync_env, device.thing_name, device.emulator)
        reported = events[-1]["current"]["state"]["reported"]
        assert reported["cameras"][csid]["ack"] == change_id

        entry = device_items(sync_env, device.thing_name)[f"CAMERA#{csid}"]
        assert entry["sync_status"] == "synced"
        assert entry["name"] == "Portal name"
        assert entry["params"]["devicePath"] == "/dev/video2"
        assert entry["params"]["gain"] == 8
        assert "pending_content" not in entry
        assert "portal_change_id" not in entry
        # No conflict was classified anywhere along the round trip.
        assert not any(sk.startswith("CONFLICT#") for sk in
                       device_items(sync_env, device.thing_name))

    def test_pending_change_is_delivered_after_reconnect(
            self, sync_env, env, tmp_path, portal_shadow_client):
        """A change issued while the device is disconnected stays pending
        (Req 5.5) — the shadow retains the desired document — and is
        applied on reconnect through the same apply_desired_changes pass
        over the current desired document that server_setup runs, then
        acknowledged and marked synced."""
        device, source_id, csid = establish_synced_registry(
            sync_env, env, tmp_path)
        portal_shadow_client.bind(device.emulator)
        operator = env.make_user(role="Operator")

        # Device disconnected: cloud-side shadow writes still succeed —
        # the shadow itself buffers the pending change (Req 5.5).
        device.emulator.edge_online = False
        status, body = invoke_registry_api(
            sync_env, "PUT", device.thing_name, operator, sub_path=f"/{csid}",
            body={"name": "Reconnect name", "type": "Camera",
                  "params": {"devicePath": "/dev/video3", "gain": 2,
                             "exposure": 700}})
        assert status == 200
        change_id = body["portal_change_id"]

        # Retained as pending while the device is offline: the registry
        # entry stays pending, the device state untouched, the desired
        # change parked in the shadow.
        entry = device_items(sync_env, device.thing_name)[f"CAMERA#{csid}"]
        assert entry["sync_status"] == "pending"
        assert device.accessor.sources[source_id]["name"] == "Line camera"
        assert csid in device.emulator.desired["changes"]

        # Reconnect: server_setup's reconnect pass reads the shadow's
        # current desired document and applies the parked changes (5.5).
        device.emulator.edge_online = True
        state = device.emulator.get_thing_shadow_state_request(
            device.thing_name, SHADOW_NAME)
        changes = (state.get("desired") or {}).get("changes")
        assert changes
        device.agent.apply_desired_changes(changes)
        _flush(device.agent, device.clock)

        assert device.accessor.sources[source_id]["name"] == "Reconnect name"
        assert device.emulator.desired.get("changes", {}) == {}

        events = ingest(sync_env, device.thing_name, device.emulator)
        reported = events[-1]["current"]["state"]["reported"]
        assert reported["cameras"][csid]["ack"] == change_id

        entry = device_items(sync_env, device.thing_name)[f"CAMERA#{csid}"]
        assert entry["sync_status"] == "synced"
        assert entry["name"] == "Reconnect name"
        assert entry["params"]["devicePath"] == "/dev/video3"
        assert not any(sk.startswith("CONFLICT#") for sk in
                       device_items(sync_env, device.thing_name))
