# Copyright 2025 Amazon Web Services, Inc.
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
"""WorkflowExecutor stream camera feed tests.

Feature: rtsp-rtmp-stream-cameras (Requirements 8.10, 10.4, 10.5, 10.6,
10.7). A fake StreamIngestManager and a fake pipeline manager exercise the
executor's stream feed without GStreamer or a Stream_Worker:

- a fresh Latest_Frame is taken under the run's own lease and pushed
  through ``run_pipeline(launch_string, frame_data)`` as packed RGB, and
  the run metadata gains ``stream.<nodeId>`` and ``frame``;
- no fresh frame fails the run on the stream node, naming the camera and
  its Stream_Health state;
- an unbound node reads the configured camera whose normalized URL equals
  its ``url``, a bound node its bound camera, and otherwise an anonymous,
  credential-less session;
- a continuous run asks for the frame its tick chose;
- a lease refused at the session limit fails the run on the node;
- the single-feed contract rejects a stream node beside another feed.
"""
import json
import time
import uuid
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import (  # sets COMPONENT_WORK_PATH first
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from dao.sqlite_db.models import ImageSource as ImageSourceRow
from model.image_source import ImageSourceType
from stream_ingest.manager import Lease, SessionLimitError, camera_key_for_url
from stream_ingest.session import StreamFrame
from stream_ingest.sources import StreamSource
from workflow_engine import gst_plugins
from workflow_engine.camera_binding import STATUS_RESOLVED, ResolutionResult
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)
from workflow_engine.stream_feed import StreamFeedError, plan_stream_feeds

REGISTRATION_ID = "wf-1:3"
CAMERA_URL = "rtsp://192.168.1.64:554/Streaming/Channels/101"
NORMALIZED_URL = "rtsp://192.168.1.64/Streaming/Channels/101"


def make_stream_document(node_id="cam", protocol="rtsp", url=CAMERA_URL, extra_points=(), **parameters):
    """A compiled_pipeline.json with one stream camera source: the compiled
    appsrc chain plus the packager's streamBinding point."""
    rendered = {"url": url, "processing_mode": "on_trigger", "frames_per_second": 1.0,
                "max_frame_age_ms": 1500, "keep_recent_runs": 20, "keep_notable_runs": 200}
    rendered.update(parameters)
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [{
            "name": "s0",
            "elements": [
                {"nodeId": node_id, "factory": "appsrc", "args": {"name": "appsrc_{0}".format(node_id)}},
                {"nodeId": node_id, "factory": "videoconvert", "args": {}},
                {"nodeId": None, "factory": "fakesink", "args": {}},
            ],
        }],
        "bindingPoints": [{
            "nodeId": node_id,
            "nodeType": "rtsp_camera_source" if protocol == "rtsp" else "rtmp_stream_source",
            "parameters": rendered,
            "slots": [],
            "streamBinding": True,
            "streamProtocol": protocol,
        }] + list(extra_points),
        "executorBindings": [],
        "pluginDependencies": [],
    }


def make_frame(seq=7, width=4, height=2):
    return StreamFrame(seq=seq, data=b"\x01" * (width * height * 3), width=width, height=height,
                       acquired_at_ms=1_790_000_000_123)


class FakeStreamManager:
    """The StreamIngestManager surface the executor uses, recording every
    call in order."""

    def __init__(self, frame=None, health=None, refuse=None):
        self.frame = frame
        self.health_document = health if health is not None else {"state": "streaming", "lastError": None}
        self.refuse = refuse
        self.calls = []
        self.held = {}

    def acquire_lease(self, camera_key, holder, source=None):
        self.calls.append(("acquire", camera_key, holder, source))
        if self.refuse is not None:
            raise self.refuse
        lease = Lease(uuid.uuid4().hex, camera_key, holder)
        self.held[lease.lease_id] = lease
        return lease

    def release_lease(self, lease):
        self.calls.append(("release", lease.camera_key, lease.holder))
        self.held.pop(lease.lease_id, None)

    def latest_frame(self, camera_key, after_seq=0, max_age_ms=None, wait_ms=0):
        self.calls.append(("latest", camera_key, after_seq, max_age_ms, wait_ms))
        return self.frame

    def health(self, camera_key):
        self.calls.append(("health", camera_key))
        return dict(self.health_document)


