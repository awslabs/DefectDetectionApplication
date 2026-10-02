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
"""The component log stays bounded under continuous stream workflows
(rtsp-rtmp-stream-cameras finding 18; Requirements 12.6, 12.10).

Found on hardware: on the MIC-730 every continuous run wrote about twelve
lines to the backend's stdout and application.log, about 1 GB a day, into
a container log that Docker never rotated and an application.log whose
rotation bounded only its age. The fix has three layers, each checked
here:

1. The pipeline runner's per-run progress and result lines are INFO, so
   the continuous-run filter drops them (the runner ran at WARNING).
2. edgemlsdk's per-call native INFO traces are logged at DEBUG. They come
   from GStreamer streaming threads, where the continuous-run context is
   not set, so the filter cannot drop them.
3. The sinks are bounded whatever the volume: the compose file caps every
   container's log, and application.log and service.log cap the total
   size of their rotated files.
"""
import logging
import os
import re
import sys
import types

import pytest
import yaml

# The pipeline runner's imports read this at import time, as in
# workflow_engine_test_utils. Nothing else is set at import: other suites
# skip on device-only variables being absent.
os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")

from dda_logging import custom_logging  # noqa: E402
from dda_logging.log_rotation import MIB, SizeCappedTimedRotatingFileHandler, suffix_pattern
from dda_logging.run_context import continuous_run, install_continuous_run_filter
from utils.edgemlsdk_trace_levels import PER_CALL_TRACES, is_per_call_trace

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
COMPOSE_PATH = os.path.join(REPO_ROOT, "src", "docker-compose.yaml")
NATIVE_SOURCES = {
    "triton_server.cpp": os.path.join(REPO_ROOT, "src", "edgemlsdk", "src", "src", "mlops", "triton",
                                      "triton_server.cpp"),
    "emltriton.cpp": os.path.join(REPO_ROOT, "src", "edgemlsdk", "src", "src", "gst", "plugins",
                                  "emltriton.cpp"),
}


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def component_handler():
    """A handler set up like the component log's: the continuous-run filter
    installed, on the root logger, at DEBUG."""
    handler = ListHandler()
    install_continuous_run_filter([handler])
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield handler
    root.removeHandler(handler)
    root.setLevel(level)


@pytest.fixture
def gstreamer_records():
    """Every record the ``gstreamer`` loggers emit, unfiltered (what a run's
    own run.log sees)."""
    handler = ListHandler()
    gstreamer = logging.getLogger("gstreamer")
    level = gstreamer.level
    # Production logs at INFO (app.py); without this the records are not
    # created under the default WARNING root level.
    gstreamer.setLevel(logging.DEBUG)
    gstreamer.addHandler(handler)
    yield handler
    gstreamer.removeHandler(handler)
    gstreamer.setLevel(level)


def _from(handler, prefix):
    return [record for record in handler.records
            if record.name == prefix or record.name.startswith(prefix + ".")]


# -- 1. the pipeline runner's per-run lines ---------------------------------------------


