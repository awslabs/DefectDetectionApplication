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

"""The ``dda-workflow-telemetry`` shadow applied on the device
(workflow-telemetry-dashboards design §5.4, §15.4; plan decision Q3).

:meth:`TelemetryDesiredState.apply` receives each **complete** ``desired``
document the ``ShadowControlAgent`` delivers, in version order, and keeps:

- the document only when ``v == 1`` and ``usecaseId`` is a UUID (anything
  else is ignored with one WARNING per version; the last applied state
  stays, and with none ever applied the agent is disabled);
- ``enabled`` (default false);
- ``summaryIntervalS`` clamped to 5-60 and ``idleHeartbeatS`` to 30-300,
  each with a WARNING naming the clamped value;
- ``personClasses``: at most 16 labels of at most 64 characters, defaulting
  to ``["person", "human"]`` when absent or invalid;
- the ``live`` map, **replaced** by the document's: an entry is applied only
  when ``expiresAt`` is in the future, ``maxFps <= 2``, the sizes are within
  the caps (1280 x 720, 307,200 bytes), ``region`` matches
  ``^[a-z]{2}(-gov)?-[a-z]+-\\d$`` and ``prefix`` equals
  ``dda-telemetry/live/<usecaseId>/<thing>/<workflowId>/``, else it is
  ignored with a WARNING; more than 8 valid entries keep the 8 with the
  latest ``expiresAt`` (ties by workflow id), one WARNING per version names
  the dropped ids and ``live.dropped`` lists them in ``reported``.

:meth:`TelemetryDesiredState.reported` builds the ``reported`` document
``{v, agentVersion, usecaseId, enabled, summaryIntervalS, idleHeartbeatS,
personClasses, appliedVersion, lastPublishAt, counters, live}``; the agent
hands it to ``ShadowControlAgent.report``, which writes it on a non-counter
change and otherwise at most once per 5 minutes.
"""

import logging
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from workflow_engine.telemetry.live_frames import (
    MAX_BYTES, MAX_FPS, MAX_HEIGHT, MAX_WIDTH, REGION_RE, Lease,
)
from workflow_engine.telemetry.summarizer import DEFAULT_PERSON_CLASSES, SummarizerSettings

logger = logging.getLogger(__name__)

__all__ = ["TelemetryDesiredState", "validate_lease", "DEFAULTS", "MAX_LIVE_ENTRIES"]

SHADOW_VERSION = 1
DEFAULT_SUMMARY_INTERVAL_S = 10
DEFAULT_IDLE_HEARTBEAT_S = 60
SUMMARY_INTERVAL_RANGE = (5, 60)
IDLE_HEARTBEAT_RANGE = (30, 300)
MAX_PERSON_CLASSES = 16
MAX_PERSON_CLASS_CHARS = 64
#: The agent holds at most this many live leases (§15.4).
MAX_LIVE_ENTRIES = 8
#: The lease defaults the Portal writes (§9.3); used for an absent field.
LEASE_DEFAULTS = {"maxFps": 1, "maxWidth": MAX_WIDTH, "maxHeight": MAX_HEIGHT, "maxBytes": MAX_BYTES}
MAX_BUCKET_CHARS = 63
DEFAULTS = {
    "enabled": False,
    "summaryIntervalS": DEFAULT_SUMMARY_INTERVAL_S,
    "idleHeartbeatS": DEFAULT_IDLE_HEARTBEAT_S,
    "personClasses": list(DEFAULT_PERSON_CLASSES),
}

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


def _as_int(value: Any) -> Optional[int]:
    if _is_int(value):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def live_prefix(usecase_id: str, thing_name: str, workflow_id: str) -> str:
    return "dda-telemetry/live/{0}/{1}/{2}/".format(usecase_id, thing_name, workflow_id)


