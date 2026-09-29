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
"""Log redaction (rtsp-rtmp-stream-cameras task 15.5 — Requirements 6.1, 6.3).

``dda_logging.redaction.RedactingFilter`` masks URL user information,
Secret_Query_Parameter values and Credential_Store values in:

- messages and their arguments, ``extra=`` fields and stack information,
- tracebacks, for stdlib records and for structlog events,
- structlog event dicts,

on every handler ``setup_logging`` creates and on the per-run capture
handler. It never raises, and never writes a record it could not redact.
The request validation handler logs and answers without echoing a
credential.
"""
import asyncio
import io
import json
import logging
import os
import sys

import pytest
import structlog
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request

from dda_logging import custom_logging, redaction
from dda_logging.redaction import (
    UNREDACTABLE_NOTICE,
    RedactingFilter,
    get_redacting_filter,
    install_redaction,
)
from exceptions.handlers import exception_handlers
from stream_ingest import credentials as credentials_module
from workflow_engine.run_log import RunLogCapture

SECRET = "s3cr3t-LOG-4f1a"
USER = "admin-LOG-81c2"
URL = f"rtsp://{USER}:{SECRET}@10.0.4.21:554/stream1"
REDACTED_URL = "rtsp://***@10.0.4.21:554/stream1"
QUERY_URL = f"rtmp://media.local/live/line1?password={SECRET}&profile=main"
STORED_SECRET = "streamkey-LOG-2b7d"


def _leaks(text):
    return [value for value in (SECRET, USER, STORED_SECRET) if value in text]


def _record(msg, args=(), exc_info=None, **attributes):
    record = logging.LogRecord("test.redaction", logging.INFO, __file__, 10, msg, args, exc_info)
    for name, value in attributes.items():
        setattr(record, name, value)
    return record


class _Capture:
    """A private logger whose one handler carries ``filter_``."""

    def __init__(self, name, filter_, fmt="%(levelname)s %(message)s"):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter(fmt))
        self.handler.addFilter(filter_)
        self.logger = logging.getLogger(name)
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

    @property
    def text(self):
        return self.stream.getvalue()

    def close(self):
        self.logger.removeHandler(self.handler)
        self.handler.close()


@pytest.fixture
def capture(request):
    captures = []

    def make(filter_=None, fmt="%(levelname)s %(message)s"):
        created = _Capture(f"test.redaction.{request.node.name}.{len(captures)}",
                           filter_ or RedactingFilter(lambda: [STORED_SECRET]), fmt)
        captures.append(created)
        return created

    yield make
    for created in captures:
        created.close()