@pytest.fixture
def runner(monkeypatch, tmp_path):
    """A GstPipelineManager whose run_pipeline can run without a device:
    its environment writes are undone, and its plugin path is a temporary
    directory. Under the suite conftest's stub ``gi`` the pipeline calls
    are mocks; under real GStreamer the launch string below really runs."""
    from gstreamer import gst_pipeline

    for name, value in (("COMPONENT_WORK_PATH", str(tmp_path)), ("GST_PLUGIN_PATH", str(tmp_path)),
                        ("GST_DEBUG_FILE", str(tmp_path / "gst-debug.log")), ("GST_DEBUG", "0"),
                        ("GST_DEBUG_NO_COLOR", "1"), ("DISPLAY", ":0")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(gst_pipeline.utils, "get_gst_plugins_path", lambda: str(tmp_path))
    # run_pipeline and parse_msg do not use the latency accessor __init__ builds.
    return gst_pipeline, object.__new__(gst_pipeline.GstPipelineManager)


class _Tags:
    def __init__(self, values):
        self._values = values

    def get_value_index(self, name, index):
        return self._values.get(name)


class _TagMessage:
    def __init__(self, gst, values):
        self.type = gst.MessageType.TAG
        self._values = values

    def parse_tag(self):
        return _Tags(self._values)


class _Latency:
    def add_timestamp(self, _name):
        pass


RUN_PROGRESS = ["Initializing GStreamer pipeline", "Setting pipeline to PLAYING state",
                "Pipeline started, waiting for Triton inference", "Running pipeline main loop",
                "Pipeline main loop completed"]


class TestPipelineRunnerLines:
    def test_a_run_logs_its_progress_at_info_and_nothing_at_warning(self, runner, gstreamer_records):
        _module, manager = runner
        manager.run_pipeline("videotestsrc num-buffers=1 ! fakesink")
        logged = [(record.levelno, record.getMessage()) for record in gstreamer_records.records]
        for line in RUN_PROGRESS:
            assert (logging.INFO, line) in logged
        assert [entry for entry in logged if entry[0] >= logging.WARNING] == []

    def test_a_continuous_run_writes_none_of_them_to_the_component_log(
            self, runner, component_handler, gstreamer_records):
        _module, manager = runner
        with continuous_run("wf-1:1"):
            manager.run_pipeline("videotestsrc num-buffers=1 ! fakesink")
        assert _from(component_handler, "gstreamer") == []
        # The run's own log still gets them.
        assert {record.getMessage() for record in gstreamer_records.records} >= set(RUN_PROGRESS)

    def test_a_triggered_run_still_writes_them_to_the_component_log(self, runner, component_handler):
        _module, manager = runner
        manager.run_pipeline("videotestsrc num-buffers=1 ! fakesink")
        messages = [record.getMessage() for record in _from(component_handler, "gstreamer")]
        for line in RUN_PROGRESS:
            assert line in messages

    def test_inference_result_lines_are_info(self, runner, component_handler, gstreamer_records):
        module, manager = runner
        message = _TagMessage(module.Gst, {"is_anomalous": 1, "confidence": 0.83})
        assert manager.parse_msg(message, latency_metrics=_Latency()) == {
            "is_anomalous": 1, "confidence": 0.83}
        logged = [(record.levelno, record.getMessage()) for record in gstreamer_records.records]
        assert logged == [(logging.INFO, "Triton inference result received: is_anomalous=1"),
                          (logging.INFO, "Triton confidence score: 0.83")]
        component_handler.records.clear()
        with continuous_run("wf-1:1"):
            manager.parse_msg(message, latency_metrics=_Latency())
        assert _from(component_handler, "gstreamer") == []


# -- 2. edgemlsdk's per-call native traces -----------------------------------------------


def _trace_info_formats(source):
    with open(NATIVE_SOURCES[source], encoding="utf-8") as handle:
        return re.findall(r'TraceInfo\("((?:[^"\\]|\\.)*)"', handle.read())


def _formatted(fmt):
    """A native ``printf`` format filled in with representative values."""
    values = iter(["model-yolo-test-jetson-xavier-jp5", "READY", "extra"])
    return re.sub(r"%(s|d|f|u|ld|lu|zu)",
                  lambda m: next(values) if m.group(1) == "s" else ("0.830646" if m.group(1) == "f" else "1"),
                  fmt)


#: The native INFO formats that are per-call chatter (finding 18). Every
#: other TraceInfo in these files is lifecycle information and stays INFO.
PER_CALL_FORMATS = {
    ("triton_server.cpp", "Model %s status is %s"),
    ("triton_server.cpp", "Model %s is already loaded"),
    ("emltriton.cpp", "Anomalous: %d"),
    ("emltriton.cpp", "Confidence: %f"),
}


class TestNativeTraceLevels:
    def test_the_per_call_formats_exist_in_the_native_sources(self):
        """If edgemlsdk rewords one of them, this fails instead of the
        flood silently returning."""
        for source, fmt in PER_CALL_FORMATS:
            assert fmt in _trace_info_formats(source), (source, fmt)

    def test_exactly_the_per_call_formats_are_demoted(self):
        demoted = set()
        for source in NATIVE_SOURCES:
            for fmt in _trace_info_formats(source):
                if is_per_call_trace(source, _formatted(fmt)):
                    demoted.add((source, fmt))
        assert demoted == PER_CALL_FORMATS

    @pytest.mark.parametrize("message_file, message, expected", [
        ("triton_server.cpp", "Model model-yolo-test-jetson-xavier-jp5 status is READY", True),
        ("/build/src/mlops/triton/triton_server.cpp", "Model m status is LOADING", True),
        ("triton_server.cpp", "Model m is already loaded", True),
        ("triton_server.cpp", "Model m is already being loaded", False),
        ("triton_server.cpp", "Enqueuing model m for loading", False),
        ("triton_server.cpp", "Model m loaded successfully", False),
        ("emltriton.cpp", "Confidence: 0.830646", True),
        ("emltriton.cpp", "Anomalous: 1", True),
        ("triton_server.cpp", "Confidence: 0.83", False),
        ("other.cpp", "Model m status is READY", False),
        (None, None, False),
    ])
    def test_classification(self, message_file, message, expected):
        assert is_per_call_trace(message_file, message) is expected

    def test_every_rule_names_a_native_source(self):
        assert {source for source, _pattern in PER_CALL_TRACES} <= set(NATIVE_SOURCES)


@pytest.fixture
def listener_module(monkeypatch):
    """``utils.edgemlsdk_trace_listener`` imported against a stand-in for
    the on-device ``panorama.trace`` module (its TraceLevel values)."""
    trace = types.ModuleType("panorama.trace")

    class TraceLevel:
        class _Value:
            def __init__(self, value):
                self.value = value

        Error, Warning, Info, Verbose = _Value(0), _Value(1), _Value(2), _Value(3)

    class TraceListener:
        def __init__(self):
            pass

    trace.TraceLevel = TraceLevel
    trace.TraceListener = TraceListener
    panorama = types.ModuleType("panorama")
    panorama.trace = trace
    monkeypatch.setitem(sys.modules, "panorama", panorama)
    monkeypatch.setitem(sys.modules, "panorama.trace", trace)
    monkeypatch.delitem(sys.modules, "utils.edgemlsdk_trace_listener", raising=False)
    import utils.edgemlsdk_trace_listener as module
    yield module, TraceLevel
    sys.modules.pop("utils.edgemlsdk_trace_listener", None)


class TestTraceListener:
    def _levels(self, module, level, message_file, message, component_handler):
        module.EdgeMLSdkLoggingTraceListener().WriteMessage(level.value, 0, 412, message_file, message)
        return [(record.levelno, record.getMessage())
                for record in _from(component_handler, "utils.edgemlsdk_trace_listener")]

    def test_per_call_info_traces_go_to_debug(self, listener_module, component_handler):
        module, levels = listener_module
        assert self._levels(module, levels.Info, "triton_server.cpp", "Model m status is READY",
                            component_handler) == [(logging.DEBUG, "[triton_server.cpp:412] Model m status is READY")]

    def test_lifecycle_info_traces_stay_info(self, listener_module, component_handler):
        module, levels = listener_module
        assert self._levels(module, levels.Info, "triton_server.cpp", "Enqueuing model m for loading",
                            component_handler) == [(logging.INFO, "[triton_server.cpp:412] Enqueuing model m for loading")]

    def test_warnings_and_errors_are_unchanged(self, listener_module, component_handler):
        module, levels = listener_module
        assert self._levels(module, levels.Warning, "emltriton.cpp", "Confidence: 0.5",
                            component_handler) == [(logging.WARNING, "[emltriton.cpp:412] Confidence: 0.5")]
        component_handler.records.clear()
        assert self._levels(module, levels.Error, "triton_server.cpp", "Model m status is READY",
                            component_handler) == [(logging.ERROR, "[triton_server.cpp:412] Model m status is READY")]


# -- 3a. application.log and service.log ---------------------------------------------------


def _write(path, size):
    with open(path, "wb") as handle:
        handle.write(b"x" * size)


def _handler(tmp_path, cap, backup_count=24 * 14):
    return SizeCappedTimedRotatingFileHandler(
        str(tmp_path / "application.log"), max_rotated_bytes=cap, when="h", interval=1,
        backupCount=backup_count, encoding="utf-8", delay=True)


def _rotated(tmp_path, hours, size):
    paths = []
    for hour in hours:
        path = tmp_path / "application.log.2026-09-30_{0:02d}".format(hour)
        _write(path, size)
        paths.append(str(path))
    return paths


class TestRotatedLogCap:
    def test_the_oldest_files_go_until_the_rotated_files_fit(self, tmp_path):
        paths = _rotated(tmp_path, range(5), 100)
        handler = _handler(tmp_path, cap=250)
        assert sorted(handler.getFilesToDelete()) == paths[:3]
        handler.close()

    def test_the_newest_rotated_file_is_always_kept(self, tmp_path):
        paths = _rotated(tmp_path, range(3), 100)
        _write(tmp_path / "application.log.2026-09-30_03", 1000)
        handler = _handler(tmp_path, cap=250)
        assert sorted(handler.getFilesToDelete()) == paths
        handler.close()

    def test_the_count_limit_still_applies(self, tmp_path):
        paths = _rotated(tmp_path, range(4), 10)
        handler = _handler(tmp_path, cap=10 * MIB, backup_count=2)
        assert sorted(handler.getFilesToDelete()) == paths[:2]
        handler.close()

    def test_under_the_cap_nothing_goes(self, tmp_path):
        _rotated(tmp_path, range(4), 10)
        handler = _handler(tmp_path, cap=10 * MIB)
        assert handler.getFilesToDelete() == []
        handler.close()

    def test_other_files_are_never_counted_or_deleted(self, tmp_path):
        paths = _rotated(tmp_path, range(2), 100)
        for name in ("service.log.2026-09-30_00", "application.log.bak", "application.log.1",
                     "other.application.log.2026-09-30_00", "application.log"):
            _write(tmp_path / name, 10_000)
        handler = _handler(tmp_path, cap=150)
        assert handler.getFilesToDelete() == [paths[0]]
        assert handler.rotated_files() == paths
        handler.close()

    def test_no_cap_keeps_the_plain_count_limit(self, tmp_path):
        _rotated(tmp_path, range(4), 10_000)
        handler = _handler(tmp_path, cap=None)
        assert handler.getFilesToDelete() == []
        handler.close()

    def test_a_rollover_applies_the_cap_and_logging_continues(self, tmp_path):
        old = _rotated(tmp_path, range(3), 1000)
        handler = _handler(tmp_path, cap=1500)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.emit(logging.makeLogRecord({"msg": "before rollover", "levelno": logging.INFO}))
        handler.doRollover()
        handler.emit(logging.makeLogRecord({"msg": "after rollover", "levelno": logging.INFO}))
        handler.close()
        remaining = handler.rotated_files()
        # The rollover's own file is the newest; the cap keeps it and the
        # newest old file, which together fit, and deletes the two oldest.
        assert old[0] not in remaining and old[1] not in remaining and old[2] in remaining
        assert len(remaining) == 2
        assert open(tmp_path / "application.log", encoding="utf-8").read() == "after rollover\n"

    def test_suffix_pattern(self):
        assert re.fullmatch(suffix_pattern("%Y-%m-%d_%H"), "2026-09-30_19")
        assert not re.fullmatch(suffix_pattern("%Y-%m-%d_%H"), "2026-09-30_19.gz")
        assert not re.fullmatch(suffix_pattern("%Y-%m-%d_%H"), "2026-09-30")


@pytest.fixture
def isolated_logging(tmp_path, monkeypatch):
    """Undo everything ``setup_logging`` changes globally."""
    import structlog

    monkeypatch.setenv("COMPONENT_WORK_PATH", str(tmp_path))
    root = logging.getLogger()
    tracked = [root] + [logging.getLogger(name) for name in
                        ("api.access", "uvicorn", "uvicorn.error", "uvicorn.access")]
    saved = [(lg, list(lg.handlers), lg.level, lg.propagate) for lg in tracked]
    excepthook = sys.excepthook
    yield tmp_path
    for lg, handlers, level, propagate in saved:
        for handler in list(lg.handlers):
            if handler not in handlers:
                lg.removeHandler(handler)
                handler.close()
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate
    sys.excepthook = excepthook
    structlog.reset_defaults()


def test_setup_logging_caps_application_and_service_logs(isolated_logging):
    root_before = set(logging.getLogger().handlers)
    access_before = set(logging.getLogger("api.access").handlers)
    custom_logging.setup_logging(json_logs=False, log_level="INFO")
    added = [handler for handler in logging.getLogger().handlers if handler not in root_before]
    added += [handler for handler in logging.getLogger("api.access").handlers if handler not in access_before]
    files = {os.path.basename(handler.baseFilename): handler for handler in added
             if isinstance(handler, logging.FileHandler)}
    assert set(files) == {"application.log", "service.log"}
    for name, cap in (("application.log", custom_logging.APPLICATION_LOG_MAX_ROTATED_BYTES),
                      ("service.log", custom_logging.SERVICE_LOG_MAX_ROTATED_BYTES)):
        handler = files[name]
        assert isinstance(handler, SizeCappedTimedRotatingFileHandler)
        assert handler.max_rotated_bytes == cap
        # The age limit is unchanged: hourly files for 14 days.
        assert handler.when == "H" and handler.backupCount == 24 * 14
    assert custom_logging.APPLICATION_LOG_MAX_ROTATED_BYTES <= 512 * MIB
    assert custom_logging.SERVICE_LOG_MAX_ROTATED_BYTES <= 128 * MIB


# -- 3b. the container logs ----------------------------------------------------------------


_SIZE = re.compile(r"(\d+)([kmg]?)")
_UNITS = {"": 1, "k": 1024, "m": MIB, "g": 1024 * MIB}


def _bytes(text):
    match = _SIZE.fullmatch(str(text).strip().lower())
    assert match, text
    return int(match.group(1)) * _UNITS[match.group(2)]


def test_every_compose_service_caps_its_container_log():
    with open(COMPOSE_PATH, encoding="utf-8") as handle:
        services = yaml.safe_load(handle)["services"]
    assert set(services) >= {"backend_tegra_gpu_enabled", "backend_generic", "backend_generic_nvidia",
                             "frontend"}
    for name, service in services.items():
        logging_config = service.get("logging") or {}
        # max-size and max-file are json-file options, so the driver is named.
        assert logging_config.get("driver") == "json-file", name
        options = logging_config.get("options") or {}
        size, count = _bytes(options.get("max-size")), int(options.get("max-file"))
        assert 1 <= count and 0 < size, name
        assert size * count <= 200 * MIB, name