class FakePipelineManager:
    def __init__(self, tag_values=None):
        self.tag_values = tag_values or {"is_anomalous": False}
        self.calls = []

    def run_pipeline(self, pipeline_str, *args, **kwargs):
        self.calls.append((pipeline_str, args, kwargs))
        return dict(self.tag_values)


class FakeCameraManager:
    def __init__(self):
        self.calls = []

    def __call__(self, camera_id, config):
        self.calls.append((camera_id, dict(config)))
        return {"data": b"\x00" * 8, "width": 4, "height": 2}


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture(autouse=True)
def no_registry_scan():
    """Never import gi in these tests."""
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


def seed_run(session_factory, artifact_path, trigger_context=None):
    session = session_factory()
    try:
        session.add(WorkflowRegistration(
            id=REGISTRATION_ID, workflow_id="wf-1", version="3", arch=DEVICE_ARCH,
            artifact_path=str(artifact_path), status="registered", registered_at=int(time.time())))
        session.add(WorkflowExecution(
            id="exec-1", registration_id=REGISTRATION_ID, started_at=int(time.time()),
            status=EXECUTION_STATUS_PENDING,
            trigger_context_json=json.dumps(trigger_context) if trigger_context is not None else None))
        session.commit()
    finally:
        session.close()
    return "exec-1"


def add_stream_camera(session_factory, image_source_id, location, source_type=ImageSourceType.RTSP):
    session = session_factory()
    try:
        session.add(ImageSourceRow(imageSourceId=image_source_id, name="Dock " + image_source_id,
                                   type=source_type, location=location))
        session.commit()
    finally:
        session.close()


def get_execution(session_factory, execution_id="exec-1"):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, execution_id)
    finally:
        session.close()


def run(session_factory, tmp_path, document, stream_manager, provider=None, trigger_context=None,
        grabber=None, pipeline=None):
    artifact_path = write_artifact_set(tmp_path, compiled=document)
    execution_id = seed_run(session_factory, artifact_path, trigger_context)
    pipeline = pipeline or FakePipelineManager()
    observed = []
    executor = WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: pipeline,
        binding_resolution_provider=provider,
        frame_grabber=grabber or FakeCameraManager(),
        stream_ingest_manager=stream_manager,
        post_run_handler=lambda registration, doc, tags: observed.append(tags),
    )
    executor.execute(execution_id)
    return pipeline, observed, get_execution(session_factory, execution_id)


def calls_of(manager, kind):
    return [call for call in manager.calls if call[0] == kind]


