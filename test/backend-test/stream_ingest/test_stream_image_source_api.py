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
"""Stream Image_Sources through the REAL image-source routes, accessor and
DAO over a private sqlite database (rtsp-rtmp-stream-cameras task 15.5 —
Requirements 4.1, 4.2, 4.4, 4.7, 4.8, 6.1, 6.2, 18.2).

- Validation messages name the field and never echo a value.
- Credentials never appear in a response or in the database; they live in
  the Credential_Store (mode 0600 in a 0700 directory) and are reported
  only as ``credentialsConfigured``.
- Updates merge settings, keep or replace or clear credentials; deletes
  remove the credentials and stop the session.
- Preview routes a stream through the StreamBroadcaster frame path.
- Existing Image_Source types behave as before.

Scaffolding follows ``static_image_camera/test_image_source_wiring.py``: a
stub ``utils.server_setup``, a router-only FastAPI app, a mocked GStreamer
executor. The stream manager is a recording fake.
"""
import os
import stat
import sys
import types
from unittest.mock import Mock

import pytest

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_DEFAULT_CAMERA_CONFIG = os.path.join(
    _REPO_ROOT, "src", "backend", "utils", "config", "default_camera_configurations.json")
sys.path.insert(0, os.path.join(_HERE, "..", "static_image_camera"))

from camera_manager_support import import_camera_manager  # noqa: E402

import_camera_manager()

import utils as _utils_package  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import dao.sqlite_db.models as db_models  # noqa: E402
from dao.sqlite_db.sqlite_db_operations import Base  # noqa: E402


def _import_image_source_endpoints():
    """``endpoints.image_source`` without the real ``utils.server_setup``
    (whose import starts the device services).

    When nothing imported ``utils.server_setup`` yet, the endpoint module is
    imported against a stub, and then the stub and the endpoint module are
    taken out of ``sys.modules`` again: this test module keeps its own
    reference, and a test that later imports the real app in the same
    session (LocalServerBaseTestCase) gets the real modules. The import
    happens at collection, before the suite conftest patches
    ``pydantic.RootModel``. The fixtures below patch every server_setup
    object the routes use, so the tests run the same either way.
    """
    if "utils.server_setup" in sys.modules or "endpoints.image_source" in sys.modules:
        import endpoints.image_source as module
        return module
    stub = types.ModuleType("utils.server_setup")
    stub.gst_pipeline_executor = Mock(name="unwired gst_pipeline_executor")
    stub.image_source_accessor = Mock(name="unwired image_source_accessor")
    stub.image_src_cfg_accessor = Mock(name="unwired image_src_cfg_accessor")
    sys.modules["utils.server_setup"] = stub
    _utils_package.server_setup = stub
    try:
        import endpoints.image_source as module
    finally:
        if sys.modules.get("utils.server_setup") is stub:
            del sys.modules["utils.server_setup"]
        if getattr(_utils_package, "server_setup", None) is stub:
            del _utils_package.server_setup
    if sys.modules.get("endpoints.image_source") is module:
        del sys.modules["endpoints.image_source"]
    endpoints_package = sys.modules.get("endpoints")
    if getattr(endpoints_package, "image_source", None) is module:
        delattr(endpoints_package, "image_source")
    return module


image_source_endpoints = _import_image_source_endpoints()
from stream_ingest import credentials as credentials_module  # noqa: E402
from stream_ingest import manager as manager_module  # noqa: E402
from utils import constants, dda_user_management_utils  # noqa: E402

PASSWORD = "hunter2-PWD-9c1e"
USERNAME = "viewer-UNAME-7f3a"
STREAM_KEY = "streamkey-USEC-12de"
SECRETS = (PASSWORD, USERNAME, STREAM_KEY)
RTSP_URL = "rtsp://10.0.4.21:554/Streaming/Channels/101"
RTMP_URL = "rtmp://media.local/live/line1"


class _ClientAddressInjector:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope.get("client"):
            scope["client"] = ("testclient", 50000)
        await self.app(scope, receive, send)


