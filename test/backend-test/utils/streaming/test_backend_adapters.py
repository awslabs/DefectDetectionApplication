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
"""Example/unit tests for the concrete ``CameraBackend`` adapters (Task 2.5).

Feature: concurrent-camera-stream-viewing.

These exercise the real :class:`~utils.streaming.backends.AravisBackend` and
:class:`~utils.streaming.backends.GStreamerBackend` adapter *logic* —
``open() -> start_stream() -> grab() -> stop_stream() -> close()`` — without any
real hardware or the native ``gi`` / Aravis / GStreamer stack (Req 2.8).

Both adapters defer all native imports to module-level helpers
(``backends._load_aravis_runtime`` / ``backends._load_gstreamer_runtime``), each
returning a ``SimpleNamespace`` of the runtime symbols the adapter uses. The
tests inject a hand-built namespace of fakes in place of those helpers via
``monkeypatch``, so the adapter runs against scripted fakes (fake ``Camera`` /
fake ``Gst`` + appsink). This keeps ``backends.py`` import-safe on a bare
checkout and lets the happy path, the bounded open-retry budget (<= 3 attempts,
Req 7.6), and the open-failure-raises path (so the broadcaster can reject the
subscribe) all be asserted deterministically.

These are example/unit tests (not property-based tests).
"""
from __future__ import annotations

import sys
import threading
from collections import deque
from types import SimpleNamespace

import pytest

# ``backends`` is import-safe without the native stack because its gi imports
# are lazy (see _load_aravis_runtime / _load_gstreamer_runtime).
from utils.streaming import backends
from utils.streaming.backends import AravisBackend, GStreamerBackend, RawFrame
from utils.streaming.models import StreamConfig


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
class _FakeTimer:
    """Stand-in for ``metrics.collector.Timer`` (a no-op context manager)."""

    def __init__(self, *args, **kwargs) -> None:
        self.metric_name = kwargs.get("metric_name")

    def __enter__(self) -> "_FakeTimer":
        return self

    def __exit__(self, *exc) -> bool:
        return False


# --------------------------------------------------------------------------- #
# Aravis fakes
# --------------------------------------------------------------------------- #
class _FakeArvBuffer:
    """A scripted Aravis buffer returned by ``stream.timeout_pop_buffer``."""

    def __init__(self, data: bytes, width: int, height: int, status) -> None:
        self._data = data
        self._width = width
        self._height = height
        self._status = status

    def get_status(self):
        return self._status

    def get_data(self) -> bytes:
        return self._data

    def get_image_width(self) -> int:
        return self._width

    def get_image_height(self) -> int:
        return self._height


class _FakeArvStream:
    """Fake Aravis stream serving a scripted queue of buffers (``None`` = timeout)."""

    def __init__(self, buffers) -> None:
        self._buffers = deque(buffers)
        self.last_timeout_us = None
        self.pop_count = 0

    def timeout_pop_buffer(self, timeout_us):
        self.last_timeout_us = timeout_us
        self.pop_count += 1
        return self._buffers.popleft() if self._buffers else None


class _FakeAravisCamera:
    """Fake of ``utils.camera_manager.Camera`` for driving ``AravisBackend``.

    Records the lifecycle calls the adapter makes (trigger / set_buffer /
    acquisition start-stop / disconnect / feature apply) so tests can assert the
    exact device interaction sequence.
    """

    def __init__(self, camera_id, *, connected, enums, success_status, buffers) -> None:
        self.camera_id = camera_id
        self._connected = connected
        self._enums = enums
        self.trigger_count = 0
        self.set_buffer_count = 0
        self.status_log = []
        self.acq_started = False
        self.acq_stopped = False
        self.disconnected = False
        self.last_config = None
        self.applied_features = []
        # The native device handle only exists once connected; ``grab`` guards on
        # ``cam.camera is None`` so an unopened camera yields no frame.
        self.camera = SimpleNamespace(software_trigger=self._software_trigger) if connected else None
        self.stream = _FakeArvStream(buffers)
        self._lock = threading.Lock()

    def _software_trigger(self) -> None:
        self.trigger_count += 1

    def get_status(self):
        if self._connected:
            return SimpleNamespace(status=self._enums.CONNECTED, error=None)
        return SimpleNamespace(status=self._enums.DISCONNECTED, error="camera not connected")

    def set_buffer(self) -> None:
        self.set_buffer_count += 1

    def update_camera_status(self, status, message=None) -> None:
        self.status_log.append((status, message))

    def start_acquisition(self, config) -> None:
        self.acq_started = True
        self.last_config = config

    def stop_acquisition(self) -> None:
        self.acq_stopped = True

    def disconnect(self) -> None:
        self.disconnected = True

    def apply_device_features(self, feature_list) -> dict:
        self.applied_features.append(feature_list)
        return {entry.get("feature"): entry.get("value") for entry in feature_list}


