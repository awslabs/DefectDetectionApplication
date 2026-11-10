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
"""The ``s3_upload`` output binding.

An ``s3_upload`` executor binding uploads the JPEG the compiled pipeline
captured on the node's input branch (``capturePaths["in"]``, rooted at
``{work_dir}``) to the rendered ``destination``: ``s3://bucket/key`` by
PutObject with the device's credential chain, or an ``https://`` presigned
URL by HTTP PUT. This module pins:

- the processor: destination rendering (dotted trigger placeholders), the
  ``strip_suffix`` / ``append_suffix`` key rewrite (s3:// only), the
  optional ``condition`` (``not sent`` skip detail), loud contained
  failures for an unresolved placeholder, a bad scheme, a missing capture
  file and an absent ``work_dir``, the masked sent-message detail, and
  the ``work_dir`` keyword's backward-compatible forwarding;
- the default uploader against a real boto3 client under
  ``botocore.stub.Stubber`` (4xx final, 5xx retried) and against a local
  ``http.server`` answering 200 / 204 / 403 / 500 (2xx uploads, 403 is
  final, 500 is retried, error text carries no ``X-Amz-Signature`` value);
- the executor: the run's ``work_dir`` reaches the binding, the uploaded
  frame is persisted as ``{capture_id}.node.<id>.in.jpg``, and an upload
  failure fails the run naming the node (frame still persisted).
"""

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import gst_plugins, output_bindings, pipeline_executor
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.output_bindings import (
    OutputBindingError,
    OutputBindingProcessor,
    UploadRejected,
    _default_s3_uploader,
)
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    WorkflowExecutor,
)

JPEG_BYTES = b"\xff\xd8fake-input-jpeg\xff\xd9"
SIGNED_URL = (
    "https://example-bucket.s3.example.invalid/images/o/u.jpg"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIAEXAMPLE%2F"
    "20250101%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Signature=deadbeef"
)
PAYLOAD = {
    "order_id": "o",
    "body": {"stage": "final", "image_key": "images/o/u.jpg",
             "upload_url": SIGNED_URL},
}
# The executor re-derives ``payload_json`` from the ``payload`` string
# (load_trigger_context), so both carry the same message here.
TRIGGER = {
    "topic": "plant/line1/evaluate",
    "payload": json.dumps(PAYLOAD),
    "payload_json": PAYLOAD,
    "qos": 1,
    "timestamp": 1700000000.0,
}


def upload_binding(node_id="upload", destination=None, capture_path=None,
                   **extra):
    parameters = {
        "destination": destination
        or "s3://example-bucket/{trigger.payload_json.body.image_key}",
        "content_type": "image/jpeg",
        "strip_suffix": None,
        "append_suffix": None,
        "condition": None,
        "timeout_s": 10,
        "retries": 1,
        "region": None,
    }
    parameters.update(extra)
    binding = {
        "nodeId": node_id,
        "binding": "s3_upload",
        "parameters": parameters,
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": [],
    }
    if capture_path is not None:
        binding["capturePaths"] = {"in": capture_path}
    else:
        binding["capturePaths"] = {"in": "{work_dir}/bedrock_frame_cam.jpg"}
    return binding


def make_document(bindings, segments=()):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": list(segments),
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }


def metadata(stage="final"):
    trigger = json.loads(json.dumps(TRIGGER))
    trigger["payload_json"]["body"]["stage"] = stage
    return {"is_anomalous": False, "confidence": 0.1, "trigger": trigger}


class RecordingUploader:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, destination, body, content_type, timeout_s, retries,
                 region):
        self.calls.append((destination, body, content_type, timeout_s,
                           retries, region))
        if self.error is not None:
            raise self.error


