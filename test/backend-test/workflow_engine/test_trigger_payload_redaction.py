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
"""Trigger-payload signed-query redaction (localserver-input-handling-hardening
Item 5, Layer A — owner decision D1 = (b1)).

The task-1 (sink level) case drives the real sink through the executor harness
in ``test_trigger_metadata_persistence.py``: it runs ``WorkflowExecutor.execute``
on a run whose trigger payload embeds a dummy presigned URL, then checks the
persisted ``{output_dir}/{capture_id}.json`` and the ``tags:`` log line formatted
through ``RedactingFilter``. It FAILS on the unfixed tree (the Credential and
Date values survive) and PASSES after Layer A. The task-2 case observes the
no-SigV4 bytes and freezes them.

Dummy SigV4 values use no AKIA/ASIA prefix and ``.invalid`` hosts. The
helper-level Properties 7/8, the serve-time masking cases and the
RecursionError guard are appended in task 4 below the frozen marker.
"""
import json
import os
import time
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import make_session_factory, write_artifact_set

from dda_logging.redaction import RedactingFilter
from workflow_engine import gst_plugins, pipeline_executor, trigger_runtime  # noqa: F401
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

# Dummy SigV4 values (no AKIA/ASIA prefix, .invalid host).
_DUMMY_KEY_ID = "DUMMYKEYID1234567890"
_DUMMY_DATE = "20250101T000000Z"
_DUMMY_TOKEN = "DUMMYSESSIONTOKEN0987654321"
_DUMMY_SIG = "ab" * 32
_SIGNED_URL = (
    "https://dummy.invalid/object.jpg?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=" + _DUMMY_KEY_ID + "%2F20250101%2Fdummy-region%2Fs3%2Faws4_request"
    "&X-Amz-Date=" + _DUMMY_DATE +
    "&X-Amz-Expires=900&X-Amz-SignedHeaders=host"
    "&X-Amz-Security-Token=" + _DUMMY_TOKEN +
    "&X-Amz-Signature=" + _DUMMY_SIG
)
_SIGNED_VALUES = [_DUMMY_KEY_ID, _DUMMY_DATE, _DUMMY_TOKEN, _DUMMY_SIG]


class _FakePipelineManager:
    def __init__(self, tag_values):
        self._tag_values = tag_values

    def run_pipeline(self, pipeline_str, frame_data=None,
                     latency_metrics=None, status_sink=None):
        return dict(self._tag_values)


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture(autouse=True)
def no_registry_scan():
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


def _seed_run(session_factory, artifact_path, trigger_context_json):
    session = session_factory()
    try:
        session.add(WorkflowRegistration(
            id="wf-1:3", workflow_id="wf-1", version="3", arch="x86_64",
            artifact_path=str(artifact_path), status="registered",
            registered_at=int(time.time()),
        ))
        session.add(WorkflowExecution(
            id="exec-1", registration_id="wf-1:3", started_at=int(time.time()),
            status=EXECUTION_STATUS_PENDING,
            trigger_context_json=trigger_context_json,
        ))
        session.commit()
    finally:
        session.close()
    return "exec-1"


def _run_and_capture(tmp_path, session_factory, context, caplog):
    """Run the executor and return (persisted JSON text, formatted tags line)."""
    artifact_path = write_artifact_set(tmp_path)
    execution_id = _seed_run(session_factory, artifact_path, json.dumps(context))
    capture_root = os.path.join(str(tmp_path), "captures")
    import logging as _logging
    with caplog.at_level(_logging.INFO, logger="workflow_engine.pipeline_executor"):
        with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root):
            WorkflowExecutor(
                session_factory=session_factory,
                pipeline_manager_factory=(
                    lambda: _FakePipelineManager({"is_anomalous": False})),
            ).execute(execution_id)

    session = session_factory()
    try:
        row = session.get(WorkflowExecution, execution_id)
        assert row.status == EXECUTION_STATUS_COMPLETED
        output_dir, capture_id = row.output_dir, row.capture_id
    finally:
        session.close()
    metadata_path = os.path.join(output_dir, "{0}.json".format(capture_id))
    with open(metadata_path, "r", encoding="utf-8") as handle:
        persisted = handle.read()

    tags_line = None
    filter_ = RedactingFilter(lambda: [])
    for record in caplog.records:
        if record.name == "workflow_engine.pipeline_executor" and \
                "completed; tags:" in str(record.msg):
            filter_.filter(record)
            tags_line = record.getMessage()
    assert tags_line is not None, "no completed-tags log line was captured"
    return persisted, tags_line