def _build_aravis_runtime(*, connect_after=1, buffers=None):
    """Build a fake Aravis runtime namespace for ``backends._load_aravis_runtime``.

    Args:
        connect_after: 1-based attempt number on which a freshly constructed
            ``Camera`` first reports CONNECTED (so ``connect_after=3`` models two
            failed opens followed by success; a large value never connects).
        buffers: scripted ``grab()`` buffers served by the camera's stream.
    """
    buffer_status = SimpleNamespace(SUCCESS="SUCCESS", ERROR="ERROR")
    aravis = SimpleNamespace(BufferStatus=buffer_status)
    enums = SimpleNamespace(CONNECTED="CONNECTED", DISCONNECTED="DISCONNECTED")

    class AravisCameraException(Exception):
        pass

    state = {"construct_count": 0}
    cameras = []

    def camera_factory(camera_id):
        state["construct_count"] += 1
        connected = state["construct_count"] >= connect_after
        cam = _FakeAravisCamera(
            camera_id,
            connected=connected,
            enums=enums,
            success_status=buffer_status.SUCCESS,
            buffers=list(buffers) if (buffers and connected) else [],
        )
        cameras.append(cam)
        return cam

    rt = SimpleNamespace(
        Aravis=aravis,
        Camera=camera_factory,
        CONFIG_FEATURE_MAP={},
        CameraStatusEnum=enums,
        Timer=_FakeTimer,
        AravisCameraException=AravisCameraException,
    )
    rt._state = state
    rt._cameras = cameras
    return rt


# --------------------------------------------------------------------------- #
# GStreamer fakes
# --------------------------------------------------------------------------- #
class _FakeGstStructure:
    def __init__(self, width, height) -> None:
        self._width = width
        self._height = height

    def get_int(self, name):
        if name == "width":
            return (True, self._width)
        if name == "height":
            return (True, self._height)
        return (False, 0)


class _FakeGstCaps:
    def __init__(self, width, height) -> None:
        self._structure = _FakeGstStructure(width, height)

    def get_size(self) -> int:
        return 1

    def get_structure(self, index):
        return self._structure


class _FakeGstBuffer:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.unmap_count = 0

    def map(self, flags):
        return (True, SimpleNamespace(data=self._data))

    def unmap(self, map_info) -> None:
        self.unmap_count += 1


class _FakeGstSample:
    """A scripted appsink sample (frame)."""

    def __init__(self, data: bytes, width: int, height: int) -> None:
        self._buffer = _FakeGstBuffer(data)
        self._caps = _FakeGstCaps(width, height)

    def get_buffer(self):
        return self._buffer

    def get_caps(self):
        return self._caps


class _FakeAppsink:
    """Fake appsink serving a scripted queue of samples (``None`` = timeout)."""

    def __init__(self, samples) -> None:
        self._samples = deque(samples)
        self.props = {}
        self.last_timeout_ns = None
        self.pull_count = 0

    def set_property(self, key, value) -> None:
        self.props[key] = value

    def try_pull_sample(self, timeout_ns):
        self.last_timeout_ns = timeout_ns
        self.pull_count += 1
        return self._samples.popleft() if self._samples else None


class _FakeGstPipeline:
    """Fake GStreamer pipeline; ``ok=False`` makes the PAUSED preroll fail."""

    def __init__(self, *, ok, samples, state, state_change_return) -> None:
        self._ok = ok
        self._state = state
        self._scr = state_change_return
        self._appsink = _FakeAppsink(samples)
        self.state_calls = []

    def get_by_name(self, name):
        return self._appsink if name == "appsink" else None

    def set_state(self, state):
        self.state_calls.append(state)
        if not self._ok and state == self._state.PAUSED:
            return self._scr.FAILURE
        return self._scr.SUCCESS

    def get_state(self, timeout_ns):
        if not self._ok:
            return (self._scr.FAILURE, self._state.NULL, self._state.PAUSED)
        return (self._scr.SUCCESS, self._state.PAUSED, self._state.PAUSED)


