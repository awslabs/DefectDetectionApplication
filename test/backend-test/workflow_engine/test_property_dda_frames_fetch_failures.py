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
"""Property test for source-naming fetch and decode failures in
``dda_frames``.

Exec's ``HELPERS_SOURCE`` into a fresh module namespace and provokes
failures from an in-process ``http.server`` bound to localhost — no
outbound network.

- **Feature: custom-python-source, Property 9: Fetch and decode
  failures raise errors naming the source** — Validates:
  Requirements 4.5, 4.6
"""
import itertools
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from hypothesis import given
from hypothesis import strategies as st

from workflow_engine.python_bridge import HELPERS_SOURCE

_path_counter = itertools.count()

#: The full non-success range; 3xx responses deliberately carry no
#: Location header, so urllib's redirect handling degrades to the
#: standard HTTPError, like any other non-2xx status.
_NON_SUCCESS_STATUSES = st.integers(min_value=300, max_value=599)


def load_helpers():
    module = types.ModuleType("dda_frames")
    exec(HELPERS_SOURCE, module.__dict__)
    return module


class _StatusHandler(BaseHTTPRequestHandler):
    """Serves each registered path with its configured (status, body)."""

    def do_GET(self):
        status, body = self.server.responses.get(self.path, (404, b""))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass


@pytest.fixture(scope="module")
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
    server.responses = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def serve(server, status, body=b""):
    path = "/failure-{0}".format(next(_path_counter))
    server.responses[path] = (status, body)
    return "http://127.0.0.1:{0}{1}".format(server.server_address[1], path)


# ---------------------------------------------------------------------------
# Feature: custom-python-source, Property 9: Fetch and decode failures raise
# errors naming the source
#
# For any HTTP response with a non-success status code, and for any fetched
# content that does not decode as an image, the Frame_Helpers raise a
# ValueError whose message contains the source string and describes the
# failure.
#
# **Validates: Requirements 4.5, 4.6**
# ---------------------------------------------------------------------------


@given(
    status=_NON_SUCCESS_STATUSES,
    junk=st.binary(min_size=0, max_size=64),
)
def test_property_9_fetch_and_decode_failures_name_the_source(
    http_server, status, junk
):
    """**Feature: custom-python-source, Property 9: Fetch and decode
    failures raise errors naming the source**

    **Validates: Requirements 4.5, 4.6**
    """
    helpers = load_helpers()

    # Non-success status: ValueError naming the URL, for both
    # load_bytes and load_image (Req 4.5).
    failing_url = serve(http_server, status)
    with pytest.raises(ValueError) as bytes_error:
        helpers.load_bytes(failing_url)
    assert failing_url in str(bytes_error.value)

    with pytest.raises(ValueError) as image_error:
        helpers.load_image(failing_url)
    assert failing_url in str(image_error.value)

    # Fetched content that is not an image (the prefix guarantees no
    # valid image magic bytes): ValueError naming the URL and describing
    # the decode failure (Req 4.6).
    undecodable_url = serve(http_server, 200, b"not-an-image:" + junk)
    with pytest.raises(ValueError) as decode_error:
        helpers.load_image(undecodable_url)
    assert undecodable_url in str(decode_error.value)
    assert "decode" in str(decode_error.value)