def validate_lease(workflow_id: str, raw: Any, usecase_id: str, thing_name: str, now_ms: int) -> Any:
    """A :class:`Lease` for a valid entry, else the rejection reason."""
    if not isinstance(raw, dict):
        return "not_an_object"
    expires_at = _as_int(raw.get("expiresAt"))
    if expires_at is None or expires_at <= now_ms:
        return "expires_at_not_in_future"
    max_fps = _as_number(raw.get("maxFps", LEASE_DEFAULTS["maxFps"]))
    if max_fps is None or not 0 < max_fps <= MAX_FPS:
        return "max_fps_out_of_range"
    max_width = _as_int(raw.get("maxWidth", LEASE_DEFAULTS["maxWidth"]))
    if max_width is None or not 1 <= max_width <= MAX_WIDTH:
        return "max_width_out_of_range"
    max_height = _as_int(raw.get("maxHeight", LEASE_DEFAULTS["maxHeight"]))
    if max_height is None or not 1 <= max_height <= MAX_HEIGHT:
        return "max_height_out_of_range"
    max_bytes = _as_int(raw.get("maxBytes", LEASE_DEFAULTS["maxBytes"]))
    if max_bytes is None or not 1 <= max_bytes <= MAX_BYTES:
        return "max_bytes_out_of_range"
    bucket = raw.get("bucket")
    if not isinstance(bucket, str) or not bucket or len(bucket) > MAX_BUCKET_CHARS:
        return "invalid_bucket"
    region = raw.get("region")
    if not isinstance(region, str) or not REGION_RE.match(region):
        return "invalid_region"
    if raw.get("prefix") != live_prefix(usecase_id, thing_name, workflow_id):
        return "invalid_prefix"
    return Lease(
        workflow_id=workflow_id, expires_at_ms=expires_at, max_fps=max_fps, max_width=max_width,
        max_height=max_height, max_bytes=max_bytes, bucket=bucket, region=region,
        prefix=raw["prefix"],
    )


def _clamp(value: Any, bounds: Tuple[int, int], default: int, name: str, version: Any) -> int:
    number = _as_int(value)
    if number is None:
        if value is not None:
            logger.warning("Telemetry shadow version %s: %s %r is not an integer; using %d",
                           version, name, value, default)
        return default
    low, high = bounds
    clamped = min(high, max(low, number))
    if clamped != number:
        logger.warning("Telemetry shadow version %s: %s %d is outside %d-%d; clamped to %d",
                       version, name, number, low, high, clamped)
    return clamped


def _person_classes(value: Any, version: Any) -> Tuple[str, ...]:
    if value is None:
        return DEFAULT_PERSON_CLASSES
    valid = (
        isinstance(value, list)
        and 1 <= len(value) <= MAX_PERSON_CLASSES
        and all(isinstance(label, str) and 0 < len(label) <= MAX_PERSON_CLASS_CHARS for label in value)
    )
    if not valid:
        logger.warning("Telemetry shadow version %s: personClasses is invalid (at most %d labels of at "
                       "most %d characters); using the default %s", version, MAX_PERSON_CLASSES,
                       MAX_PERSON_CLASS_CHARS, list(DEFAULT_PERSON_CLASSES))
        return DEFAULT_PERSON_CLASSES
    return tuple(value)