def _build_gstreamer_runtime(*, fail_until=0, samples=None):
    """Build a fake GStreamer runtime namespace for ``_load_gstreamer_runtime``.

    Args:
        fail_until: number of leading ``parse_launch`` pipelines whose PAUSED
            preroll fails (so ``fail_until=2`` models two failed opens then a
            success; a large value never opens).
        samples: scripted ``grab()`` samples served by the appsink.
    """
    state_change_return = SimpleNamespace(
        FAILURE="FAILURE", SUCCESS="SUCCESS", NO_PREROLL="NO_PREROLL", ASYNC="ASYNC"
    )
    gst_state = SimpleNamespace(NULL="NULL", READY="READY", PAUSED="PAUSED", PLAYING="PLAYING")
    map_flags = SimpleNamespace(READ="READ")

    class PipelineExecutionException(Exception):
        pass

    state = {"launch_count": 0}
    pipelines = []

    def parse_launch(pipeline_str):
        state["launch_count"] += 1
        ok = state["launch_count"] > fail_until
        pipeline = _FakeGstPipeline(
            ok=ok,
            samples=list(samples) if (samples and ok) else [],
            state=gst_state,
            state_change_return=state_change_return,
        )
        pipelines.append(pipeline)
        return pipeline

    gst = SimpleNamespace(
        State=gst_state,
        StateChangeReturn=state_change_return,
        MapFlags=map_flags,
        SECOND=1_000_000_000,
        init=lambda *a, **k: None,
        parse_launch=parse_launch,
    )
    utils_ns = SimpleNamespace(get_gst_plugins_path=lambda: "/tmp/fake/gst/plugins")

    rt = SimpleNamespace(
        Gst=gst,
        GstApp=SimpleNamespace(),
        GLib=SimpleNamespace(),
        GstPipelineBuilder=object,
        Timer=_FakeTimer,
        PipelineExecutionException=PipelineExecutionException,
        utils=utils_ns,
    )
    rt._state = state
    rt._pipelines = pipelines
    return rt


# --------------------------------------------------------------------------- #
# AravisBackend tests
# --------------------------------------------------------------------------- #
def test_aravis_backend_subscribe_grab_close_happy_path(monkeypatch):
    """open -> start_stream -> grab -> stop_stream -> close drives the device and yields a RawFrame."""
    payload = b"aravis-frame-bytes"
    buffer = _FakeArvBuffer(payload, width=64, height=48, status="SUCCESS")
    rt = _build_aravis_runtime(connect_after=1, buffers=[buffer])
    monkeypatch.setattr(backends, "_load_aravis_runtime", lambda: rt)

    backend = AravisBackend("Fake_1", image_source_config={"gain": 1.0})

    backend.open()
    assert rt._state["construct_count"] == 1
    cam = rt._cameras[-1]

    backend.start_stream()
    assert cam.acq_started is True
    assert cam.last_config == {"gain": 1.0}

    frame = backend.grab(timeout_ms=2000)
    assert isinstance(frame, RawFrame)
    assert frame.data == payload
    assert frame.width == 64
    assert frame.height == 48
    # timeout is converted ms -> us, the camera is software-triggered, and the
    # stream is re-armed with a fresh buffer for the next grab.
    assert cam.stream.last_timeout_us == 2000 * 1000
    assert cam.trigger_count == 1
    assert cam.set_buffer_count == 1
    assert ("CONNECTED", None) in cam.status_log

    backend.stop_stream()
    assert cam.acq_stopped is True

    backend.close()
    assert cam.disconnected is True
    assert backend._camera is None


def test_aravis_grab_timeout_returns_none_and_marks_disconnected(monkeypatch):
    """A grab that pops no buffer returns None (disconnect signal) and flags DISCONNECTED."""
    rt = _build_aravis_runtime(connect_after=1, buffers=[])  # empty -> pop returns None
    monkeypatch.setattr(backends, "_load_aravis_runtime", lambda: rt)

    backend = AravisBackend("Fake_1")
    backend.open()
    backend.start_stream()

    assert backend.grab(timeout_ms=1000) is None
    cam = rt._cameras[-1]
    assert any(status == "DISCONNECTED" for status, _ in cam.status_log)


