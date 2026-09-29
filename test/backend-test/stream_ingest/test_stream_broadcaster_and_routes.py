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
"""The StreamBroadcaster over stream sessions, the connection test, and the
stream routes (rtsp-rtmp-stream-cameras tasks 17.1-17.3 — Requirements 4.3,
8.1, 8.8, 16.1, 18.5).

- Viewers of one stream camera share one Stream_Session and one lease.
- A viewer disconnecting never stops a session a workflow leases.
- Every existing image-source type selects exactly the backend it did.
- The connection test reports success with a frame, a failure's category
  as soon as it happens, a timeout, or the session limit, and restarts a
  session that is only waiting to retry.
- ``test-connection``, ``stream-health`` and ``/streams/capabilities``.

Real ``StreamBroadcaster``, ``StreamIngestManager`` and ``StreamSession``;
the worker process is the protocol-speaking fake.
"""
import functools
import os
import sys
import threading
import time
import types
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402

from stream_fakes import CAPABILITIES, FakeReader, RecordingTimer, Spawner, rtsp_source  # noqa: E402
# At collection, before the suite conftest patches pydantic.RootModel: the
# endpoint modules' response models must be built with the real one.
import test_stream_image_source_api as api_tests  # noqa: E402
from endpoints import streams as streams_endpoints  # noqa: E402
from stream_ingest import health  # noqa: E402
from stream_ingest import manager as manager_module  # noqa: E402
from stream_ingest.connection_test import run_connection_test  # noqa: E402
from stream_ingest.manager import SessionLimitError, StreamIngestManager  # noqa: E402
from stream_ingest.session import StreamSession  # noqa: E402

KEY = "cfg-cam1"
CONFIG = {"type": "RTSP", "imageSourceId": "cam1"}


class StaticCapabilities:
    def start(self):
        pass

    def get(self, wait_s=0):
        return dict(CAPABILITIES)

    def peek(self):
        return dict(CAPABILITIES)


@pytest.fixture
def ingest():
    """A real manager whose sessions start blocking fake workers."""
    spawner = Spawner(blocking=True, streaming=True)
    factory = functools.partial(StreamSession, spawn=spawner, reader_factory=FakeReader, timer=RecordingTimer())
    manager = StreamIngestManager(session_factory=factory, max_sessions=lambda: 4,
                                  capabilities=StaticCapabilities(),
                                  configured_source=lambda image_source_id: rtsp_source(),
                                  idle_grace_s=0.2, supervise=False)
    manager_module.set_stream_ingest_manager(manager)
    stop = threading.Event()

    def produce():
        while not stop.wait(0.05):
            for worker in list(spawner.workers):
                if not worker.killed and not worker.exited and not worker.stopped:
                    worker.produce()

    producer = threading.Thread(target=produce, daemon=True)
    producer.start()
    yield types.SimpleNamespace(manager=manager, spawner=spawner)
    stop.set()
    manager.shutdown()
    manager_module.set_stream_ingest_manager(None)


