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

"""The per-Pair session state machine (workflow-debug-mode design §3, §5.1
``sessions.py``, §5.3).

A :class:`SessionState` per workflow id on this device: the validated shadow
entry, ``state`` (``pending -> active | no_registration | rejected -> expired
| stopped``), the counters, the open-run set and the batcher the agent hands
it on activation. :class:`SessionManager.apply_desired` receives the
**complete** ``sessions`` map of the ``dda-workflow-debug`` shadow: a new
valid entry becomes ``pending``, a changed entry of the same session updates
in place, a different session id under the same key ends the old session and
starts the new one, an invalid entry is ``rejected`` with its reason, a key
absent from the map ends its session (``stopped``), an unchanged entry is a
no-op. ``pending -> active`` is decided from the shared ``RegistrationView``
snapshot (a ``registered`` row whose ``workflow_id`` is the key) and
re-evaluated on every tick, flipping ``active <-> no_registration`` as
registrations come and go (``no_registration`` is reported 30 s after an
entry was applied without a row). Expiry is checked every tick against
``expiresAt``, the absolute stamp the Portal wrote from ``enabledAt``.

Transitions are returned by :meth:`SessionManager.tick` and
:meth:`SessionManager.apply_desired` as :class:`Transition` events; the agent
turns them into ``session`` records, batchers and tap attachment.
"""

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from workflow_engine.telemetry import topics

logger = logging.getLogger(__name__)

__all__ = ["SessionState", "SessionManager", "Transition", "validate_entry",
           "STATES", "COUNTER_KEYS"]

PENDING = "pending"
ACTIVE = "active"
NO_REGISTRATION = "no_registration"
REJECTED = "rejected"
EXPIRED = "expired"
STOPPED = "stopped"
STATES = (PENDING, ACTIVE, NO_REGISTRATION, REJECTED, EXPIRED, STOPPED)
TERMINAL_STATES = (EXPIRED, STOPPED)
LIVE_STATES = (PENDING, ACTIVE, NO_REGISTRATION)

#: ``no_registration`` is reported this long after an entry was applied
#: without a ``registered`` row (seconds).
NO_REGISTRATION_AFTER_S = 30.0
#: Clock-skew allowance on ``enabledAt`` (milliseconds).
ENABLED_AT_SKEW_MS = 5 * 60 * 1000
#: The longest session, plus the skew allowance (milliseconds).
MAX_SESSION_MS = 24 * 60 * 60 * 1000 + ENABLED_AT_SKEW_MS

DEFAULT_IMAGE_MAX_KB = 150
DEFAULT_MAX_MESSAGES_PER_SECOND = 10
DEFAULT_MAX_BYTES_PER_SECOND = 262144

COUNTER_KEYS = ("records", "bytes", "queueDrops", "tapDrops", "truncations", "rateDeferred",
                "bytesDeferred", "spoolFull", "imagesSent", "imagesCapped", "imagesTooLarge",
                "publishFailures", "unattributedLines", "otherPairLines")

_REGION_RE = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str) or not _UUID_RE.match(value):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int_in_range(entry: dict, key: str, low: int, high: int, default: int,
                  reason: str) -> Optional[str]:
    value = entry.get(key, default)
    if value is None:
        value = default
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not _is_int(value) or not low <= value <= high:
        return reason
    entry[key] = value
    return None