class TelemetryDesiredState:
    """See the module docstring. ``on_change(state)`` is called after every
    applied document (the agent re-configures the summarizer and the sender
    from it); ``wall`` is injectable (epoch seconds)."""

    def __init__(
        self,
        thing_name: str,
        *,
        wall: Callable[[], float] = time.time,
        on_change: Optional[Callable[["TelemetryDesiredState"], None]] = None,
    ) -> None:
        self.thing_name = str(thing_name)
        self._wall = wall
        self._on_change = on_change
        self._lock = threading.Lock()
        self.enabled = False
        self.usecase_id: Optional[str] = None
        self.summary_interval_s = DEFAULT_SUMMARY_INTERVAL_S
        self.idle_heartbeat_s = DEFAULT_IDLE_HEARTBEAT_S
        self.person_classes: Tuple[str, ...] = DEFAULT_PERSON_CLASSES
        self.leases: Dict[str, Lease] = {}
        self.dropped_live: List[str] = []
        self.rejected_live: Dict[str, str] = {}
        self.applied_version: Optional[int] = None
        self.ignored_documents = 0
        self.applied_documents = 0
        self._ignored_versions: set = set()

    # -- apply ----------------------------------------------------------------------

    def apply(self, desired: Any, version: Any) -> bool:
        """Apply one complete ``desired`` document; returns whether it was
        applied (false for an unsupported ``v`` or a missing use case id)."""
        now_ms = int(self._wall() * 1000)
        document = desired if isinstance(desired, dict) else {}
        if document.get("v") != SHADOW_VERSION or not _is_uuid(document.get("usecaseId")):
            self._ignore(version, document)
            return False
        usecase_id = document["usecaseId"]
        enabled = document.get("enabled", False)
        enabled = enabled if isinstance(enabled, bool) else False
        summary_interval = _clamp(document.get("summaryIntervalS", DEFAULT_SUMMARY_INTERVAL_S),
                                  SUMMARY_INTERVAL_RANGE, DEFAULT_SUMMARY_INTERVAL_S,
                                  "summaryIntervalS", version)
        idle_heartbeat = _clamp(document.get("idleHeartbeatS", DEFAULT_IDLE_HEARTBEAT_S),
                                IDLE_HEARTBEAT_RANGE, DEFAULT_IDLE_HEARTBEAT_S, "idleHeartbeatS", version)
        person_classes = _person_classes(document.get("personClasses"), version)
        leases, dropped, rejected = self._leases(document.get("live"), usecase_id, version, now_ms)
        with self._lock:
            self.enabled = enabled
            self.usecase_id = usecase_id
            self.summary_interval_s = summary_interval
            self.idle_heartbeat_s = idle_heartbeat
            self.person_classes = person_classes
            self.leases = leases
            self.dropped_live = dropped
            self.rejected_live = rejected
            self.applied_version = version if _is_int(version) else self.applied_version
            self.applied_documents += 1
        logger.info("Telemetry shadow version %s applied: enabled=%s usecase=%s interval=%ds heartbeat=%ds "
                    "live=%d", version, enabled, usecase_id, summary_interval, idle_heartbeat, len(leases))
        if self._on_change is not None:
            try:
                self._on_change(self)
            except Exception:  # noqa: BLE001 - the agent's handler is contained
                logger.exception("Telemetry desired state: on_change failed")
        return True

    def _ignore(self, version: Any, document: dict) -> None:
        with self._lock:
            self.ignored_documents += 1
            first = version not in self._ignored_versions
            self._ignored_versions.add(version)
        if first:
            if document.get("v") != SHADOW_VERSION:
                logger.warning("Telemetry shadow version %s ignored: unsupported document version %r "
                               "(expected %d)", version, document.get("v"), SHADOW_VERSION)
            else:
                logger.warning("Telemetry shadow version %s ignored: usecaseId is not a UUID; the agent "
                               "publishes nothing until the Portal writes one", version)

    def _leases(self, raw_live: Any, usecase_id: str, version: Any,
                now_ms: int) -> Tuple[Dict[str, Lease], List[str], Dict[str, str]]:
        valid: Dict[str, Lease] = {}
        rejected: Dict[str, str] = {}
        if isinstance(raw_live, dict):
            for workflow_id, raw in raw_live.items():
                if not isinstance(workflow_id, str) or not workflow_id or raw is None:
                    continue  # a null entry is a removal
                result = validate_lease(workflow_id, raw, usecase_id, self.thing_name, now_ms)
                if isinstance(result, Lease):
                    valid[workflow_id] = result
                else:
                    rejected[workflow_id] = result
                    logger.warning("Telemetry shadow version %s: live entry for workflow %s ignored: %s",
                                   version, workflow_id, result)
        elif raw_live is not None:
            logger.warning("Telemetry shadow version %s: live is not an object; no leases applied", version)
        dropped: List[str] = []
        if len(valid) > MAX_LIVE_ENTRIES:
            ordered = sorted(valid.values(), key=lambda lease: (-lease.expires_at_ms, lease.workflow_id))
            survivors = ordered[:MAX_LIVE_ENTRIES]
            dropped = sorted(lease.workflow_id for lease in ordered[MAX_LIVE_ENTRIES:])
            valid = {lease.workflow_id: lease for lease in survivors}
            logger.warning("Telemetry shadow version %s: %d live entries exceed the cap of %d; keeping the "
                           "%d with the latest expiresAt and dropping %s", version,
                           len(ordered), MAX_LIVE_ENTRIES, MAX_LIVE_ENTRIES, ", ".join(dropped))
        return valid, dropped, rejected

    # -- views ------------------------------------------------------------------------

    @property
    def active(self) -> bool:
        """Whether the agent may publish: enabled with a use case id."""
        with self._lock:
            return bool(self.enabled and self.usecase_id)

    def summarizer_settings(self) -> SummarizerSettings:
        with self._lock:
            return SummarizerSettings(
                enabled=bool(self.enabled and self.usecase_id),
                summary_interval_s=self.summary_interval_s,
                idle_heartbeat_s=self.idle_heartbeat_s,
                person_classes=tuple(self.person_classes),
            )

    def current_leases(self) -> Dict[str, Lease]:
        """The leases the sender may hold: none while the agent is inactive."""
        with self._lock:
            if not (self.enabled and self.usecase_id):
                return {}
            return dict(self.leases)

    def reported(
        self,
        *,
        agent_version: str,
        counters: Dict[str, int],
        last_publish_at: Optional[int],
        live_status: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """The ``reported`` document (§5.4). ``counters`` is ``{published,
        queueDrops, publishFailures}``; ``live_status`` is the sender's
        ``{<wf>: {activeUntil, lastSeq, framesTooLarge}}``."""
        with self._lock:
            live: Dict[str, Any] = {}
            for workflow_id, lease in self.leases.items():
                entry = {"activeUntil": lease.expires_at_ms, "lastSeq": None, "framesTooLarge": 0}
                if live_status and workflow_id in live_status:
                    entry.update({k: v for k, v in live_status[workflow_id].items()
                                  if k in ("activeUntil", "lastSeq", "framesTooLarge")})
                live[workflow_id] = entry
            if self.dropped_live:
                live["dropped"] = list(self.dropped_live)
            return {
                "v": SHADOW_VERSION,
                "agentVersion": str(agent_version),
                "usecaseId": self.usecase_id,
                "enabled": bool(self.enabled),
                "summaryIntervalS": self.summary_interval_s,
                "idleHeartbeatS": self.idle_heartbeat_s,
                "personClasses": list(self.person_classes),
                "appliedVersion": self.applied_version,
                "lastPublishAt": last_publish_at,
                "counters": {
                    "published": int(counters.get("published", 0) or 0),
                    "queueDrops": int(counters.get("queueDrops", 0) or 0),
                    "publishFailures": int(counters.get("publishFailures", 0) or 0),
                },
                "live": live,
            }

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "usecaseId": self.usecase_id,
                "summaryIntervalS": self.summary_interval_s,
                "idleHeartbeatS": self.idle_heartbeat_s,
                "personClasses": list(self.person_classes),
                "appliedVersion": self.applied_version,
                "appliedDocuments": self.applied_documents,
                "ignoredDocuments": self.ignored_documents,
                "leases": sorted(self.leases),
                "droppedLive": list(self.dropped_live),
                "rejectedLive": dict(self.rejected_live),
            }
