#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The Edge_Sync_Agent and stream cameras (rtsp-rtmp-stream-cameras tasks
18.1, 18.2 and 18.4 — Requirements 4.5, 4.6, 5.5, 5.6, 16.5, 18.2).

- An inventory, and a report, without stream sources is exactly today's.
- Stream cameras report ``url``, settings, ``credentialsConfigured`` and the
  coarse ``capabilities.stream``; health changes re-report at most once per
  camera per 30 s; ``deviceCapabilities.streamIngest`` is reported once the
  probe finished.
- A Portal create or update carrying a Credential_Reference is fetched,
  applied and stored, in that order; a fetch ``AccessDenied`` fails the
  change with a secret-free reason and changes nothing; a failed store
  write leaves no half-created camera; a held reference is not fetched
  again; a clear removes the credentials.

The apply tests use the real ``ImageSourceAccessor`` over a private sqlite
database and a real Credential_Store; the shadow, the discovery and the
stream manager are fakes.
"""
import json
import os
import sys
from unittest.mock import Mock

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "static_image_camera"))

from camera_manager_support import import_camera_manager  # noqa: E402

import_camera_manager()

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import dao.sqlite_db.models as db_models  # noqa: E402
from camera_sync import CameraSyncStateStore, EdgeSyncAgent  # noqa: E402
from camera_sync.inventory import build_inventory  # noqa: E402
from camera_sync.stream_reporting import StreamReportDebouncer, stream_ingest_section  # noqa: E402
from dao.sqlite_db.sqlite_db_operations import Base  # noqa: E402
from stream_ingest import credential_fetch  # noqa: E402
from stream_ingest import credentials as credentials_module  # noqa: E402
from stream_ingest.credential_fetch import CredentialFetchError  # noqa: E402
from utils import constants, dda_user_management_utils  # noqa: E402

_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_DEFAULT_CAMERA_CONFIG = os.path.join(_REPO_ROOT, "src", "backend", "utils", "config",
                                      "default_camera_configurations.json")

PASSWORD = "pw-AGENT-2e81"
REF = {"secretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:dda-portal/stream-camera-credentials/thing/cam-AbCdEf",
       "versionId": "0b4f7e7c-1d7a-4f45-9d3a-2d1c5c9f7a11"}
REF2 = dict(REF, versionId="5e1f0000-1d7a-4f45-9d3a-2d1c5c9f7a22")


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Shadow:
    def __init__(self):
        self.reported = []
        self.desired = []

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        return None

    def update_thing_shadow_state_request(self, thing_name, shadow_name, state):
        if "reported" in state:
            self.reported.append(json.loads(json.dumps(state["reported"])))
        if "desired" in state:
            self.desired.append(state["desired"])


class Discovery:
    latest_snapshot = None


class FakeStreamManager:
    def __init__(self):
        self.health_listeners = []
        self.capability_listeners = []
        self.health_by_id = {}
        self.capabilities_value = None
        self.notified = []
        self.deleted = []

    def add_health_listener(self, listener):
        self.health_listeners.append(listener)

    def on_capabilities(self, listener):
        self.capability_listeners.append(listener)

    def health_for_image_source(self, image_source_id):
        return self.health_by_id.get(image_source_id)

    def capabilities(self, wait_s=0):
        return self.capabilities_value

    def notify_config_changed(self, image_source_id):
        self.notified.append(image_source_id)

    def notify_deleted(self, image_source_id):
        self.deleted.append(image_source_id)


class Timer:
    def __init__(self):
        self.pending = []

    def __call__(self, delay, action):
        self.pending.append((delay, action))


class Fetcher:
    def __init__(self, credentials=None, error=None):
        self.credentials = credentials if credentials is not None else {"username": "viewer",
                                                                        "password": PASSWORD}
        self.error = error
        self.calls = []

    def __call__(self, reference):
        self.calls.append(dict(reference))
        if self.error is not None:
            raise self.error
        return dict(self.credentials)


@pytest.fixture
def world(tmp_path, monkeypatch):
    from resources.accessors.image_source_accessor import ImageSourceAccessor
    from stream_ingest import manager as manager_module

    monkeypatch.setattr(constants, "DEFAULT_CAMERA_CONFIG_FILE_PATH", _DEFAULT_CAMERA_CONFIG)
    monkeypatch.setattr(constants, "IMAGE_CAPTURE_DIR", str(tmp_path / "capture"))
    monkeypatch.setattr(dda_user_management_utils, "create_dda_user_directory",
                        lambda folder_path: (os.makedirs(folder_path, exist_ok=True), folder_path)[1])
    monkeypatch.setenv("COMPONENT_WORK_PATH", str(tmp_path))
    engine = create_engine(f"sqlite:///{tmp_path / 'agent.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    store = credentials_module.CredentialStore(directory=str(tmp_path / "stream_credentials"))
    credentials_module.set_credential_store(store)
    stream = FakeStreamManager()
    manager_module.set_stream_ingest_manager(stream)
    clock = Clock()
    timer = Timer()
    fetcher = Fetcher()
    shadow = Shadow()
    accessor = ImageSourceAccessor()

    def make_agent(**overrides):
        arguments = dict(iot_shadow_accessor=shadow, image_source_accessor=accessor,
                         camera_discovery=Discovery(), db_session_factory=factory,
                         state_store=CameraSyncStateStore(str(tmp_path / "state.json")),
                         thing_name="thing", clock=clock, wall_clock=lambda: 1_790_000_000.0,
                         debounce_seconds=0.0, stream_ingest=stream, stream_timer=timer,
                         credential_fetcher=fetcher, credential_store=store)
        arguments.update(overrides)
        return EdgeSyncAgent(**arguments)

    class World:
        pass

    world = World()
    world.__dict__.update(locals())
    yield world
    credentials_module.set_credential_store(None)
    manager_module.set_stream_ingest_manager(None)
    engine.dispose()


def _report(agent, shadow):
    agent.report_inventory()
    agent.pump()
    return shadow.reported[-1]


def _image_sources(world):
    with world.factory() as db:
        return db.query(db_models.ImageSource).all()


def _stream_settings(world, image_source_id):
    with world.factory() as db:
        source = db.get(db_models.ImageSource, image_source_id)
        return dict(source.imageSourceConfiguration.streamSettings or {})


def _create_change(**params):
    base = {"url": "rtsp://10.0.4.21:554/Streaming/Channels/101", "transport": "udp", "latencyMs": 400,
            "credentialRef": REF, "credentialsConfigured": True, "credentialsUpdatedAt": 1_790_000_000_123}
    base.update(params)
    return {"op": "create", "portalChangeId": "chg-1", "name": "Dock 3", "type": "RTSP",
            "params": {key: value for key, value in base.items() if value is not None}}


class TestInventoryWithoutStreams:
    SOURCES = [
        {"imageSourceId": "f1", "name": "Folder", "type": "Folder", "location": "/aws_dda/images"},
        {"imageSourceId": "c1", "name": "Cam", "type": "Camera", "cameraId": "cam-0042",
         "imageSourceConfiguration": {"gain": 3, "exposure": 1000, "device": "/dev/video0"}},
        {"imageSourceId": "i1", "name": "ICam", "type": "ICam"},
        {"imageSourceId": "n1", "name": "CSI", "type": "NvidiaCSI"},
    ]

    def test_build_inventory_is_unchanged_by_the_stream_inputs(self):
        before = build_inventory(self.SOURCES, None)
        after = build_inventory(self.SOURCES, None, stream_health={"f1": {"state": "streaming"}},
                                stream_credentials_configured={"c1": True})
        assert after == before

    def test_the_report_without_stream_ingest_has_exactly_the_old_keys(self, world):
        agent = world.make_agent(stream_ingest=None)
        document = _report(agent, world.shadow)
        assert set(document) == {"schemaVersion", "reportedAt", "cameras", "failures", "discoveryErrors"}

    def test_without_stream_sources_nothing_stream_related_is_consulted(self, world):
        store = Mock(wraps=world.store)
        agent = world.make_agent(credential_store=store)
        world.stream.capabilities_value = None
        document = _report(agent, world.shadow)
        assert document["cameras"] == {}
        assert "deviceCapabilities" not in document
        store.configured.assert_not_called()


class TestStreamReporting:
    def _create(self, world, credentials=True):
        body = {"name": "Dock 3", "type": "RTSP", "location": "rtsp://10.0.4.21:554/s1",
                "streamSettings": {"latencyMs": 400}}
        if credentials:
            body["credentials"] = {"username": "viewer", "password": PASSWORD}
        with world.factory() as db:
            return world.accessor.create_image_source(body, db)["imageSourceId"]

    def test_a_stream_camera_reports_its_settings_and_coarse_health(self, world):
        image_source_id = self._create(world)
        world.stream.health_by_id[image_source_id] = {"state": "reconnecting", "codec": "h264",
                                                      "width": 1920, "height": 1080, "decoder": "software",
                                                      "lastError": {"message": "timeout"}}
        document = _report(world.make_agent(), world.shadow)
        entry = document["cameras"][f"cfg-{image_source_id}"]
        assert entry["type"] == "RTSP" and entry["origin"] == "edge-configured"
        assert entry["params"] == {"url": "rtsp://10.0.4.21:554/s1", "transport": "tcp", "latencyMs": 400,
                                   "decoder": "auto", "maxFrameDimension": 1920, "stallTimeoutS": 10,
                                   "credentialsConfigured": True,
                                   "credentialsUpdatedAt": entry["params"]["credentialsUpdatedAt"]}
        assert entry["capabilities"] == {"stream": {"state": "reconnecting", "codec": "h264", "width": 1920,
                                                    "height": 1080, "decoder": "software"}}
        assert PASSWORD not in json.dumps(document)

    def test_health_changes_re_report_at_most_once_per_camera_per_30_s(self, world):
        image_source_id = self._create(world, credentials=False)
        agent = world.make_agent()
        agent._attach_stream_ingest()
        [listener] = world.stream.health_listeners
        world.stream.health_by_id[image_source_id] = {"state": "streaming", "codec": "h264",
                                                      "width": 1280, "height": 720, "decoder": "software"}
        first = _report(agent, world.shadow)
        key = f"cfg-{image_source_id}"
        assert first["cameras"][key]["capabilities"]["stream"]["state"] == "streaming"
        writes = len(world.shadow.reported)
        # A flap within the window: nothing is published or reported yet.
        world.clock.now += 5
        listener(key, {"state": "reconnecting"})
        listener(key, {"state": "streaming", "codec": "h264", "width": 1280, "height": 720, "decoder": "software"})
        listener(key, {"state": "failed", "codec": "h264", "width": 1280, "height": 720, "decoder": "software"})
        assert agent.pump() is None and len(world.shadow.reported) == writes
        # Another trigger inside the window still reports the published value.
        assert _report(agent, world.shadow)["cameras"][key]["capabilities"]["stream"]["state"] == "streaming"
        assert [delay for delay, _ in world.timer.pending] == [25.0]
        world.clock.now += 25
        world.timer.pending.pop()[1]()
        agent.pump()
        assert world.shadow.reported[-1]["cameras"][key]["capabilities"]["stream"]["state"] == "failed"

    def test_an_idle_camera_reports_nothing_in_use(self, world):
        image_source_id = self._create(world, credentials=False)
        # A stopped session still remembers its last codec; idle reports none.
        world.stream.health_by_id[image_source_id] = {"state": "stopped", "codec": "h264", "width": 1920,
                                                      "height": 1080, "decoder": "hardware"}
        entry = _report(world.make_agent(), world.shadow)["cameras"][f"cfg-{image_source_id}"]
        assert entry["capabilities"] == {"stream": {"state": "idle", "codec": None, "width": None,
                                                    "height": None, "decoder": None}}
        assert entry["params"]["credentialsConfigured"] is False

    def test_an_unchanged_projection_is_not_a_change(self, world):
        image_source_id = self._create(world, credentials=False)
        agent = world.make_agent()
        agent._attach_stream_ingest()
        [listener] = world.stream.health_listeners
        _report(agent, world.shadow)
        world.clock.now += 100
        # Only the coarse fields count: an error message or fps is no change.
        listener(f"cfg-{image_source_id}", {"state": "stopped", "sourceFps": 25})
        assert agent.pump() is None

    def test_capabilities_are_reported_once_the_probe_finished(self, world):
        agent = world.make_agent()
        agent._attach_stream_ingest()
        assert "deviceCapabilities" not in _report(agent, world.shadow)
        world.stream.capabilities_value = {
            "rtsp": True, "rtmp": True, "tls": True, "rtspTls": True,
            "codecs": {"h264": {"hardware": "nvv4l2decoder", "software": "avdec_h264"},
                       "h265": {"hardware": None, "software": "avdec_h265"}},
            "gstreamer": "1.20.3", "pyav": "17.1.0", "ffmpeg": "8.1.1", "probedAtMs": 1790000000000}
        [on_capabilities] = world.stream.capability_listeners
        on_capabilities(world.stream.capabilities_value)
        agent.pump()
        section = world.shadow.reported[-1]["deviceCapabilities"]["streamIngest"]
        assert section == {"rtsp": True, "rtmp": True, "tls": True,
                           "codecs": {"h264": {"hardware": "nvv4l2decoder", "software": "avdec_h264"},
                                      "h265": {"software": "avdec_h265"}},
                           "gstreamer": "1.20.3", "pyav": "17.1.0", "ffmpeg": "8.1.1",
                           "probedAtMs": 1790000000000}
        assert len(json.dumps(section)) <= 1024

    def test_a_probe_still_running_reports_no_section(self):
        assert stream_ingest_section({"probeError": "the capability probe has not finished"}) is None
        assert stream_ingest_section(None) is None
        failed = stream_ingest_section({"probeError": "the capability probe timed out", "codecs": {}})
        assert failed["rtsp"] is False and failed["codecs"] == {}


class TestDebouncer:
    def test_first_change_now_later_ones_at_the_window_end_newest_wins(self):
        clock, timer, published = Clock(0.0), Timer(), []
        debouncer = StreamReportDebouncer(clock=clock, timer=timer, on_publish=lambda: published.append(1))
        assert debouncer.published("a", {"state": "connecting"})["stream"]["state"] == "reconnecting"
        clock.now = 31
        assert debouncer.offer("a", {"state": "streaming", "codec": "h265"}) is True
        clock.now = 40
        assert debouncer.offer("a", {"state": "reconnecting"}) is False
        assert debouncer.offer("a", {"state": "failed"}) is False
        assert debouncer.published("a", None)["stream"]["state"] == "streaming"
        assert [delay for delay, _ in timer.pending] == [21.0]
        clock.now = 61
        assert debouncer.flush() is True and published == [1]
        assert debouncer.published("a", None)["stream"]["state"] == "failed"

    def test_returning_to_the_published_value_cancels_the_pending_change(self):
        clock, timer = Clock(0.0), Timer()
        debouncer = StreamReportDebouncer(clock=clock, timer=timer)
        debouncer.offer("a", {"state": "streaming"})
        clock.now = 5
        debouncer.offer("a", {"state": "reconnecting"})
        debouncer.offer("a", {"state": "streaming"})
        clock.now = 31
        assert debouncer.flush() is False

    def test_cameras_are_independent_and_forgotten_when_deleted(self):
        clock, timer = Clock(0.0), Timer()
        debouncer = StreamReportDebouncer(clock=clock, timer=timer)
        assert debouncer.offer("a", {"state": "streaming"}) is True
        assert debouncer.offer("b", {"state": "streaming"}) is True
        debouncer.forget_except(["b"])
        assert debouncer.offer("a", {"state": "failed"}) is True


class TestApplyStreamChanges:
    def _apply(self, agent, csid, change):
        agent.apply_desired_changes({csid: change})
        agent.pump()

    def test_a_credentialed_create_fetches_creates_and_stores(self, world):
        agent = world.make_agent()
        self._apply(agent, "portal-cam-1", _create_change())
        assert world.fetcher.calls == [REF]
        [source] = _image_sources(world)
        assert (source.type.value, source.location) == ("RTSP", "rtsp://10.0.4.21:554/Streaming/Channels/101")
        settings = _stream_settings(world, source.imageSourceId)
        assert (settings["transport"], settings["latencyMs"], settings["decoder"]) == ("udp", 400, "auto")
        assert settings["credentialRef"] == REF and settings["credentialsUpdatedAt"] == 1_790_000_000_123
        assert world.store.get(source.imageSourceId) == {"username": "viewer", "password": PASSWORD}
        document = world.shadow.reported[-1]
        entry = document["cameras"][f"cfg-{source.imageSourceId}"]
        assert entry["ack"] == "chg-1" and entry["params"]["credentialRef"] == REF
        assert document["cameras"]["portal-cam-1"]["ack"] == "chg-1"
        assert PASSWORD not in json.dumps(document)

    def test_a_fetch_access_denied_fails_the_change_and_changes_nothing(self, world):
        world.fetcher.error = CredentialFetchError("AccessDenied")
        agent = world.make_agent()
        self._apply(agent, "portal-cam-1", _create_change())
        assert _image_sources(world) == []
        document = world.shadow.reported[-1]
        assert document["failures"] == {"portal-cam-1": {
            "reason": "credential retrieval failed: AccessDenied", "portalChangeId": "chg-1"}}
        assert world.shadow.desired[-1] == {"changes": {"portal-cam-1": None}}

    def test_a_failed_store_write_leaves_no_half_created_camera(self, world, monkeypatch):
        def broken_put(image_source_id, credentials):
            raise credentials_module.CredentialStoreError("The Credential_Store could not be written (OSError)")

        monkeypatch.setattr(world.store, "put", broken_put)
        agent = world.make_agent()
        self._apply(agent, "portal-cam-1", _create_change())
        assert _image_sources(world) == []
        reason = world.shadow.reported[-1]["failures"]["portal-cam-1"]["reason"]
        assert "credentials could not be stored" in reason and PASSWORD not in reason

    def test_a_credential_free_create_needs_no_fetch(self, world):
        agent = world.make_agent()
        self._apply(agent, "portal-cam-1", _create_change(credentialRef=None, credentialsConfigured=False,
                                                          credentialsUpdatedAt=None))
        assert world.fetcher.calls == []
        [source] = _image_sources(world)
        assert world.store.get(source.imageSourceId) is None
        assert "credentialRef" not in _stream_settings(world, source.imageSourceId)

    def _created(self, world, agent):
        self._apply(agent, "portal-cam-1", _create_change())
        [source] = _image_sources(world)
        world.fetcher.calls.clear()
        return source.imageSourceId

    def test_an_update_with_the_held_reference_does_not_fetch_again(self, world):
        agent = world.make_agent()
        image_source_id = self._created(world, agent)
        change = _create_change(latencyMs=None)
        change.update(op="update", portalChangeId="chg-2")
        self._apply(agent, f"cfg-{image_source_id}", change)
        assert world.fetcher.calls == []
        settings = _stream_settings(world, image_source_id)
        assert settings["latencyMs"] == 200, "an absent setting returns to its default"
        assert settings["credentialRef"] == REF
        assert world.store.get(image_source_id)["password"] == PASSWORD
        assert world.stream.notified[-1] == image_source_id

    def test_a_new_reference_is_fetched_and_replaces_the_credentials(self, world):
        agent = world.make_agent()
        image_source_id = self._created(world, agent)
        world.fetcher.credentials = {"password": "rotated-AGENT-77d0"}
        change = _create_change(credentialRef=REF2, credentialsUpdatedAt=1_790_000_900_000)
        change.update(op="update", portalChangeId="chg-3")
        self._apply(agent, f"cfg-{image_source_id}", change)
        assert world.fetcher.calls == [REF2]
        assert world.store.get(image_source_id) == {"password": "rotated-AGENT-77d0"}
        settings = _stream_settings(world, image_source_id)
        assert settings["credentialRef"] == REF2 and settings["credentialsUpdatedAt"] == 1_790_000_900_000

    def test_a_clear_removes_the_credentials_and_the_reference(self, world):
        agent = world.make_agent()
        image_source_id = self._created(world, agent)
        change = _create_change(credentialRef=None, credentialsConfigured=False,
                                credentialsUpdatedAt=1_790_000_950_000)
        change.update(op="update", portalChangeId="chg-4")
        self._apply(agent, f"cfg-{image_source_id}", change)
        assert world.store.get(image_source_id) is None
        settings = _stream_settings(world, image_source_id)
        assert "credentialRef" not in settings and settings["credentialsUpdatedAt"] == 1_790_000_950_000
        entry = world.shadow.reported[-1]["cameras"][f"cfg-{image_source_id}"]
        assert entry["params"]["credentialsConfigured"] is False and entry["ack"] == "chg-4"

    def test_a_failed_update_fetch_leaves_the_camera_as_it_was(self, world):
        agent = world.make_agent()
        image_source_id = self._created(world, agent)
        world.fetcher.error = CredentialFetchError("ResourceNotFoundException")
        change = _create_change(credentialRef=REF2, latencyMs=900)
        change.update(op="update", portalChangeId="chg-5")
        self._apply(agent, f"cfg-{image_source_id}", change)
        settings = _stream_settings(world, image_source_id)
        assert settings["latencyMs"] == 400 and settings["credentialRef"] == REF
        assert world.store.get(image_source_id)["password"] == PASSWORD
        failure = world.shadow.reported[-1]["failures"][f"cfg-{image_source_id}"]
        assert failure["reason"] == "credential retrieval failed: ResourceNotFoundException"

    def test_a_portal_delete_removes_the_camera_and_its_credentials(self, world):
        agent = world.make_agent()
        image_source_id = self._created(world, agent)
        self._apply(agent, f"cfg-{image_source_id}", {"op": "delete", "portalChangeId": "chg-6"})
        assert _image_sources(world) == [] and world.store.get(image_source_id) is None
        assert world.stream.deleted == [image_source_id]


class _ClientError(Exception):
    def __init__(self, code, message="denied"):
        super().__init__(f"An error occurred ({code}) when calling the GetSecretValue operation: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class TestCredentialFetch:
    def _client(self, response=None, error=None, calls=None):
        client = Mock()
        if error is not None:
            client.get_secret_value.side_effect = error
        else:
            client.get_secret_value.return_value = response
        return lambda region: (calls.append(region) if calls is not None else None) or client

    def test_the_exact_version_is_read_in_the_secrets_region(self):
        calls = []
        factory = self._client({"SecretString": json.dumps({"username": "viewer", "password": PASSWORD})},
                               calls=calls)
        assert credential_fetch.fetch(REF, client_factory=factory) == {"username": "viewer", "password": PASSWORD}
        assert calls == ["us-east-1"]

    @pytest.mark.parametrize("error, reason", [
        (_ClientError("AccessDeniedException", f"not authorized to read {PASSWORD}"), "AccessDeniedException"),
        (_ClientError("AccessDenied"), "AccessDenied"),
        (_ClientError("ResourceNotFoundException"), "ResourceNotFoundException"),
        (TimeoutError("read timed out"), "TimeoutError"),
    ])
    def test_errors_are_reduced_to_their_code(self, error, reason):
        with pytest.raises(CredentialFetchError) as raised:
            credential_fetch.fetch(REF, client_factory=self._client(error=error))
        assert str(raised.value) == f"credential retrieval failed: {reason}"
        assert PASSWORD not in str(raised.value)

    @pytest.mark.parametrize("secret, reason", [
        (None, "MalformedSecret"), ("not json", "MalformedSecret"), (json.dumps([1]), "MalformedSecret"),
        (json.dumps({"token": "x"}), "MalformedSecret"), (json.dumps({}), "EmptySecret"),
    ])
    def test_a_malformed_secret_is_refused(self, secret, reason):
        with pytest.raises(CredentialFetchError) as raised:
            credential_fetch.fetch(REF, client_factory=self._client({"SecretString": secret}))
        assert raised.value.reason == reason

    @pytest.mark.parametrize("reference", [
        None, {}, {"secretArn": "arn:aws:s3:::bucket/key", "versionId": "v"},
        {"secretArn": REF["secretArn"]}, {"secretArn": REF["secretArn"], "versionId": "../x"},
    ])
    def test_an_invalid_reference_is_refused_before_any_call(self, reference):
        with pytest.raises(CredentialFetchError) as raised:
            credential_fetch.fetch(reference, client_factory=lambda region: pytest.fail("no call"))
        assert raised.value.reason == "InvalidReference"
