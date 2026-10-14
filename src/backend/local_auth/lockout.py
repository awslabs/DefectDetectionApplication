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
"""Per-account Local_Login lockout tracking (Requirement 8.10).

``LockoutTracker`` keeps an in-memory consecutive-failure counter per
username. After 5 consecutive failed login attempts the account is locked
for 15 minutes; while the lock window is active every attempt is rejected
regardless of the credentials submitted and does not extend the window.

Design points (design decision D9):

- **In-memory only**: the LocalServer is a single process and the
  requirement does not demand persistence across restarts, so state lives
  in a plain dict guarded by a lock (login endpoints may run on multiple
  worker threads).
- **Injected clock**: the tracker takes a ``clock`` callable (defaults to
  ``time.time``) and every method also accepts an explicit ``now`` so
  tests are fully deterministic. No network or file I/O anywhere.
- **Frozen window (8.10)**: once locked, further ``record_failure`` /
  ``record_success`` calls are no-ops until the window elapses — attempts
  during the window neither extend the lock nor clear it. When the window
  elapses the state resets, so a subsequent correct-credential attempt
  succeeds and a fresh streak of 5 failures is required to lock again.

Intended call pattern from the login endpoint (task 9.1)::

    if tracker.is_locked(username):
        reject  # uniform 401, before any credential verification
    elif verify_credentials(username, password):
        tracker.record_success(username)
    else:
        tracker.record_failure(username)
"""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: Consecutive failed attempts that trigger a lockout (8.10).
MAX_CONSECUTIVE_FAILURES = 5

#: Lockout window length in seconds: 15 minutes (8.10).
LOCKOUT_DURATION_SECONDS = 15 * 60


@dataclass
class _AccountState:
    """Mutable per-username lockout state."""

    failures: int = 0
    locked_until: Optional[float] = None


class LockoutTracker:
    """In-memory per-username consecutive-failure lockout (8.10, D9)."""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        max_failures: int = MAX_CONSECUTIVE_FAILURES,
        lockout_seconds: float = LOCKOUT_DURATION_SECONDS,
    ):
        self._clock = clock
        self._max_failures = max_failures
        self._lockout_seconds = lockout_seconds
        self._states: Dict[str, _AccountState] = {}
        self._lock = threading.Lock()

    def is_locked(self, username: str, now: Optional[float] = None) -> bool:
        """Whether ``username`` is inside an active lockout window.

        An elapsed window resets the account's state, so the next failure
        streak starts from zero (a correct-credential attempt after the
        window succeeds).
        """
        with self._lock:
            return self._is_locked_locked(username, self._now(now))

    def record_failure(self, username: str, now: Optional[float] = None) -> None:
        """Record a failed login attempt for ``username``.

        The 5th consecutive failure starts the 15-minute lock window.
        Attempts while already locked are no-ops: they neither extend the
        window nor accumulate (8.10).
        """
        current = self._now(now)
        with self._lock:
            if self._is_locked_locked(username, current):
                return
            state = self._states.setdefault(username, _AccountState())
            state.failures += 1
            if state.failures >= self._max_failures:
                state.locked_until = current + self._lockout_seconds
                logger.warning(
                    "Local login lockout: account %s locked for %d seconds "
                    "after %d consecutive failed attempts",
                    username,
                    int(self._lockout_seconds),
                    state.failures,
                )

    def record_success(self, username: str, now: Optional[float] = None) -> None:
        """Record a successful login: resets the consecutive-failure count.

        A no-op while the account is locked — during the window every
        attempt is rejected regardless of credentials, so a success cannot
        clear an active lock (8.10).
        """
        current = self._now(now)
        with self._lock:
            if self._is_locked_locked(username, current):
                return
            self._states.pop(username, None)

    def _now(self, now: Optional[float]) -> float:
        return self._clock() if now is None else now

    def _is_locked_locked(self, username: str, now: float) -> bool:
        """Lock-held check; resets and prunes state once the window elapses."""
        state = self._states.get(username)
        if state is None or state.locked_until is None:
            return False
        if now < state.locked_until:
            return True
        del self._states[username]
        return False


# Module-level default instance shared by the login endpoint.
_default_tracker = LockoutTracker()


def get_default_tracker() -> LockoutTracker:
    return _default_tracker
