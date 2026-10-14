# Copyright 2026 Amazon Web Services, Inc.
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
"""Targeted unit tests for the Bedrock Payload_Reference path
(detection-guided-bedrock-inspection task 7.3, Requirements 3.1-3.5,
3.7, 3.8).

A Bedrock_Binding carrying ``reference_payload_path`` resolves the
dotted path against the run's Trigger_Context ``payload_json``
(``tag_values["trigger"]["payload_json"]``), fetches/decodes the
resolved value through ``workflow_engine.payload_fetch`` (gated by the
binding's newline-separated ``allowed_uri_prefixes``), and sends the
bytes as the Converse "Reference image" content block — taking the
place of a captured reference frame. ANY failure (missing trigger
payload, unresolvable path, prefix denial, fetch failure, size cap,
non-image bytes) is a RECORDED error outcome at
``bedrock.{nodeId}.error`` with NO single-image fallback (Requirement
3.5, design Property 4); the run and sibling bindings proceed. The run
log carries the resolved source string only (URI or "base64 payload
data"), never the decoded bytes (Requirement 3.8). An absent parameter
keeps today's reference-port behavior byte-identical (Requirement 3.1).

The comprehensive unit + property suite lands with task 7.5; these
tests pin the payload-reference mechanics and the no-new-params
preservation.
"""
import base64
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import cv2
import pytest

from workflow_engine import payload_fetch
from workflow_engine.node_status import (
    STATUS_FAILURE,
    NodeStatusCollector,
)
from workflow_engine.output_bindings import (
    BedrockInferenceProcessor,
    RunContext,
)
from workflow_engine.payload_fetch import BASE64_SOURCE

NODE_ID = "bedrock1"
FRAME_NAME = "bedrock_frame_cam.jpg"
CAPTURE_ID = "cap-1"


def encode_png():
    """A small real PNG the reference validator accepts."""
    array = np.arange(48, dtype=np.uint8).reshape(4, 4, 3)
    ok, encoded = cv2.imencode(".png", array)
    assert ok
    return encoded.tobytes()


PNG_BYTES = encode_png()
PNG_BASE64 = base64.b64encode(PNG_BYTES).decode("ascii")


def make_binding(parameters=None, node_id=NODE_ID, capture_paths=None):
    return {
        "nodeId": node_id,
        "binding": "bedrock_inference",
        "parameters": dict(parameters or {}),
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
        "capturePaths": (
            {"in": "{work_dir}/" + FRAME_NAME, "reference": None}
            if capture_paths is None else capture_paths
        ),
    }


def make_document(bindings):
    return {"schemaVersion": 1, "executorBindings": list(bindings)}


