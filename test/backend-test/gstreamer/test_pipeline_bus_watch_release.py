#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""Pipeline runs release their bus signal watch (found on hardware,
rtsp-rtmp-stream-cameras task 25.3).

``bus.add_signal_watch()`` attaches a GSource to the default GLib main
context that holds the bus, and so the ``message`` handler and everything
its closure captures. Both pipeline runners, ``GstPipelineManager.
run_pipeline`` and ``python_bridge.run_bridged_pipeline``, added one per run
and never removed it. A continuous workflow therefore grew the backend by
25-40 KB per run on JP5 and JP6 and slowed from 3 to 1 run/s over two
hours: every later ``loop.run()`` polled every leaked watch.

The check: once a run returns, nothing its message handler captured is
still reachable. With the watch left in place, the handler keeps the
pipeline manager (and the ``status_sink``) alive from the GLib main context
forever.

Real GStreamer: run in the flask-app image (with or without the suite
conftest; the conftest's stub ``gi`` is replaced by the real bindings, as in
``test_property_appsrc_frame_stride.py``).
"""
import atexit
import gc
import os
import shutil
import sys
import tempfile
import weakref

import pytest

WORK_DIR = tempfile.mkdtemp(prefix="bus-watch-release-")
atexit.register(shutil.rmtree, WORK_DIR, ignore_errors=True)
os.environ.setdefault("COMPONENT_WORK_PATH", WORK_DIR)
os.environ.setdefault("INFERENCE_COMPONENT_DECOMPRESED_PATH", os.path.join(WORK_DIR, "plugins"))
os.makedirs(os.environ["INFERENCE_COMPONENT_DECOMPRESED_PATH"], exist_ok=True)
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")


def _real_gi():
    """The real pygobject with GStreamer initialized, replacing the suite
    conftest's stub ``gi`` if it is loaded; None without GStreamer."""
    loaded = sys.modules.get("gi")
    if loaded is None or not hasattr(loaded, "get_required_version"):
        for name in [n for n in list(sys.modules) if n == "gi" or n.startswith("gi.")]:
            del sys.modules[name]
    try:
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("Aravis", "0.8")
        from gi.repository import Gst
        Gst.init(None)
        return Gst
    except (ImportError, ValueError):
        return None


Gst = _real_gi()
if Gst is None or not isinstance(Gst.version_string(), str):
    pytest.skip("real GStreamer and Aravis bindings are not available", allow_module_level=True)

from exceptions.api.gst_pipeline_exception import PipelineExecutionException  # noqa: E402
from gstreamer import gst_pipeline  # noqa: E402
from gstreamer.gst_pipeline import GstPipelineManager, release_bus_watch  # noqa: E402

if gst_pipeline.Gst is not Gst:
    # Another suite in this process imported the runner over the conftest's
    # stub gi, whose mocks keep references to everything they are called
    # with; run this file in its own process.
    pytest.skip("gstreamer.gst_pipeline was imported with a stub gi in this process",
                allow_module_level=True)

FED_LAUNCH = "appsrc name=appsrc caps=video/x-raw,format=RGB ! videoconvert ! fakesink"
#: Fails asynchronously, on the bus: a 4x2 frame cannot become 8x8.
NOT_NEGOTIATED_LAUNCH = ("appsrc name=appsrc caps=video/x-raw,format=RGB ! videoconvert "
                         "! capsfilter caps=video/x-raw,width=8,height=8 ! fakesink")
#: Fails synchronously, going to PLAYING.
MISSING_FILE_LAUNCH = "filesrc location=/nonexistent/dda-bus-watch-release ! fakesink"


def frame(width=4, height=2, pixel_format=None):
    data = {"data": bytes(width * height * 3), "width": width, "height": height}
    if pixel_format:
        data["format"] = pixel_format
    return data


def collected(ref) -> bool:
    for _ in range(3):
        gc.collect()
    return ref() is None


class StatusSink:
    """A weak-referenceable ``status_sink``."""

    def __call__(self, element_name, kind, detail):
        pass