class TestFreshFrameFeed:
    def test_fresh_frame_is_fed_into_the_pipeline(self, tmp_path, session_factory):
        """10.4: the run takes the Latest_Frame under its own lease, pushes
        it as packed RGB through the Frame_Feed, and records it."""
        frame = make_frame(seq=42, width=8, height=6)
        manager = FakeStreamManager(frame=frame)

        pipeline, observed, row = run(session_factory, tmp_path, make_stream_document(), manager)

        key = camera_key_for_url(CAMERA_URL)
        acquire, latest, release = (calls_of(manager, name) for name in ("acquire", "latest", "release"))
        assert [call[:3] for call in acquire] == [("acquire", key, "run:exec-1")]
        # 10.4: no older than max_frame_age_ms, waiting up to that long.
        assert latest == [("latest", key, 0, 1500, 1500)]
        assert release == [("release", key, "run:exec-1")]
        assert manager.held == {}
        # The lease is taken before the frame and released after it.
        kinds = [call[0] for call in manager.calls]
        assert kinds.index("acquire") < kinds.index("latest") < kinds.index("release")

        assert len(pipeline.calls) == 1
        launch, args, kwargs = pipeline.calls[0]
        assert launch == "appsrc name=appsrc caps=video/x-raw,format=RGB ! videoconvert ! fakesink"
        assert args == ({"data": frame.data, "width": 8, "height": 6, "format": "RGB"},)
        assert set(kwargs) == {"latency_metrics", "status_sink"}

        assert row.status == EXECUTION_STATUS_COMPLETED
        assert observed == [{
            "is_anomalous": False,
            "trigger": {},
            "stream": {"cam": {"seq": 42, "acquiredAtMs": 1_790_000_000_123, "width": 8, "height": 6,
                               "cameraSourceId": None}},
            "frame": {"width": 8, "height": 6},
        }]

    def test_pipeline_keys_are_never_overwritten(self, tmp_path, session_factory):
        manager = FakeStreamManager(frame=make_frame())
        pipeline = FakePipelineManager({"frame": {"width": 1, "height": 1}, "stream": {"cam": "pipeline"}})

        _pipeline, observed, _row = run(session_factory, tmp_path, make_stream_document(), manager,
                                        pipeline=pipeline)

        assert observed[0]["frame"] == {"width": 1, "height": 1}
        assert observed[0]["stream"] == {"cam": "pipeline"}

    def test_continuous_run_asks_for_the_frame_its_tick_chose(self, tmp_path, session_factory):
        manager = FakeStreamManager(frame=make_frame(seq=42))
        context = {"source": "continuous", "frameSeq": 42, "frameAcquiredAtMs": 1, "tickAtMs": 2}

        _pipeline, observed, row = run(session_factory, tmp_path,
                                       make_stream_document(processing_mode="continuous"), manager,
                                       trigger_context=context)

        assert calls_of(manager, "latest") == [("latest", camera_key_for_url(CAMERA_URL), 41, 1500, 1500)]
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert observed[0]["trigger"] == context

    def test_a_manual_context_shaped_like_a_frame_request_is_ignored(self, tmp_path, session_factory):
        """Only a continuous run selects a frame sequence number."""
        manager = FakeStreamManager(frame=make_frame())

        run(session_factory, tmp_path, make_stream_document(), manager,
            trigger_context={"source": "mqtt", "frameSeq": 42})

        assert calls_of(manager, "latest")[0][2] == 0


class TestStaleFrame:
    def test_no_fresh_frame_fails_on_the_stream_node(self, tmp_path, session_factory):
        """10.5: the failing node is the stream node, and the error names
        the camera and its Stream_Health state."""
        manager = FakeStreamManager(frame=None, health={
            "state": "reconnecting",
            "lastError": {"category": "network_error", "message": "connection refused", "atMs": 1}})

        pipeline, observed, row = run(session_factory, tmp_path, make_stream_document(), manager)

        assert pipeline.calls == []
        assert observed == []
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id == "cam"
        assert row.error == ("stream camera {0} delivered no frame newer than 1500 ms "
                             "(state reconnecting: connection refused)".format(NORMALIZED_URL))
        # The run's lease is released on the failure path too.
        assert manager.held == {}

    def test_a_configured_camera_is_named_by_its_camera_source(self, tmp_path, session_factory):
        add_stream_camera(session_factory, "7", CAMERA_URL)
        manager = FakeStreamManager(frame=None, health={"state": "connecting", "lastError": None})

        _pipeline, _observed, row = run(session_factory, tmp_path, make_stream_document(), manager)

        assert row.failing_node_id == "cam"
        assert row.error == "stream camera cfg-7 delivered no frame newer than 1500 ms (state connecting)"

    def test_the_node_frame_age_bounds_the_wait(self, tmp_path, session_factory):
        manager = FakeStreamManager(frame=None, health={})

        _pipeline, _observed, row = run(session_factory, tmp_path,
                                        make_stream_document(max_frame_age_ms=250), manager)

        assert calls_of(manager, "latest")[0][3:] == (250, 250)
        assert "no frame newer than 250 ms (state not streaming)" in row.error

    def test_a_refused_run_lease_fails_on_the_stream_node(self, tmp_path, session_factory):
        """8.10: a lease refused at the device session limit fails the run
        with the refusal reason."""
        manager = FakeStreamManager(refuse=SessionLimitError(2))

        pipeline, _observed, row = run(session_factory, tmp_path, make_stream_document(), manager)

        assert pipeline.calls == []
        assert calls_of(manager, "latest") == []
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id == "cam"
        assert "unavailable" in row.error and "maximum of 2 stream camera sessions" in row.error


