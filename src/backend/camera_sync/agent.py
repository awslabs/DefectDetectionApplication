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
"""Edge_Sync_Agent report path over the ``dda-camera-registry`` named
shadow (Requirements 3.1, 3.3, 3.4, 12.4).

Every report is the *complete current inventory* in the reported-document
shape from the design (``schemaVersion``, ``cameras`` keyed by stable id,
``failures``, ``discoveryErrors``). Because a report is always the full
state, the first successful write after a connectivity outage is
automatically the complete catch-up publication (3.3) — no separate queue
of unpublished deltas is needed.

Report triggers (all funnel through :meth:`EdgeSyncAgent.report_inventory`):

- LocalServer start: :meth:`EdgeSyncAgent.start` schedules an immediate
  full report (3.4).
- Image_Source CRUD: the existing FastAPI route layer calls
  :func:`camera_sync.hooks.notify_image_source_changed`, which invokes
  ``report_inventory`` on the active agent.
- Camera_Discovery ``on_change``: wire
  :meth:`EdgeSyncAgent.on_discovery_change` as the discovery callback.
- Portal-change application: :meth:`EdgeSyncAgent.on_delta` applies each
  ``desired.changes[csid]`` through the existing accessors (Requirements
  5.2, 5.3, 5.4, 5.6, 11.3) and calls ``report_inventory`` afterwards, so
  the applied state (with ``ack``/failure entries) is what gets reported.

Reports are debounced to one shadow write per
:data:`DEBOUNCE_SECONDS`-second window — comfortably inside the 30 s bound
of Requirement 3.1. Failed shadow writes (device offline) are retried with
exponential backoff, capped at :data:`BACKOFF_MAX_SECONDS`, retrying
indefinitely until connectivity returns.

All shadow I/O goes through the existing ``IoTShadowAccessor`` (Greengrass
IPC — the device's own AWS IoT identity and policies, Requirement 12.4);
delta notifications arrive through the existing MQTT ``SubscriptionHandler``
pattern on ``$aws/things/{thing}/shadow/name/dda-camera-registry/update/#``
(see :func:`make_shadow_stream_handler`).

The clock, the shadow transport, the state-store path, and the DB session
factory are all injectable so tests drive the agent deterministically with
fakes; :meth:`EdgeSyncAgent.pump` exposes one scheduling step for
fake-clock tests, while the on-device daemon thread simply loops over it.
"""
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from fastapi import HTTPException
from marshmallow import ValidationError

from camera_sync.inventory import (
    CameraSourceState,
    build_inventory,
    configured_camera_source_id,
)
from camera_sync.version_state import CameraSyncStateStore, versions_from_reported

logger = logging.getLogger(__name__)

#: The named shadow carrying camera-registry sync state (design decision:
#: Sync_Channel transport).
SHADOW_NAME = "dda-camera-registry"

#: Reported-document schema version.
SCHEMA_VERSION = 1

#: Debounce window: at most one shadow write per this many seconds — well
#: inside Requirement 3.1's 30-second publication bound.
DEBOUNCE_SECONDS = 5.0

#: Requirement 3.1's publication bound, kept here for reference/tests.
MAX_REPORT_DELAY_SECONDS = 30.0

#: A report exceeding this size gets its capability metadata truncated
#: (the shadow document limit is 8 KB; 7 KB leaves headroom for the state
#: wrapper and shadow metadata).
MAX_REPORT_BYTES = 7 * 1024

#: Exponential backoff for failed shadow writes (offline device). Retries
#: never give up: the first post-reconnect success is the catch-up state.
BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0

#: Failure reason for portal changes targeting sources the device manages
#: through discovery (defense in depth behind the portal-side rejection,
#: Requirement 5.6).
REASON_DISCOVERY_MANAGED = "discovery-managed"

#: Stable-id prefixes: configured Image_Sources report as ``cfg-{id}``;
#: discovered-only hardware reports under its ``disc-…`` discovery id.
_CONFIGURED_PREFIX = "cfg-"
_DISCOVERED_PREFIX = "disc-"