class TestRunPipeline:
    def test_a_finished_run_keeps_nothing_its_handler_captured(self):
        manager, sink = GstPipelineManager(), StatusSink()
        manager_ref, sink_ref = weakref.ref(manager), weakref.ref(sink)
        manager.run_pipeline(FED_LAUNCH, frame_data=frame(), status_sink=sink)
        del manager, sink
        assert collected(manager_ref), "the bus watch still holds the run's message handler"
        assert collected(sink_ref)

    def test_a_run_without_a_status_sink_keeps_nothing_either(self):
        # Every Pipeline_Configuration caller passes no sink.
        manager = GstPipelineManager()
        manager_ref = weakref.ref(manager)
        manager.run_pipeline(FED_LAUNCH, frame_data=frame())
        del manager
        assert collected(manager_ref)

    @pytest.mark.parametrize("launch", [NOT_NEGOTIATED_LAUNCH, MISSING_FILE_LAUNCH],
                             ids=["error-on-the-bus", "fails-to-start"])
    def test_a_failed_run_keeps_nothing_its_handler_captured(self, launch):
        manager, sink = GstPipelineManager(), StatusSink()
        manager_ref, sink_ref = weakref.ref(manager), weakref.ref(sink)
        frame_data = frame() if launch.startswith("appsrc") else None
        with pytest.raises(PipelineExecutionException):
            manager.run_pipeline(launch, frame_data=frame_data, status_sink=sink)
        del manager, sink
        assert collected(manager_ref)
        assert collected(sink_ref)

    def test_many_runs_leave_nothing_behind(self):
        refs = []
        for _ in range(25):
            manager = GstPipelineManager()
            refs.append(weakref.ref(manager))
            manager.run_pipeline(FED_LAUNCH, frame_data=frame())
            del manager
        gc.collect()
        assert [ref() for ref in refs if ref() is not None] == []


class TestRunBridgedPipeline:
    def test_a_bridged_run_keeps_nothing_its_handler_captured(self, monkeypatch):
        from workflow_engine.python_bridge import run_bridged_pipeline

        created = []

        class RecordingManager(GstPipelineManager):
            def __init__(self):
                super().__init__()
                created.append(weakref.ref(self))

        monkeypatch.setattr(gst_pipeline, "GstPipelineManager", RecordingManager)
        run_bridged_pipeline(FED_LAUNCH, [], frame_data=frame(pixel_format="RGB"))
        with pytest.raises(PipelineExecutionException):
            run_bridged_pipeline(NOT_NEGOTIATED_LAUNCH, [], frame_data=frame(pixel_format="RGB"))
        assert len(created) == 2
        assert all(collected(ref) for ref in created), "a bus watch still holds a run's handler"


class TestPipelineStartSerialization:
    """emltriton initializes inside ``set_state(PLAYING)``; both runners take
    ``TRITON_NATIVE_LOCK`` for it (see ``dda_triton/native_calls.py``)."""

    @staticmethod
    def _record_playing_starts(monkeypatch):
        from dda_triton.native_calls import TRITON_NATIVE_LOCK

        starts = []
        original = Gst.Element.set_state

        def set_state(element, state):
            if state == Gst.State.PLAYING:
                starts.append(TRITON_NATIVE_LOCK._is_owned())
            return original(element, state)

        monkeypatch.setattr(Gst.Element, "set_state", set_state)
        return starts

    def test_run_pipeline_starts_under_the_lock(self, monkeypatch):
        starts = self._record_playing_starts(monkeypatch)
        GstPipelineManager().run_pipeline(FED_LAUNCH, frame_data=frame())
        assert starts == [True]

    def test_run_bridged_pipeline_starts_under_the_lock(self, monkeypatch):
        from workflow_engine.python_bridge import run_bridged_pipeline

        starts = self._record_playing_starts(monkeypatch)
        run_bridged_pipeline(FED_LAUNCH, [], frame_data=frame(pixel_format="RGB"))
        assert starts == [True]


class TestReleaseBusWatch:
    def test_it_is_safe_without_a_bus_and_after_a_release(self):
        release_bus_watch(None)
        release_bus_watch(None, 7)
        pipeline = Gst.parse_launch("fakesrc num-buffers=1 ! fakesink")
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        handler_id = bus.connect("message", lambda *_: None)
        release_bus_watch(bus, handler_id)
