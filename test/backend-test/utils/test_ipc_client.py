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
"""Unit tests for the process-wide shared Greengrass IPC client (DD-19576).

The shared client exists to eliminate the connect()/close() churn and
GC-timed client finalization that tripped the aws-c-event-stream
"Continuation ref count has gone negative" fatal abort (backend exit 255).
These tests pin the guarantees that prevent that regression. ``get_ipc_client()``
returns one stable handle (rtsp-rtmp-stream-cameras finding 24), so the client
behind it is checked by identity through ``current_client()``:

- exactly one connection is created and reused (no churn);
- concurrent first callers still create only one connection;
- reset forces a single reconnect;
- the retry helper retries once, on the shared reconnect, when the connection
  was closed (``ConnectionClosedError``), and a second one propagates;
- any other error propagates after one attempt, with no reconnect.
"""
import threading
import time
import unittest
from concurrent.futures import Future
from unittest.mock import patch, MagicMock

from awsiot.eventstreamrpc import ConnectionClosedError

import utils.ipc_client as ipc_client_module
from utils.ipc_client import (
    get_ipc_client,
    reset_ipc_client,
    call_with_ipc_retry,
    current_client,
)

#: The reconnect timing, in milliseconds.
FAST_TIMINGS = {"CONNECT_TIMEOUT_S": 0.5, "WATCHDOG_INTERVAL_S": 0.02,
                "RECONNECT_BACKOFF_S": (0.01, 0.02, 0.04, 0.08, 0.1), "RETRY_WAIT_S": 2.0}


def _acknowledged():
    """A reconnect attempt's connect future, acknowledged at once."""
    future = Future()
    future.set_result(None)
    return future


class TestSharedIpcClient(unittest.TestCase):
    def setUp(self):
        # Each test starts with no cached connection.
        reset_ipc_client()
        timings = patch.multiple(ipc_client_module, **FAST_TIMINGS)
        timings.start()
        self.addCleanup(timings.stop)

    def tearDown(self):
        reset_ipc_client()

    @patch("awsiot.greengrasscoreipc.connect")
    def test_single_connection_is_reused(self, mock_connect):
        """Many get_ipc_client() calls connect exactly once and hand back the
        same handle over the same client — this is the whole point: no
        per-call connection churn."""
        sentinel = MagicMock(name="ipc-client")
        mock_connect.return_value = sentinel

        handles = [get_ipc_client() for _ in range(50)]

        self.assertTrue(all(h is handles[0] for h in handles))
        self.assertIs(current_client(), sentinel)
        mock_connect.assert_called_once()

    @patch("awsiot.greengrasscoreipc.connect")
    def test_reset_forces_single_reconnect(self, mock_connect):
        """reset_ipc_client() drops the cache so the next call reconnects
        once, then that new client is reused again; the old one is never
        closed (N1)."""
        first, second = MagicMock(name="first"), MagicMock(name="second")
        mock_connect.side_effect = [first, second]

        get_ipc_client()
        self.assertIs(current_client(), first)
        get_ipc_client()
        self.assertIs(current_client(), first)  # still cached
        reset_ipc_client()
        self.assertIsNone(current_client())
        get_ipc_client()
        self.assertIs(current_client(), second)  # reconnected
        get_ipc_client()
        self.assertIs(current_client(), second)  # cached again
        self.assertEqual(mock_connect.call_count, 2)
        first.close.assert_not_called()

    @patch("awsiot.greengrasscoreipc.connect")
    def test_concurrent_first_callers_create_one_client(self, mock_connect):
        """Under a thundering herd of first callers, the lock ensures a single
        connection is created and every thread observes the same client."""
        # A slow connect widens the race window for the double-checked lock.
        created = MagicMock(name="ipc-client")

        def slow_connect(**kwargs):
            time.sleep(0.02)
            return created

        mock_connect.side_effect = slow_connect

        results = []
        results_lock = threading.Lock()

        def worker():
            get_ipc_client()
            with results_lock:
                results.append(current_client())

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 20)
        self.assertTrue(all(c is created for c in results))
        mock_connect.assert_called_once()

    @patch("awsiot.greengrasscoreipc.connect")
    def test_call_with_ipc_retry_reconnects_once_on_failure(self, mock_connect):
        """A closed connection (ConnectionClosedError) makes the shared
        reconnect thread open one new connection, and the operation is retried
        once on it, so one bad connection does not wedge the caller (and no
        per-call churn is reintroduced). The broken client is never closed."""
        broken, healthy = MagicMock(name="broken"), MagicMock(name="healthy")
        broken.new_get_thing_shadow.side_effect = ConnectionClosedError()
        mock_connect.return_value = broken
        opened = []

        def open_connection(lifecycle):
            opened.append(lifecycle)
            return healthy, _acknowledged()

        calls = []

        def operation(client):
            calls.append(current_client())
            client.new_get_thing_shadow()
            return "ok"

        with patch.object(ipc_client_module, "_open_connection", open_connection):
            result = call_with_ipc_retry(operation)

        self.assertEqual(result, "ok")
        self.assertEqual(calls, [broken, healthy])
        mock_connect.assert_called_once()
        self.assertEqual(len(opened), 1)
        broken.close.assert_not_called()

    @patch("awsiot.greengrasscoreipc.connect")
    def test_call_with_ipc_retry_propagates_second_failure(self, mock_connect):
        """If the retry finds its connection closed too, that second error
        propagates rather than looping — the caller decides what to do. It is
        reported for the connection it used, so a second reconnect follows."""
        first, second, third = (MagicMock(name="c1"), MagicMock(name="c2"),
                                MagicMock(name="c3"))
        mock_connect.return_value = first
        replacements = [second, third]

        def open_connection(lifecycle):
            return replacements.pop(0), _acknowledged()

        attempts = []

        def always_closed(client):
            attempts.append(current_client())
            raise ConnectionClosedError("still closed")

        with patch.object(ipc_client_module, "_open_connection", open_connection):
            get_ipc_client()
            g0 = ipc_client_module.generation()
            with self.assertRaisesRegex(ConnectionClosedError, "still closed"):
                call_with_ipc_retry(always_closed)
            self.assertTrue(ipc_client_module.wait_for_new_connection(g0 + 1, 2.0))

        self.assertEqual(attempts, [first, second])
        self.assertIs(current_client(), third)
        mock_connect.assert_called_once()

    @patch("awsiot.greengrasscoreipc.connect")
    def test_call_with_ipc_retry_does_not_retry_other_errors(self, mock_connect):
        """A RuntimeError that is not a ConnectionClosedError says nothing
        about the connection: it propagates after one attempt, with no
        reconnect (R5)."""
        mock_connect.return_value = MagicMock(name="c1")
        opened = []
        attempts = []

        def always_fails(client):
            attempts.append(current_client())
            raise RuntimeError("still broken")

        with patch.object(ipc_client_module, "_open_connection",
                          lambda lifecycle: opened.append(lifecycle)):
            with self.assertRaisesRegex(RuntimeError, "still broken"):
                call_with_ipc_retry(always_fails)

        self.assertEqual(len(attempts), 1)
        mock_connect.assert_called_once()
        self.assertEqual(opened, [])
        self.assertTrue(ipc_client_module.connection_usable())


if __name__ == "__main__":
    unittest.main()
