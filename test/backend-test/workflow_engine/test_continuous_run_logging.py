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
"""The ``continuous_run`` log filter (rtsp-rtmp-stream-cameras
Requirement 12.6): a continuous run's INFO lines stay out of the
component log and in its own run.log, and nothing else changes."""
import logging
import os
import sys
import threading

import pytest
import structlog

from dda_logging import custom_logging
from dda_logging.run_context import (
    CONTINUOUS_RUN,
    ContinuousRunLogFilter,
    continuous_run,
    install_continuous_run_filter,
)
from workflow_engine.run_log import RunLogCapture


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def component_handler():
    handler = ListHandler()
    install_continuous_run_filter([handler])
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield handler
    root.removeHandler(handler)
    root.setLevel(level)


def messages(handler):
    return [record.getMessage() for record in handler.records]


class TestFilter:
    def test_info_from_the_run_loggers_is_dropped_inside_a_continuous_run(self, component_handler):
        with continuous_run("wf-1:3"):
            logging.getLogger("workflow_engine.pipeline_executor").info("starting pipeline")
            logging.getLogger("gstreamer.gst_pipeline").info("Quitting loop")
            logging.getLogger("workflow_engine").debug("detail")
        assert messages(component_handler) == []

    def test_warnings_and_errors_always_pass(self, component_handler):
        with continuous_run("wf-1:3"):
            logging.getLogger("workflow_engine.pipeline_executor").warning("slow model")
            logging.getLogger("gstreamer").error("element error")
        assert messages(component_handler) == ["slow model", "element error"]

    def test_other_loggers_and_runs_outside_the_context_are_unchanged(self, component_handler):
        with continuous_run("wf-1:3"):
            logging.getLogger("stream_ingest.session").info("session reconnected")
            logging.getLogger("workflow_engine_extra").info("not the engine")
        logging.getLogger("workflow_engine.pipeline_executor").info("a triggered run")
        assert messages(component_handler) == ["session reconnected", "not the engine", "a triggered run"]

    def test_the_context_is_per_thread(self, component_handler):
        inside = threading.Event()
        release = threading.Event()

        def continuous():
            with continuous_run("wf-1:3"):
                inside.set()
                release.wait(5)
                logging.getLogger("workflow_engine.pipeline_executor").info("continuous line")

        thread = threading.Thread(target=continuous)
        thread.start()
        inside.wait(5)
        logging.getLogger("workflow_engine.pipeline_executor").info("manual run line")
        release.set()
        thread.join(5)
        assert messages(component_handler) == ["manual run line"]
        assert CONTINUOUS_RUN.get() is None

    def test_the_run_log_keeps_every_line(self, component_handler, tmp_path):
        log_path = tmp_path / "wf-1" / "exec-1" / "run.log"
        with continuous_run("wf-1:3"), RunLogCapture("exec-1", str(log_path)):
            logging.getLogger("workflow_engine.pipeline_executor").info("starting pipeline")
            logging.getLogger("gstreamer.gst_pipeline").info("Quitting loop")
        text = log_path.read_text(encoding="utf-8")
        assert "starting pipeline" in text and "Quitting loop" in text
        assert messages(component_handler) == []

    def test_installation_is_idempotent(self):
        handler = ListHandler()
        install_continuous_run_filter([handler])
        install_continuous_run_filter([handler])
        assert sum(isinstance(item, ContinuousRunLogFilter) for item in handler.filters) == 1


@pytest.fixture
def isolated_logging(tmp_path, monkeypatch):
    """Undo everything ``setup_logging`` changes globally."""
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


def test_setup_logging_filters_the_component_handlers(isolated_logging):
    before = set(logging.getLogger().handlers)
    custom_logging.setup_logging(json_logs=False, log_level="INFO")
    added = [handler for handler in logging.getLogger().handlers if handler not in before]
    assert len(added) == 2
    assert all(any(isinstance(item, ContinuousRunLogFilter) for item in handler.filters) for handler in added)

    with continuous_run("wf-1:3"):
        logging.getLogger("workflow_engine.pipeline_executor").info("per-run line")
        logging.getLogger("workflow_engine.pipeline_executor").warning("per-run warning")
    logging.getLogger("workflow_engine.pipeline_executor").info("triggered line")
    for handler in added:
        handler.flush()
    application = open(os.path.join(str(isolated_logging), "logs", "application.log"), encoding="utf-8").read()
    assert "per-run line" not in application
    assert "per-run warning" in application and "triggered line" in application