def validate_entry(workflow_id: str, raw: Any, thing_name: str, now_ms: int) -> Any:
    """The normalized entry (a new dict with the defaults filled in) or the
    rejection reason string (§3)."""
    if not isinstance(raw, dict):
        return "not_an_object"
    entry = dict(raw)
    if not _is_uuid(entry.get("sessionId")):
        return "invalid_session_id"
    if not _is_uuid(entry.get("usecaseId")):
        return "invalid_usecase_id"
    enabled_at = entry.get("enabledAt")
    expires_at = entry.get("expiresAt")
    if not _is_int(enabled_at) or enabled_at > now_ms + ENABLED_AT_SKEW_MS:
        return "enabled_at_invalid"
    if not _is_int(expires_at) or expires_at <= enabled_at \
            or expires_at - enabled_at > MAX_SESSION_MS:
        return "expires_at_out_of_range"
    level = entry.get("level", "info")
    if level not in ("info", "debug"):
        return "invalid_level"
    entry["level"] = level
    images = entry.get("images", False)
    if not isinstance(images, bool):
        return "invalid_images"
    entry["images"] = images
    reason = _int_in_range(entry, "imageMaxKb", 50, 300, DEFAULT_IMAGE_MAX_KB,
                           "image_max_kb_out_of_range")
    if reason:
        return reason
    reason = _int_in_range(entry, "maxMessagesPerSecond", 1, 20, DEFAULT_MAX_MESSAGES_PER_SECOND,
                           "max_messages_per_second_out_of_range")
    if reason:
        return reason
    reason = _int_in_range(entry, "maxBytesPerSecond", 32768, 1048576, DEFAULT_MAX_BYTES_PER_SECOND,
                           "max_bytes_per_second_out_of_range")
    if reason:
        return reason
    region = entry.get("region")
    if region is not None or images:
        if not isinstance(region, str) or not _REGION_RE.match(region):
            return "invalid_region"
    bucket = entry.get("bucket")
    if bucket is not None or images:
        if not isinstance(bucket, str) or not bucket:
            return "invalid_bucket"
    expected_prefix = "dda-telemetry/debug/{0}/{1}/{2}/{3}/".format(
        entry["usecaseId"], thing_name, workflow_id, entry["sessionId"])
    if entry.get("prefix") != expected_prefix:
        return "invalid_prefix"
    return entry


@dataclass
class SessionState:
    """One Pair's session (see the module docstring)."""

    workflow_id: str
    entry: dict
    state: str
    applied_at_ms: int
    version: int
    reason: Optional[str] = None
    registration_id: Optional[str] = None
    workflow_version: Optional[str] = None
    pending_since_ms: Optional[int] = None
    counters: Dict[str, int] = field(default_factory=lambda: {k: 0 for k in COUNTER_KEYS})
    open_runs: set = field(default_factory=set)
    batcher: Any = None
    images_sent: int = 0
    last_heartbeat_ms: Optional[int] = None
    started_announced: bool = False
    topic: Optional[str] = None

    @property
    def session_id(self) -> Optional[str]:
        return self.entry.get("sessionId")

    @property
    def usecase_id(self) -> Optional[str]:
        return self.entry.get("usecaseId")

    @property
    def level(self) -> str:
        return self.entry.get("level", "info")

    @property
    def images(self) -> bool:
        return bool(self.entry.get("images", False))

    @property
    def enabled_at_ms(self) -> Optional[int]:
        return self.entry.get("enabledAt")

    @property
    def expires_at_ms(self) -> Optional[int]:
        return self.entry.get("expiresAt")

    @property
    def is_active(self) -> bool:
        return self.state == ACTIVE

    @property
    def is_live(self) -> bool:
        return self.state in LIVE_STATES

    def reported(self) -> dict:
        document = {
            "sessionId": self.session_id,
            "state": self.state,
            "appliedAt": self.applied_at_ms,
            "counters": dict(self.counters),
        }
        if self.reason:
            document["reason"] = self.reason
        return document


@dataclass(frozen=True)
class Transition:
    session: SessionState
    previous: Optional[str]
    state: str
    reason: str