def test_aravis_open_retries_then_succeeds_within_budget(monkeypatch):
    """open retries the connect (<= max_open_attempts) and succeeds on the 3rd try (Req 7.6)."""
    rt = _build_aravis_runtime(connect_after=3, buffers=[])
    monkeypatch.setattr(backends, "_load_aravis_runtime", lambda: rt)

    backend = AravisBackend("Fake_1", stream_config=StreamConfig(max_open_attempts=3))
    backend.open()

    # Exactly three Camera constructions: two not-connected then one connected.
    assert rt._state["construct_count"] == 3
    assert backend._camera is not None


def test_aravis_open_failure_raises_after_max_attempts(monkeypatch):
    """When the camera never connects, open raises after <= 3 attempts so subscribe can be rejected."""
    rt = _build_aravis_runtime(connect_after=999, buffers=[])
    monkeypatch.setattr(backends, "_load_aravis_runtime", lambda: rt)

    backend = AravisBackend("Fake_1", stream_config=StreamConfig(max_open_attempts=3))

    with pytest.raises(rt.AravisCameraException):
        backend.open()

    assert rt._state["construct_count"] == 3
    assert backend._camera is None


# --------------------------------------------------------------------------- #
# GStreamerBackend tests
# --------------------------------------------------------------------------- #
def test_gstreamer_backend_subscribe_grab_close_happy_path(monkeypatch):
    """open -> start_stream -> grab -> stop_stream -> close drives the pipeline and yields a RawFrame."""
    payload = b"gstreamer-frame-bytes"
    sample = _FakeGstSample(payload, width=128, height=72)
    rt = _build_gstreamer_runtime(fail_until=0, samples=[sample])
    monkeypatch.setattr(backends, "_load_gstreamer_runtime", lambda: rt)

    backend = GStreamerBackend("Fake_2", pipeline_description="videotestsrc")

    backend.open()
    assert rt._state["launch_count"] == 1
    pipeline = rt._pipelines[-1]
    # appsink is configured for drop-to-latest, pull-based acquisition.
    assert pipeline._appsink.props == {
        "sync": False,
        "max-buffers": 1,
        "drop": True,
        "emit-signals": False,
    }

    backend.start_stream()
    assert "PLAYING" in pipeline.state_calls

    frame = backend.grab(timeout_ms=1000)
    assert isinstance(frame, RawFrame)
    assert frame.data == payload
    assert frame.width == 128
    assert frame.height == 72
    # timeout converted ms -> ns for the bounded appsink pull.
    assert pipeline._appsink.last_timeout_ns == 1000 * 1_000_000

    backend.stop_stream()
    assert pipeline.state_calls.count("PAUSED") >= 1

    backend.close()
    assert "NULL" in pipeline.state_calls
    assert backend._pipeline is None
    assert backend._appsink is None


def test_gstreamer_grab_timeout_returns_none(monkeypatch):
    """A pull that yields no sample returns None so the worker can treat it as a disconnect."""
    rt = _build_gstreamer_runtime(fail_until=0, samples=[])  # no samples -> pull returns None
    monkeypatch.setattr(backends, "_load_gstreamer_runtime", lambda: rt)

    backend = GStreamerBackend("Fake_2", pipeline_description="videotestsrc")
    backend.open()
    backend.start_stream()

    assert backend.grab(timeout_ms=1000) is None


def test_gstreamer_open_retries_then_succeeds_within_budget(monkeypatch):
    """open retries the preroll (<= max_open_attempts) and succeeds on the 3rd try (Req 7.6)."""
    sample = _FakeGstSample(b"x", width=8, height=8)
    rt = _build_gstreamer_runtime(fail_until=2, samples=[sample])
    monkeypatch.setattr(backends, "_load_gstreamer_runtime", lambda: rt)

    backend = GStreamerBackend(
        "Fake_2", pipeline_description="videotestsrc", stream_config=StreamConfig(max_open_attempts=3)
    )
    backend.open()

    assert rt._state["launch_count"] == 3
    assert backend._pipeline is not None


def test_gstreamer_open_failure_raises_after_max_attempts(monkeypatch):
    """When the preroll always fails, open raises after <= 3 attempts so subscribe can be rejected."""
    rt = _build_gstreamer_runtime(fail_until=999, samples=[])
    monkeypatch.setattr(backends, "_load_gstreamer_runtime", lambda: rt)

    backend = GStreamerBackend(
        "Fake_2", pipeline_description="videotestsrc", stream_config=StreamConfig(max_open_attempts=3)
    )

    with pytest.raises(rt.PipelineExecutionException):
        backend.open()

    assert rt._state["launch_count"] == 3
    assert backend._pipeline is None