class RecordingManager:
    """The StreamIngestManager seam the CRUD path uses, recorded."""

    def __init__(self):
        self.changed, self.deleted = [], []
        self.health_by_id = {}

    def notify_config_changed(self, image_source_id):
        self.changed.append(image_source_id)

    def notify_deleted(self, image_source_id):
        self.deleted.append(image_source_id)

    def health_for_image_source(self, image_source_id):
        return self.health_by_id.get(image_source_id)


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine("sqlite:///{}".format(tmp_path / "stream.db"),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def store(tmp_path):
    test_store = credentials_module.CredentialStore(directory=str(tmp_path / "stream_credentials"))
    credentials_module.set_credential_store(test_store)
    yield test_store
    credentials_module.set_credential_store(None)


@pytest.fixture
def manager():
    fake = RecordingManager()
    manager_module.set_stream_ingest_manager(fake)
    yield fake
    manager_module.set_stream_ingest_manager(None)


@pytest.fixture
def gst_executor(monkeypatch):
    executor = Mock(name="gst_pipeline_executor")
    executor.execute_image_source_pipeline.return_value = {"image": "base64-image-stub"}
    monkeypatch.setattr(image_source_endpoints, "gst_pipeline_executor", executor)
    return executor


@pytest.fixture
def client(monkeypatch, tmp_path, db_session, store, manager, gst_executor):
    monkeypatch.setattr(constants, "DEFAULT_CAMERA_CONFIG_FILE_PATH", _DEFAULT_CAMERA_CONFIG)
    monkeypatch.setattr(constants, "IMAGE_CAPTURE_DIR", str(tmp_path / "image-capture"))
    monkeypatch.setattr(dda_user_management_utils, "create_dda_user_directory",
                        lambda folder_path: (os.makedirs(folder_path, exist_ok=True), folder_path)[1])
    monkeypatch.setattr(image_source_endpoints, "notify_image_source_changed", lambda: None)
    from resources.accessors.image_source_accessor import ImageSourceAccessor

    accessor = ImageSourceAccessor()
    monkeypatch.setattr(image_source_endpoints, "image_source_accessor", accessor)
    monkeypatch.setattr(image_source_endpoints, "image_src_cfg_accessor",
                        accessor.image_source_config_accessor)
    app = FastAPI()
    app.include_router(image_source_endpoints.router)
    app.dependency_overrides[image_source_endpoints.get_db] = lambda: db_session
    return TestClient(_ClientAddressInjector(app))


def assert_no_secret(text, where):
    for secret in SECRETS:
        assert secret not in text, f"a credential leaked into {where}"


def create_rtsp(client, **body):
    request = {"type": "RTSP", "name": "Dock 3", "location": RTSP_URL,
               "streamSettings": {"latencyMs": 400},
               "credentials": {"username": USERNAME, "password": PASSWORD}}
    request.update(body)
    response = client.post("/image-sources", json=request)
    assert response.status_code == 200, response.text
    assert_no_secret(response.text, "the create response")
    return response.json()["imageSourceId"]


def database_text(db_session, tmp_path):
    db_session.commit()
    return (tmp_path / "stream.db").read_bytes().decode("latin-1")


# --------------------------------------------------------------------------
# Create (Requirements 4.1, 5.x on the device, 6.1, 6.2)
# --------------------------------------------------------------------------

class TestCreate:
    def test_rtsp_source_stores_settings_and_credentials_apart(
            self, client, db_session, store, manager, tmp_path):
        image_source_id = create_rtsp(client)

        view = client.get(f"/image-sources/{image_source_id}")
        assert view.status_code == 200, view.text
        assert_no_secret(view.text, "GET /image-sources/{id}")
        record = view.json()
        assert record["type"] == "RTSP"
        assert record["location"] == RTSP_URL
        assert record["credentialsConfigured"] is True
        assert record["streamHealth"] is None
        assert record["cameraStatus"]["status"] == "Disconnected"
        assert record["imageCapturePath"].endswith(image_source_id)
        assert os.path.isdir(record["imageCapturePath"])
        settings = record["imageSourceConfiguration"]["streamSettings"]
        assert {key: settings[key] for key in
                ("transport", "latencyMs", "decoder", "maxFrameDimension", "stallTimeoutS")} == {
            "transport": "tcp", "latencyMs": 400, "decoder": "auto",
            "maxFrameDimension": 1920, "stallTimeoutS": 10}
        assert isinstance(settings["credentialsUpdatedAt"], int)
        assert "credentialRef" not in settings

        # The credentials are in the store only: 0600 in a 0700 directory.
        assert store.get(image_source_id) == {"username": USERNAME, "password": PASSWORD}
        assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(os.path.dirname(store.path)).st_mode) == 0o700
        assert_no_secret(database_text(db_session, tmp_path), "the database")
        assert manager.changed == [image_source_id]

        listed = client.get("/image-sources", params={"type": "RTSP"})
        assert listed.status_code == 200, listed.text
        assert [entry["imageSourceId"] for entry in listed.json()] == [image_source_id]
        assert_no_secret(listed.text, "GET /image-sources")

    def test_rtmp_source_with_a_stream_key(self, client, store):
        response = client.post("/image-sources", json={
            "type": "RTMP", "name": "Line 1", "location": RTMP_URL,
            "credentials": {"urlSecret": STREAM_KEY}})
        assert response.status_code == 200, response.text
        image_source_id = response.json()["imageSourceId"]
        settings = client.get(f"/image-sources/{image_source_id}").json()[
            "imageSourceConfiguration"]["streamSettings"]
        # RTMP has no RTSP-only settings.
        assert "transport" not in settings and "latencyMs" not in settings
        assert store.get(image_source_id) == {"urlSecret": STREAM_KEY}

    def test_a_credential_free_source_stores_nothing(self, client, store):
        response = client.post("/image-sources", json={
            "type": "RTSP", "name": "Open", "location": RTSP_URL})
        assert response.status_code == 200, response.text
        image_source_id = response.json()["imageSourceId"]
        record = client.get(f"/image-sources/{image_source_id}").json()
        assert record["credentialsConfigured"] is False
        assert "credentialsUpdatedAt" not in record["imageSourceConfiguration"]["streamSettings"]
        assert store.get(image_source_id) is None

    @pytest.mark.parametrize("body,field", [
        ({"location": f"rtsp://{USERNAME}:{PASSWORD}@10.0.4.21/live"}, "location"),
        ({"location": "rtmp://media.local/live"}, "location"),
        ({"location": f"{RTSP_URL}?password={PASSWORD}"}, "location"),
        ({"location": None}, "location"),
        ({"streamSettings": {"latencyMs": 9000}}, "streamSettings.latencyMs"),
        ({"streamSettings": {"transport": "http"}}, "streamSettings.transport"),
        ({"streamSettings": {"maxFrameDimension": 100}}, "streamSettings.maxFrameDimension"),
        ({"streamSettings": {"stallTimeoutS": True}}, "streamSettings.stallTimeoutS"),
        ({"streamSettings": {"credentialRef": {"secretArn": "x"}}}, "streamSettings.credentialRef"),
        ({"streamSettings": {"gain": 3}}, "streamSettings.gain"),
        ({"credentials": {"passwd": PASSWORD}}, "credentials"),
        ({"credentials": {"password": 1234}}, "credentials.password"),
        ({"credentials": PASSWORD}, "credentials"),
    ])
    def test_invalid_input_is_rejected_naming_the_field(self, client, store, body, field):
        request = {"type": "RTSP", "name": "Dock 3", "location": RTSP_URL}
        request.update(body)
        response = client.post("/image-sources", json=request)
        assert response.status_code == 400, response.text
        assert f"'{field}'" in response.json()["detail"]
        assert_no_secret(response.text, "a validation error")
        assert store.secret_values() == []

    def test_rtsp_only_settings_are_rejected_for_rtmp(self, client):
        response = client.post("/image-sources", json={
            "type": "RTMP", "name": "Line 1", "location": RTMP_URL,
            "streamSettings": {"transport": "udp"}})
        assert response.status_code == 400
        assert "'streamSettings.transport'" in response.json()["detail"]
        assert "RTSP cameras only" in response.json()["detail"]

    def test_a_failed_credential_write_removes_the_new_source(self, client, store, monkeypatch, db_session):
        def refuse(*_args, **_kwargs):
            raise credentials_module.CredentialStoreError("The Credential_Store could not be written (OSError)")
        monkeypatch.setattr(store, "put", refuse)
        response = client.post("/image-sources", json={
            "type": "RTSP", "name": "Dock 3", "location": RTSP_URL,
            "credentials": {"password": PASSWORD}})
        assert response.status_code == 500
        assert_no_secret(response.text, "the failure response")
        assert db_session.query(db_models.ImageSource).count() == 0


