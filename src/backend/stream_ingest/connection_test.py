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
"""The connection test of a stream Image_Source (rtsp-rtmp-stream-cameras
Requirements 4.3, 16.1; design components 10 and 11).

The test holds a lease on the camera's shared session for at most 18 s, so
the route answers within 20 s, and reports one of:

- success, with the first frame, as soon as one arrives;
- the failure category and redacted message of the first failure that
  happens after the test started (an authentication failure is reported
  within a second or two, not after the full wait);
- ``timeout`` when neither happens in time;
- ``session_limit`` when the device runs its maximum of sessions.

A session that is only waiting to retry (a configuration-class failure's
5-minute wait, or a backoff delay) is restarted for the test: the operator
asked for an attempt now. A session that is connecting or streaming is left
alone, so a test never interrupts a running workflow.
"""
from dataclasses import dataclass
import time
from typing import Any, Callable, Dict, Optional

from stream_ingest import health
from stream_ingest.health import StreamError

#: The test's own budget; the route answers within 20 s.
TEST_BUDGET_S = 18.0
POLL_MS = 250


@dataclass
class ConnectionTestResult:
    ok: bool
    category: Optional[str]
    message: str
    health: Dict[str, Any]
    frame: Any = None


def run_connection_test(manager, camera_key: str, holder: str = "connection-test",
                        budget_s: float = TEST_BUDGET_S, clock: Callable[[], float] = time.monotonic,
                        wall_clock: Callable[[], float] = time.time, poll_ms: int = POLL_MS,
                        source: Any = None) -> ConnectionTestResult:
    """Test ``camera_key`` through ``manager`` (see the module docstring)."""
    started_ms = int(wall_clock() * 1000)
    try:
        lease = manager.acquire_lease(camera_key, holder, source=source)
    except StreamError as error:
        return ConnectionTestResult(False, error.category, error.message,
                                    manager.health(camera_key) or health.empty_health(camera_key))
    try:
        # Only a frame delivered after the test started proves the camera
        # streams now: the session's cached frame can predate an outage.
        baseline_seq = manager.newest_seq(camera_key)
        document = manager.health(camera_key) or {}
        waiting = document.get("nextAttemptInS")
        if waiting is not None and waiting > 0.5:
            session = manager.session(camera_key)
            if session is not None:
                session.restart("connection test", configuration_changed=False)
        deadline = clock() + budget_s
        while True:
            remaining_ms = max(0, int((deadline - clock()) * 1000))
            frame = manager.latest_frame(camera_key, after_seq=baseline_seq,
                                         wait_ms=min(poll_ms, remaining_ms))
            document = manager.health(camera_key) or health.empty_health(camera_key)
            if frame is not None:
                return ConnectionTestResult(True, None, "Connected: the camera is streaming", document, frame)
            error = document.get("lastError") or {}
            if error.get("category") and int(error.get("atMs") or 0) >= started_ms:
                return ConnectionTestResult(False, error["category"], error.get("message") or "", document)
            if clock() >= deadline:
                return ConnectionTestResult(False, health.TIMEOUT,
                                            f"no frame arrived within {budget_s:g} s", document)
    finally:
        manager.release_lease(lease)
