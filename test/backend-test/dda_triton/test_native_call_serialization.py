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
"""Python callers of the native Triton wrapper's shared result buffers hold
``TRITON_NATIVE_LOCK`` (found on hardware, rtsp-rtmp-stream-cameras task
25.3: two pipelines starting at once aborted the backend with an uncaught
nlohmann ``parse_error`` from ``TritonServer::ModelMetadata``). See
``dda_triton/native_calls.py``.
"""
import json
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("panorama", MagicMock(name="panorama"))

from dda_triton import native_calls  # noqa: E402
from dda_triton.triton_edge_client import TritonEdgeClient  # noqa: E402


class LockCheckingServer:
    """A native-server stand-in that records whether each call ran with the
    lock held by the calling thread."""

    def __init__(self):
        self.calls = []

    def _record(self, name):
        self.calls.append((name, native_calls.TRITON_NATIVE_LOCK._is_owned()))

    def model_metadata(self, model_id):
        self._record("model_metadata")
        return json.dumps({"name": model_id, "state": "READY"})

    def get_model_status(self, model_id):
        self._record("get_model_status")
        return "READY"


def client_with(server):
    client = TritonEdgeClient.__new__(TritonEdgeClient)
    client.triton_instance = server
    return client


def test_the_lock_is_reentrant():
    with native_calls.TRITON_NATIVE_LOCK:
        with native_calls.TRITON_NATIVE_LOCK:
            assert native_calls.TRITON_NATIVE_LOCK._is_owned()


def test_model_metadata_is_read_under_the_lock():
    server = LockCheckingServer()
    description = client_with(server).get_model_description("model-yolo")
    assert description["status"] == "READY"
    assert server.calls == [("model_metadata", True)]
    assert not native_calls.TRITON_NATIVE_LOCK._is_owned(), "released after the call"


def test_model_status_is_read_under_the_lock():
    server = LockCheckingServer()
    assert client_with(server).get_model_status("model-yolo") == "READY"
    assert server.calls == [("get_model_status", True)]