# --------------------------------------------------------------------------
# Update and delete (Requirements 4.2, 4.7, 8.11 seam)
# --------------------------------------------------------------------------

class TestUpdateAndDelete:
    def test_settings_merge_and_blank_credentials_keep_what_is_stored(self, client, store, manager):
        image_source_id = create_rtsp(client)
        before = store.get(image_source_id)
        response = client.patch(f"/image-sources/{image_source_id}", json={
            "streamSettings": {"decoder": "software"}, "credentials": {"password": ""}})
        assert response.status_code == 200, response.text
        settings = client.get(f"/image-sources/{image_source_id}").json()[
            "imageSourceConfiguration"]["streamSettings"]
        assert settings["latencyMs"] == 400 and settings["decoder"] == "software"
        assert store.get(image_source_id) == before
        assert manager.changed == [image_source_id, image_source_id]

    def test_new_credentials_replace_the_stored_ones(self, client, store):
        image_source_id = create_rtsp(client)
        response = client.patch(f"/image-sources/{image_source_id}",
                                json={"credentials": {"password": "n3w-pass-7d"}})
        assert response.status_code == 200, response.text
        assert store.get(image_source_id) == {"password": "n3w-pass-7d"}

    def test_clearing_removes_the_credentials(self, client, store):
        image_source_id = create_rtsp(client)
        response = client.patch(f"/image-sources/{image_source_id}", json={"clearCredentials": True})
        assert response.status_code == 200, response.text
        assert store.get(image_source_id) is None
        assert client.get(f"/image-sources/{image_source_id}").json()["credentialsConfigured"] is False

    @pytest.mark.parametrize("body,field", [
        ({"location": f"rtsp://{USERNAME}:{PASSWORD}@h/x"}, "location"),
        ({"streamSettings": {"latencyMs": -1}}, "streamSettings.latencyMs"),
        ({"credentials": {"password": PASSWORD}, "clearCredentials": True}, "credentials"),
    ])
    def test_invalid_updates_name_the_field_and_change_nothing(self, client, store, body, field):
        image_source_id = create_rtsp(client)
        before = client.get(f"/image-sources/{image_source_id}").json()
        response = client.patch(f"/image-sources/{image_source_id}", json=body)
        assert response.status_code == 400, response.text
        assert f"'{field}'" in response.json()["detail"]
        assert_no_secret(response.text, "an update validation error")
        after = client.get(f"/image-sources/{image_source_id}").json()
        assert after["location"] == before["location"]
        assert after["imageSourceConfigId"] == before["imageSourceConfigId"]
        assert store.get(image_source_id) == {"username": USERNAME, "password": PASSWORD}

    def test_delete_removes_the_credentials_and_stops_the_session(self, client, store, manager):
        image_source_id = create_rtsp(client)
        response = client.delete(f"/image-sources/{image_source_id}")
        assert response.status_code == 200, response.text
        assert store.get(image_source_id) is None
        assert manager.deleted == [image_source_id]


