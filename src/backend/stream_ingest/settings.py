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
"""Device-level stream limits (rtsp-rtmp-stream-cameras Requirements 8.9,
12.2, 12.4; design component 11, ``settings.py``).

Backed by the ``device_settings`` table of the configuration database. An
absent, unreadable or out-of-range value means the default, so a bad row can
never switch a limit off. Values are cached for a few seconds: the session
limit is read on every new session, and the retention caps on every
housekeeping pass.
"""
from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_SESSIONS = "streamIngest.maxSessions"
RETENTION_BYTES = "continuous.retentionBytes"
STAGING_BYTES = "continuous.stagingBytes"

_MIB = 1024 * 1024
_GIB = 1024 * _MIB

#: key -> (default, minimum, maximum)
LIMITS: Dict[str, Tuple[int, int, int]] = {
    MAX_SESSIONS: (4, 1, 16),
    RETENTION_BYTES: (2 * _GIB, 64 * _MIB, 1024 * _GIB),
    STAGING_BYTES: (256 * _MIB, 16 * _MIB, 4 * _GIB),
}

CACHE_TTL_S = 5.0


@dataclass(frozen=True)
class Limits:
    max_sessions: int
    retention_bytes: int
    staging_bytes: int


def default_limits() -> Limits:
    return Limits(LIMITS[MAX_SESSIONS][0], LIMITS[RETENTION_BYTES][0], LIMITS[STAGING_BYTES][0])


def validate(key: str, value: Any) -> int:
    """``value`` as a valid ``key`` limit; ValueError naming the range."""
    if key not in LIMITS:
        raise ValueError(f"{key} is not a device setting")
    _default, minimum, maximum = LIMITS[key]
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be a whole number from {minimum} to {maximum}")
    return value


class DeviceSettings:
    """The device limits, read from the configuration database."""

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None,
                 clock: Callable[[], float] = time.monotonic, ttl_s: float = CACHE_TTL_S):
        self._session_factory = session_factory
        self._clock = clock
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._cached: Optional[Limits] = None
        self._cached_at = 0.0
        self._warned = set()

    def _sessions(self):
        if self._session_factory is None:
            from dao.sqlite_db.sqlite_db_operations import SessionLocal
            self._session_factory = SessionLocal
        return self._session_factory()

    def _read(self) -> Dict[str, Any]:
        from stream_ingest.models import DeviceSetting

        with self._sessions() as db:
            rows = db.query(DeviceSetting).filter(DeviceSetting.key.in_(tuple(LIMITS))).all()
            return {row.key: row.value for row in rows}

    def _resolved(self, stored: Dict[str, Any], key: str) -> int:
        if key not in stored or stored[key] is None:
            return LIMITS[key][0]
        try:
            return validate(key, stored[key])
        except ValueError as error:
            if key not in self._warned:
                self._warned.add(key)
                logger.warning("Ignoring device setting %s: %s; using the default", key, error)
            return LIMITS[key][0]

    def limits(self) -> Limits:
        """The current limits (cached for a few seconds)."""
        with self._lock:
            if self._cached is not None and self._clock() - self._cached_at < self._ttl_s:
                return self._cached
            try:
                stored = self._read()
            except Exception as error:  # noqa: BLE001 - defaults keep every limit on
                if "read" not in self._warned:
                    self._warned.add("read")
                    logger.warning("Device settings could not be read (%s); using the defaults",
                                   type(error).__name__)
                stored = {}
            self._cached = Limits(self._resolved(stored, MAX_SESSIONS),
                                  self._resolved(stored, RETENTION_BYTES),
                                  self._resolved(stored, STAGING_BYTES))
            self._cached_at = self._clock()
            return self._cached

    def set(self, key: str, value: Optional[int]) -> None:
        """Store ``value`` for ``key``; None restores the default."""
        from stream_ingest.models import DeviceSetting

        if value is not None:
            value = validate(key, value)
        elif key not in LIMITS:
            raise ValueError(f"{key} is not a device setting")
        with self._sessions() as db:
            row = db.get(DeviceSetting, key)
            if value is None:
                if row is not None:
                    db.delete(row)
            elif row is None:
                db.add(DeviceSetting(key=key, value=value, updated_at=int(time.time() * 1000)))
            else:
                row.value, row.updated_at = value, int(time.time() * 1000)
            db.commit()
        with self._lock:
            self._cached = None


_settings: Optional[DeviceSettings] = None
_settings_lock = threading.Lock()


def get_device_settings() -> DeviceSettings:
    global _settings
    with _settings_lock:
        if _settings is None:
            _settings = DeviceSettings()
        return _settings


def set_device_settings(settings: Optional[DeviceSettings]) -> None:
    global _settings
    with _settings_lock:
        _settings = settings