@pytest.fixture
def work_dir(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    (path / "bedrock_frame_cam.jpg").write_bytes(JPEG_BYTES)
    return str(path)


@pytest.fixture
def uploader():
    return RecordingUploader()


@pytest.fixture
def processor(uploader):
    return OutputBindingProcessor(s3_uploader=uploader)


def run(processor, document, meta, work_dir, details=None):
    sink = (lambda node_id, detail: details.append((node_id, detail))) \
        if details is not None else None
    processor.process_subset(
        document, meta,
        [b["nodeId"] for b in document["executorBindings"]],
        detail_sink=sink, work_dir=work_dir,
    )


# ---------------------------------------------------------------------------
# Processor behaviour (injected uploader)
# ---------------------------------------------------------------------------

class TestRunS3Upload:
    def test_uploads_the_captured_frame_to_the_rendered_s3_destination(
        self, processor, uploader, work_dir
    ):
        details = []
        run(processor, make_document([upload_binding()]), metadata(),
            work_dir, details)
        assert uploader.calls == [(
            "s3://example-bucket/images/o/u.jpg", JPEG_BYTES, "image/jpeg",
            10, 1, None,
        )]
        assert details == [
            ("upload", "uploaded s3://example-bucket/images/o/u.jpg ({0} bytes)"
             .format(len(JPEG_BYTES)))]

    def test_suffix_rewrite_names_the_annotated_twin(
        self, processor, uploader, work_dir
    ):
        run(processor, make_document([upload_binding(
            strip_suffix=".jpg", append_suffix=".annotated.jpg")]),
            metadata(), work_dir)
        assert uploader.calls[0][0] == \
            "s3://example-bucket/images/o/u.annotated.jpg"

    def test_strip_suffix_not_matching_the_key_is_ignored(
        self, processor, uploader, work_dir
    ):
        run(processor, make_document([upload_binding(
            strip_suffix=".png", append_suffix=".annotated.jpg")]),
            metadata(), work_dir)
        assert uploader.calls[0][0] == \
            "s3://example-bucket/images/o/u.jpg.annotated.jpg"

    def test_region_timeout_retries_and_content_type_are_forwarded(
        self, processor, uploader, work_dir
    ):
        run(processor, make_document([upload_binding(
            content_type="image/png", timeout_s=3, retries=0,
            region="us-east-1")]), metadata(), work_dir)
        assert uploader.calls == [(
            "s3://example-bucket/images/o/u.jpg", JPEG_BYTES, "image/png",
            3, 0, "us-east-1",
        )]

    def test_condition_true_uploads_and_false_skips_with_not_sent_detail(
        self, processor, uploader, work_dir
    ):
        condition = 'trigger.payload_json.body.stage == "final"'
        document = make_document([upload_binding(condition=condition)])
        details = []
        run(processor, document, metadata("final"), work_dir, details)
        assert len(uploader.calls) == 1
        assert details[0][1].startswith("uploaded ")
        details = []
        run(processor, document, metadata("pick"), work_dir, details)
        assert len(uploader.calls) == 1  # nothing new
        assert details == [("upload", "not sent: condition {0!r} evaluated "
                            "false".format(condition))]

    def test_https_destination_is_put_verbatim_and_the_detail_is_masked(
        self, processor, uploader, work_dir
    ):
        details = []
        run(processor, make_document([upload_binding(
            destination="{trigger.payload_json.body.upload_url}",
            strip_suffix=".jpg", append_suffix=".annotated.jpg")]),
            metadata(), work_dir, details)
        # Suffixes never touch a presigned URL (signed for one exact key).
        assert uploader.calls[0][0] == SIGNED_URL
        (detail,) = [d for _, d in details]
        assert "deadbeef" not in detail
        assert "AKIAEXAMPLE" not in detail
        assert "X-Amz-Signature=***" in detail
        assert detail.startswith("uploaded https://example-bucket.s3.example.invalid/")

    @pytest.mark.parametrize("destination", [
        "s3://example-bucket/{trigger.payload_json.body.missing}",
        "s3://example-bucket/{unknown_flat}",
    ])
    def test_unresolved_placeholder_fails_the_node_without_uploading(
        self, processor, uploader, work_dir, destination
    ):
        with pytest.raises(OutputBindingError) as excinfo:
            run(processor, make_document([upload_binding(
                destination=destination)]), metadata(), work_dir)
        assert excinfo.value.node_ids == ["upload"]
        assert "unresolved placeholder" in str(excinfo.value)
        assert uploader.calls == []

    @pytest.mark.parametrize("destination", [
        "http://example.invalid/not-tls", "ftp://example.invalid/x",
        "example-bucket/images/o/u.jpg", "s3://only-bucket",
    ])
    def test_bad_destination_fails_the_node(
        self, processor, uploader, work_dir, destination
    ):
        with pytest.raises(OutputBindingError) as excinfo:
            run(processor, make_document([upload_binding(
                destination=destination)]), metadata(), work_dir)
        assert excinfo.value.node_ids == ["upload"]
        assert uploader.calls == []

    def test_missing_capture_file_fails_the_node(
        self, processor, uploader, tmp_path
    ):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(OutputBindingError) as excinfo:
            run(processor, make_document([upload_binding()]), metadata(),
                str(empty))
        assert excinfo.value.node_ids == ["upload"]
        assert "not captured" in str(excinfo.value)
        assert uploader.calls == []

    def test_absent_work_dir_fails_the_node(self, processor, uploader):
        with pytest.raises(OutputBindingError) as excinfo:
            run(processor, make_document([upload_binding()]), metadata(), None)
        assert excinfo.value.node_ids == ["upload"]
        assert "work directory" in str(excinfo.value)
        assert uploader.calls == []

    def test_binding_without_capture_path_fails_the_node(
        self, processor, uploader, work_dir
    ):
        binding = upload_binding()
        binding["capturePaths"] = {"in": None}
        with pytest.raises(OutputBindingError) as excinfo:
            run(processor, make_document([binding]), metadata(), work_dir)
        assert "capturePaths" in str(excinfo.value)
        assert uploader.calls == []

    def test_upload_failure_is_contained_and_other_bindings_still_run(
        self, work_dir
    ):
        failing = RecordingUploader(error=RuntimeError("HTTP PUT failed"))
        mqtt_calls = []
        processor = OutputBindingProcessor(
            s3_uploader=failing,
            mqtt_publisher=lambda *args, **kwargs: mqtt_calls.append(args))
        document = make_document([
            upload_binding(),
            {"nodeId": "mqtt", "binding": "mqtt_publish",
             "parameters": {"broker_host": "broker.local", "topic": "t"},
             "upstreamNodeIds": ["cam"], "downstreamNodeIds": []},
        ])
        with pytest.raises(OutputBindingError) as excinfo:
            run(processor, document, metadata(), work_dir)
        assert excinfo.value.node_ids == ["upload"]
        assert len(failing.calls) == 1
        assert len(mqtt_calls) == 1


class TestWorkDirForwarding:
    def test_call_forwards_work_dir_to_process(self, processor, uploader, work_dir):
        details = []
        processor(None, make_document([upload_binding()]), metadata(),
                  detail_sink=lambda n, d: details.append(d), work_dir=work_dir)
        assert len(uploader.calls) == 1
        assert details[0].startswith("uploaded ")

    def test_subclass_overriding_process_without_work_dir_keeps_working(self):
        seen = []

        class Legacy(OutputBindingProcessor):
            def process(self, registration, document, tag_values,
                        detail_sink=None):
                seen.append((document, detail_sink))

        Legacy()(None, {"executorBindings": []}, {}, work_dir="/tmp/x")
        assert len(seen) == 1

    def test_process_without_work_dir_fails_only_the_upload_node(
        self, processor, uploader
    ):
        with pytest.raises(OutputBindingError) as excinfo:
            processor.process(None, make_document([upload_binding()]),
                              metadata())
        assert excinfo.value.node_ids == ["upload"]


# ---------------------------------------------------------------------------
# Default uploader: real boto3 client under Stubber, real HTTP server stub
# ---------------------------------------------------------------------------

def _stubbed_client():
    import boto3
    from botocore.stub import Stubber
    client = boto3.client(
        "s3", region_name="us-east-1",
        aws_access_key_id="testing", aws_secret_access_key="testing")
    stubber = Stubber(client)
    stubber.activate()
    return client, stubber


class TestDefaultUploaderS3:
    def test_put_object_carries_bucket_key_body_and_content_type(self):
        client, stubber = _stubbed_client()
        stubber.add_response("put_object", {"ETag": '"x"'}, {
            "Bucket": "example-bucket", "Key": "images/o/u.jpg",
            "Body": JPEG_BYTES, "ContentType": "image/jpeg"})
        with patch.object(output_bindings, "_s3_client", return_value=client):
            _default_s3_uploader(
                "s3://example-bucket/images/o/u.jpg", JPEG_BYTES, "image/jpeg",
                10, 1, "us-east-1")
        stubber.assert_no_pending_responses()

    def test_access_denied_is_final_and_not_retried(self):
        client, stubber = _stubbed_client()
        stubber.add_client_error(
            "put_object", service_error_code="AccessDenied",
            service_message="Access Denied", http_status_code=403)
        with patch.object(output_bindings, "_s3_client", return_value=client):
            with pytest.raises(UploadRejected) as excinfo:
                _default_s3_uploader(
                    "s3://example-bucket/images/o/u.jpg", JPEG_BYTES,
                    "image/jpeg", 10, 3, None)
        # A second attempt would have raised Stubber's unstubbed error.
        assert "AccessDenied" in str(excinfo.value)
        assert "HTTP 403" in str(excinfo.value)

    def test_server_error_is_retried_within_the_budget(self):
        client, stubber = _stubbed_client()
        stubber.add_client_error(
            "put_object", service_error_code="InternalError",
            service_message="We encountered an internal error",
            http_status_code=500)
        stubber.add_response("put_object", {}, {
            "Bucket": "example-bucket", "Key": "images/o/u.jpg",
            "Body": JPEG_BYTES, "ContentType": "image/jpeg"})
        with patch.object(output_bindings, "_s3_client", return_value=client):
            _default_s3_uploader(
                "s3://example-bucket/images/o/u.jpg", JPEG_BYTES, "image/jpeg",
                10, 1, None)
        stubber.assert_no_pending_responses()

    def test_server_error_beyond_the_budget_raises_the_last_error(self):
        client, stubber = _stubbed_client()
        for _ in range(2):
            stubber.add_client_error(
                "put_object", service_error_code="InternalError",
                service_message="internal", http_status_code=500)
        with patch.object(output_bindings, "_s3_client", return_value=client):
            with pytest.raises(RuntimeError) as excinfo:
                _default_s3_uploader(
                    "s3://example-bucket/images/o/u.jpg", JPEG_BYTES,
                    "image/jpeg", 10, 1, None)
        assert not isinstance(excinfo.value, UploadRejected)
        assert "HTTP 500" in str(excinfo.value)
        stubber.assert_no_pending_responses()

    def test_client_is_built_with_region_and_sdk_retries_off(self):
        client = output_bindings._s3_client("us-east-1", 7)
        assert client.meta.region_name == "us-east-1"
        assert client.meta.config.connect_timeout == 7
        assert client.meta.config.read_timeout == 7
        # botocore normalizes max_attempts (retries after the first request)
        # to total_max_attempts: exactly one request, the binding retries.
        assert client.meta.config.retries == {"mode": "standard", "total_max_attempts": 1}


class _PutHandler(BaseHTTPRequestHandler):
    """Answers PUT with the status encoded in the path's first segment and
    records every request (path, Content-Type, body)."""

    requests = []

    def do_PUT(self):  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.requests.append(
            (self.path, self.headers.get("Content-Type"), body))
        status = int(self.path.split("/")[1])
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def put_server():
    _PutHandler.requests = []
    server = HTTPServer(("127.0.0.1", 0), _PutHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{0}".format(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def _url(base, status):
    return "{0}/{1}/images/o/u.jpg?X-Amz-Credential=AKIAEXAMPLE&" \
           "X-Amz-Signature=deadbeef".format(base, status)


class TestDefaultUploaderPresignedUrl:
    @pytest.mark.parametrize("status", [200, 204])
    def test_2xx_uploads_the_body_with_the_content_type(self, put_server, status):
        _default_s3_uploader(_url(put_server, status), JPEG_BYTES, "image/jpeg",
                             5, 1, None)
        assert len(_PutHandler.requests) == 1
        path, content_type, body = _PutHandler.requests[0]
        assert path.startswith("/{0}/images/o/u.jpg?".format(status))
        assert content_type == "image/jpeg"
        assert body == JPEG_BYTES

    def test_403_is_final_and_the_error_carries_no_signature_value(self, put_server):
        with pytest.raises(UploadRejected) as excinfo:
            _default_s3_uploader(_url(put_server, 403), JPEG_BYTES, "image/jpeg",
                                 5, 3, None)
        assert len(_PutHandler.requests) == 1
        text = str(excinfo.value)
        assert "status 403" in text
        assert "deadbeef" not in text and "AKIAEXAMPLE" not in text
        assert "X-Amz-Signature=***" in text

    def test_500_is_retried_then_raised_without_signature_values(self, put_server):
        with pytest.raises(RuntimeError) as excinfo:
            _default_s3_uploader(_url(put_server, 500), JPEG_BYTES, "image/jpeg",
                                 5, 1, None)
        assert not isinstance(excinfo.value, UploadRejected)
        assert len(_PutHandler.requests) == 2
        text = str(excinfo.value)
        assert "status 500" in text
        assert "deadbeef" not in text

    def test_connection_failure_is_retried_and_reported_masked(self):
        # Nothing listens on this port of the loopback interface.
        import socket
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        url = "http://127.0.0.1:{0}/images/o/u.jpg?X-Amz-Signature=deadbeef".format(port)
        with pytest.raises(RuntimeError) as excinfo:
            _default_s3_uploader(url, JPEG_BYTES, "image/jpeg", 2, 1, None)
        assert "deadbeef" not in str(excinfo.value)
        assert "X-Amz-Signature=***" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Executor: work_dir plumbing, node-frame persistence, failure path
# ---------------------------------------------------------------------------

def capture_segment(location="{work_dir}/bedrock_frame_cam.jpg"):
    return {
        "name": "s0",
        "from": None,
        "linkTo": None,
        "elements": [
            {"nodeId": "cam", "factory": "videotestsrc", "args": {}},
            {"nodeId": None, "factory": "videoconvert", "args": {}},
            {"nodeId": None, "factory": "jpegenc", "args": {}},
            {"nodeId": None, "factory": "multifilesink",
             "args": {"location": location}},
        ],
    }


class CapturingPipelineManager:
    """Writes the multifilesink location found in the launch string, as the
    real capture sink chain would."""

    def __init__(self):
        self.calls = []

    def run_pipeline(self, pipeline_str, *args, **kwargs):
        self.calls.append(pipeline_str)
        for token in pipeline_str.split():
            if token.startswith("location=") and token.endswith(".jpg"):
                with open(token[len("location="):], "wb") as f:
                    f.write(JPEG_BYTES)
        return {"is_anomalous": False, "confidence": 0.1}


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture(autouse=True)
def no_registry_scan():
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


@pytest.fixture
def capture_root(tmp_path):
    root = os.path.join(str(tmp_path), "captures")
    with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", root):
        yield root


def seed_run(session_factory, artifact_path):
    session = session_factory()
    try:
        session.add(WorkflowRegistration(
            id="wf-1:3", workflow_id="wf-1", version="3", arch=DEVICE_ARCH,
            artifact_path=str(artifact_path), status="registered",
            registered_at=int(time.time()),
        ))
        execution = WorkflowExecution(
            id="exec-1", registration_id="wf-1:3",
            started_at=int(time.time()), status="pending",
        )
        execution.trigger_context_json = json.dumps(TRIGGER)
        session.add(execution)
        session.commit()
    finally:
        session.close()
    return "exec-1"


def get_execution(session_factory):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, "exec-1")
    finally:
        session.close()


def run_executor(session_factory, document, tmp_path, handler):
    artifact_path = write_artifact_set(tmp_path, compiled=document)
    execution_id = seed_run(session_factory, artifact_path)
    manager = CapturingPipelineManager()
    executor = WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: manager,
        post_run_handler=handler,
    )
    executor.execute(execution_id)
    return manager, get_execution(session_factory)