# --------------------------------------------------------------------------
# Health, preview and capture (Requirements 4.4, 16.1)
# --------------------------------------------------------------------------

class TestHealthAndFrames:
    def test_camera_status_follows_stream_health(self, client, manager):
        image_source_id = create_rtsp(client)
        manager.health_by_id[image_source_id] = {
            "cameraKey": f"cfg-{image_source_id}", "state": "streaming",
            "lastFrameAtMs": 1_790_000_000_123}
        record = client.get(f"/image-sources/{image_source_id}").json()
        assert record["cameraStatus"]["status"] == "Connected"
        assert record["streamHealth"]["state"] == "streaming"
        manager.health_by_id[image_source_id] = {
            "state": "failed",
            "lastError": {"category": "authentication_failed",
                          "message": "401 from 10.0.4.21", "atMs": 1_790_000_000_000}}
        status = client.get(f"/image-sources/{image_source_id}").json()["cameraStatus"]
        assert status["status"] == "Disconnected"
        assert status["error"] == "401 from 10.0.4.21"

    def test_preview_uses_the_broadcaster_frame(self, client, gst_executor, monkeypatch):
        import utils.streaming.broadcaster as broadcaster_module
        frames = []

        class FakeBroadcaster:
            def get_inference_frame(self, camera_id, config):
                frames.append((camera_id, config))
                return {"data": b"\x00" * 12, "width": 2, "height": 2}

        monkeypatch.setattr(broadcaster_module, "get_broadcaster", lambda: FakeBroadcaster())
        image_source_id = create_rtsp(client)
        response = client.post(f"/image-sources/{image_source_id}/preview", json={})
        assert response.status_code == 200, response.text
        assert frames == [(f"cfg-{image_source_id}", {"type": "RTSP", "imageSourceId": image_source_id})]
        call = gst_executor.execute_image_source_pipeline.call_args
        assert call.kwargs["is_preview"] is True
        assert call.kwargs["frame_data"]["width"] == 2

        capture = client.post(f"/image-sources/{image_source_id}/capture", json={"filePrefix": "x"})
        assert capture.status_code == 200, capture.text
        assert gst_executor.execute_image_source_pipeline.call_args.kwargs["is_preview"] is False

    def test_preview_without_a_frame_is_503_naming_the_state(self, client, manager, monkeypatch):
        import utils.streaming.broadcaster as broadcaster_module

        class EmptyBroadcaster:
            def get_inference_frame(self, camera_id, config):
                return None

        monkeypatch.setattr(broadcaster_module, "get_broadcaster", lambda: EmptyBroadcaster())
        image_source_id = create_rtsp(client)
        manager.health_by_id[image_source_id] = {"state": "reconnecting",
                                                 "lastError": {"message": "timeout"}}
        response = client.post(f"/image-sources/{image_source_id}/preview", json={})
        assert response.status_code == 503, response.text
        assert "state reconnecting: timeout" in response.json()["detail"]


# --------------------------------------------------------------------------
# Existing types (Requirement 18.2)
# --------------------------------------------------------------------------

class TestExistingTypes:
    def test_a_folder_source_is_unchanged(self, client, tmp_path):
        folder = str(tmp_path / "folder")
        response = client.post("/image-sources", json={"type": "Folder", "name": "F", "location": folder})
        assert response.status_code == 200, response.text
        record = client.get(f"/image-sources/{response.json()['imageSourceId']}").json()
        assert "credentialsConfigured" not in record and "streamHealth" not in record
        assert record["cameraStatus"] is None

    def test_stream_fields_are_refused_for_other_types(self, client, tmp_path):
        folder = str(tmp_path / "folder")
        response = client.post("/image-sources", json={
            "type": "Folder", "name": "F", "location": folder, "streamSettings": {"latencyMs": 1}})
        assert response.status_code == 400
        assert "streamSettings" in response.json()["detail"]
        created = client.post("/image-sources", json={"type": "Folder", "name": "F", "location": folder})
        image_source_id = created.json()["imageSourceId"]
        response = client.patch(f"/image-sources/{image_source_id}",
                                json={"credentials": {"password": PASSWORD}})
        assert response.status_code == 400
        assert_no_secret(response.text, "a Folder update error")