def _wait_for(predicate, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def broadcaster():
    from utils.streaming.broadcaster import StreamBroadcaster
    from utils.streaming.models import StreamConfig

    instance = StreamBroadcaster(stream_config=StreamConfig(frame_timeout_ms=1000, first_frame_timeout_s=5))
    yield instance
    for camera_id in list(instance._sessions):
        with instance._lock:
            instance._stop_session(camera_id)


class TestBroadcasterSharing:
    def test_viewers_of_one_camera_share_one_session_and_one_lease(self, ingest, broadcaster):
        from utils.streaming.models import FrameStatus

        first = broadcaster.subscribe(KEY, dict(CONFIG))
        second = broadcaster.subscribe(KEY, dict(CONFIG))
        assert first.accepted and second.accepted and second.viewer_count == 2
        assert ingest.manager.session_keys() == [KEY]
        assert ingest.manager.lease_count(KEY) == 1
        assert len(ingest.spawner.workers) == 1
        assert _wait_for(lambda: broadcaster.get_frame(KEY, first.viewer_id).status == FrameStatus.OK)
        result = broadcaster.get_frame(KEY, second.viewer_id)
        assert result.status == FrameStatus.OK and result.frame.width == 4 and result.frame.height == 2
        assert len(result.frame.data) == 4 * 2 * 3

    def test_a_viewer_disconnect_never_stops_a_leased_session(self, ingest, broadcaster):
        workflow_lease = ingest.manager.acquire_lease(KEY, "workflow")
        session = ingest.manager.session(KEY)
        viewer = broadcaster.subscribe(KEY, dict(CONFIG))
        assert viewer.accepted and ingest.manager.lease_count(KEY) == 2
        broadcaster.unsubscribe(KEY, viewer.viewer_id)
        assert broadcaster.viewer_count(KEY) == 0
        assert ingest.manager.lease_count(KEY) == 1
        time.sleep(0.3)
        ingest.manager.tick()
        assert ingest.manager.session(KEY) is session and session.state != health.STOPPED
        worker = ingest.spawner.workers[0]
        assert not worker.killed and not worker.stopped
        # The workflow's frames keep coming.
        assert ingest.manager.latest_frame(KEY, wait_ms=2000) is not None
        ingest.manager.release_lease(workflow_lease)
        time.sleep(0.3)
        ingest.manager.tick()
        assert ingest.manager.session(KEY) is None and worker.stopped

    def test_inference_frames_reuse_the_session_without_a_viewer(self, ingest, broadcaster):
        ingest.manager.acquire_lease(KEY, "workflow")
        assert _wait_for(lambda: ingest.manager.latest_frame(KEY) is not None)
        frame = broadcaster.get_inference_frame(KEY, dict(CONFIG))
        assert frame is not None and (frame["width"], frame["height"]) == (4, 2)
        assert len(ingest.spawner.workers) == 1

    def test_the_session_limit_makes_the_camera_unavailable(self, ingest, broadcaster):
        ingest.manager._max_sessions = lambda: 1
        ingest.manager.acquire_lease("cfg-other", "workflow")
        result = broadcaster.subscribe(KEY, dict(CONFIG))
        assert not result.accepted and result.reason == "camera_unavailable"
        assert ingest.manager.session_keys() == ["cfg-other"]


class TestBackendSelection:
    @pytest.mark.parametrize("config", [
        None, {}, {"type": None}, {"type": "Camera"}, {"type": "Folder"}, {"type": "unknown"},
        {"type": "NvidiaCSI"}, {"type": "ICam"}, {"type": "Camera", "imageSourceConfiguration": {"gain": 3}},
    ])
    def test_existing_types_select_exactly_the_backend_they_did(self, config):
        from utils.streaming.backends import AravisBackend, GStreamerBackend
        from utils.streaming.broadcaster import StreamBroadcaster

        backend = StreamBroadcaster()._default_backend_factory("cam", config)
        source_type = (config or {}).get("type")
        expected = GStreamerBackend if source_type in ("NvidiaCSI", "ICam") else AravisBackend
        assert type(backend) is expected

    def test_enum_members_of_existing_types_are_unchanged(self):
        from model.image_source import ImageSourceType
        from utils.streaming.backends import AravisBackend
        from utils.streaming.broadcaster import StreamBroadcaster

        # A (non-str) enum member never matched the string tuple before and
        # still does not.
        for member in (ImageSourceType.CAMERA, ImageSourceType.NVIDIA_CSI, ImageSourceType.ICAM):
            assert type(StreamBroadcaster()._default_backend_factory("cam", {"type": member})) is AravisBackend

    @pytest.mark.parametrize("source_type", ["RTSP", "RTMP"])
    def test_stream_types_select_the_stream_backend(self, source_type):
        from model.image_source import ImageSourceType
        from utils.streaming.backends import StreamIngestBackend
        from utils.streaming.broadcaster import StreamBroadcaster

        for value in (source_type, ImageSourceType(source_type)):
            backend = StreamBroadcaster()._default_backend_factory("cfg-x", {"type": value, "imageSourceId": "x"})
            assert type(backend) is StreamIngestBackend
            assert backend.camera_key() == "cfg-x"

    def test_the_stream_backend_leases_grabs_newer_frames_and_releases(self, ingest):
        from utils.streaming.backends import StreamIngestBackend

        backend = StreamIngestBackend(KEY, image_source=dict(CONFIG), manager=ingest.manager)
        assert backend.grab(10) is None  # not open
        backend.open()
        backend.start_stream()
        assert ingest.manager.lease_count(KEY) == 1
        first = backend.grab(2000)
        second = backend.grab(2000)
        assert first is not None and second is not None
        assert first.data != second.data or first is not second
        assert backend.apply_features({"gain": 3}) == {}
        backend.stop_stream()
        backend.close()
        backend.close()
        assert ingest.manager.lease_count(KEY) == 0

    def test_the_stream_backend_never_serves_a_frame_cached_before_an_outage(self, ingest):
        from utils.streaming.backends import StreamIngestBackend

        lease = ingest.manager.acquire_lease(KEY, "workflow")
        assert ingest.manager.latest_frame(KEY, wait_ms=2000) is not None
        ingest.spawner.streaming = False
        ingest.spawner.workers[-1].error(health.NETWORK_ERROR, "connection lost")
        backend = StreamIngestBackend(KEY, image_source=dict(CONFIG), manager=ingest.manager)
        backend.open()
        try:
            # A preview or capture while the camera reconnects gets no frame
            # (the route answers 503 with the state), never the old one.
            assert backend.grab(300) is None
        finally:
            backend.close()
            ingest.manager.release_lease(lease)

    def test_a_key_without_an_image_source_id_is_refused(self):
        from utils.streaming.backends import StreamIngestBackend

        with pytest.raises(ValueError):
            StreamIngestBackend("camera-7", image_source={"type": "RTSP"}).camera_key()


class TestConnectionTest:
    def test_success_returns_the_first_frame(self, ingest):
        result = run_connection_test(ingest.manager, KEY, budget_s=5)
        assert result.ok and result.category is None and result.frame is not None
        assert result.health["state"] == health.STREAMING
        assert ingest.manager.lease_count(KEY) == 0, "the test's lease is released"

    def test_a_failure_after_the_start_is_reported_at_once(self, ingest):
        ingest.spawner.streaming = False
        timer = threading.Timer(0.2, lambda: ingest.spawner.workers[-1].error(
            health.AUTHENTICATION_FAILED, "401 Unauthorized"))
        timer.start()
        started = time.monotonic()
        result = run_connection_test(ingest.manager, KEY, budget_s=10)
        assert time.monotonic() - started < 3
        assert (result.ok, result.category) == (False, health.AUTHENTICATION_FAILED)
        assert "401" in result.message

    def test_an_earlier_failure_is_not_this_tests_result(self, ingest):
        ingest.spawner.streaming = False
        lease = ingest.manager.acquire_lease(KEY, "workflow")
        ingest.spawner.workers[-1].error(health.NETWORK_ERROR, "refused")
        time.sleep(0.05)
        ingest.spawner.streaming = True
        result = run_connection_test(ingest.manager, KEY, budget_s=5)
        assert result.ok, result
        ingest.manager.release_lease(lease)

    def test_a_session_waiting_to_retry_is_restarted_for_the_test(self, ingest):
        ingest.spawner.streaming = False
        lease = ingest.manager.acquire_lease(KEY, "workflow")
        ingest.spawner.workers[-1].error(health.AUTHENTICATION_FAILED, "401")
        assert ingest.manager.health(KEY)["nextAttemptInS"] > 200
        ingest.spawner.streaming = True
        result = run_connection_test(ingest.manager, KEY, budget_s=5)
        assert result.ok and len(ingest.spawner.workers) == 2
        ingest.manager.release_lease(lease)

    def test_no_frame_in_time_is_a_timeout(self, ingest):
        ingest.spawner.streaming = False
        result = run_connection_test(ingest.manager, KEY, budget_s=0.5)
        assert (result.ok, result.category) == (False, health.TIMEOUT)

    def test_a_frame_cached_before_a_configuration_change_is_not_a_success(self, ingest):
        # Found on hardware (task 25): a wrong password PATCHed while the
        # session is still leased was reported as "Connected" from the
        # frame the previous credentials had produced.
        lease = ingest.manager.acquire_lease(KEY, "workflow")
        assert run_connection_test(ingest.manager, KEY, budget_s=5).ok
        ingest.spawner.streaming = False
        ingest.manager.notify_config_changed("cam1")
        timer = threading.Timer(0.2, lambda: ingest.spawner.workers[-1].error(
            health.AUTHENTICATION_FAILED, "401 Unauthorized"))
        timer.start()
        result = run_connection_test(ingest.manager, KEY, budget_s=5)
        assert (result.ok, result.category) == (False, health.AUTHENTICATION_FAILED), result
        assert result.frame is None
        ingest.manager.release_lease(lease)

    def test_a_test_of_a_relay_path_that_went_away_keeps_the_transient_retry(self, ingest):
        # Requirement 8.6's exception: the path streamed, so a 404 during
        # the test is reported as not_found but retried on the ladder.
        lease = ingest.manager.acquire_lease(KEY, "workflow")
        assert ingest.manager.latest_frame(KEY, wait_ms=2000) is not None
        ingest.spawner.streaming = False
        ingest.spawner.workers[-1].error(health.NOT_FOUND, "Not Found (404)")
        session = ingest.manager.session(KEY)
        assert session.state == health.RECONNECTING
        session._next_attempt_at = session._clock() + 30  # waiting: the test restarts it
        time.sleep(0.05)  # the earlier failure is not this test's result
        timer = threading.Timer(0.3, lambda: ingest.spawner.workers[-1].error(health.NOT_FOUND, "Not Found (404)"))
        timer.start()
        result = run_connection_test(ingest.manager, KEY, budget_s=5)
        assert (result.ok, result.category) == (False, health.NOT_FOUND), result
        assert session.state == health.RECONNECTING, "the test's restart kept the streamed history"
        ingest.manager.release_lease(lease)

    def test_a_frame_cached_before_an_outage_is_not_a_success(self, ingest):
        lease = ingest.manager.acquire_lease(KEY, "workflow")
        assert ingest.manager.latest_frame(KEY, wait_ms=2000) is not None
        ingest.spawner.streaming = False
        ingest.spawner.workers[-1].error(health.NETWORK_ERROR, "connection lost")
        # After the 1 s backoff the session is reconnecting with a new
        # worker that has not streamed yet, so the test does not restart it.
        session = ingest.manager.session(KEY)
        assert _wait_for(lambda: (session.tick(), len(ingest.spawner.workers) == 2)[1], timeout_s=3)
        assert session.state == health.RECONNECTING
        result = run_connection_test(ingest.manager, KEY, budget_s=1)
        assert not result.ok and result.frame is None, result
        assert len(ingest.spawner.workers) == 2, "the reconnecting session was left alone"
        ingest.manager.release_lease(lease)

    def test_the_session_limit_is_reported(self, ingest):
        ingest.manager._max_sessions = lambda: 1
        ingest.manager.acquire_lease("cfg-other", "workflow")
        result = run_connection_test(ingest.manager, KEY, budget_s=1)
        assert (result.ok, result.category) == (False, health.SESSION_LIMIT)
        assert "1" in result.message


# -- routes ------------------------------------------------------------------

@pytest.fixture
def routes(ingest, tmp_path, monkeypatch):
    """The image-source router over a private database, plus the streams
    router, as in test_stream_image_source_api.py."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from dao.sqlite_db.sqlite_db_operations import Base
    from resources.accessors.image_source_accessor import ImageSourceAccessor
    from stream_ingest import credentials as credentials_module
    from utils import constants, dda_user_management_utils

    endpoints = api_tests.image_source_endpoints
    engine = create_engine(f"sqlite:///{tmp_path / 'routes.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    store = credentials_module.CredentialStore(directory=str(tmp_path / "stream_credentials"))
    credentials_module.set_credential_store(store)
    monkeypatch.setattr(constants, "DEFAULT_CAMERA_CONFIG_FILE_PATH", api_tests._DEFAULT_CAMERA_CONFIG)
    monkeypatch.setattr(constants, "IMAGE_CAPTURE_DIR", str(tmp_path / "capture"))
    monkeypatch.setattr(dda_user_management_utils, "create_dda_user_directory",
                        lambda folder_path: (os.makedirs(folder_path, exist_ok=True), folder_path)[1])
    monkeypatch.setattr(endpoints, "notify_image_source_changed", lambda: None)
    accessor = ImageSourceAccessor()
    monkeypatch.setattr(endpoints, "image_source_accessor", accessor)
    monkeypatch.setattr(endpoints, "image_src_cfg_accessor", accessor.image_source_config_accessor)
    executor = Mock(name="gst_pipeline_executor")
    executor.execute_image_source_pipeline.return_value = {"image": "base64-preview"}
    monkeypatch.setattr(endpoints, "gst_pipeline_executor", executor)

    app = FastAPI()
    app.include_router(endpoints.router)
    app.include_router(streams_endpoints.router)
    app.dependency_overrides[endpoints.get_db] = lambda: db
    client = TestClient(api_tests._ClientAddressInjector(app))
    ingest.manager._configured_source = lambda image_source_id: rtsp_source(
        credentials=store.get(image_source_id))
    yield types.SimpleNamespace(client=client, executor=executor, store=store, ingest=ingest)
    db.close()
    engine.dispose()
    credentials_module.set_credential_store(None)


def _create(client, **body):
    request = {"type": "RTSP", "name": "Dock 3", "location": "rtsp://10.0.4.21:554/s1",
               "credentials": {"username": "viewer", "password": "pw-ROUTE-51e0"}}
    request.update(body)
    response = client.post("/image-sources", json=request)
    assert response.status_code == 200, response.text
    return response.json()["imageSourceId"]


class TestRoutes:
    def test_a_successful_connection_test_returns_a_preview(self, routes):
        image_source_id = _create(routes.client)
        response = routes.client.post(f"/image-sources/{image_source_id}/test-connection")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True and body["image"] == "base64-preview"
        assert body["streamHealth"]["state"] == health.STREAMING
        frame_data = routes.executor.execute_image_source_pipeline.call_args.kwargs["frame_data"]
        assert (frame_data["width"], frame_data["height"]) == (4, 2)
        assert "pw-ROUTE-51e0" not in response.text
        assert routes.ingest.spawner.workers[-1].config["credentials"]["password"] == "pw-ROUTE-51e0"

    def test_a_failed_connection_test_reports_the_category(self, routes):
        image_source_id = _create(routes.client)
        routes.ingest.spawner.streaming = False
        threading.Timer(0.2, lambda: routes.ingest.spawner.workers[-1].error(
            health.AUTHENTICATION_FAILED, "401 Unauthorized for pw-ROUTE-51e0")).start()
        response = routes.client.post(f"/image-sources/{image_source_id}/test-connection")
        body = response.json()
        assert response.status_code == 200
        assert (body["ok"], body["category"]) == (False, health.AUTHENTICATION_FAILED)
        assert "pw-ROUTE-51e0" not in response.text
        assert body["image"] is None

    def test_a_preview_failure_still_reports_the_connection(self, routes):
        image_source_id = _create(routes.client)
        routes.executor.execute_image_source_pipeline.side_effect = RuntimeError("bad pipeline")
        body = routes.client.post(f"/image-sources/{image_source_id}/test-connection").json()
        assert body["ok"] is True and body["image"] is None and body["imageError"]

    def test_stream_health_of_an_idle_and_a_running_camera(self, routes):
        image_source_id = _create(routes.client)
        idle = routes.client.get(f"/image-sources/{image_source_id}/stream-health").json()
        assert idle["state"] == health.STOPPED and idle["credentialsConfigured"] is True
        routes.ingest.manager.acquire_lease(f"cfg-{image_source_id}", "workflow")
        assert _wait_for(lambda: routes.client.get(
            f"/image-sources/{image_source_id}/stream-health").json()["state"] == health.STREAMING)
        running = routes.client.get(f"/image-sources/{image_source_id}/stream-health").json()
        assert running["leases"] == 1 and running["cameraKey"] == f"cfg-{image_source_id}"

    def test_the_stream_routes_refuse_other_types_and_unknown_ids(self, routes, tmp_path):
        folder = tmp_path / "folder"
        folder.mkdir()
        response = routes.client.post("/image-sources", json={"type": "Folder", "name": "f",
                                                              "location": str(folder)})
        folder_id = response.json()["imageSourceId"]
        for path in (f"/image-sources/{folder_id}/stream-health",):
            assert routes.client.get(path).status_code == 400
        assert routes.client.post(f"/image-sources/{folder_id}/test-connection").status_code == 400
        assert routes.client.get("/image-sources/missing/stream-health").status_code == 404
        assert routes.client.post("/image-sources/missing/test-connection").status_code == 404

    def test_capabilities_are_served_and_a_running_probe_is_a_503(self, routes):
        response = routes.client.get("/streams/capabilities")
        assert response.status_code == 200 and response.json()["codecs"]["h264"]["software"] == "avdec_h264"

        class Pending(StaticCapabilities):
            def get(self, wait_s=0):
                return {"probeError": "the capability probe has not finished"}

        routes.ingest.manager._capabilities = Pending()
        assert routes.client.get("/streams/capabilities").status_code == 503