class TestCameraSelection:
    def test_url_match_selects_the_configured_camera(self, tmp_path, session_factory):
        """10.6: an unbound node reads the configured camera whose
        normalized Stream_URL equals its url (here spelled with the
        default port and an uppercase host)."""
        add_stream_camera(session_factory, "7", "rtsp://192.168.1.64/Streaming/Channels/101")
        add_stream_camera(session_factory, "8", "rtsp://192.168.1.64/Streaming/Channels/102")
        manager = FakeStreamManager(frame=make_frame())
        document = make_stream_document(url="rtsp://192.168.1.64:554/Streaming/Channels/101")

        _pipeline, observed, row = run(session_factory, tmp_path, document, manager)

        acquire = calls_of(manager, "acquire")
        assert acquire == [("acquire", "cfg-7", "run:exec-1", None)]
        assert observed[0]["stream"]["cam"]["cameraSourceId"] == "cfg-7"
        assert row.status == EXECUTION_STATUS_COMPLETED

    def test_a_camera_of_the_other_type_never_matches(self, tmp_path, session_factory):
        add_stream_camera(session_factory, "9", "rtmp://media.local/live/line1", ImageSourceType.RTMP)
        manager = FakeStreamManager(frame=make_frame())

        run(session_factory, tmp_path, make_stream_document(), manager)

        assert calls_of(manager, "acquire")[0][1] == camera_key_for_url(CAMERA_URL)

    def test_without_a_match_an_anonymous_session_is_used(self, tmp_path, session_factory):
        """10.6: otherwise a credential-less session to the URL."""
        manager = FakeStreamManager(frame=make_frame())
        document = make_stream_document(protocol="rtmp", url="rtmp://Media.Local/live/line1")

        run(session_factory, tmp_path, document, manager)

        (_kind, key, holder, source), = calls_of(manager, "acquire")
        assert key == camera_key_for_url("rtmp://media.local/live/line1")
        assert isinstance(source, StreamSource)
        assert (source.protocol, source.url) == ("rtmp", "rtmp://media.local/live/line1")
        assert source.credentials == {}
        assert holder == "run:exec-1"

    def test_a_bound_camera_is_read(self, tmp_path, session_factory):
        """10.1: the binding's camera wins over a URL match."""
        add_stream_camera(session_factory, "7", CAMERA_URL)
        document = make_stream_document()
        resolution = ResolutionResult(document=document, status=STATUS_RESOLVED, stream_assignments={
            "cam": {"cameraSourceId": "cfg-3", "params": {"url": "rtsp://10.0.0.3/main"}}})
        manager = FakeStreamManager(frame=make_frame())

        _pipeline, observed, _row = run(session_factory, tmp_path, document, manager,
                                        provider=lambda registration_id: resolution)

        assert calls_of(manager, "acquire")[0][:2] == ("acquire", "cfg-3")
        assert observed[0]["stream"]["cam"]["cameraSourceId"] == "cfg-3"

    def test_an_override_url_is_matched_like_a_rendered_one(self, tmp_path, session_factory):
        add_stream_camera(session_factory, "5", "rtsp://10.0.0.5/main")
        document = make_stream_document()
        resolution = ResolutionResult(document=document, status=STATUS_RESOLVED, stream_assignments={
            "cam": {"cameraSourceId": None, "params": {"url": "rtsp://10.0.0.5:554/main",
                                                       "max_frame_age_ms": 900}}})
        manager = FakeStreamManager(frame=make_frame())

        run(session_factory, tmp_path, document, manager, provider=lambda registration_id: resolution)

        assert calls_of(manager, "acquire")[0][:2] == ("acquire", "cfg-5")
        assert calls_of(manager, "latest")[0][3:] == (900, 900)


