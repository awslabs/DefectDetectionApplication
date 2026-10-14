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
"""Property test for HTTP(S) fetch round trips through ``dda_frames``.

Exec's ``HELPERS_SOURCE`` into a fresh module namespace (exactly as the
Python_Runner registers ``dda_frames``) and fetches from an in-process
``http.server`` bound to localhost — no outbound network.

- **Feature: custom-python-source, Property 8: HTTP(S) fetches
  round-trip content** — Validates: Requirements 4.1, 4.3
"""
import itertools
import os
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

from workflow_engine.python_bridge import HELPERS_SOURCE

_path_counter = itertools.count()


def load_helpers():
    """Exec HELPERS_SOURCE into a fresh module namespace, as the runner
    does when it registers ``dda_frames`` in ``sys.modules``."""
    module = types.ModuleType("dda_frames")
    exec(HELPERS_SOURCE, module.__dict__)
    return module


def encode_png(array):
    ok, encoded = cv2.imencode(".png", array)
    assert ok, "cv2.imencode failed for a plain uint8 array"
    return encoded.tobytes()


class _ContentHandler(BaseHTTPRequestHandler):
    """Serves the bytes registered on the server under each path."""

    def do_GET(self):
        body = self.server.content.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # keep test output clean


@pytest.fixture(scope="module")
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ContentHandler)
    server.content = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def serve(server, body):
    """Register ``body`` under a fresh path; return its localhost URL."""
    path = "/content-{0}".format(next(_path_counter))
    server.content[path] = body
    return "http://127.0.0.1:{0}{1}".format(server.server_address[1], path)


# ---------------------------------------------------------------------------
# Feature: custom-python-source, Property 8: HTTP(S) fetches round-trip
# content
#
# For any image served losslessly over a local HTTP endpoint,
# dda_frames.load_image(url) returns the image's exact decoded pixels as a
# uint8 BGR (or 2-D grayscale) array; and for any byte payload served over
# HTTP or written to a local path, dda_frames.load_bytes(source) returns
# exactly those bytes undecoded.
#
# **Validates: Requirements 4.1, 4.3**
# ---------------------------------------------------------------------------


@given(data=st.data())
def test_property_8_http_fetches_round_trip_content(http_server, data):
    """**Feature: custom-python-source, Property 8: HTTP(S) fetches
    round-trip content**

    **Validates: Requirements 4.1, 4.3**
    """
    helpers = load_helpers()

    # load_image over HTTP: PNG (lossless) round-trips to the exact
    # decoded pixels — BGR for color, 2-D for grayscale (Req 4.1).
    height = data.draw(st.integers(min_value=1, max_value=32))
    width = data.draw(st.integers(min_value=1, max_value=32))
    grayscale = data.draw(st.booleans())
    shape = (height, width) if grayscale else (height, width, 3)
    array = data.draw(hnp.arrays(np.uint8, shape))
    image_url = serve(http_server, encode_png(array))
    loaded = helpers.load_image(image_url)
    assert loaded.dtype == np.uint8
    assert np.array_equal(loaded, array)

    # load_bytes over HTTP: exactly the served bytes, undecoded (Req 4.3).
    payload = data.draw(st.binary(min_size=0, max_size=512))
    bytes_url = serve(http_server, payload)
    assert helpers.load_bytes(bytes_url) == payload

    # load_bytes of a local path: exactly the written bytes (Req 4.3).
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = os.path.join(tmp_dir, "payload.bin")
        with open(path, "wb") as f:
            f.write(payload)
        assert helpers.load_bytes(path) == payload