def _context(url):
    return {
        "topic": "line/inspect",
        "payload": json.dumps({"part_id": "P-1", "image_url": url}),
        "qos": 1,
        "timestamp": 1760000000.25,
    }


class TestItem5SinkLevelRedaction:
    """task 1: no dummy SigV4 value reaches the persisted JSON or the tags
    line. FAILS unfixed (Credential and Date survive); PASSES after Layer A."""

    def test_signed_values_are_masked_in_both_sinks(
        self, tmp_path, session_factory, caplog
    ):
        persisted, tags_line = _run_and_capture(
            tmp_path, session_factory, _context(_SIGNED_URL), caplog)
        for value in _SIGNED_VALUES:
            assert value not in persisted, "persisted JSON leaked " + value
            assert value not in tags_line, "tags line leaked " + value


class TestItem5Preservation:
    """task 2 (observation-first): a no-SigV4 context is byte-identical.

    The frozen literals below were captured on the unfixed tree and confirmed
    deterministic across two runs; they must stay byte-identical after the fix.
    """

    _NO_SIGV4_URL = "https://dummy.invalid/a.jpg?part=42&lane=3"

    def test_no_sigv4_context_is_byte_identical(
        self, tmp_path, session_factory, caplog
    ):
        persisted, tags_line = _run_and_capture(
            tmp_path, session_factory, _context(self._NO_SIGV4_URL), caplog)
        trigger = json.loads(persisted)["trigger"]
        assert trigger["payload_json"] == {"part_id": "P-1",
                                           "image_url": self._NO_SIGV4_URL}
        assert self._NO_SIGV4_URL in persisted
        assert self._NO_SIGV4_URL in tags_line
        # No mask is introduced for a secret-free context.
        assert "***" not in persisted
        assert "***" not in tags_line


# ---- appended in task 4 (helper level and serve time) ----
import copy

from hypothesis import example, given, settings
from hypothesis import strategies as st

from workflow_engine import api as workflow_engine_api
from workflow_engine.trigger_runtime import (
    _MASK,
    redact_signed_query,
    redacted_run_view,
)

_SIGV4_NAMES = [
    "X-Amz-Algorithm", "X-Amz-Credential", "X-Amz-Date", "X-Amz-Expires",
    "X-Amz-SignedHeaders", "X-Amz-Signature", "X-Amz-Security-Token",
]
# Value bytes drawn from the shared redactor's value class; each ends at a
# terminator or the end of string. Filler cannot itself form a SigV4 name=.
_VALUE_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwyz0123456789%._~/+=-"
_TERMINATORS = "&;,)]}# '\"\t\n"


# Signed parameters are matched case-insensitively (design Property 7, "random
# order and casing"). Drawing each name with per-character random casing pins
# the masker's case-insensitive flag: a lowercase ``x-amz-credential=`` must
# still be masked. The matched name's own bytes are preserved, so the expected
# reconstruction below reuses the drawn (cased) name.
_sigv4_name = st.sampled_from(_SIGV4_NAMES).flatmap(
    lambda name: st.tuples(
        *(st.sampled_from([ch.lower(), ch.upper()]) for ch in name)
    ).map(lambda chars: "".join(chars))
)