class TestSingleFeedContract:
    @pytest.mark.parametrize("other", ["aravis", "python"])
    def test_a_stream_node_beside_another_feed_is_rejected(self, tmp_path, session_factory, other):
        """10.6 / custom-python-source 8.5: the single-feed contract counts
        stream points, so the run fails naming both nodes before the
        stream camera is touched."""
        if other == "aravis":
            point = {"nodeId": "arv", "nodeType": "aravis_camera_source",
                     "parameters": {"camera_id": "Aravis-Fake-GV01"}, "slots": [], "aravisBinding": True}
        else:
            point = {"nodeId": "arv", "nodeType": "custom_python_source",
                     "parameters": {}, "slots": [], "pythonSourceBinding": True}
        document = make_stream_document(extra_points=[point])
        document["segments"].append({"name": "s1", "elements": [
            {"nodeId": "arv", "factory": "appsrc", "args": {"name": "appsrc_arv"}},
            {"nodeId": None, "factory": "fakesink", "args": {}}]})
        manager = FakeStreamManager(frame=make_frame())

        pipeline, _observed, row = run(session_factory, tmp_path, document, manager)

        assert pipeline.calls == []
        assert manager.calls == []
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id is None
        assert "2 frame-feed source binding points ('cam', 'arv')" in row.error

    def test_two_stream_nodes_are_rejected_by_the_planner(self):
        second = {"nodeId": "cam2", "nodeType": "rtmp_stream_source",
                  "parameters": {"url": "rtmp://media.local/live/x"}, "slots": [], "streamBinding": True}

        with pytest.raises(StreamFeedError) as raised:
            plan_stream_feeds(make_stream_document(extra_points=[second]))

        assert raised.value.node_id is None
        assert "'cam'" in str(raised.value) and "'cam2'" in str(raised.value)

    @pytest.mark.parametrize("url", [None, "", "   ", 42])
    def test_a_node_without_a_url_fails_on_the_node(self, url):
        with pytest.raises(StreamFeedError) as raised:
            plan_stream_feeds(make_stream_document(url=url))

        assert raised.value.node_id == "cam"
        assert "no valid stream URL" in str(raised.value)

    def test_an_invalid_anonymous_url_fails_the_run_on_the_node(self, tmp_path, session_factory):
        manager = FakeStreamManager(frame=make_frame())

        _pipeline, _observed, row = run(session_factory, tmp_path,
                                        make_stream_document(url="http://not-a-stream/x"), manager)

        assert manager.calls == []
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id == "cam"
        assert row.error.startswith("stream camera http://not-a-stream/x: ")


class TestStreamFreeDocuments:
    def test_no_stream_point_never_touches_the_stream_service(self, tmp_path, session_factory):
        """10.7: the manager is never even created for a stream-free run."""
        document = make_stream_document()
        document["bindingPoints"] = []
        created = []

        def factory():
            created.append(1)
            return FakeStreamManager()

        with patch("stream_ingest.manager.get_stream_ingest_manager", factory):
            artifact_path = write_artifact_set(tmp_path, compiled=document)
            execution_id = seed_run(session_factory, artifact_path)
            pipeline = FakePipelineManager()
            WorkflowExecutor(session_factory=session_factory,
                             pipeline_manager_factory=lambda: pipeline).execute(execution_id)

        assert created == []
        assert get_execution(session_factory).status == EXECUTION_STATUS_COMPLETED


class TestTerminalSink:
    """A model whose results go only to executor bindings (a counter, an
    event gate, an output) ends its branch in ``emltriton``: found on
    hardware (task 25.3), every such continuous run failed with
    GST_FLOW_NOT_LINKED until the executor terminated the branch."""

    @staticmethod
    def branch(*factories, link_to=None):
        return {"segments": [{"name": "s0", "linkTo": link_to, "elements": [
            {"factory": factory, "nodeId": "n{0}".format(index), "args": {}}
            for index, factory in enumerate(factories)]}]}

    def test_a_branch_ending_in_inference_gets_a_fakesink(self):
        document = self.branch("appsrc", "videoconvert", "capsfilter", "emltriton")
        WorkflowExecutor._ensure_terminal_sink(document)
        assert document["segments"][0]["elements"][-1] == {"factory": "fakesink", "nodeId": "n3", "args": {}}

    @pytest.mark.parametrize("factories", [
        ("appsrc", "videoconvert", "fakesink"),
        ("appsrc", "emltriton", "jpegenc", "multifilesink"),
    ])
    def test_a_branch_that_ends_in_a_sink_is_unchanged(self, factories):
        document = self.branch(*factories)
        before = json.dumps(document, sort_keys=True)
        WorkflowExecutor._ensure_terminal_sink(document)
        assert json.dumps(document, sort_keys=True) == before

    def test_a_branch_feeding_a_funnel_is_unchanged(self):
        document = self.branch("appsrc", "emltriton", link_to="s1")
        WorkflowExecutor._ensure_terminal_sink(document)
        assert [element["factory"] for element in document["segments"][0]["elements"]] == ["appsrc", "emltriton"]