#: Capability-truncation ladder: (max formats per camera, max resolutions
#: per format). ``None`` means unlimited. Tried in order until the document
#: fits :data:`MAX_REPORT_BYTES`; the last rung drops capabilities entirely.
_TRUNCATION_LADDER: Tuple[Tuple[Optional[int], int], ...] = (
    (None, 8),
    (None, 4),
    (None, 2),
    (None, 1),
    (4, 1),
    (2, 1),
    (1, 1),
    (0, 0),
)


def delta_topic_prefix(thing_name: str, shadow_name: str = SHADOW_NAME) -> str:
    """The shadow update topic prefix the MQTT ``SubscriptionHandler``
    subscribes to (with its ``#`` wildcard); the ``delta`` subtopic carries
    portal-originated desired changes."""
    return "$aws/things/{}/shadow/name/{}/update/".format(thing_name, shadow_name)


# --- reported document (pure) -------------------------------------------------


def _encoded_size(document: Mapping) -> int:
    return len(json.dumps(document, separators=(",", ":")).encode("utf-8"))


def _camera_entry(
    entry: CameraSourceState, version: int, ack: Optional[str]
) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "version": version,
        "name": entry.name,
        "type": entry.type,
        "origin": entry.origin,
        "params": dict(entry.params),
        "capabilities": _copy_capabilities(entry.capabilities),
        "discovered": entry.discovered,
        "absent": entry.absent,
    }
    if entry.absent and entry.absent_since is not None:
        doc["absentSince"] = entry.absent_since
    if ack:
        doc["ack"] = ack
    return doc


def _copy_capabilities(capabilities: Mapping[str, Any]) -> Dict[str, Any]:
    copied = dict(capabilities)
    formats = copied.get("formats")
    if isinstance(formats, list):
        copied["formats"] = [
            {**fmt, "resolutions": [list(r) for r in fmt.get("resolutions", [])]}
            if isinstance(fmt, Mapping)
            else fmt
            for fmt in formats
        ]
    return copied


def _shrink_capabilities(
    capabilities: Mapping[str, Any],
    max_formats: Optional[int],
    max_resolutions: int,
) -> Tuple[Dict[str, Any], bool]:
    """Truncate capability metadata to the top resolutions per format.

    Returns ``(shrunk, changed)`` — ``changed`` is True when any metadata
    was actually dropped, which is what sets ``capabilitiesTruncated``.
    """
    formats = capabilities.get("formats")
    if not isinstance(formats, list) or not formats:
        return dict(capabilities), False

    changed = False
    kept_formats = formats
    if max_formats is not None and len(formats) > max_formats:
        kept_formats = formats[:max_formats]
        changed = True

    new_formats = []
    for fmt in kept_formats:
        if not isinstance(fmt, Mapping):
            new_formats.append(fmt)
            continue
        resolutions = fmt.get("resolutions") or []
        if len(resolutions) > max_resolutions:
            top = sorted(
                resolutions,
                key=lambda r: (r[0] * r[1]) if len(r) >= 2 else 0,
                reverse=True,
            )[:max_resolutions]
            changed = True
        else:
            top = list(resolutions)
        new_formats.append({**fmt, "resolutions": top})

    shrunk = dict(capabilities)
    shrunk["formats"] = new_formats
    return shrunk, changed


def _truncate_document(
    document: Mapping[str, Any],
    max_formats: Optional[int],
    max_resolutions: int,
) -> Dict[str, Any]:
    truncated = dict(document)
    cameras: Dict[str, Any] = {}
    for csid, entry in document["cameras"].items():
        shrunk, changed = _shrink_capabilities(
            entry.get("capabilities") or {}, max_formats, max_resolutions
        )
        if changed:
            new_entry = dict(entry)
            new_entry["capabilities"] = shrunk
            new_entry["capabilitiesTruncated"] = True
            cameras[csid] = new_entry
        else:
            cameras[csid] = entry
    truncated["cameras"] = cameras
    return truncated