class TestItem5Helpers:
    """task 4.1: the helper-level properties (host)."""

    @settings(max_examples=50, deadline=None)
    @given(
        pairs=st.lists(
            st.tuples(_sigv4_name,
                      st.text(alphabet=_VALUE_CHARS, min_size=1, max_size=20),
                      st.sampled_from(list(_TERMINATORS) + [""])),
            min_size=1, max_size=6),
        filler=st.text(alphabet="abcdefghijklmnopqrstuvwyz /:.", max_size=10),
    )
    @example(pairs=[("X-Amz-Credential", "DUMMYKEYID", "&")], filler="")
    @example(pairs=[("x-amz-credential", "DUMMYKEYID", "&")], filler="")
    def test_property_7_masking_preservation_fixed_point(self, pairs, filler):
        # Build a URL-ish string of SigV4 name=value pairs separated by their
        # drawn terminators, with non-SigV4 filler between.
        parts = []
        for name, value, term in pairs:
            parts.append("{0}={1}{2}".format(name, value, term))
        text = filler + "&".join(parts)
        masked = redact_signed_query(text)
        # Rebuild the expected string byte-for-byte: every value becomes ***,
        # the parameter names, terminators and filler are preserved exactly.
        expected_parts = []
        for name, _value, term in pairs:
            expected_parts.append("{0}={1}{2}".format(name, _MASK, term))
        expected = filler + "&".join(expected_parts)
        assert masked == expected
        assert masked.count(_MASK) == len(pairs)
        # Fixed point.
        assert redact_signed_query(masked) == masked

    @settings(max_examples=50, deadline=None)
    @given(payload=st.one_of(st.none(), st.integers(), st.lists(st.text(max_size=5))))
    def test_property_7_non_string_passthrough(self, payload):
        assert redact_signed_query(payload) is payload

    def test_property_8_no_sigv4_context_is_byte_identical(self):
        context = {"trigger": {"topic": "t", "payload":
                   json.dumps({"image_url": "https://dummy.invalid/a.jpg?x=1&y=2"}),
                   "qos": 1, "nested": {"k": ["https://dummy.invalid/b?z=3"]}},
                   "is_anomalous": False}
        view = redacted_run_view(context)
        assert json.dumps(view) == json.dumps(context)
        assert redact_signed_query(context["trigger"]["payload"]) == \
            context["trigger"]["payload"]

    def test_redacted_run_view_does_not_mutate_and_keeps_tuples(self):
        context = {"trigger": {"url": "https://dummy.invalid/o?X-Amz-Signature=" +
                               "cd" * 32, "pair": ("https://dummy.invalid/p?X-Amz-Date=" +
                               "20250101T000000Z", 3)}}
        snapshot = copy.deepcopy(context)
        view = redacted_run_view(context)
        # Input unchanged.
        assert context == snapshot
        # The signed values are masked in the view.
        assert "***" in view["trigger"]["url"]
        # A tuple leaf stays a tuple.
        assert isinstance(view["trigger"]["pair"], tuple)
        assert view["trigger"]["pair"][1] == 3