class TestMessages:
    def test_url_user_information_in_an_argument_is_masked(self, capture):
        log = capture()
        log.logger.info("connecting to %s", URL)

        assert REDACTED_URL in log.text
        assert _leaks(log.text) == []

    def test_url_user_information_in_the_message_is_masked(self, capture):
        log = capture()
        log.logger.warning(f"pull of {URL} failed")

        assert REDACTED_URL in log.text
        assert _leaks(log.text) == []

    def test_secret_query_values_are_masked(self, capture):
        log = capture()
        log.logger.error("RTMP pull %s refused", QUERY_URL)

        assert "password=***" in log.text
        assert "profile=main" in log.text
        assert _leaks(log.text) == []

    def test_credential_store_values_are_masked_anywhere(self, capture):
        log = capture()
        log.logger.info("stream key %s rejected; raw=%r", STORED_SECRET, {"key": STORED_SECRET})

        assert _leaks(log.text) == []
        assert log.text.count("***") == 2

    def test_secret_free_records_render_exactly_as_before(self):
        formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
        for msg, args in (("count=%d of %s", (3, "rtsp://10.0.4.21/stream1")),
                          ("plain", ()), ("mapping %(a)s", ({"a": 1},))):
            plain = _record(msg, args)
            filtered = _record(msg, args)
            assert RedactingFilter(lambda: [STORED_SECRET]).filter(filtered) is True
            assert formatter.format(filtered) == formatter.format(plain)

    def test_a_newly_stored_secret_is_masked_from_the_next_record_on(self, capture):
        secrets = []
        log = capture(RedactingFilter(lambda: list(secrets)))
        log.logger.info("before %s", "late-secret-19ab")
        secrets.append("late-secret-19ab")
        log.logger.info("after %s", "late-secret-19ab")

        lines = log.text.splitlines()
        assert lines == ["INFO before late-secret-19ab", "INFO after ***"]

    def test_the_default_secrets_come_from_the_credential_store(self, tmp_path, capture):
        store = credentials_module.CredentialStore(directory=str(tmp_path / "stream_credentials"))
        store.put("src-1", {"password": SECRET, "urlSecret": STORED_SECRET})
        credentials_module.set_credential_store(store)
        try:
            log = capture(RedactingFilter())
            log.logger.info("values %s and %s", SECRET, STORED_SECRET)
        finally:
            credentials_module.set_credential_store(None)

        assert log.text.strip() == "INFO values *** and ***"

    def test_without_a_store_urls_are_still_redacted(self, capture, monkeypatch):
        def no_store():
            raise KeyError("COMPONENT_WORK_PATH")

        monkeypatch.setattr(credentials_module, "get_credential_store", no_store)
        assert redaction.credential_store_secrets() == []
        log = capture(RedactingFilter())
        log.logger.info("connecting to %s", URL)

        assert REDACTED_URL in log.text
        assert _leaks(log.text) == []

    def test_extra_fields_are_masked(self, capture):
        log = capture(fmt="%(message)s url=%(url)s meta=%(meta)s")
        log.logger.info("opening", extra={"url": URL, "meta": {"urls": [URL], "port": 554}})

        assert _leaks(log.text) == []
        assert f"url={REDACTED_URL}" in log.text
        assert "'port': 554" in log.text

    def test_a_message_formatted_by_an_earlier_handler_is_masked(self):
        # A handler without the filter that ran first leaves ``message`` on
        # the record, and structlog's ExtraAdder would render it.
        record = _record("pull of %s failed", (URL,))
        logging.Formatter("%(message)s").format(record)
        assert URL in record.message

        RedactingFilter(lambda: []).filter(record)

        assert record.message == f"pull of {REDACTED_URL} failed"

    def test_stack_info_is_masked(self):
        record = _record("opening", stack_info=f'Stack (most recent call last):\n  open("{URL}")')

        RedactingFilter(lambda: []).filter(record)

        assert REDACTED_URL in record.stack_info
        assert _leaks(record.stack_info) == []


class TestTracebacks:
    def test_a_traceback_carrying_a_secret_is_masked(self, capture):
        log = capture()
        try:
            raise ValueError(f"cannot open {URL}")
        except ValueError:
            log.logger.exception("pull failed")

        assert "ValueError" in log.text
        assert "Traceback (most recent call last)" in log.text
        assert REDACTED_URL in log.text
        assert _leaks(log.text) == []

    def test_a_secret_free_traceback_keeps_its_exception(self):
        try:
            raise ValueError("no secret here")
        except ValueError:
            record = _record("pull failed", exc_info=sys.exc_info())

        RedactingFilter(lambda: [STORED_SECRET]).filter(record)

        assert record.exc_info is not None
        assert record.msg == "pull failed"

    def test_a_cached_traceback_text_is_redacted_too(self):
        record = _record("pull failed", exc_info=(ValueError, ValueError("x"), None),
                         exc_text=f"Traceback ...\nValueError: {URL}")

        RedactingFilter(lambda: []).filter(record)

        rendered = logging.Formatter("%(message)s").format(record)
        assert REDACTED_URL in rendered
        assert _leaks(rendered) == []