def build_report_document(
    inventory: Iterable[CameraSourceState],
    versions: Mapping[str, int],
    reported_at_ms: int,
    failures: Optional[Mapping[str, Mapping[str, Any]]] = None,
    discovery_errors: Optional[Iterable[Mapping[str, Any]]] = None,
    acks: Optional[Mapping[str, str]] = None,
    aliases: Optional[Mapping[str, str]] = None,
    max_bytes: int = MAX_REPORT_BYTES,
) -> Dict[str, Any]:
    """Pure builder of the complete reported document (design section 3).

    Always the full current inventory — never a delta. When the encoded
    document would exceed ``max_bytes``, capability metadata is truncated
    to the top resolutions per format (then progressively fewer formats),
    and every entry that lost metadata carries ``capabilitiesTruncated``.

    ``aliases`` maps a portal-supplied create csid to the configured csid
    the create produced (``cfg-{imageSourceId}``): the alias key mirrors
    the created entry (including its ``ack``) so the Portal reducer can
    match the pending create entry's ``portal_change_id`` (Requirement
    5.3). Aliases are one-shot — omitted from the next report, they age
    out of the registry through the reducer's deletion path.
    """
    acks = acks or {}
    cameras = {
        entry.camera_source_id: _camera_entry(
            entry,
            versions.get(entry.camera_source_id, 1),
            acks.get(entry.camera_source_id),
        )
        for entry in inventory
    }
    for alias_csid, real_csid in (aliases or {}).items():
        if real_csid in cameras and alias_csid not in cameras:
            cameras[alias_csid] = dict(cameras[real_csid])
    document: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "reportedAt": int(reported_at_ms),
        "cameras": cameras,
        "failures": {k: dict(v) for k, v in (failures or {}).items()},
        "discoveryErrors": [dict(e) for e in (discovery_errors or [])],
    }
    if _encoded_size(document) <= max_bytes:
        return document

    truncated = document
    for max_formats, max_resolutions in _TRUNCATION_LADDER:
        truncated = _truncate_document(document, max_formats, max_resolutions)
        if _encoded_size(truncated) <= max_bytes:
            return truncated
    return truncated  # smallest achievable form


# --- portal-change apply path (pure helpers) -----------------------------------

#: Reported ``params`` keys that live on the Image_Source record itself;
#: everything else belongs to the attached image-source configuration.
_TOP_LEVEL_PARAM_KEYS = ("cameraId", "location", "description")


def change_to_image_source_data(change: Mapping[str, Any]) -> Dict[str, Any]:
    """Invert a portal change's reported ``params`` shape back into the
    Image_Source data the existing accessors accept (the exact inverse of
    the ``build_inventory`` params projection).

    ``cameraId``, ``location``, and ``description`` map to Image_Source
    columns; ``devicePath`` maps back to the configuration's ``device``;
    every other params key (``gain``, ``exposure``, ``deviceName``, …) is
    passed through to ``imageSourceConfiguration`` so the accessors'
    schema validation judges it unchanged (Requirements 5.2, 11.3).
    """
    data: Dict[str, Any] = {}
    if change.get("name") is not None:
        data["name"] = change["name"]
    if change.get("type") is not None:
        data["type"] = change["type"]
    configuration: Dict[str, Any] = {}
    for key, value in (change.get("params") or {}).items():
        if key in _TOP_LEVEL_PARAM_KEYS:
            data[key] = value
        elif key == "devicePath":
            configuration["device"] = value
        else:
            configuration[key] = value
    if configuration:
        data["imageSourceConfiguration"] = configuration
    return data


def _error_reason(err: Exception) -> str:
    """The accessor error message, verbatim (Requirement 5.4)."""
    if isinstance(err, HTTPException):
        return str(err.detail)
    if isinstance(err, ValidationError):
        return str(err.messages)
    return str(err)


# --- the agent -----------------------------------------------------------------


