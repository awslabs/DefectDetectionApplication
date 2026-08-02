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
These tests pin the guarantees that prevent that regression:

- exactly one connection is created and reused (no churn);
- concurrent first callers still create only one connection;
- reset forces a single reconnect;
- the retry helper reconnects once on a transient failure.
"""
import threading
import unittest
from unittest.mock import patch, MagicMock

import utils.ipc_client as ipc_client_module
from utils.ipc_client import (
    get_ipc_client,
    reset_ipc_client,
    call_with_ipc_retry,
)


class TestSharedIpcClient(unittest.TestCase):
    def setUp(self):
        # Each test starts with no cached connection.
        reset_ipc_client()

    def tearDown(self):
        reset_ipc_client()

    @patch("awsiot.greengrasscoreipc.connect")
    def test_single_connection_is_reused(self, mock_connect):
        """Many get_ipc_client() calls connect exactly once and hand back the
        same client — this is the whole point: no per-call connection churn."""
        sentinel = MagicMock(name="ipc-client")
        mock_connect.return_value = sentinel

        clients = [get_ipc_client() for _ in range(50)]

        self.assertTrue(all(c is sentinel for c in clients))
        mock_connect.assert_called_once()

    @patch("awsiot.greengrasscoreipc.connect")
    def test_reset_forces_single_reconnect(self, mock_connect):
        """reset_ipc_client() drops the cache so the next call reconnects
        once, then that new client is reused again."""
        first, second = MagicMock(name="first"), MagicMock(name="second")
        mock_connect.side_effect = [first, second]

        self.assertIs(get_ipc_client(), first)
        self.assertIs(get_ipc_client(), first)  # still cached
        reset_ipc_client()
        self.assertIs(get_ipc_client(), second)  # reconnected
        self.assertIs(get_ipc_client(), second)  # cached again
        self.assertEqual(mock_connect.call_count, 2)

    @patch("awsiot.greengrasscoreipc.connect")
    def test_concurrent_first_callers_create_one_client(self, mock_connect):
        """Under a thundering herd of first callers, the lock ensures a single
        connection is created and every thread observes the same client."""
        # A slow connect widens the race window for the double-checked lock.
        created = MagicMock(name="ipc-client")

        def slow_connect():
            import time
            time.sleep(0.02)
            return created

        mock_connect.side_effect = slow_connect

        results = []
        results_lock = threading.Lock()

        def worker():
            client = get_ipc_client()
            with results_lock:
                results.append(client)

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
        """A transient IPC failure resets the shared client and retries once
        against a fresh connection, so one bad connection does not wedge the
        caller (and no per-call churn is reintroduced)."""
        broken, healthy = MagicMock(name="broken"), MagicMock(name="healthy")
        mock_connect.side_effect = [broken, healthy]

        calls = []

        def operation(client):
            calls.append(client)
            if client is broken:
                raise RuntimeError("socket is closed")
            return "ok"

        result = call_with_ipc_retry(operation)

        self.assertEqual(result, "ok")
        self.assertEqual(calls, [broken, healthy])
        self.assertEqual(mock_connect.call_count, 2)

    @patch("awsiot.greengrasscoreipc.connect")
    def test_call_with_ipc_retry_propagates_second_failure(self, mock_connect):
        """If the reconnect also fails, the second error propagates rather
        than looping — the caller decides what to do."""
        mock_connect.side_effect = [MagicMock(name="c1"), MagicMock(name="c2")]

        def always_fails(client):
            raise RuntimeError("still broken")

        with self.assertRaises(RuntimeError):
            call_with_ipc_retry(always_fails)
        self.assertEqual(mock_connect.call_count, 2)


if __name__ == "__main__":
    unittest.main()