class SessionManager:
    """See the module docstring.

    ``registry_view`` exposes ``snapshot() -> {registration_id:
    RegistrationInfo}``; ``clock`` is monotonic seconds, ``wall`` epoch
    seconds (both injectable); ``agent_started_ms`` decides whether a
    session's first record says ``started`` or ``resumed``.
    """

    def __init__(
        self,
        thing_name: str,
        registry_view: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        agent_started_ms: Optional[int] = None,
        debug_root: str = topics.DEBUG_ROOT,
    ) -> None:
        self.thing_name = str(thing_name)
        self._view = registry_view
        self._clock = clock
        self._wall = wall
        self.agent_started_ms = int(agent_started_ms if agent_started_ms is not None
                                    else wall() * 1000)
        self._debug_root = debug_root
        self.sessions: Dict[str, SessionState] = {}
        self._last_version: Optional[int] = None
        self._tombstones: set = set()
        self._rejections_logged: set = set()

    def now_ms(self) -> int:
        return int(self._wall() * 1000)

    # -- the shadow -----------------------------------------------------------------

    def apply_desired(self, sessions_map: Any, version: Any, v: Any = 1) -> List[Transition]:
        """Apply the complete ``sessions`` map (the shadow's ``desired.v`` in
        ``v``). Returns the transitions it caused."""
        if isinstance(version, int) and not isinstance(version, bool):
            if self._last_version is not None and version <= self._last_version:
                logger.debug("Debug sessions: shadow version %s not newer than %s; ignored",
                             version, self._last_version)
                return []
            self._last_version = version
        else:
            version = self._last_version if self._last_version is not None else 0
        if not isinstance(sessions_map, dict):
            sessions_map = {}
        now_ms = self.now_ms()
        transitions: List[Transition] = []

        for workflow_id, raw in sessions_map.items():
            if not isinstance(workflow_id, str) or not workflow_id:
                continue
            if raw is None:
                continue  # a null entry is a removal: handled below as absent
            if v != 1:
                normalized: Any = "unsupported_version"
            else:
                normalized = validate_entry(workflow_id, raw, self.thing_name, now_ms)
            current = self.sessions.get(workflow_id)
            if isinstance(normalized, str):
                reason = normalized
                if current is not None and current.state == REJECTED and current.entry == raw:
                    continue  # the same invalid entry: already reported
                if current is not None and current.is_live:
                    transitions.append(self._end(current, STOPPED, "replaced"))
                rejected = SessionState(workflow_id=workflow_id, entry=dict(raw) if isinstance(raw, dict)
                                        else {"raw": str(raw)}, state=REJECTED,
                                        applied_at_ms=now_ms, version=version, reason=reason)
                self.sessions[workflow_id] = rejected
                key = (workflow_id, version)
                if key not in self._rejections_logged:
                    self._rejections_logged.add(key)
                    logger.warning("Debug session entry for workflow %s rejected: %s",
                                   workflow_id, reason)
                transitions.append(Transition(rejected, current.state if current else None,
                                              REJECTED, reason))
                continue
            entry = normalized
            if current is not None and current.is_live:
                if current.entry == entry:
                    continue  # unchanged: no-op
                if current.session_id == entry["sessionId"]:
                    current.entry = entry  # the same session, updated in place
                    current.version = version
                    if current.batcher is not None:
                        _retune(current.batcher, entry)
                    continue
                transitions.append(self._end(current, STOPPED, "replaced"))
            elif current is not None and current.state in TERMINAL_STATES \
                    and current.session_id == entry["sessionId"]:
                continue  # an ended session is not revived by the same entry
            session = SessionState(workflow_id=workflow_id, entry=entry, state=PENDING,
                                   applied_at_ms=now_ms, version=version, pending_since_ms=now_ms)
            session.topic = topics.convention_topic(self._debug_root, entry["usecaseId"],
                                                    workflow_id, "record")
            self.sessions[workflow_id] = session
            self._tombstones.discard(workflow_id)
            transitions.append(Transition(session, current.state if current else None, PENDING,
                                          "applied"))

        for workflow_id, session in list(self.sessions.items()):
            present = workflow_id in sessions_map and sessions_map.get(workflow_id) is not None
            if present:
                continue
            if session.is_live:
                transitions.append(self._end(session, STOPPED, "stopped"))
            elif session.state in TERMINAL_STATES or session.state == REJECTED:
                # Reported once already; forget it and clear its reported key.
                del self.sessions[workflow_id]
                self._tombstones.add(workflow_id)
        transitions.extend(self._evaluate(now_ms))
        return transitions

    def _end(self, session: SessionState, state: str, reason: str) -> Transition:
        previous = session.state
        session.state = state
        session.reason = None
        return Transition(session, previous, state, reason)

    # -- the tick --------------------------------------------------------------------

    def tick(self) -> List[Transition]:
        """Expiry and registration re-evaluation; returns the transitions."""
        return self._evaluate(self.now_ms())

    def _evaluate(self, now_ms: int) -> List[Transition]:
        transitions: List[Transition] = []
        try:
            snapshot = self._view.snapshot() if self._view is not None else {}
        except Exception:  # noqa: BLE001 - the view never fails a tick
            logger.debug("Debug sessions: registration snapshot unavailable", exc_info=True)
            snapshot = {}
        by_workflow: Dict[str, Any] = {}
        for registration_id, info in snapshot.items():
            workflow_id = getattr(info, "workflow_id", None)
            if workflow_id is None:
                continue
            existing = by_workflow.get(workflow_id)
            # Prefer the highest version when several rows are registered.
            if existing is None or _version_key(getattr(info, "version", "")) > _version_key(
                    getattr(existing[1], "version", "")):
                by_workflow[workflow_id] = (registration_id, info)
        for workflow_id, session in self.sessions.items():
            if not session.is_live:
                continue
            expires_at = session.expires_at_ms
            if expires_at is not None and now_ms >= expires_at:
                transitions.append(self._end(session, EXPIRED, "expired"))
                continue
            row = by_workflow.get(workflow_id)
            if row is not None:
                registration_id, info = row
                session.registration_id = registration_id
                session.workflow_version = str(getattr(info, "version", "") or "")
                if session.state != ACTIVE:
                    previous = session.state
                    session.state = ACTIVE
                    session.reason = None
                    transitions.append(Transition(session, previous, ACTIVE, "registered"))
            else:
                if session.state == ACTIVE:
                    session.state = NO_REGISTRATION
                    session.registration_id = None
                    session.pending_since_ms = now_ms
                    transitions.append(Transition(session, ACTIVE, NO_REGISTRATION,
                                                  "registration_left"))
                elif session.state == PENDING:
                    since = session.pending_since_ms if session.pending_since_ms is not None \
                        else session.applied_at_ms
                    if now_ms - since >= NO_REGISTRATION_AFTER_S * 1000:
                        session.state = NO_REGISTRATION
                        transitions.append(Transition(session, PENDING, NO_REGISTRATION,
                                                      "no_registration"))
        return transitions

    # -- queries ----------------------------------------------------------------------

    def active_sessions(self) -> List[SessionState]:
        return [s for s in self.sessions.values() if s.state == ACTIVE]

    def live_sessions(self) -> List[SessionState]:
        return [s for s in self.sessions.values() if s.is_live]

    def session_for_workflow(self, workflow_id: str) -> Optional[SessionState]:
        return self.sessions.get(workflow_id)

    def session_for_registration(self, registration_id: Optional[str]) -> Optional[SessionState]:
        if registration_id is None:
            return None
        for session in self.sessions.values():
            if session.registration_id == registration_id and session.is_live:
                return session
        return None

    def any_debug_level_active(self) -> bool:
        return any(s.level == "debug" for s in self.active_sessions())

    def first_record_reason(self, session: SessionState) -> str:
        enabled_at = session.enabled_at_ms
        if enabled_at is not None and enabled_at < self.agent_started_ms:
            return "resumed"
        return "started"

    def status(self) -> Dict[str, dict]:
        """``{workflow_id: {active, expiresAt, state, sessionId}}`` for the
        device run API's ``debug`` field."""
        result = {}
        for workflow_id, session in self.sessions.items():
            result[workflow_id] = {
                "active": session.state == ACTIVE,
                "expiresAt": session.expires_at_ms if session.is_live else None,
                "state": session.state,
                "sessionId": session.session_id,
            }
        return result

    def reported(self, instance: str, agent_version: str) -> dict:
        """The ``reported`` document; keys ended and forgotten since the last
        call are written as ``null`` once so the shadow drops them."""
        sessions: Dict[str, Any] = {wf: s.reported() for wf, s in self.sessions.items()}
        for workflow_id in self._tombstones:
            sessions.setdefault(workflow_id, None)
        self._tombstones = set()
        return {"v": 1, "agentVersion": agent_version, "instance": instance, "sessions": sessions}


def _retune(batcher: Any, entry: dict) -> None:
    """Apply a changed entry's caps to the running batcher."""
    try:
        batcher.messages_bucket.rate = float(entry["maxMessagesPerSecond"])
        batcher.messages_bucket.capacity = max(batcher.messages_bucket.rate, 1.0)
        batcher.bytes_bucket.rate = float(entry["maxBytesPerSecond"])
        batcher.bytes_bucket.capacity = max(batcher.bytes_bucket.rate, 120.0 * 1024)
    except Exception:  # noqa: BLE001 - a fake batcher in tests
        pass


def _version_key(version: Any):
    text = str(version or "")
    parts = []
    for piece in text.split("."):
        try:
            parts.append((0, int(piece)))
        except ValueError:
            parts.append((1, piece))
    return parts