class EdgeSyncAgent:
    """Reports the device's complete Camera_Source inventory over the
    ``dda-camera-registry`` named shadow (Requirements 3.1, 3.3, 3.4, 12.4).

    ``iot_shadow_accessor`` is the existing ``IoTShadowAccessor`` (or a
    fake exposing ``get_thing_shadow_state_request`` /
    ``update_thing_shadow_state_request``). ``camera_discovery`` provides
    ``latest_snapshot``; ``db_session_factory`` yields SQLAlchemy sessions
    for the read-only ``ImageSourceAccessor`` calls (defaults to the
    LocalServer ``SessionLocal``). ``clock`` is a monotonic-seconds source
    injected by fake-clock tests.
    """

    def __init__(
        self,
        iot_shadow_accessor,
        image_source_accessor,
        input_configuration_accessor=None,
        camera_discovery=None,
        db_session_factory: Optional[Callable] = None,
        state_store: Optional[CameraSyncStateStore] = None,
        thing_name: Optional[str] = None,
        shadow_name: str = SHADOW_NAME,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        debounce_seconds: float = DEBOUNCE_SECONDS,
        backoff_initial_seconds: float = BACKOFF_INITIAL_SECONDS,
        backoff_max_seconds: float = BACKOFF_MAX_SECONDS,
    ):
        self._shadow = iot_shadow_accessor
        self._image_source_accessor = image_source_accessor
        self._input_configuration_accessor = input_configuration_accessor
        self._discovery = camera_discovery
        self._db_session_factory = db_session_factory
        self._state_store = state_store if state_store is not None else CameraSyncStateStore()
        self.thing_name = (
            thing_name
            if thing_name is not None
            else os.environ.get("AWS_IOT_THING_NAME", "")
        )
        self.shadow_name = shadow_name
        self._clock = clock
        self._wall_clock = wall_clock
        self._debounce = float(debounce_seconds)
        self._backoff_initial = float(backoff_initial_seconds)
        self._backoff_max = float(backoff_max_seconds)

        self._lock = threading.Lock()
        self._dirty = False
        self._not_before = 0.0  # earliest monotonic time of the next write
        self._retry_delay = self._backoff_initial
        self._reported_versions: Dict[str, int] = {}

        # Portal-change apply state (Requirements 5.3, 5.4). All three are
        # one-shot: retained across failed shadow writes (offline retry)
        # and cleared once a report carrying them is successfully written.
        # - _apply_failures[csid] = {reason, portalChangeId}: reported in
        #   the `failures` map; the failed source is omitted from
        #   `cameras` in that report (design reported-document shape).
        # - _pending_acks[csid] = portal_change_id: echoed as `ack` on the
        #   camera entry (5.3).
        # - _create_aliases[portal_csid] = cfg_csid: a create's placeholder
        #   csid mirrored onto the created cfg- entry for one report so the
        #   Portal reducer matches its pending create entry.
        self._apply_failures: Dict[str, Dict[str, Any]] = {}
        self._pending_acks: Dict[str, str] = {}
        self._create_aliases: Dict[str, str] = {}
        self._consumed_acks: Dict[str, str] = {}
        self._consumed_aliases: Dict[str, str] = {}
        self._consumed_failures: Dict[str, Dict[str, Any]] = {}

        self._stop_event = threading.Event()
        self._wakeup = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Schedule the full startup report (3.4) and start the report
        worker on a daemon thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Edge sync agent already running")
            return

        self._refresh_reported_versions()
        self._stop_event = threading.Event()
        self._wakeup = threading.Event()
        with self._lock:
            self._dirty = True  # LocalServer start => full report (3.4)
            self._not_before = 0.0
            self._retry_delay = self._backoff_initial
        self._thread = threading.Thread(
            target=self._run, name="camera-sync-agent", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the report worker and wait for it to exit."""
        self._stop_event.set()
        self._wakeup.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join()
        self._thread = None

    # --- report triggers (Requirement 3.1) --------------------------------

    def report_inventory(self) -> None:
        """Request a (debounced) report of the complete current inventory.

        Called from the FastAPI route layer on Image_Source CRUD, from
        discovery ``on_change``, and after portal-change application. Safe
        from any thread; coalesces bursts into one shadow write per
        debounce window.
        """
        with self._lock:
            self._dirty = True
        self._wakeup.set()

    def on_discovery_change(self, snapshot) -> None:
        """Camera_Discovery ``on_change`` callback: the tracked inventory
        changed, so report it."""
        self.report_inventory()

    def on_delta(self, message: Mapping[str, Any]) -> None:
        """Portal-originated desired changes (shadow delta) — the apply
        path (Requirements 5.2, 5.3, 5.4, 5.6, 11.3).

        The delta payload carries ``{"state": {"changes": {csid: change}}}``
        (a bare state mapping is tolerated for reconnect-time application
        of the shadow's current desired document).
        """
        logger.info("Received camera-registry shadow delta: %s", message)
        state = message.get("state") if isinstance(message, Mapping) else None
        if not isinstance(state, Mapping):
            state = message if isinstance(message, Mapping) else {}
        changes = state.get("changes")
        if isinstance(changes, Mapping) and changes:
            self.apply_desired_changes(changes)

    # --- portal-change apply path (Requirements 5.2–5.6, 11.3) ------------

    def apply_desired_changes(self, changes: Mapping[str, Any]) -> None:
        """Apply every ``desired.changes[csid]`` entry through the existing
        accessors, record acks/failures, clear the processed desired
        entries (writing ``null``), and report the resulting state."""
        processed: List[str] = []
        for csid in sorted(changes):
            change = changes[csid]
            if not isinstance(change, Mapping):
                continue  # already-cleared (null) entries carry no work
            processed.append(csid)
            self._apply_one_change(str(csid), change)
        if not processed:
            return
        self._clear_desired_entries(processed)
        self.report_inventory()

    def _apply_one_change(self, csid: str, change: Mapping[str, Any]) -> None:
        op = str(change.get("op") or "")
        portal_change_id = change.get("portalChangeId")

        # Discovery-managed sources are immutable from the Portal
        # (defense in depth behind the portal-side rejection, Req 5.6):
        # only cfg- configured sources can be updated or deleted, and a
        # create must not target a disc- discovery id.
        targets_discovered = csid.startswith(_DISCOVERED_PREFIX)
        if targets_discovered or (
            op != "create" and not csid.startswith(_CONFIGURED_PREFIX)
        ):
            self._record_failure(csid, REASON_DISCOVERY_MANAGED, portal_change_id)
            return

        try:
            if op == "create":
                self._apply_create(csid, change, portal_change_id)
            elif op == "update":
                self._apply_update(csid, change, portal_change_id)
            elif op == "delete":
                self._apply_delete(csid, portal_change_id)
            else:
                self._record_failure(
                    csid, "unsupported operation '{}'".format(op), portal_change_id
                )
        except (ValidationError, HTTPException) as err:
            # Accessor validation rejected the change: the message travels
            # verbatim as the failure reason (Requirement 5.4).
            self._record_failure(csid, _error_reason(err), portal_change_id)
        except Exception as err:  # noqa: BLE001 - apply isolation (11.2)
            logger.exception("Applying portal change for %s failed", csid)
            self._record_failure(csid, str(err), portal_change_id)

    def _apply_create(
        self, csid: str, change: Mapping[str, Any], portal_change_id: Optional[str]
    ) -> None:
        """Create through ``ImageSourceAccessor.create_image_source`` —
        schema validation, camera-manager side effects, folder creation,
        and default-configuration handling all preserved (5.2, 11.3). A
        supplied configuration is applied with a follow-up accessor update
        (the create path builds the type default itself); if that fails,
        the created source is compensated away so a schema-invalid change
        leaves the device state unchanged (5.4)."""
        data = change_to_image_source_data(change)
        configuration = data.pop("imageSourceConfiguration", None)
        with self._make_session() as session:
            result = self._image_source_accessor.create_image_source(data, session)
            new_id = str(result["imageSourceId"])
            if configuration:
                try:
                    self._image_source_accessor.update_image_source(
                        new_id,
                        {"imageSourceConfiguration": dict(configuration)},
                        session,
                    )
                except Exception:
                    try:
                        self._image_source_accessor.delete_image_source(
                            new_id, session
                        )
                    except Exception:  # noqa: BLE001 - best-effort rollback
                        logger.exception(
                            "Could not roll back half-created image source %s",
                            new_id,
                        )
                    raise
        new_csid = configured_camera_source_id(new_id)
        with self._lock:
            self._apply_failures.pop(csid, None)
            if portal_change_id:
                self._pending_acks[new_csid] = str(portal_change_id)
                if csid != new_csid:
                    self._create_aliases[csid] = new_csid

    def _apply_update(
        self, csid: str, change: Mapping[str, Any], portal_change_id: Optional[str]
    ) -> None:
        image_source_id = csid[len(_CONFIGURED_PREFIX):]
        data = change_to_image_source_data(change)
        with self._make_session() as session:
            self._image_source_accessor.update_image_source(
                image_source_id, data, session
            )
        with self._lock:
            self._apply_failures.pop(csid, None)
            if portal_change_id:
                self._pending_acks[csid] = str(portal_change_id)

    def _apply_delete(self, csid: str, portal_change_id: Optional[str]) -> None:
        image_source_id = csid[len(_CONFIGURED_PREFIX):]
        with self._make_session() as session:
            self._image_source_accessor.delete_image_source(
                image_source_id, session
            )
        # No ack entry: the source vanishes from the full report, which the
        # Portal reducer resolves as agreement with its pending delete.
        with self._lock:
            self._apply_failures.pop(csid, None)
            self._pending_acks.pop(csid, None)

    def _record_failure(
        self, csid: str, reason: str, portal_change_id: Optional[str]
    ) -> None:
        failure: Dict[str, Any] = {"reason": reason}
        if portal_change_id:
            failure["portalChangeId"] = str(portal_change_id)
        with self._lock:
            self._apply_failures[csid] = failure
            self._pending_acks.pop(csid, None)

    def _clear_desired_entries(self, csids: Iterable[str]) -> None:
        """Clear applied or failed desired entries by writing ``null``
        (standard shadow discipline: the delta must not re-fire)."""
        payload = {"desired": {"changes": {csid: None for csid in csids}}}
        try:
            self._shadow.update_thing_shadow_state_request(
                self.thing_name, self.shadow_name, payload
            )
        except Exception:  # noqa: BLE001 - offline clear retries via delta redelivery
            logger.exception(
                "Could not clear applied desired changes from the "
                "camera-registry shadow"
            )

    # --- scheduling core ---------------------------------------------------

    def pump(self) -> Optional[float]:
        """Run one scheduling step; the worker thread's loop body.

        Returns ``None`` when idle (nothing pending), or the number of
        seconds until the next actionable moment (debounce expiry or
        backoff retry). Fake-clock tests call this directly to drive the
        agent deterministically.
        """
        with self._lock:
            if self._stop_event.is_set() or not self._dirty:
                return None
            now = self._clock()
            if now < self._not_before:
                return self._not_before - now
            self._dirty = False

        success = self._write_report()

        with self._lock:
            now = self._clock()
            if success:
                self._retry_delay = self._backoff_initial
                self._not_before = now + self._debounce
            else:
                # Retain the pending state and retry with backoff; the
                # eventual success is the complete catch-up report (3.3).
                self._dirty = True
                self._not_before = now + self._retry_delay
                self._retry_delay = min(self._retry_delay * 2.0, self._backoff_max)
            if not self._dirty:
                return None
            return max(0.0, self._not_before - now)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            delay = self.pump()
            if delay is None:
                self._wakeup.wait()
                self._wakeup.clear()
            else:
                self._stop_event.wait(delay)

    # --- report construction ----------------------------------------------

    def _refresh_reported_versions(self) -> None:
        """Version floor for state-file loss: the shadow's current
        reported versions (never lowers a version, Requirement 3.5)."""
        try:
            state = self._shadow.get_thing_shadow_state_request(
                self.thing_name, self.shadow_name
            )
        except Exception:  # noqa: BLE001 - offline start must not crash (11.2)
            logger.exception("Could not read the camera-registry shadow at start")
            state = None
        reported = state.get("reported") if isinstance(state, Mapping) else None
        self._reported_versions = versions_from_reported(reported)

    def _build_current_document(self) -> Dict[str, Any]:
        snapshot = (
            self._discovery.latest_snapshot if self._discovery is not None else None
        )
        inventory = self._load_inventory(snapshot)
        versions = self._state_store.advance(inventory, self._reported_versions)
        discovery_errors = [
            {"devicePath": f.get("device_path"), "error": f.get("error")}
            for f in (snapshot.failures if snapshot is not None else ())
        ]
        with self._lock:
            failures = {k: dict(v) for k, v in self._apply_failures.items()}
            acks = dict(self._pending_acks)
            aliases = dict(self._create_aliases)
            # Acks, aliases, and failures are one-shot: remember what this
            # document carries so a successful write clears exactly that.
            self._consumed_acks = dict(acks)
            self._consumed_aliases = dict(aliases)
            self._consumed_failures = {k: dict(v) for k, v in failures.items()}
        # A source with an outstanding apply failure reports through the
        # `failures` map, not `cameras` (design reported-document shape):
        # the Portal keeps its recorded entry marked failed (Req 5.4).
        reported_inventory = [
            entry for entry in inventory
            if entry.camera_source_id not in failures
        ]
        return build_report_document(
            reported_inventory,
            versions,
            reported_at_ms=int(self._wall_clock() * 1000),
            failures=failures,
            discovery_errors=discovery_errors,
            acks=acks,
            aliases=aliases,
        )

    def _make_session(self):
        """A DB session from the injected factory (default: the LocalServer
        ``SessionLocal``), usable as a context manager."""
        factory = self._db_session_factory
        if factory is None:
            from dao.sqlite_db.sqlite_db_operations import SessionLocal

            factory = SessionLocal
        return factory()

    def _load_inventory(self, snapshot) -> List[CameraSourceState]:
        """Read Image_Sources through the existing accessor (read-only,
        Requirement 11.3) and merge with the discovery snapshot; the merge
        runs inside the session so relationship attributes resolve."""
        with self._make_session() as session:
            image_sources = self._image_source_accessor.list_image_sources(
                None, session
            )
            return build_inventory(image_sources, snapshot)

    def _write_report(self) -> bool:
        try:
            document = self._build_current_document()
            self._shadow.update_thing_shadow_state_request(
                self.thing_name, self.shadow_name, {"reported": document}
            )
            with self._lock:
                # One-shot consumption of the acks/aliases/failures this
                # document carried; entries re-recorded meanwhile (a newer
                # delta racing the write) stay pending for the next report.
                for csid, change_id in self._consumed_acks.items():
                    if self._pending_acks.get(csid) == change_id:
                        del self._pending_acks[csid]
                for alias, target in self._consumed_aliases.items():
                    if self._create_aliases.get(alias) == target:
                        del self._create_aliases[alias]
                for csid, failure in self._consumed_failures.items():
                    if self._apply_failures.get(csid) == failure:
                        del self._apply_failures[csid]
                self._consumed_acks = {}
                self._consumed_aliases = {}
                self._consumed_failures = {}
            return True
        except Exception:  # noqa: BLE001 - offline/transport errors retry
            logger.exception(
                "Camera-registry shadow report failed; retrying with backoff"
            )
            return False


# --- delta subscription (SubscriptionHandler pattern) --------------------------


def make_shadow_stream_handler(agent: EdgeSyncAgent):
    """A ``SubscribeToIoTCoreStreamHandler`` dispatching the agent's shadow
    topics, following the existing ``CloudIoTShadowAccessor`` /
    ``SubscriptionHandler`` pattern: pass this handler and
    :func:`delta_topic_prefix` to an ``mqtt.SubscriptionHandler`` when
    wiring the agent (task 2.8).

    The awsiot import is deferred so this module stays importable without
    the Greengrass IPC runtime (tests use fakes).
    """
    import awsiot.greengrasscoreipc.client as client

    from dao.iotshadow.ShadowUtils import decode_shadow_payload, remove_prefix

    prefix = delta_topic_prefix(agent.thing_name, agent.shadow_name)

    class _CameraRegistryShadowHandler(client.SubscribeToIoTCoreStreamHandler):
        def on_stream_event(self, event) -> None:
            try:
                topic_name = event.message.topic_name
                subtopic = remove_prefix(topic_name, prefix)
                if subtopic == "delta":
                    message = decode_shadow_payload(event.message.payload)
                    agent.on_delta(message)
                elif subtopic == "rejected":
                    message = decode_shadow_payload(event.message.payload)
                    logger.warning(
                        "Camera-registry shadow update rejected: %s", message
                    )
                # accepted/documents notifications need no edge-side action
            except Exception:  # noqa: BLE001 - handler isolation (11.2)
                logger.exception("Error handling camera-registry shadow message")

        def on_stream_error(self, error: Exception) -> bool:
            logger.error("Camera-registry shadow stream error: %s", error)
            return True  # close the stream; the wiring layer resubscribes

        def on_stream_closed(self) -> None:
            logger.info("Camera-registry shadow stream closed")

    return _CameraRegistryShadowHandler()