class TestStructlogEvents:
    def test_event_values_are_redacted_and_their_types_kept(self):
        event = {"event": f"GET {URL}", "nested": {"url": URL, "port": 554},
                 "items": [URL, 3], "pair": (URL, None), "flag": True}
        record = _record(event)

        RedactingFilter(lambda: []).filter(record)

        assert record.msg == {"event": f"GET {REDACTED_URL}",
                              "nested": {"url": REDACTED_URL, "port": 554},
                              "items": [REDACTED_URL, 3], "pair": (REDACTED_URL, None),
                              "flag": True}
        assert event["event"] == f"GET {URL}", "the caller's dict is not mutated"

    @pytest.mark.parametrize("as_instance", [False, True])
    def test_an_event_exception_carrying_a_secret_becomes_a_redacted_exception(self, as_instance):
        try:
            raise RuntimeError(f"cannot open {URL}")
        except RuntimeError as error:
            record = _record({"event": "pull failed", "exc_info": error if as_instance else True})
            RedactingFilter(lambda: []).filter(record)

        assert "exc_info" not in record.msg
        assert "RuntimeError" in record.msg["exception"]
        assert REDACTED_URL in record.msg["exception"]
        assert _leaks(record.msg["exception"]) == []

    def test_a_secret_free_event_exception_is_left_alone(self):
        try:
            raise RuntimeError("nothing secret")
        except RuntimeError:
            record = _record({"event": "pull failed", "exc_info": True})
            RedactingFilter(lambda: [STORED_SECRET]).filter(record)

        assert record.msg == {"event": "pull failed", "exc_info": True}

    def test_an_event_record_with_a_stdlib_traceback_keeps_a_dict_message(self):
        try:
            raise RuntimeError(f"cannot open {URL}")
        except RuntimeError:
            record = _record({"event": "pull failed"}, exc_info=sys.exc_info())
        RedactingFilter(lambda: []).filter(record)

        assert isinstance(record.msg, dict)
        assert record.exc_info is None
        assert _leaks(record.msg["exception"]) == []


class TestFilterSafety:
    def test_a_failing_secret_source_withholds_the_record(self):
        def broken():
            raise RuntimeError("store unavailable")

        try:
            raise ValueError(URL)
        except ValueError:
            record = _record("connecting to %s", (URL,), exc_info=sys.exc_info())

        assert RedactingFilter(broken).filter(record) is True
        assert record.getMessage() == UNREDACTABLE_NOTICE
        assert record.exc_info is None and record.exc_text is None

    def test_a_record_logged_while_redacting_does_not_recurse(self, capture):
        holder = {}

        def secrets_that_log():
            holder["log"].logger.warning("store reloaded")
            return [STORED_SECRET]

        log = capture(RedactingFilter(secrets_that_log))
        holder["log"] = log
        log.logger.info("key %s", STORED_SECRET)

        assert log.text.splitlines() == ["WARNING store reloaded", "INFO key ***"]

    def test_install_redaction_is_idempotent(self):
        handler = logging.StreamHandler(io.StringIO())
        install_redaction([handler])
        install_redaction([handler])

        redacting = [item for item in handler.filters if isinstance(item, RedactingFilter)]
        assert redacting == [get_redacting_filter()]

    def test_a_handler_with_its_own_redacting_filter_gets_no_second_one(self):
        handler = logging.StreamHandler(io.StringIO())
        own = RedactingFilter(lambda: [])
        handler.addFilter(own)

        install_redaction([handler])

        assert handler.filters == [own]

    def test_the_filter_is_shared(self):
        assert get_redacting_filter() is get_redacting_filter()


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


class TestSetupLogging:
    @pytest.mark.parametrize("json_logs", [False, True])
    def test_every_handler_redacts(self, isolated_logging, json_logs):
        before = set(logging.getLogger().handlers + logging.getLogger("api.access").handlers)
        custom_logging.setup_logging(json_logs=json_logs, log_level="INFO")
        # Only the handlers setup_logging adds (pytest adds its own).
        handlers = [handler for handler in
                    logging.getLogger().handlers + logging.getLogger("api.access").handlers
                    if handler not in before]
        assert len(handlers) == 3
        assert all(any(isinstance(item, RedactingFilter) for item in handler.filters)
                   for handler in handlers)

        stdlib_logger = logging.getLogger("stream_ingest.redaction_test")
        stdlib_logger.error("pull of %s failed", URL)
        try:
            raise RuntimeError(f"cannot open {URL}")
        except RuntimeError:
            stdlib_logger.exception("worker crashed")
            structlog.stdlib.get_logger("stream_ingest.redaction_test").exception(
                "structlog crash", url=URL)
        structlog.stdlib.get_logger("api.access").info("request", path=f"/preview?url={URL}")
        for handler in handlers:
            handler.flush()

        logs = os.path.join(str(isolated_logging), "logs")
        application = open(os.path.join(logs, "application.log"), encoding="utf-8").read()
        service = open(os.path.join(logs, "service.log"), encoding="utf-8").read()
        assert _leaks(application) == [] and _leaks(service) == []
        assert application.count("RuntimeError") >= 2
        assert "worker crashed" in application and "structlog crash" in application
        assert REDACTED_URL in application
        assert REDACTED_URL in service