class TestItem5ServeTimeMasking:
    """task 4.2 (AC10 bullet 3): a pre-upgrade run is served masked."""

    def _seed(self, session_factory, tmp_path):
        output_dir = os.path.join(str(tmp_path), "run")
        os.makedirs(output_dir)
        capture_id = "cap-pre"
        metadata = {"is_anomalous": False, "trigger": _context(_SIGNED_URL)}
        with open(os.path.join(output_dir, capture_id + ".json"), "w") as handle:
            json.dump(metadata, handle)
        log_path = os.path.join(output_dir, "run.log")
        with open(log_path, "w") as handle:
            handle.write("completed; tags: " + _SIGNED_URL + "\n")
        session = session_factory()
        try:
            session.add(WorkflowRegistration(
                id="wf-1:3", workflow_id="wf-1", version="3", arch="x86_64",
                artifact_path=output_dir, status="registered",
                registered_at=int(time.time())))
            session.add(WorkflowExecution(
                id="exec-pre", registration_id="wf-1:3",
                started_at=int(time.time()), finished_at=int(time.time()),
                status=EXECUTION_STATUS_COMPLETED, output_dir=output_dir,
                capture_id=capture_id, log_path=log_path))
            session.commit()
        finally:
            session.close()

    def _client(self, session_factory):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(workflow_engine_api.router)

        def override_get_db():
            db = session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[workflow_engine_api.get_db] = override_get_db
        return TestClient(app)

    def test_metadata_and_log_are_served_masked(self, tmp_path, session_factory):
        self._seed(session_factory, tmp_path)
        client = self._client(session_factory)
        metadata = client.get("/workflows/executions/exec-pre/metadata")
        assert metadata.status_code == 200
        trigger = metadata.json()["trigger"]
        body = json.dumps(trigger)
        for value in _SIGNED_VALUES:
            assert value not in body
        # Non-signed fields preserved.
        assert metadata.json()["is_anomalous"] is False
        assert json.loads(trigger["payload"])["part_id"] == "P-1"
        log = client.get("/workflows/executions/exec-pre/log")
        assert log.status_code == 200
        for value in _SIGNED_VALUES:
            assert value not in log.text


class TestItem5RecursionGuard:
    """task 4.2 (L2): a RecursionError at the tags: site is contained."""

    def test_a_recursion_error_keeps_the_run_completed_with_a_placeholder(
        self, tmp_path, session_factory, caplog
    ):
        import logging as _logging
        artifact_path = write_artifact_set(tmp_path)
        execution_id = _seed_run(
            session_factory, artifact_path, json.dumps(_context(_SIGNED_URL)))
        capture_root = os.path.join(str(tmp_path), "captures")
        with caplog.at_level(_logging.INFO, logger="workflow_engine.pipeline_executor"):
            with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root):
                with patch.object(pipeline_executor, "redacted_run_view",
                                  side_effect=RecursionError("deep")):
                    WorkflowExecutor(
                        session_factory=session_factory,
                        pipeline_manager_factory=(
                            lambda: _FakePipelineManager({"is_anomalous": False})),
                    ).execute(execution_id)
        session = session_factory()
        try:
            row = session.get(WorkflowExecution, execution_id)
            assert row.status == EXECUTION_STATUS_COMPLETED
        finally:
            session.close()
        tags_lines = [r.getMessage() for r in caplog.records
                      if r.name == "workflow_engine.pipeline_executor"
                      and "completed; tags:" in str(r.msg)]
        assert tags_lines
        assert "<tags withheld: too deeply nested to mask>" in tags_lines[-1]


# ---- appended for the no-SigV4 byte-identity cases (AC12) ----
# The frozen task-2 case (TestItem5Preservation) checks that the no-SigV4 URL
# survives and that no mask is introduced. These literals pin byte identity at
# every sink. They were captured by running this file's own harness against a
# ``git archive 296c230c`` copy of the tree (the pre-change code), confirmed
# identical across two host runs (CPython 3.9.5) and the device container
# (3.11.9), so the check stays observation-first: the fix must reproduce these
# bytes exactly. ``_run_and_capture`` embeds no absolute path, so the strings
# are fully deterministic.
_NO_SIGV4_URL = TestItem5Preservation._NO_SIGV4_URL

_NO_SIGV4_PERSISTED = (
    "{\n"
    '  "is_anomalous": false,\n'
    '  "trigger": {\n'
    '    "topic": "line/inspect",\n'
    '    "payload": "{\\"part_id\\": \\"P-1\\", \\"image_url\\": '
    '\\"https://dummy.invalid/a.jpg?part=42&lane=3\\"}",\n'
    '    "qos": 1,\n'
    '    "timestamp": 1760000000.25,\n'
    '    "payload_json": {\n'
    '      "part_id": "P-1",\n'
    '      "image_url": "https://dummy.invalid/a.jpg?part=42&lane=3"\n'
    "    }\n"
    "  }\n"
    "}"
)