def write_frame(work_dir, width=100, height=80, name=FRAME_NAME):
    """A real JPEG frame for the captured 'in' input."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    path = os.path.join(work_dir, name)
    assert cv2.imwrite(path, frame)
    return path


class RecordingInvoker:
    def __init__(self, answer='{"is_anomalous": true, "confidence": 0.9}'):
        self.answer = answer
        self.calls = []

    def __call__(self, model, prompt, images, region, max_tokens):
        self.calls.append({"images": list(images)})
        return self.answer


def make_run_context(tmp_path, payload_json, node_status=None):
    """A RunContext whose Trigger_Context carries ``payload_json``."""
    return RunContext(
        tag_values={"trigger": {"payload_json": payload_json}},
        output_dir=str(tmp_path),
        capture_id=CAPTURE_ID,
        graph_document=None,
        node_status=node_status,
    )


def run_binding(tmp_path, parameters, payload_json, invoker=None,
                node_status=None, capture_paths=None):
    write_frame(str(tmp_path))
    invoker = invoker or RecordingInvoker()
    processor = BedrockInferenceProcessor(invoker=invoker)
    binding = make_binding(parameters, capture_paths=capture_paths)
    metadata = processor.process(
        make_document([binding]), {}, str(tmp_path),
        run_context=make_run_context(
            tmp_path, payload_json, node_status=node_status))
    return metadata, invoker


class _ContentHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.content.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass


@pytest.fixture(scope="module")
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ContentHandler)
    server.content = {"/ref.png": PNG_BYTES}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


# ---------------------------------------------------------------------------
# Happy path (Requirements 3.2, 3.3): the fetched/decoded bytes become
# the Converse "Reference image" block
# ---------------------------------------------------------------------------

class TestPayloadReferenceHappyPath:
    def test_data_url_reference_becomes_reference_image_block(
        self, tmp_path
    ):
        payload = {"refs": [
            {"id": "plate-A", "image": "data:image/png;base64," + PNG_BASE64},
        ]}
        metadata, invoker = run_binding(
            tmp_path, {"reference_payload_path": "refs.0.image"}, payload)

        assert len(invoker.calls) == 1
        images = invoker.calls[0]["images"]
        assert [label for label, _ in images] == \
            ["Input image", "Reference image"]
        assert images[1][1] == PNG_BYTES
        assert metadata["is_anomalous"] is True
        assert "error" not in metadata["bedrock"][NODE_ID]

    def test_bare_base64_reference_decodes(self, tmp_path):
        payload = {"reference": PNG_BASE64}
        metadata, invoker = run_binding(
            tmp_path, {"reference_payload_path": "reference"}, payload)

        images = invoker.calls[0]["images"]
        assert images[1] == ("Reference image", PNG_BYTES)
        assert "error" not in metadata["bedrock"][NODE_ID]

    def test_http_reference_allowed_by_newline_separated_prefixes(
        self, tmp_path, http_server
    ):
        # ``allowed_uri_prefixes`` is a newline-separated string; a
        # non-first line matching the URL permits the fetch (3.4).
        url = "http://127.0.0.1:{0}/ref.png".format(
            http_server.server_address[1])
        prefixes = "https://trusted.example/\nhttp://127.0.0.1:"
        payload = {"image": url}
        metadata, invoker = run_binding(
            tmp_path,
            {"reference_payload_path": "image",
             "allowed_uri_prefixes": prefixes},
            payload)

        assert invoker.calls[0]["images"][1] == ("Reference image", PNG_BYTES)
        assert "error" not in metadata["bedrock"][NODE_ID]

    def test_payload_reference_takes_the_place_of_a_captured_reference(
        self, tmp_path
    ):
        # Even with a fed reference capture path (a misconfiguration the
        # validator flags), the payload reference wins deterministically:
        # the captured reference file is never read.
        reference_path = os.path.join(str(tmp_path), "captured_ref.jpg")
        with open(reference_path, "wb") as f:
            f.write(b"never sent")
        payload = {"image": PNG_BASE64}
        metadata, invoker = run_binding(
            tmp_path, {"reference_payload_path": "image"}, payload,
            capture_paths={
                "in": "{work_dir}/" + FRAME_NAME,
                "reference": reference_path,
            })

        assert invoker.calls[0]["images"][1] == ("Reference image", PNG_BYTES)
        assert "error" not in metadata["bedrock"][NODE_ID]


# ---------------------------------------------------------------------------
# Recorded error outcomes (Requirement 3.5, design Property 4): every
# failure class records an error — never a raise, never a single-image
# fallback (the invoker is never called)
# ---------------------------------------------------------------------------

class TestPayloadReferenceRecordedErrors:
    def assert_recorded_error_no_fallback(self, metadata, invoker):
        error = metadata["bedrock"][NODE_ID]["error"]
        assert NODE_ID in error
        assert invoker.calls == []  # NO single-image fallback
        assert "is_anomalous" not in metadata  # no flat verdict keys
        return error

    def test_unresolvable_path_records_error_naming_segment(self, tmp_path):
        payload = {"refs": [{"image": PNG_BASE64}]}
        metadata, invoker = run_binding(
            tmp_path, {"reference_payload_path": "refs.0.picture"}, payload)

        error = self.assert_recorded_error_no_fallback(metadata, invoker)
        assert "picture" in error

    def test_missing_trigger_payload_json_records_error(self, tmp_path):
        write_frame(str(tmp_path))
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)
        binding = make_binding({"reference_payload_path": "refs.0.image"})
        # A run context without any trigger context at all.
        run_context = RunContext(
            tag_values={}, output_dir=str(tmp_path), capture_id=CAPTURE_ID)

        metadata = processor.process(
            make_document([binding]), {}, str(tmp_path),
            run_context=run_context)

        error = self.assert_recorded_error_no_fallback(metadata, invoker)
        assert "payload_json" in error

    def test_non_json_trigger_payload_records_error(self, tmp_path):
        # payload_json is None when the trigger payload was not JSON.
        metadata, invoker = run_binding(
            tmp_path, {"reference_payload_path": "refs.0.image"}, None)

        error = self.assert_recorded_error_no_fallback(metadata, invoker)
        assert "payload_json" in error

    def test_prefix_denied_records_error_without_fetching(self, tmp_path):
        payload = {"image": "s3://untrusted-bucket/ref.png"}
        metadata, invoker = run_binding(
            tmp_path,
            {"reference_payload_path": "image",
             "allowed_uri_prefixes":
                 "https://trusted.example/\ns3://allowed-bucket/"},
            payload)

        error = self.assert_recorded_error_no_fallback(metadata, invoker)
        assert "denied" in error
        assert "s3://untrusted-bucket/ref.png" in error

    def test_fetch_failure_records_error_naming_source(self, tmp_path):
        # Port 1 on localhost refuses connections immediately.
        url = "http://127.0.0.1:1/ref.png"
        metadata, invoker = run_binding(
            tmp_path, {"reference_payload_path": "image"}, {"image": url})

        error = self.assert_recorded_error_no_fallback(metadata, invoker)
        assert url in error

    def test_non_image_bytes_record_error_without_the_bytes(self, tmp_path):
        secret = b"not an image at all"
        payload = {"image": base64.b64encode(secret).decode("ascii")}
        metadata, invoker = run_binding(
            tmp_path, {"reference_payload_path": "image"}, payload)

        error = self.assert_recorded_error_no_fallback(metadata, invoker)
        assert "not a decodable image" in error
        assert BASE64_SOURCE in error
        assert secret.decode("ascii") not in error
        assert payload["image"] not in error

    def test_size_cap_records_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(payload_fetch, "MAX_REFERENCE_BYTES", 16)
        payload = {"image": PNG_BASE64}
        metadata, invoker = run_binding(
            tmp_path, {"reference_payload_path": "image"}, payload)

        error = self.assert_recorded_error_no_fallback(metadata, invoker)
        assert "size cap" in error

    def test_recorded_error_marks_node_status_failure(self, tmp_path):
        collector = NodeStatusCollector(extra_node_ids=[NODE_ID])
        run_binding(
            tmp_path, {"reference_payload_path": "missing.path"},
            {"other": 1}, node_status=collector)

        assert collector.status_of(NODE_ID) == STATUS_FAILURE

    def test_sibling_bindings_and_run_proceed_after_recorded_error(
        self, tmp_path
    ):
        write_frame(str(tmp_path))
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)
        errored = make_binding(
            {"reference_payload_path": "no.such.path"}, node_id="b_err")
        sibling = make_binding({}, node_id="b_ok")

        metadata = processor.process(
            make_document([errored, sibling]), {"kept": 1}, str(tmp_path),
            run_context=make_run_context(tmp_path, {"refs": []}))

        assert len(invoker.calls) == 1  # only the sibling invoked
        assert metadata["is_anomalous"] is True
        assert metadata["kept"] == 1
        assert "error" in metadata["bedrock"]["b_err"]
        assert metadata["bedrock"]["b_ok"]["text"] == invoker.answer


# ---------------------------------------------------------------------------
# Resolved-source-only logging (Requirement 3.8)
# ---------------------------------------------------------------------------

class TestResolvedSourceLogging:
    def test_base64_reference_logs_source_string_never_the_bytes(
        self, tmp_path, caplog
    ):
        payload = {"image": PNG_BASE64}
        with caplog.at_level(logging.INFO, "workflow_engine.output_bindings"):
            run_binding(
                tmp_path, {"reference_payload_path": "image"}, payload)

        log_text = "\n".join(
            record.getMessage() for record in caplog.records)
        assert BASE64_SOURCE in log_text
        assert PNG_BASE64 not in log_text

    def test_uri_reference_logs_the_resolved_uri(self, tmp_path, caplog):
        # The resolved source lands in the log even when the fetch is
        # later denied by the prefix gate.
        uri = "s3://some-bucket/ref.png"
        with caplog.at_level(logging.INFO, "workflow_engine.output_bindings"):
            run_binding(
                tmp_path,
                {"reference_payload_path": "image",
                 "allowed_uri_prefixes": "https://only.example/"},
                {"image": uri})

        log_text = "\n".join(
            record.getMessage() for record in caplog.records)
        assert uri in log_text


# ---------------------------------------------------------------------------
# No-new-params preservation (Requirement 3.1)
# ---------------------------------------------------------------------------

class TestReferencePortPreservation:
    def test_absent_param_keeps_single_image_reference_port_behavior(
        self, tmp_path
    ):
        frame_path = write_frame(str(tmp_path))
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)
        binding = make_binding({})  # no reference_payload_path

        metadata = processor.process(
            make_document([binding]), {}, str(tmp_path),
            run_context=make_run_context(
                tmp_path, {"image": PNG_BASE64}))

        with open(frame_path, "rb") as f:
            whole_frame = f.read()
        images = invoker.calls[0]["images"]
        assert images == [("Input image", whole_frame)]
        assert "error" not in metadata["bedrock"][NODE_ID]

    def test_absent_param_keeps_fed_reference_port_bytes(self, tmp_path):
        write_frame(str(tmp_path))
        reference_path = os.path.join(str(tmp_path), "captured_ref.jpg")
        reference_bytes = PNG_BYTES
        with open(reference_path, "wb") as f:
            f.write(reference_bytes)
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)
        binding = make_binding({}, capture_paths={
            "in": "{work_dir}/" + FRAME_NAME,
            "reference": reference_path,
        })

        processor.process(
            make_document([binding]), {}, str(tmp_path),
            run_context=make_run_context(tmp_path, {"image": "x"}))

        images = invoker.calls[0]["images"]
        assert [label for label, _ in images] == \
            ["Input image", "Reference image"]
        assert images[1][1] == reference_bytes

    def test_empty_string_param_keeps_reference_port_behavior(
        self, tmp_path
    ):
        # The compiler copies parameters untouched; an empty string is
        # "absent" (matching the crop-index handling).
        write_frame(str(tmp_path))
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)
        binding = make_binding({"reference_payload_path": "  "})

        metadata = processor.process(
            make_document([binding]), {}, str(tmp_path),
            run_context=make_run_context(tmp_path, {"image": PNG_BASE64}))

        assert [label for label, _ in invoker.calls[0]["images"]] == \
            ["Input image"]
        assert "error" not in metadata["bedrock"][NODE_ID]