class TestRunLogCapture:
    def test_the_run_log_is_redacted(self, tmp_path):
        run_log = tmp_path / "runs" / "exec-1.log"
        with RunLogCapture("exec-1", str(run_log)):
            capturing = [handler for handler in logging.getLogger("workflow_engine").handlers
                         if isinstance(handler, logging.FileHandler)]
            assert capturing and all(any(isinstance(item, RedactingFilter) for item in handler.filters)
                                     for handler in capturing)
            logging.getLogger("workflow_engine.stream_feed").info("feeding from %s", URL)
            try:
                raise OSError(f"could not open resource {URL}")
            except OSError:
                logging.getLogger("gstreamer.elements").exception("element error")

        text = run_log.read_text(encoding="utf-8")
        assert "feeding from " + REDACTED_URL in text
        assert "OSError" in text
        assert _leaks(text) == []


class TestRequestBodies:
    def test_sensitive_keys_are_masked_at_any_depth(self):
        body = {"name": "line1", "credentials": {"username": USER, "password": SECRET},
                "items": [{"urlSecret": STORED_SECRET, "port": 554}],
                "nested": {"Password": SECRET, "token": ""}}

        masked = exception_handlers._body_for_log(body)

        assert masked == {"name": "line1", "credentials": "***",
                          "items": [{"urlSecret": "***", "port": 554}],
                          "nested": {"Password": "***", "token": ""}}

    @pytest.mark.parametrize("raw", [
        json.dumps({"credentials": {"password": SECRET}}),
        json.dumps({"credentials": {"password": SECRET}}).encode("utf-8"),
    ])
    def test_a_raw_body_naming_a_sensitive_key_is_withheld(self, raw):
        assert exception_handlers._body_for_log(raw) == "[body withheld: it may contain credentials]"

    @pytest.mark.parametrize("value", ["plain text", b"bytes", 42, None, ["a", 1]])
    def test_other_bodies_are_unchanged(self, value):
        expected = list(value) if isinstance(value, list) else value
        assert exception_handlers._body_for_log(value) == expected

    @staticmethod
    def _handle(errors, body):
        request = Request({"type": "http", "method": "POST", "path": "/image-sources",
                           "query_string": b"", "headers": []})
        exc = RequestValidationError(errors, body=body)
        response = asyncio.run(exception_handlers.request_validation_exception_handler(request, exc))
        return exc, response.status_code, json.loads(response.body)

    def test_a_validation_error_echoing_credentials_is_answered_and_logged_masked(self, caplog):
        body = {"type": "RTSP", "credentials": {"username": USER, "password": SECRET}}
        errors = [{"type": "missing", "loc": ("body", "name"), "msg": "Field required",
                   "input": body}]

        with caplog.at_level(logging.ERROR, logger=exception_handlers.__name__):
            _, status, payload = self._handle(errors, body)

        assert status == 400
        assert "Field required" in payload["message"]
        assert _leaks(payload["message"]) == []
        assert _leaks(caplog.text) == []
        assert "'credentials': '***'" in caplog.text

    def test_a_secret_free_validation_error_keeps_its_exact_message(self):
        body = {"type": "Folder", "name": "not-an-int"}
        errors = [{"type": "int_parsing", "loc": ("body", "gain"),
                   "msg": "Input should be a valid integer", "input": "not-an-int"}]

        exc, status, payload = self._handle(errors, body)

        assert status == 400
        assert payload["message"] == str(exc)
