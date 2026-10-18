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
"""The additive ``run_log.CAPTURE_FILTERS`` hook (workflow-debug-mode design
§5.1 ``logtap.py``; tasks.md 1.5).

A filter appended to ``CAPTURE_FILTERS`` is on every capture handler created
afterwards; an empty list leaves the handler's filters exactly as before
(the redaction filter only).

Validates: debug R4.2, R13.1.
"""
import logging

import pytest

from workflow_engine import run_log
from workflow_engine.run_log import RunLogCapture


@pytest.fixture(autouse=True)
def clean_capture_filters():
    saved = list(run_log.CAPTURE_FILTERS)
    del run_log.CAPTURE_FILTERS[:]
    yield
    del run_log.CAPTURE_FILTERS[:]
    run_log.CAPTURE_FILTERS.extend(saved)


class _DropDebug(logging.Filter):
    def filter(self, record):
        return record.levelno >= logging.INFO


def test_empty_list_leaves_the_handler_filters_as_today(tmp_path):
    assert run_log.CAPTURE_FILTERS == []
    capture = RunLogCapture("exec-1", str(tmp_path / "run.log"))
    capture.start()
    try:
        handler = capture._handler
        assert handler is not None
        # Exactly the redaction filter the capture always installed.
        assert len(handler.filters) == 1
        assert type(handler.filters[0]).__name__ == "RedactingFilter"
    finally:
        capture.stop()


def test_a_filter_appended_to_capture_filters_is_on_the_capture_handler(tmp_path):
    drop_debug = _DropDebug()
    run_log.CAPTURE_FILTERS.append(drop_debug)
    capture = RunLogCapture("exec-2", str(tmp_path / "run.log"),
                            logger_names=("workflow_engine.capture_filters_test",))
    capture.start()
    try:
        handler = capture._handler
        assert drop_debug in handler.filters
        assert handler.filters.index(drop_debug) == len(handler.filters) - 1
        lg = logging.getLogger("workflow_engine.capture_filters_test")
        saved_level = lg.level
        lg.setLevel(logging.DEBUG)
        try:
            lg.debug("debug line must not be written")
            lg.info("info line is written")
        finally:
            lg.setLevel(saved_level)
    finally:
        capture.stop()
    text = (tmp_path / "run.log").read_text(encoding="utf-8")
    assert "info line is written" in text
    assert "debug line must not be written" not in text


def test_a_filter_added_after_start_is_not_retrofitted(tmp_path):
    capture = RunLogCapture("exec-3", str(tmp_path / "run.log"))
    capture.start()
    try:
        late = _DropDebug()
        run_log.CAPTURE_FILTERS.append(late)
        assert late not in capture._handler.filters
    finally:
        capture.stop()