_NO_SIGV4_TAGS_LINE = (
    "Workflow execution exec-1 completed; tags: "
    "{'is_anomalous': False, 'trigger': {'topic': 'line/inspect', "
    "'payload': '{\"part_id\": \"P-1\", \"image_url\": "
    "\"https://dummy.invalid/a.jpg?part=42&lane=3\"}', 'qos': 1, "
    "'timestamp': 1760000000.25, 'payload_json': {'part_id': 'P-1', "
    "'image_url': 'https://dummy.invalid/a.jpg?part=42&lane=3'}}}"
)

# What FastAPI serializes for the GET /metadata body (compact separators) and
# the GET /log body (the tags line plus the writer's trailing newline).
_NO_SIGV4_METADATA_BODY = (
    '{"is_anomalous":false,"trigger":{"topic":"line/inspect",'
    '"payload":"{\\"part_id\\": \\"P-1\\", \\"image_url\\": '
    '\\"https://dummy.invalid/a.jpg?part=42&lane=3\\"}","qos":1,'
    '"timestamp":1760000000.25,"payload_json":{"part_id":"P-1",'
    '"image_url":"https://dummy.invalid/a.jpg?part=42&lane=3"}}}'
)
_NO_SIGV4_LOG_BODY = _NO_SIGV4_TAGS_LINE + "\n"


def _serve_client(session_factory):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(workflow_engine_api.router)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[workflow_engine_api.get_db] = override_get_db
    return TestClient(app)


class TestItem5NoSigV4ByteIdentity:
    """task 2 byte identity (AC12): a no-SigV4 run persists, logs and serves
    the exact pre-change bytes after the fix, at every sink. The literals were
    captured on the unfixed tree (see above), so this pins what the frozen
    substring case only observes."""

    def test_persisted_json_and_tags_line_are_byte_identical(
        self, tmp_path, session_factory, caplog
    ):
        persisted, tags_line = _run_and_capture(
            tmp_path, session_factory, _context(_NO_SIGV4_URL), caplog)
        assert persisted == _NO_SIGV4_PERSISTED
        assert tags_line == _NO_SIGV4_TAGS_LINE

    def test_metadata_and_log_are_served_byte_identical(
        self, tmp_path, session_factory
    ):
        output_dir = os.path.join(str(tmp_path), "run")
        os.makedirs(output_dir)
        capture_id = "cap-nosig"
        with open(os.path.join(output_dir, capture_id + ".json"), "w",
                  encoding="utf-8") as handle:
            handle.write(_NO_SIGV4_PERSISTED)
        log_path = os.path.join(output_dir, "run.log")
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(_NO_SIGV4_LOG_BODY)
        session = session_factory()
        try:
            session.add(WorkflowRegistration(
                id="wf-1:3", workflow_id="wf-1", version="3", arch="x86_64",
                artifact_path=output_dir, status="registered",
                registered_at=int(time.time())))
            session.add(WorkflowExecution(
                id="exec-nosig", registration_id="wf-1:3",
                started_at=int(time.time()), finished_at=int(time.time()),
                status=EXECUTION_STATUS_COMPLETED, output_dir=output_dir,
                capture_id=capture_id, log_path=log_path))
            session.commit()
        finally:
            session.close()
        client = _serve_client(session_factory)
        metadata = client.get("/workflows/executions/exec-nosig/metadata")
        assert metadata.status_code == 200
        assert metadata.content.decode("utf-8") == _NO_SIGV4_METADATA_BODY
        log = client.get("/workflows/executions/exec-nosig/log")
        assert log.status_code == 200
        assert log.text == _NO_SIGV4_LOG_BODY
