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
"""Stream camera reporting for the Edge_Sync_Agent (rtsp-rtmp-stream-cameras
Requirements 4.5, 4.6, 5.5, 16.5; design component 12).

- :class:`StreamReportDebouncer` keeps the reported ``capabilities.stream``
  of each stream camera. A change of its coarse state, codec, resolution or
  decoder is published at most once per camera per 30 s: the first change
  after a quiet window at once, later ones when the window ends, the newest
  winning. Every report reads the published value, so a report triggered
  by anything else cannot publish a change early either.
- :func:`stream_ingest_section` is the ``deviceCapabilities.streamIngest``
  section: the Device_Stream_Capabilities in the shape the Portal stores.
- :func:`stream_change_parts` splits a Portal change to a stream camera
  into the Image_Source data and its credential intent.
"""
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from camera_sync.inventory import (
    STREAM_CREDENTIAL_REF,
    STREAM_CREDENTIALS_CONFIGURED,
    STREAM_CREDENTIALS_UPDATED_AT,
    STREAM_REPORTED_SETTINGS,
    stream_capabilities,
)

#: Requirement 4.6: at most one capability re-report per camera per 30 s.
STREAM_REPORT_INTERVAL_S = 30.0

STREAM_TYPES = tuple(STREAM_REPORTED_SETTINGS)


def _default_timer(delay_s: float, action: Callable[[], None]) -> None:
    timer = threading.Timer(delay_s, action)
    timer.daemon = True
    timer.start()


class StreamReportDebouncer:
    """The published ``capabilities`` of each stream camera (see the module
    docstring). ``on_publish()`` is called whenever a deferred change
    becomes due, so the agent can report it."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 interval_s: float = STREAM_REPORT_INTERVAL_S,
                 timer: Callable[[float, Callable[[], None]], None] = _default_timer,
                 on_publish: Optional[Callable[[], None]] = None):
        self._clock = clock
        self._interval_s = interval_s
        self._timer = timer
        self.on_publish = on_publish
        self._lock = threading.Lock()
        self._published: Dict[str, Dict[str, Any]] = {}
        self._published_at: Dict[str, float] = {}
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._timer_due: Optional[float] = None

    def offer(self, image_source_id: str, health: Optional[Mapping[str, Any]]) -> bool:
        """A camera's health changed. True when its reported capabilities
        changed now; a change within the window is deferred instead."""
        projection = stream_capabilities(health)
        key = str(image_source_id)
        schedule_in = None
        with self._lock:
            if self._published.get(key) == projection:
                self._pending.pop(key, None)
                return False
            now = self._clock()
            last = self._published_at.get(key)
            if last is None or now - last >= self._interval_s:
                self._published[key], self._published_at[key] = projection, now
                self._pending.pop(key, None)
                return True
            self._pending[key] = projection
            due = last + self._interval_s
            if self._timer_due is None or due < self._timer_due:
                self._timer_due = due
                schedule_in = max(0.0, due - now)
        if schedule_in is not None:
            self._timer(schedule_in, self.flush)
        return False

    def flush(self) -> bool:
        """Publish the deferred changes whose window ended; True when any
        was. Reschedules itself for the ones still waiting."""
        published = False
        schedule_in = None
        with self._lock:
            now = self._clock()
            self._timer_due = None
            for key, projection in list(self._pending.items()):
                if now - self._published_at.get(key, now - self._interval_s) >= self._interval_s:
                    del self._pending[key]
                    if self._published.get(key) != projection:
                        self._published[key], self._published_at[key] = projection, now
                        published = True
            if self._pending:
                due = min(self._published_at[key] + self._interval_s for key in self._pending)
                self._timer_due = due
                schedule_in = max(0.0, due - now)
        if schedule_in is not None:
            self._timer(schedule_in, self.flush)
        if published and self.on_publish is not None:
            self.on_publish()
        return published

    def published(self, image_source_id: str, live_health: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        """The capabilities to report for a camera. A camera reported for
        the first time takes its live value, which starts its window."""
        key = str(image_source_id)
        with self._lock:
            if key not in self._published:
                self._published[key] = stream_capabilities(live_health)
                self._published_at[key] = self._clock()
            return dict(self._published[key])

    def forget_except(self, image_source_ids) -> None:
        """Drop the cameras that no longer exist."""
        keep = {str(value) for value in image_source_ids}
        with self._lock:
            for store in (self._published, self._published_at, self._pending):
                for key in [key for key in store if key not in keep]:
                    del store[key]


_CAPABILITY_FLAGS = ("rtsp", "rtmp", "tls")
_CAPABILITY_VERSIONS = ("gstreamer", "pyav", "ffmpeg")
_MAX_TEXT = 64


def stream_ingest_section(capabilities: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """``deviceCapabilities.streamIngest``: the probed capabilities in the
    shape the Portal keeps (flags, per-codec decoder elements, versions,
    probe time). None while the probe has not finished."""
    if not isinstance(capabilities, Mapping):
        return None
    if capabilities.get("probeError") == "the capability probe has not finished":
        return None
    section: Dict[str, Any] = {flag: bool(capabilities.get(flag)) for flag in _CAPABILITY_FLAGS}
    for key in _CAPABILITY_VERSIONS:
        value = capabilities.get(key)
        if isinstance(value, str) and value:
            section[key] = value[:_MAX_TEXT]
    codecs = {}
    for codec, entry in sorted((capabilities.get("codecs") or {}).items()):
        if not isinstance(entry, Mapping):
            continue
        codecs[str(codec)] = {kind: element[:_MAX_TEXT] for kind, element in sorted(entry.items())
                              if kind in ("hardware", "software") and isinstance(element, str) and element}
    section["codecs"] = codecs
    probed = capabilities.get("probedAtMs")
    if isinstance(probed, int) and not isinstance(probed, bool):
        section["probedAtMs"] = probed
    return section


def is_stream_change(change: Mapping[str, Any]) -> bool:
    return isinstance(change, Mapping) and change.get("type") in STREAM_TYPES


def stream_change_parts(change: Mapping[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], bool, Optional[int]]:
    """``(data, credential_ref, clear, credentials_updated_at)`` of a
    Portal change to a stream camera.

    ``data`` is what the Image_Source accessor takes: ``name``, ``type``,
    ``location`` (the ``url`` param) and ``streamSettings``, which lists
    every setting of the type, a missing one as None so it returns to its
    default (the Portal leaves unset settings out). ``credential_ref`` is
    the delivered Credential_Reference; ``clear`` is a delivered
    ``credentialsConfigured: false`` without one. The inverse of the
    inventory projection (``camera_sync.inventory.stream_params``).
    """
    params = change.get("params") if isinstance(change.get("params"), Mapping) else {}
    data: Dict[str, Any] = {}
    if change.get("name") is not None:
        data["name"] = change["name"]
    data["type"] = change.get("type")
    if params.get("url") is not None:
        data["location"] = params["url"]
    data["streamSettings"] = {name: params.get(name) for name in STREAM_REPORTED_SETTINGS[change["type"]]}
    reference = params.get(STREAM_CREDENTIAL_REF)
    reference = dict(reference) if isinstance(reference, Mapping) and reference else None
    clear = reference is None and params.get(STREAM_CREDENTIALS_CONFIGURED) is False
    updated_at = params.get(STREAM_CREDENTIALS_UPDATED_AT)
    if not isinstance(updated_at, int) or isinstance(updated_at, bool):
        updated_at = None
    return data, reference, clear, updated_at
