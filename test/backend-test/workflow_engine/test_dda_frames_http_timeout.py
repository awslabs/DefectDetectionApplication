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
"""Example tests for the bounded HTTP timeout in ``dda_frames``
(Requirement 4.4).

An unresponsive localhost endpoint (accepts connections, never
responds) must make ``load_bytes``/``load_image`` raise a ValueError
naming the source within the timeout bound — an unresponsive endpoint
cannot hold the Frame_Producer open past its wall-clock limit.
"""
import socket
import threading
import time
import types

import pytest

from workflow_engine.python_bridge import HELPERS_SOURCE

#: Small timeout override so the test completes quickly; the helper
#: reads the module-level HTTP_TIMEOUT_SEC at call time.
_TEST_TIMEOUT_SEC = 0.5
#: Generous wall-clock ceiling proving the fetch is bounded by the
#: timeout rather than hanging indefinitely.
_ELAPSED_CEILING_SEC = 10.0


def load_helpers():
    module = types.ModuleType("dda_frames")
    exec(HELPERS_SOURCE, module.__dict__)
    return module


@pytest.fixture()
def unresponsive_endpoint():
    """A localhost socket that accepts connections and never responds."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    connections = []
    stop = threading.Event()

    def accept_forever():
        while not stop.is_set():
            try:
                server.settimeout(0.2)
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            connections.append(conn)  # hold open, never respond

    thread = threading.Thread(target=accept_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{0}/never-answers".format(
            server.getsockname()[1]
        )
    finally:
        stop.set()
        thread.join(timeout=5)
        for conn in connections:
            try:
                conn.close()
            except OSError:
                pass
        server.close()


def test_http_timeout_default_is_bounded():
    """The default timeout is a bounded constant (Req 4.4)."""
    helpers = load_helpers()
    assert helpers.HTTP_TIMEOUT_SEC == 20.0


def test_load_bytes_times_out_naming_the_source(unresponsive_endpoint):
    """load_bytes raises within the timeout bound, naming the source."""
    helpers = load_helpers()
    helpers.HTTP_TIMEOUT_SEC = _TEST_TIMEOUT_SEC
    started = time.monotonic()
    with pytest.raises(ValueError) as error:
        helpers.load_bytes(unresponsive_endpoint)
    elapsed = time.monotonic() - started
    assert unresponsive_endpoint in str(error.value)
    assert elapsed < _ELAPSED_CEILING_SEC


def test_load_image_times_out_naming_the_source(unresponsive_endpoint):
    """load_image raises within the timeout bound, naming the source."""
    helpers = load_helpers()
    helpers.HTTP_TIMEOUT_SEC = _TEST_TIMEOUT_SEC
    started = time.monotonic()
    with pytest.raises(ValueError) as error:
        helpers.load_image(unresponsive_endpoint)
    elapsed = time.monotonic() - started
    assert unresponsive_endpoint in str(error.value)
    assert elapsed < _ELAPSED_CEILING_SEC