CAPTURE_ID = "wf-1-exec-1"


def node_files(output_dir):
    try:
        return sorted(n for n in os.listdir(output_dir) if ".node." in n)
    except OSError:
        return []


class TestExecutorIntegration:
    def test_run_uploads_the_captured_frame_and_persists_it(
        self, tmp_path, session_factory, capture_root
    ):
        uploader = RecordingUploader()
        document = make_document([upload_binding()], [capture_segment()])
        manager, row = run_executor(
            session_factory, document, tmp_path,
            OutputBindingProcessor(s3_uploader=uploader))
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert uploader.calls == [(
            "s3://example-bucket/images/o/u.jpg", JPEG_BYTES, "image/jpeg",
            10, 1, None)]
        # The launch string carried the resolved work dir, not the token.
        assert "{work_dir}" not in manager.calls[0]
        output_dir = os.path.join(capture_root, "wf-1", "exec-1")
        assert node_files(output_dir) == [CAPTURE_ID + ".node.upload.in.jpg"]
        with open(os.path.join(output_dir, CAPTURE_ID + ".node.upload.in.jpg"),
                  "rb") as f:
            assert f.read() == JPEG_BYTES
        assert row.has_image_results is True
        status = json.loads(row.node_status_json)
        assert status["upload"]["status"] == "success"
        assert status["upload"]["detail"].startswith(
            "uploaded s3://example-bucket/images/o/u.jpg")

    def test_upload_failure_fails_the_run_naming_the_node_and_keeps_the_frame(
        self, tmp_path, session_factory, capture_root
    ):
        uploader = RecordingUploader(error=RuntimeError("HTTP PUT to x failed"))
        document = make_document([upload_binding()], [capture_segment()])
        _, row = run_executor(
            session_factory, document, tmp_path,
            OutputBindingProcessor(s3_uploader=uploader))
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id == "upload"
        assert len(uploader.calls) == 1
        output_dir = os.path.join(capture_root, "wf-1", "exec-1")
        assert node_files(output_dir) == [CAPTURE_ID + ".node.upload.in.jpg"]
        status = json.loads(row.node_status_json)
        assert status["upload"]["status"] == "failure"
        assert "HTTP PUT to x failed" in status["upload"]["detail"]

    def test_condition_skip_leaves_the_run_completed_without_an_upload(
        self, tmp_path, session_factory, capture_root
    ):
        uploader = RecordingUploader()
        document = make_document(
            [upload_binding(condition='trigger.payload_json.body.stage == "pick"')],
            [capture_segment()])
        _, row = run_executor(
            session_factory, document, tmp_path,
            OutputBindingProcessor(s3_uploader=uploader))
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert uploader.calls == []
        status = json.loads(row.node_status_json)
        assert status["upload"]["detail"].startswith("not sent: condition")

    def test_legacy_handler_without_work_dir_is_invoked_as_before(
        self, tmp_path, session_factory, capture_root
    ):
        seen = []

        def legacy(registration, document, tag_values):
            seen.append(tag_values)

        document = make_document([upload_binding()], [capture_segment()])
        _, row = run_executor(session_factory, document, tmp_path, legacy)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert len(seen) == 1

    def test_run_metadata_and_metadata_view_carry_the_outputs_masked(
        self, tmp_path, session_factory, capture_root, presign
    ):
        """design 6.1 rule 2 / 4.4: the post-run path merges the returned
        outputs into the run metadata; the persisted file and ``/metadata``
        carry ``s3_upload.upload.*`` with the signed query values masked."""
        uploader = RecordingUploader()
        document = make_document([upload_binding(presign_get_expiry_s=900)],
                                 [capture_segment()])
        _, row = run_executor(
            session_factory, document, tmp_path,
            OutputBindingProcessor(s3_uploader=uploader))
        assert row.status == EXECUTION_STATUS_COMPLETED
        with open(os.path.join(row.output_dir, row.capture_id + ".json"),
                  encoding="utf-8") as handle:
            persisted = json.load(handle)
        upload = persisted["s3_upload"]["upload"]
        assert upload["key"] == "images/o/u.jpg"
        assert upload["uri"] == "s3://example-bucket/images/o/u.jpg"
        assert upload["https_url"] == \
            "https://example-bucket.s3.example.invalid/images/o/u.jpg"
        assert_signed_get_url(upload["presigned_url"], "images/o/u.jpg")
        assert "X-Amz-Signature=***" in upload["presigned_url"]
        assert "X-Amz-Credential=***" in upload["presigned_url"]
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from workflow_engine import api as workflow_engine_api
        app = FastAPI()
        app.include_router(workflow_engine_api.router)

        def override_get_db():
            db = session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[workflow_engine_api.get_db] = override_get_db
        served = TestClient(app).get("/workflows/executions/exec-1/metadata")
        assert served.status_code == 200
        assert served.json()["s3_upload"]["upload"]["key"] == "images/o/u.jpg"
        assert "X-Amz-Signature=***" in served.json()["s3_upload"]["upload"]["presigned_url"]
        assert "testing" not in served.text  # the fake access key id never leaves


# ---------------------------------------------------------------------------
# Outputs, presigned GET URL and ``source`` (bedrock-inspection-chain)
# ---------------------------------------------------------------------------

def assert_signed_get_url(url, key):
    """A presigned GET for ``key`` on the neutral test endpoint: botocore
    signs a custom endpoint path-style, the real endpoint virtual-hosted, so
    only the host suffix, the key and the signature are asserted."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc.endswith("s3.example.invalid")
    assert parsed.path.endswith("/" + key)
    assert "X-Amz-Signature=" in parsed.query
    assert "X-Amz-Algorithm=" in parsed.query


def _presign_client():
    """A real client with fake credentials and a neutral endpoint; signing
    is local, no request is made."""
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3", region_name="us-east-1", endpoint_url="https://s3.example.invalid",
        aws_access_key_id="testing", aws_secret_access_key="testing",
        config=Config(signature_version="s3v4"))


@pytest.fixture
def presign():
    with patch.object(output_bindings, "_s3_presign_client",
                      side_effect=lambda region, timeout_s: _presign_client()):
        yield


def run_with_outputs(processor, document, meta, work_dir, details=None):
    sink = (lambda node_id, detail: details.append((node_id, detail))) \
        if details is not None else None
    return processor.process_subset(
        document, meta, [b["nodeId"] for b in document["executorBindings"]],
        detail_sink=sink, work_dir=work_dir)


class TestS3UploadOutputs:
    def test_s3_destination_exposes_the_result_without_a_presigned_url(
        self, processor, uploader, work_dir, presign
    ):
        details = []
        produced = run_with_outputs(
            processor, make_document([upload_binding(region="us-east-1")]),
            metadata(), work_dir, details)
        assert produced == {"s3_upload": {"upload": {
            "bucket": "example-bucket", "key": "images/o/u.jpg",
            "uri": "s3://example-bucket/images/o/u.jpg", "bytes": len(JPEG_BYTES),
            "content_type": "image/jpeg", "destination_kind": "s3",
            "https_url": "https://example-bucket.s3.example.invalid/images/o/u.jpg",
            "region": "us-east-1",
        }}}
        assert details == [("upload", "uploaded s3://example-bucket/images/o/u.jpg "
                                      "({0} bytes)".format(len(JPEG_BYTES)))]

    def test_presign_get_expiry_adds_a_signed_get_url_the_detail_never_shows(
        self, processor, uploader, work_dir, presign
    ):
        from datetime import datetime, timedelta, timezone
        details = []
        before = datetime.now(timezone.utc)
        produced = run_with_outputs(
            processor, make_document([upload_binding(
                presign_get_expiry_s=900, strip_suffix=".jpg",
                append_suffix=".annotated.jpg")]),
            metadata(), work_dir, details)
        outputs = produced["s3_upload"]["upload"]
        url = outputs["presigned_url"]
        assert_signed_get_url(url, "images/o/u.annotated.jpg")
        assert re.search(r"X-Amz-Signature=[0-9a-f]{64}", url)
        assert "X-Amz-Expires=900" in url
        expires_at = datetime.fromisoformat(outputs["presigned_url_expires_at"])
        assert expires_at.tzinfo is not None
        assert timedelta(seconds=895) <= expires_at - before <= timedelta(seconds=905)
        assert "X-Amz" not in details[0][1]
        assert details[0][1].startswith(
            "uploaded s3://example-bucket/images/o/u.annotated.jpg")

    def test_zero_expiry_exposes_no_url_keys(self, processor, work_dir, presign):
        produced = run_with_outputs(
            processor, make_document([upload_binding(presign_get_expiry_s=0)]),
            metadata(), work_dir)
        assert not {"presigned_url", "presigned_url_expires_at"} & \
            set(produced["s3_upload"]["upload"])

    def test_https_destination_exposes_kind_masked_url_and_bytes(
        self, processor, uploader, work_dir, presign
    ):
        produced = run_with_outputs(
            processor,
            make_document([upload_binding(destination=SIGNED_URL)]),
            metadata(), work_dir)
        outputs = produced["s3_upload"]["upload"]
        assert set(outputs) == {"destination_kind", "masked_url", "bytes"}
        assert outputs["destination_kind"] == "https"
        assert outputs["bytes"] == len(JPEG_BYTES)
        assert "X-Amz-Signature=***" in outputs["masked_url"]
        assert "deadbeef" not in outputs["masked_url"]

    def test_presign_failure_is_a_warning_and_the_upload_stands(
        self, processor, uploader, work_dir, caplog
    ):
        details = []
        with patch.object(output_bindings, "_s3_presign_client",
                          side_effect=RuntimeError("no region")):
            with caplog.at_level("WARNING", logger="workflow_engine.output_bindings"):
                produced = run_with_outputs(
                    processor, make_document([upload_binding(presign_get_expiry_s=900)]),
                    metadata(), work_dir, details)
        outputs = produced["s3_upload"]["upload"]
        assert set(outputs) == {"bucket", "key", "uri", "bytes", "content_type",
                                "destination_kind"}
        assert len(uploader.calls) == 1
        assert details[0][1].startswith("uploaded ")
        assert any("could not derive the result URL" in r.getMessage()
                   for r in caplog.records)

    def test_https_url_is_built_from_the_client_endpoint_and_quotes_the_key(
        self, processor, work_dir
    ):
        import boto3
        from botocore.config import Config
        client = boto3.client(
            "s3", region_name="eu-central-1",
            endpoint_url="https://objects.example.invalid:9443",
            aws_access_key_id="testing", aws_secret_access_key="testing",
            config=Config(signature_version="s3v4"))
        with patch.object(output_bindings, "_s3_presign_client", return_value=client):
            produced = run_with_outputs(
                processor, make_document([upload_binding(
                    destination="s3://example-bucket/images/o/part 1.jpg")]),
                metadata(), work_dir)
        outputs = produced["s3_upload"]["upload"]
        assert outputs["https_url"] == \
            "https://example-bucket.objects.example.invalid:9443/images/o/part%201.jpg"
        assert outputs["region"] == "eu-central-1"
        assert outputs["key"] == "images/o/part 1.jpg"

    def test_outputs_of_the_successful_sibling_survive_a_failed_binding(
        self, work_dir, tmp_path, session_factory, capture_root, presign
    ):
        """One upload fails, the other succeeds: the raised error carries
        the successful outputs and the executor still persists them."""
        class FailFirst(RecordingUploader):
            def __call__(self, destination, *args, **kwargs):
                self.calls.append(destination)
                if destination.endswith("/u.jpg"):
                    raise RuntimeError("PutObject failed")

        processor = OutputBindingProcessor(s3_uploader=FailFirst())
        document = make_document([
            upload_binding(), upload_binding(node_id="upload2", append_suffix=".2")])
        with pytest.raises(OutputBindingError) as excinfo:
            run_with_outputs(processor, document, metadata(), work_dir)
        assert excinfo.value.node_ids == ["upload"]
        assert excinfo.value.outputs == {"s3_upload": {"upload2": {
            "bucket": "example-bucket", "key": "images/o/u.jpg.2",
            "uri": "s3://example-bucket/images/o/u.jpg.2", "bytes": len(JPEG_BYTES),
            "content_type": "image/jpeg", "destination_kind": "s3",
            "https_url": "https://example-bucket.s3.example.invalid/images/o/u.jpg.2",
            "region": "us-east-1"}}}
        document = make_document([
            upload_binding(), upload_binding(node_id="upload2", append_suffix=".2")],
            [capture_segment()])
        _, row = run_executor(session_factory, document, tmp_path,
                              OutputBindingProcessor(s3_uploader=FailFirst()))
        assert row.status == EXECUTION_STATUS_FAILED
        with open(os.path.join(row.output_dir, row.capture_id + ".json"),
                  encoding="utf-8") as handle:
            persisted = json.load(handle)
        assert list(persisted["s3_upload"]) == ["upload2"]
        assert persisted["s3_upload"]["upload2"]["key"] == "images/o/u.jpg.2"

    def test_presign_client_is_built_like_the_upload_client_plus_sigv4(self):
        client = output_bindings._s3_presign_client("us-east-1", 7)
        assert client.meta.region_name == "us-east-1"
        assert client.meta.config.connect_timeout == 7
        assert client.meta.config.read_timeout == 7
        assert client.meta.config.retries == {"mode": "standard", "total_max_attempts": 1}
        assert client.meta.config.signature_version == "s3v4"
        # Same region/timeouts/retries as the (unchanged) upload client.
        upload_client = output_bindings._s3_client("us-east-1", 7)
        for field in ("region_name", "connect_timeout", "read_timeout", "retries"):
            assert getattr(client.meta.config, field) == \
                getattr(upload_client.meta.config, field)


class KeywordRecordingUploader(RecordingUploader):
    """A fake accepting the optional ``expected_bucket_owner`` keyword."""

    def __call__(self, destination, body, content_type, timeout_s, retries,
                 region, **kwargs):
        self.calls.append((destination, body, content_type, timeout_s,
                           retries, region, kwargs))


class TestExpectedBucketOwner:
    def test_unset_owner_keeps_the_pre_feature_uploader_call(self, work_dir, presign):
        uploader = KeywordRecordingUploader()
        processor = OutputBindingProcessor(s3_uploader=uploader)
        for owner in (None, ""):
            run_with_outputs(processor, make_document([upload_binding(
                expected_bucket_owner=owner)]), metadata(), work_dir)
        assert [call[6] for call in uploader.calls] == [{}, {}]

    def test_set_owner_reaches_the_uploader_for_s3_only(self, work_dir, presign):
        uploader = KeywordRecordingUploader()
        processor = OutputBindingProcessor(s3_uploader=uploader)
        run_with_outputs(processor, make_document([
            upload_binding(expected_bucket_owner="123456789012"),
            upload_binding(node_id="put", destination=SIGNED_URL,
                           expected_bucket_owner="123456789012"),
        ]), metadata(), work_dir)
        assert uploader.calls[0][6] == {"expected_bucket_owner": "123456789012"}
        assert uploader.calls[1][6] == {}

    def test_default_uploader_sends_expected_bucket_owner_to_put_object(self):
        client, stubber = _stubbed_client()
        stubber.add_response("put_object", {}, {
            "Bucket": "example-bucket", "Key": "images/o/u.jpg",
            "Body": JPEG_BYTES, "ContentType": "image/jpeg",
            "ExpectedBucketOwner": "123456789012"})
        with patch.object(output_bindings, "_s3_client", return_value=client):
            _default_s3_uploader(
                "s3://example-bucket/images/o/u.jpg", JPEG_BYTES, "image/jpeg",
                10, 0, "us-east-1", expected_bucket_owner="123456789012")
        stubber.assert_no_pending_responses()


def bedrock_fed_upload(source, node_id="upload_annotated", upstream=("check",)):
    binding = upload_binding(node_id=node_id, source=source, strip_suffix=".jpg",
                             append_suffix=".annotated.jpg", region="us-east-1")
    binding["upstreamNodeIds"] = list(upstream)
    return binding


def metadata_with_images(tmp_path, **paths):
    meta = metadata()
    entry = {"outcome": "pass", "verdict": "pass"}
    for key, content in paths.items():
        path = tmp_path / (key + ".jpg")
        path.write_bytes(content)
        entry[key] = str(path)
    meta["bedrock"] = {"check": entry}
    return meta


class TestS3UploadSource:
    @pytest.mark.parametrize("source, key", [
        ("annotated_image", "annotated_image_path"),
        ("input_image", "input_image_path"),
    ])
    def test_image_sources_upload_the_bedrock_image_not_the_capture(
        self, processor, uploader, work_dir, tmp_path, presign, source, key
    ):
        content = b"\xff\xd8" + source.encode() + b"\xff\xd9"
        meta = metadata_with_images(tmp_path, **{key: content})
        produced = run_with_outputs(
            processor, make_document([bedrock_fed_upload(source)]), meta, work_dir)
        (destination, body, *_), = uploader.calls
        assert destination == "s3://example-bucket/images/o/u.annotated.jpg"
        assert body == content
        assert produced["s3_upload"]["upload_annotated"]["bytes"] == len(content)

    def test_frame_source_default_reads_the_capture_file(
        self, processor, uploader, work_dir, tmp_path, presign
    ):
        meta = metadata_with_images(tmp_path, annotated_image_path=b"other")
        run_with_outputs(processor, make_document([
            bedrock_fed_upload("frame"), bedrock_fed_upload(None, node_id="plain")]),
            meta, work_dir)
        assert [call[1] for call in uploader.calls] == [JPEG_BYTES, JPEG_BYTES]

    @pytest.mark.parametrize("source", ["annotated_image", "input_image"])
    def test_missing_image_skips_with_a_detail_and_no_outputs(
        self, processor, uploader, work_dir, tmp_path, presign, source
    ):
        details = []
        meta = metadata_with_images(tmp_path)  # error path: no image written
        produced = run_with_outputs(
            processor, make_document([bedrock_fed_upload(source)]), meta, work_dir,
            details)
        assert uploader.calls == []
        assert produced == {}
        assert details == [("upload_annotated",
                            "not uploaded: no {0} image for this run".format(source))]

    def test_missing_bedrock_section_skips_too(self, processor, uploader, work_dir, presign):
        details = []
        produced = run_with_outputs(
            processor, make_document([bedrock_fed_upload("annotated_image")]),
            metadata(), work_dir, details)
        assert uploader.calls == [] and produced == {}
        assert details[0][1] == "not uploaded: no annotated_image image for this run"

    def test_source_without_exactly_one_upstream_fails_the_node(
        self, processor, uploader, work_dir, tmp_path, presign
    ):
        meta = metadata_with_images(tmp_path, annotated_image_path=b"x")
        with pytest.raises(OutputBindingError) as excinfo:
            run_with_outputs(processor, make_document([
                bedrock_fed_upload("annotated_image", upstream=())]), meta, work_dir)
        assert "exactly one upstream" in str(excinfo.value)
        assert uploader.calls == []

    def test_skipped_source_leaves_the_run_completed_in_the_executor(
        self, tmp_path, session_factory, capture_root, presign
    ):
        uploader = RecordingUploader()
        document = make_document([bedrock_fed_upload("annotated_image", node_id="upload")],
                                 [capture_segment()])
        _, row = run_executor(
            session_factory, document, tmp_path,
            OutputBindingProcessor(s3_uploader=uploader))
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert uploader.calls == []
        status = json.loads(row.node_status_json)
        assert status["upload"]["detail"] == \
            "not uploaded: no annotated_image image for this run"
