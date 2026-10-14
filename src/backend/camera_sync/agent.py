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

The one exception to "full state only" is key RETIREMENT
(:data:`RETIRED_CAMERA_SOURCE_IDS`, feature
static-image-camera-binding-and-pin-discoverability Requirement 2.9):
shadow updates MERGE nested maps, so a camera key a newer build stops
reporting stays alive in the shadow document and the Portal's
missing-from-report deletion path never fires. Such a key is therefore
written ONCE with an explicit ``null``, which removes it — the same
mechanism the Portal already uses in ``_clear_static_camera_shadow_key``.
Only keys the device really published are retired, and only once.

The same merge semantics apply to every configured camera deleted on the
device and to every one-shot create alias (rtsp-rtmp-stream-cameras
hardware finding): each previously published key that the current
inventory no longer holds is retired the same way
(:func:`deleted_source_retirements`). Without that, deleted cameras live
on in the shadow and the Portal forever, and their accumulated entries
push the merged document past the shadow size limit, after which every
camera report of the device is rejected. Keys whose disappearance is
reported as absence (discovered hardware, the virtual static cameras)
are never retired this way.

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
  Each ``(csid, portalChangeId)`` is applied at most once per process, a
  failed clear of the applied desired entries is retried from
  :meth:`EdgeSyncAgent.pump` instead of waiting for a delta redelivery, and
  :meth:`EdgeSyncAgent.on_subscription_active` applies the pending entries
  each time the delta subscription becomes active (rtsp-rtmp-stream-cameras
  finding 23, Requirement 5.13).

Two apply rules come from rtsp-rtmp-stream-cameras finding 21 (Requirements
5.6 and 5.11, design component 12). A change whose credential fetch is denied,
because the device read grant has not propagated yet, is parked and retried
2, 4, 8, 16 and 30 s apart on the ``change_retry_timer`` seam before it fails.
A Portal delete of a camera the device never created is acknowledged, and the
create's old failure key is removed from the shadow with an explicit ``null``.

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

The shadow carries two cloud-initiated pin slots, each owned by its own
pin worker: ``staticImagePin`` for the Static_Image_Camera
(cloud-static-camera-provisioning) and ``staticVideoPin`` for the
Static_Video_Camera (static-camera-video-loop, :func:`make_video_pin_worker`).
Both virtual cameras report their own inventory entry with their own
absence lifecycle.
"""
import collections
import functools
import itertools
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from fastapi import HTTPException
from marshmallow import ValidationError

from camera_discovery.aravis import STABLE_ID_PREFIX as ARAVIS_STABLE_ID_PREFIX
from camera_sync.inventory import (
    STATIC_IMAGE_ARAVIS_STABLE_ID,
    CameraSourceState,
    build_inventory,
    configured_camera_source_id,
)
from camera_sync.pin_worker import (
    MARKER_FILE_NAME,
    OP_REMOVE,
    STATUS_APPLIED,
    StaticImagePinWorker,
)
from camera_sync.stream_reporting import (
    StreamReportDebouncer,
    _default_timer,
    is_stream_change,
    stream_change_parts,
    stream_ingest_section,
)
from camera_sync.version_state import CameraSyncStateStore, versions_from_reported
from utils.static_image_camera import STATIC_IMAGE_CAMERA_ID, get_store
from utils.static_video_camera import (
    STATIC_VIDEO_CAMERA_ID,
    StaticVideoPinError,
)
from utils.static_video_camera import get_store as get_video_store
from utils.video_loop import MAX_PIN_VIDEO_BYTES

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

#: The report cap follows the shadow document size limit in effect (feature
#: static-camera-video-loop, design Decision 7). A report exceeding the cap
#: gets its capability metadata truncated. The limit covers the whole state
#: document (desired + reported), so the cap leaves
#: :data:`PIN_SLOTS_RESERVE_BYTES` for the state wrapper and the two pin
#: slots sharing this shadow, each a desired section plus its reported
#: echo: ``staticImagePin`` (cloud-static-camera-provisioning design
#: Decision 1) and ``staticVideoPin``. Both slots at their measured worst
#: case (128-character thing name and file name, 63-character bucket, the
#: video reason at its 256-character bound) take 3,144 bytes; the reserve
#: adds 440 bytes for an image failure reason.
#:
#: The limit is the AWS IoT "Maximum size of a JSON state document" quota
#: of the device's account (8 KB unless raised). The Portal writes the
#: account's value into ShadowManager's ``shadowDocumentSizeLimitBytes`` at
#: deployment time, and the agent reads it from there
#: (:func:`shadow_manager_size_limit_provider`), so the device's local limit
#: and the cloud limit agree.
DEFAULT_SHADOW_DOCUMENT_LIMIT_BYTES = 8192
PIN_SLOTS_RESERVE_BYTES = 3584

#: The cap for the default 8 KB limit: 8,192 - 3,584 = 4,608 bytes. Also
#: the ``build_report_document`` default.
MAX_REPORT_BYTES = DEFAULT_SHADOW_DOCUMENT_LIMIT_BYTES - PIN_SLOTS_RESERVE_BYTES

#: The highest cap, whatever the limit: 10 KB.
MAX_REPORT_BYTES_CEILING = 10 * 1024

#: The lowest cap a size rejection can back off to.
MIN_REPORT_BYTES = 1024

#: ShadowManager's component name and its size-limit configuration key.
SHADOW_MANAGER_COMPONENT = "aws.greengrass.ShadowManager"
SHADOW_MANAGER_SIZE_LIMIT_KEY = "shadowDocumentSizeLimitBytes"

#: How often the agent re-reads the size limit (a deployment can raise it
#: without restarting the LocalServer).
SHADOW_LIMIT_REFRESH_SECONDS = 300.0


def report_cap_for_shadow_limit(limit_bytes: Optional[Any]) -> int:
    """The report cap for a shadow document size limit: the limit minus
    :data:`PIN_SLOTS_RESERVE_BYTES`, at most
    :data:`MAX_REPORT_BYTES_CEILING` and at least :data:`MIN_REPORT_BYTES`.
    An unknown or invalid limit means the 8 KB default."""
    try:
        if isinstance(limit_bytes, bool):
            raise TypeError("a bool is not a size limit")
        limit = int(limit_bytes)
    except (TypeError, ValueError, OverflowError):
        limit = DEFAULT_SHADOW_DOCUMENT_LIMIT_BYTES
    if limit <= 0:
        limit = DEFAULT_SHADOW_DOCUMENT_LIMIT_BYTES
    return max(MIN_REPORT_BYTES,
               min(MAX_REPORT_BYTES_CEILING, limit - PIN_SLOTS_RESERVE_BYTES))


def shadow_manager_size_limit_provider(
    component_config_reader: Callable[[str], Any],
) -> Callable[[], Optional[int]]:
    """A provider of ShadowManager's configured document size limit.

    ``component_config_reader`` is Greengrass IPC GetConfiguration for a
    component name (``DefectDetectionConfig.get_component_config``), which
    returns the component's configuration mapping. The provider returns
    ``shadowDocumentSizeLimitBytes`` as an int, or ``None`` when it is not
    set (ShadowManager then enforces its 8 KB default) or unreadable."""

    def provider() -> Optional[int]:
        config = component_config_reader(SHADOW_MANAGER_COMPONENT)
        if not isinstance(config, Mapping):
            return None
        value = config.get(SHADOW_MANAGER_SIZE_LIMIT_KEY)
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None

    return provider


def _is_size_rejection(error: BaseException) -> bool:
    """Whether a failed shadow update was rejected for its size: the local
    ShadowManager's ``InvalidArgumentsError`` ("The payload exceeds the
    maximum size allowed", its error code 413) or a service-style "payload
    too large" / ``RequestEntityTooLarge``.

    Greengrass IPC errors carry their text in a ``message`` attribute and
    stringify to an empty string, so both are inspected."""
    parts = [type(error).__name__, str(error)]
    message = getattr(error, "message", None)
    if isinstance(message, str):
        parts.append(message)
    text = " ".join(parts).lower()
    if _SIZE_REJECTION_CODE.search(text):
        return True
    return any(marker in text for marker in (
        "too large", "toolarge", "exceeds the maximum", "size limit"))


#: ShadowManager's (and the service's) size-rejection error code, as a
#: standalone number (not part of, say, a thing name).
_SIZE_REJECTION_CODE = re.compile(r"(?<![0-9a-z])413(?![0-9a-z])")

#: The Static_Video_Camera's pin slot in this shadow (static-camera-video-
#: loop design Decision 7); the image slot stays ``staticImagePin``.
VIDEO_PIN_SECTION = "staticVideoPin"

#: Bound on the echoed failure reason for the video slot (JSON-escaped
#: characters, see ``pin_worker.bound_reason``), part of the budget above.
VIDEO_PIN_REASON_MAX_CHARS = 256

#: The video store's "nothing to remove" wording, which the video worker
#: maps to an applied no-op like the image worker's marker.
_NO_VIDEO_PINNED_MARKER = "no video is pinned"

#: Exponential backoff for failed shadow writes (offline device). Retries
#: never give up: the first post-reconnect success is the catch-up state.
BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0

#: Failure reason for portal changes targeting sources the device manages
#: through discovery (defense in depth behind the portal-side rejection,
#: Requirement 5.6).
REASON_DISCOVERY_MANAGED = "discovery-managed"

#: A change whose credential fetch is denied is retried this many seconds
#: after each attempt ends (rtsp-rtmp-stream-cameras Requirement 5.6, finding
#: 21): at most six attempts, the last about 60 s after the first denial.
CREDENTIAL_RETRY_DELAYS_S: Tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 30.0)

#: Appended to the failure reason when the last retry is denied too:
#: ``credential retrieval failed: <code> (retried for 60 s)``.
CREDENTIAL_RETRY_NOTE = "retried for 60 s"

#: The ``(csid, portalChangeId)`` keys of processed Portal changes the agent
#: remembers, so a redelivered change is not applied again
#: (rtsp-rtmp-stream-cameras Requirement 5.13, finding 23). The oldest is
#: forgotten first.
PROCESSED_CHANGES_CAP = 256

#: A failed clear of applied desired entries is retried from ``pump()``, 1 s
#: after the failure, doubling up to 30 s apart (Requirement 5.13).
CLEAR_RETRY_INITIAL_SECONDS = 1.0
CLEAR_RETRY_MAX_SECONDS = 30.0

#: Stable-id prefixes: configured Image_Sources report as ``cfg-{id}``;
#: discovered-only hardware reports under its ``disc-…`` discovery id.
_CONFIGURED_PREFIX = "cfg-"
_DISCOVERED_PREFIX = "disc-"

#: Camera keys this build no longer reports and that must be RETIRED from
#: the shadow — deleted with an explicit ``null`` rather than merely
#: omitted (feature static-image-camera-binding-and-pin-discoverability,
#: Requirement 2.9). Currently just the aravis-enumerated duplicate of the
#: Static_Image_Camera, whose exclusion from the reported inventory landed
#: in :mod:`camera_sync.inventory` (third hardware finding). Derived from
#: the shipped enumeration identity, never hardcoded (Requirement 2.11).
RETIRED_CAMERA_SOURCE_IDS: Tuple[str, ...] = (STATIC_IMAGE_ARAVIS_STABLE_ID,)

#: Camera keys whose disappearance is reported as ABSENCE and that are
#: therefore never retired as deleted sources: discovered hardware
#: (Camera_Discovery marks a vanished ``disc-``/``arv-`` camera absent,
#: camera-registry-sync Requirement 2.4) and the two virtual static cameras
#: (reported explicitly absent, cloud-static-camera-provisioning
#: Requirement 6.2 and static-camera-video-loop Requirement 4.7).
ABSENCE_TRACKED_PREFIXES: Tuple[str, ...] = (_DISCOVERED_PREFIX, ARAVIS_STABLE_ID_PREFIX)
ABSENCE_TRACKED_IDS = frozenset({STATIC_IMAGE_CAMERA_ID, STATIC_VIDEO_CAMERA_ID})


def deleted_source_retirements(
    previously_published: Iterable[str], live_keys: Iterable[str]
) -> Set[str]:
    """The previously published camera keys a report must delete from the
    shadow with an explicit ``null``: every one the current report does not
    carry (a configured camera deleted on the device, a one-shot create
    alias after its report), except the absence-tracked keys.

    Shadow updates merge nested maps, so omitting such a key from a full
    report leaves it in the shadow, and the Portal's missing-from-report
    deletion path never fires (rtsp-rtmp-stream-cameras hardware finding).
    """
    live = set(live_keys)
    return {
        key for key in previously_published
        if key not in live
        and key not in ABSENCE_TRACKED_IDS
        and not key.startswith(ABSENCE_TRACKED_PREFIXES)
    }

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


def default_video_marker_path() -> str:
    """The video pin worker's idempotence marker, next to the video
    store's files (``static_video_camera/applied_pin_request.json``) so it
    shares the Pinned_Video's lifetime. Resolved lazily, like the image
    worker's, so constructing the agent never requires
    ``COMPONENT_WORK_PATH``."""
    return os.path.join(
        os.environ["COMPONENT_WORK_PATH"], "static_video_camera", MARKER_FILE_NAME
    )


def make_video_pin_worker(
    iot_shadow_accessor,
    thing_name: str,
    shadow_name: str = SHADOW_NAME,
    **overrides: Any,
) -> StaticImagePinWorker:
    """The Static_Video_Camera's pin worker (static-camera-video-loop,
    design Decision 7): the image worker class on the ``staticVideoPin``
    slot, applying through the video store, with its own marker, the
    100 MB download cap, the video store's error type and "nothing to
    remove" wording, and a bounded echo reason.

    ``overrides`` replace any constructor argument — the collaborator
    seams (``store_factory``, ``s3_client_factory``, ``marker_path``,
    ``clock``, ``sleep``, ``wall_clock``) for tests."""
    options: Dict[str, Any] = {
        "store_factory": get_video_store,
        "section_name": VIDEO_PIN_SECTION,
        "max_download_bytes": MAX_PIN_VIDEO_BYTES,
        "no_media_marker": _NO_VIDEO_PINNED_MARKER,
        "pin_error_type": StaticVideoPinError,
        "reason_max_chars": VIDEO_PIN_REASON_MAX_CHARS,
        "marker_path_factory": default_video_marker_path,
        "label": "static-video",
    }
    options.update(overrides)
    return StaticImagePinWorker(
        iot_shadow_accessor, thing_name, shadow_name, **options
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
        if not isinstance(entry, Mapping):
            cameras[csid] = entry  # retirement tombstone (a null value)
            continue
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
    retirements: Optional[Iterable[str]] = None,
    max_bytes: int = MAX_REPORT_BYTES,
    device_capabilities: Optional[Mapping[str, Any]] = None,
    failure_retirements: Optional[Iterable[str]] = None,
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
    5.3). Aliases are one-shot — the next report retires them (see
    ``retirements``), so they age out of the registry through the
    reducer's deletion path.

    ``retirements`` are already-published camera keys that must cease to
    exist in the shadow (feature
    static-image-camera-binding-and-pin-discoverability, Requirement 2.9):
    each is written with an explicit ``null`` value, which is the only way
    to REMOVE a key from a shadow document — updates merge nested maps, so
    a key merely omitted from a full report stays alive in every documents
    event and the Portal's missing-from-report deletion path never fires.
    A key present in the live inventory is never retired (a live entry
    always wins), and the null never reaches the Portal parser: the
    documents event carries the post-merge state, from which the key is
    gone. Retirements are one-shot, driven by the caller.

    ``device_capabilities`` (rtsp-rtmp-stream-cameras Requirement 16.5) is
    the ``deviceCapabilities.streamIngest`` section; when None the document
    has no ``deviceCapabilities`` key, exactly as before the feature.

    ``failure_retirements`` (rtsp-rtmp-stream-cameras Requirement 5.11) are
    failure keys the shadow holds that a delete of a camera the device
    never created retired: each is written as ``failures[csid] = None``,
    unless the document carries a failure for it, which wins. The default
    leaves the document exactly as before.
    """
    acks = acks or {}
    cameras: Dict[str, Any] = {
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
    for retired_csid in sorted(retirements or ()):
        if retired_csid not in cameras:
            cameras[retired_csid] = None
    failure_entries: Dict[str, Any] = {k: dict(v) for k, v in (failures or {}).items()}
    for retired_csid in sorted(failure_retirements or ()):
        if retired_csid not in failure_entries:
            failure_entries[retired_csid] = None
    document: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "reportedAt": int(reported_at_ms),
        "cameras": cameras,
        "failures": failure_entries,
        "discoveryErrors": [dict(e) for e in (discovery_errors or [])],
    }
    if device_capabilities is not None:
        document["deviceCapabilities"] = {"streamIngest": dict(device_capabilities)}
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

    A stream camera (``RTSP``/``RTMP``, rtsp-rtmp-stream-cameras) maps
    ``url`` to ``location`` and its settings to ``streamSettings`` (see
    ``camera_sync.stream_reporting.stream_change_parts``); its credential
    bookkeeping keys are handled by the apply path, never passed on.
    """
    if is_stream_change(change):
        return stream_change_parts(change)[0]
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


def _managed_settings(credential_ref, updated_at) -> Dict[str, Any]:
    """The device-managed stream settings a Portal change sets: the
    delivered Credential_Reference (None removes it) and, when the Portal
    sent one, the time the credentials changed."""
    managed: Dict[str, Any] = {"credentialRef": credential_ref}
    if updated_at is not None:
        managed["credentialsUpdatedAt"] = updated_at
    return managed


def _record_field(record, key: str):
    """``key`` of an Image_Source record, dict- or attribute-shaped."""
    if isinstance(record, Mapping):
        return record.get(key)
    return getattr(record, key, None)


def _change_identity(change: Mapping[str, Any]) -> Any:
    """What a pending clear compares a ``desired.changes`` entry with: its
    ``str(portalChangeId)``, or the whole change when it carries none."""
    portal_change_id = change.get("portalChangeId")
    return str(portal_change_id) if portal_change_id else dict(change)


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
    injected by fake-clock tests. ``change_retry_timer(delay_s, action)``
    starts the retry timers of changes whose credential fetch was denied
    (default: ``stream_reporting._default_timer``, a daemon
    ``threading.Timer``); tests inject a recording fake and fire it by hand.
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
        pin_worker: Optional[StaticImagePinWorker] = None,
        video_pin_worker: Optional[StaticImagePinWorker] = None,
        shadow_size_limit_provider: Optional[Callable[[], Optional[int]]] = None,
        limit_refresh_seconds: float = SHADOW_LIMIT_REFRESH_SECONDS,
        stream_ingest=None,
        stream_timer: Optional[Callable[[float, Callable[[], None]], None]] = None,
        credential_fetcher: Optional[Callable[[Mapping[str, Any]], Dict[str, str]]] = None,
        credential_store=None,
        change_retry_timer: Optional[Callable[[float, Callable[[], None]], None]] = None,
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

        # Cloud-initiated static-image pin worker (feature
        # cloud-static-camera-provisioning): the agent owns the worker's
        # lifecycle and shares the shadow transport with it; the worker's
        # terminal outcomes trigger an inventory report so the
        # `static-image-camera` entry publishes promptly (Requirement 6.1).
        if pin_worker is None:
            pin_worker = StaticImagePinWorker(
                iot_shadow_accessor, self.thing_name, shadow_name
            )
        self.pin_worker = pin_worker
        self.pin_worker.report_inventory = self.report_inventory

        # Cloud-initiated static-video pin worker (feature
        # static-camera-video-loop, design Decision 7): a second instance
        # of the same worker on the `staticVideoPin` slot, applying through
        # the video store, with its own marker, a 100 MB download cap and a
        # bounded echo reason. Owned and started exactly like the first.
        if video_pin_worker is None:
            video_pin_worker = make_video_pin_worker(
                iot_shadow_accessor, self.thing_name, shadow_name
            )
        self.video_pin_worker = video_pin_worker
        self.video_pin_worker.report_inventory = self.report_inventory

        # Report cap from the shadow document size limit in effect (see
        # report_cap_for_shadow_limit). Without a provider the limit is the
        # 8 KB default, i.e. the MAX_REPORT_BYTES cap. A size rejection
        # halves the cap until the limit next changes.
        self._size_limit_provider = shadow_size_limit_provider
        self._limit_refresh = float(limit_refresh_seconds)
        self._shadow_limit_bytes: Optional[int] = None
        self._limit_read_at: Optional[float] = None
        self._cap_override: Optional[int] = None
        self._last_report_cap: Optional[int] = None

        self._lock = threading.Lock()
        self._dirty = False
        self._not_before = 0.0  # earliest monotonic time of the next write
        self._retry_delay = self._backoff_initial
        self._reported_versions: Dict[str, int] = {}

        # Stable absence timestamp for an unpinned, previously reported
        # Static_Image_Camera (Requirement 6.2 — second hardware finding:
        # shadow updates MERGE nested maps, so the entry must be reported
        # explicitly absent, never merely omitted). Derived once per
        # absence episode (see _static_image_absent_since) and cleared
        # when the store is pinned again, so it never churns between
        # reports (a churning timestamp would version-bump every report).
        self._static_absent_since_ms: Optional[int] = None
        # The same, for the Static_Video_Camera's own absence episode
        # (static-camera-video-loop, Requirement 4.7).
        self._video_absent_since_ms: Optional[int] = None

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

        # One-shot retirement of already-published camera keys this build
        # no longer reports (feature
        # static-image-camera-binding-and-pin-discoverability, Requirement
        # 2.9 — the aravis-enumerated Static_Image_Camera duplicate).
        # Same lifecycle as the acks/aliases/failures above: pending until
        # a report carrying them is written SUCCESSFULLY (so an offline
        # retry still carries the deletion), then retired for the rest of
        # the process and pruned from the version floor so nothing
        # re-emits it.
        self._pending_retirements: set = set()
        self._consumed_retirements: set = set()
        self._retired_registrations: set = set()
        # The camera keys the shadow holds from this device: seeded from the
        # start-time shadow GET (so keys earlier builds or processes left
        # behind are known), else from the version state store, and kept
        # current by every successful write. Deleted sources are retired
        # from it (deleted_source_retirements); None until seeded.
        self._published_keys: Optional[Set[str]] = None

        self._stop_event = threading.Event()
        self._wakeup = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Stream cameras (rtsp-rtmp-stream-cameras, design component 12).
        # ``stream_ingest`` is the StreamIngestManager, or None for no
        # stream reporting at all (the documents are then exactly as
        # before). Coarse health changes re-report at most once per camera
        # per 30 s (Requirement 4.6).
        self._stream_ingest = stream_ingest
        self._stream_reports = StreamReportDebouncer(
            clock=clock, on_publish=self.report_inventory,
            **({"timer": stream_timer} if stream_timer is not None else {}))
        self._credential_fetcher = credential_fetcher
        self._credential_store = credential_store

        # Denied credential fetches (rtsp-rtmp-stream-cameras Requirement
        # 5.6, design component 12). ``_apply_lock`` serializes every change
        # application: a delta's batch, the activation catch-up and each
        # retry. The lock order is ``_apply_lock``, then ``_lock``; nothing
        # under ``_apply_lock`` writes the shadow, starts a timer or requests
        # a report. ``_credential_retries[csid]`` is the parked change,
        # ``{change, portalChangeId, attempt, token}``. Timers cannot be
        # cancelled, so each parking takes a fresh token and a timer whose
        # token is no longer parked does nothing. ``change_retry_timer``'s
        # contract: the action runs later, on another thread.
        self._change_retry_timer = (
            change_retry_timer if change_retry_timer is not None else _default_timer
        )
        self._apply_lock = threading.Lock()
        self._credential_retries: Dict[str, Dict[str, Any]] = {}
        self._retry_tokens = itertools.count(1)

        # At most once per Portal change (Requirement 5.13, design component
        # 12): the ``(csid, str(portalChangeId))`` of every change finished
        # here, oldest first, capped at PROCESSED_CHANGES_CAP. Used only
        # under ``_apply_lock``. A parked change is never recorded.
        self._processed_changes: "collections.OrderedDict[Tuple[str, str], None]" = (
            collections.OrderedDict())

        # Failed clears of applied desired entries, retried from pump()
        # (Requirement 5.13), guarded by ``_lock``: ``_pending_clears[csid]``
        # is the identity of the change whose entry is still to be nulled,
        # its ``str(portalChangeId)`` or else ``dict(change)``. The retry
        # reads the shadow first and nulls only an entry with that identity.
        self._pending_clears: Dict[str, Any] = {}
        self._clear_not_before = 0.0
        self._clear_retry_delay = CLEAR_RETRY_INITIAL_SECONDS
        self._clear_retries = 0  # the retries of the current episode, for the logs

        # Failure keys of deletes of cameras the device never created
        # (Requirement 5.11, design component 12), with the camera
        # retirements' rule: only a key the shadow holds is nulled.
        # - _published_failure_keys: the failure keys the shadow holds,
        #   seeded from the start-time shadow GET and kept current by every
        #   successful write (empty when that GET fails).
        # - _pending_failure_retirements: the ids such a delete retired.
        # - _consumed_failure_retirements: the nulls the document being
        #   written carries; only a successful write retires them.
        self._published_failure_keys: Set[str] = set()
        self._pending_failure_retirements: Set[str] = set()
        self._consumed_failure_retirements: Set[str] = set()

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Schedule the full startup report (3.4) and start the report
        worker on a daemon thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Edge sync agent already running")
            return

        state = self._refresh_reported_versions()
        self.pin_worker.start()
        self.video_pin_worker.start()
        self._handoff_desired_pin(state)
        self._handoff_desired_video_pin(state)
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
        self._attach_stream_ingest()

    def _attach_stream_ingest(self) -> None:
        """Listen to stream health, and report the Device_Stream_Capabilities
        once the startup probe has them (Requirement 16.5). The probe starts
        here, so every device reports its capabilities, even before its
        first stream camera is added."""
        manager = self._stream_ingest
        if manager is None:
            return
        try:
            manager.add_health_listener(self._on_stream_health)
            manager.on_capabilities(lambda _capabilities: self.report_inventory())
        except Exception:  # noqa: BLE001 - stream reporting must not break the agent
            logger.exception("Could not attach stream camera reporting")

    def _on_stream_health(self, camera_key: str, health: Mapping[str, Any]) -> None:
        """A stream session's state changed: re-report when the camera's
        coarse state, codec, resolution or decoder changed, at most once
        per camera per 30 s (Requirement 4.6)."""
        from stream_ingest.manager import image_source_id_for_key

        image_source_id = image_source_id_for_key(camera_key)
        if image_source_id is not None and self._stream_reports.offer(image_source_id, health):
            self.report_inventory()

    def stop(self) -> None:
        """Stop the report worker (and the owned pin worker) and wait for
        them to exit. Parked credential retries are dropped, so a retry
        timer that fires afterwards does nothing."""
        self._stop_event.set()
        self._wakeup.set()
        with self._apply_lock:
            self._credential_retries.clear()
        self.pin_worker.stop()
        self.video_pin_worker.stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join()
        self._thread = None

    def _handoff_desired_pin(self, state: Optional[Mapping]) -> None:
        """Startup/reconnect catch-up (Requirement 5.2): the same shadow
        GET that seeds the version floor hands an unprocessed
        ``desired.staticImagePin`` to the pin worker — a request already
        carried to a terminal outcome (marker match) is skipped; the
        worker's own marker check re-reports it on delta redelivery."""
        desired = state.get("desired") if isinstance(state, Mapping) else None
        pin = desired.get("staticImagePin") if isinstance(desired, Mapping) else None
        if not isinstance(pin, Mapping) or not pin.get("requestId"):
            return
        try:
            applied = self.pin_worker.applied_request_id()
        except Exception:  # noqa: BLE001 - marker read must not break start
            logger.exception(
                "Could not read the static-image pin marker at start; "
                "handing the desired pin document to the worker"
            )
            applied = None
        if applied is not None and pin.get("requestId") == applied:
            return
        self.pin_worker.on_desired(pin)

    def _handoff_desired_video_pin(self, state: Optional[Mapping]) -> None:
        """The same startup catch-up for the video slot: an unprocessed
        ``desired.staticVideoPin`` goes to the video pin worker, and a
        request its marker already records is skipped
        (static-camera-video-loop, Requirement 8.6)."""
        desired = state.get("desired") if isinstance(state, Mapping) else None
        pin = desired.get(VIDEO_PIN_SECTION) if isinstance(desired, Mapping) else None
        if not isinstance(pin, Mapping) or not pin.get("requestId"):
            return
        try:
            applied = self.video_pin_worker.applied_request_id()
        except Exception:  # noqa: BLE001 - marker read must not break start
            logger.exception(
                "Could not read the static-video pin marker at start; "
                "handing the desired pin document to the worker"
            )
            applied = None
        if applied is not None and pin.get("requestId") == applied:
            return
        self.video_pin_worker.on_desired(pin)

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
        # Cloud-initiated static-image pin (cloud-static-camera-provisioning):
        # the `staticImagePin` section routes to the owned pin worker; the
        # camera `changes` apply path below is untouched.
        static_pin = state.get("staticImagePin")
        if isinstance(static_pin, Mapping) and static_pin:
            self.pin_worker.on_desired(static_pin)
        # Cloud-initiated static-video pin (static-camera-video-loop): the
        # `staticVideoPin` section routes to the video pin worker.
        static_video_pin = state.get(VIDEO_PIN_SECTION)
        if isinstance(static_video_pin, Mapping) and static_video_pin:
            self.video_pin_worker.on_desired(static_video_pin)
        changes = state.get("changes")
        if isinstance(changes, Mapping) and changes:
            self.apply_desired_changes(changes)

    def on_subscription_active(self) -> bool:
        """The delta subscription became active (Requirement 5.13, design
        component 12, **Catch-up on activation**). The subscription's worker
        runs this at the catch-up's place in its queue after every
        activation, the first included.

        Once the shadow GET is readable (a mapping, or False when there is no
        shadow), reports and a pending clear resume at once, and the shadow's
        ``desired.changes`` is applied. Returns False when the shadow could
        not be read, so the subscription runs the catch-up again. A catch-up
        that could not read the shadow leaves the report and clear backoffs
        as they are: retried while the stream is up, it neither forces a
        report nor restarts a failing write's backoff."""
        state = self._shadow.get_thing_shadow_state_request(self.thing_name, self.shadow_name)
        if state is None:
            logger.warning(
                "Could not read the camera-registry shadow after subscribing; retrying")
            return False
        with self._lock:
            self._not_before = 0.0
            self._retry_delay = self._backoff_initial
            self._dirty = True
            self._clear_not_before = 0.0
            self._clear_retry_delay = CLEAR_RETRY_INITIAL_SECONDS
        self._wakeup.set()
        desired = state.get("desired") if isinstance(state, Mapping) else None
        changes = desired.get("changes") if isinstance(desired, Mapping) else None
        if isinstance(changes, Mapping) and changes:
            self.apply_desired_changes(changes)
        return True

    # --- portal-change apply path (Requirements 5.2–5.6, 11.3) ------------

    def apply_desired_changes(self, changes: Mapping[str, Any]) -> None:
        """Apply every ``desired.changes[csid]`` entry through the existing
        accessors, record acks/failures, clear the processed desired
        entries (writing ``null``), and report the resulting state.

        The batch is applied under ``_apply_lock``. The retry timers of the
        changes a denied fetch parked, the desired-entry clear and the
        report request run only after the lock is released (design
        component 12, **Denied credential fetch**). A change already
        processed is listed too, so its entry is cleared. When the clear
        fails, its entries are retried from :meth:`pump` (Requirement 5.13)."""
        processed: List[str] = []
        identities: Dict[str, Any] = {}
        retries: List[Tuple[str, float, int]] = []
        with self._apply_lock:
            for csid in sorted(changes):
                change = changes[csid]
                if not isinstance(change, Mapping):
                    continue  # already-cleared (null) entries carry no work
                processed.append(csid)
                identities[csid] = _change_identity(change)
                retry = self._apply_one_change(str(csid), change)
                if retry is not None:
                    retries.append(retry)
        for retry in retries:
            self._start_retry_timer(*retry)
        if not processed:
            return
        cleared = self._clear_desired_entries(processed)
        with self._lock:
            if cleared:
                for csid in processed:
                    self._pending_clears.pop(csid, None)
            else:
                self._pending_clears.update(identities)
                self._clear_not_before = self._clock() + self._clear_retry_delay
        if not cleared:
            self._wakeup.set()
        self.report_inventory()

    def _apply_one_change(
        self, csid: str, change: Mapping[str, Any], attempt: int = 0
    ) -> Optional[Tuple[str, float, int]]:
        """Apply one Portal change; the caller holds ``_apply_lock``.

        ``attempt`` is 0 for a delivered change, and the number of the retry
        for a parked one (:meth:`_retry_parked_change`). Returns the retry to
        schedule, ``(csid, delay_s, token)``, when a denied fetch parked the
        change, and otherwise None. It never starts a timer itself: its
        callers start the returned retry once they release ``_apply_lock``.

        Task 29's parked-change check runs first. Then a delivered change
        that was already processed (the same csid and ``portalChangeId``) is
        not applied again (Requirement 5.13). Any other change is dispatched,
        and recorded once it has finished: applied, failed, refused or
        acknowledged, never while it is parked.
        """
        op = str(change.get("op") or "")
        portal_change_id = change.get("portalChangeId")

        if attempt == 0 and self._redelivers_parked_change(csid, portal_change_id):
            return None

        key = (csid, str(portal_change_id)) if portal_change_id else None
        if attempt == 0 and key is not None and key in self._processed_changes:
            logger.info(
                "Portal change %s for %s was already processed; clearing its "
                "desired entry without applying it again", portal_change_id, csid,
            )
            self._processed_changes.move_to_end(key)
            return None

        retry = self._dispatch_change(csid, change, op, portal_change_id, attempt)
        if retry is None and key is not None:
            self._processed_changes[key] = None
            self._processed_changes.move_to_end(key)
            while len(self._processed_changes) > PROCESSED_CHANGES_CAP:
                self._processed_changes.popitem(last=False)
        return retry

    def _dispatch_change(
        self,
        csid: str,
        change: Mapping[str, Any],
        op: str,
        portal_change_id: Optional[str],
        attempt: int,
    ) -> Optional[Tuple[str, float, int]]:
        """Apply one Portal change by the rules below; the caller,
        :meth:`_apply_one_change`, holds ``_apply_lock`` and gets the retry
        to schedule, or None."""
        # The rules of design component 12, in order (Requirements 5.6, 5.11;
        # defense in depth behind the portal-side rejection):
        # 1. Any change to a disc- discovery id is refused as
        #    discovery-managed, creates included, and so is any change to the
        #    literal `static-image-camera` (feature
        #    cloud-static-camera-provisioning, Requirement 6.5) and
        #    `static-video-camera` (static-camera-video-loop) ids.
        # 2. Creates, and any change to a cfg- configured source, take their
        #    existing paths.
        # 3. A delete of an arv- id, and any op other than a create or a
        #    delete on any other id, is refused as discovery-managed.
        # 4. Any other delete names a camera the device never created, such
        #    as a Portal create that failed or never arrived, and is
        #    acknowledged (_apply_never_created_delete).
        configured = csid.startswith(_CONFIGURED_PREFIX)
        targets_discovered = (
            csid.startswith(_DISCOVERED_PREFIX)
            or csid == STATIC_IMAGE_CAMERA_ID
            or csid == STATIC_VIDEO_CAMERA_ID
        )
        never_created_delete = (
            op == "delete"
            and not configured
            and not targets_discovered
            and not csid.startswith(ARAVIS_STABLE_ID_PREFIX)
        )
        if targets_discovered or (
            op != "create" and not configured and not never_created_delete
        ):
            self._record_failure(csid, REASON_DISCOVERY_MANAGED, portal_change_id)
            return None

        from stream_ingest.credential_fetch import CredentialFetchError

        failure_code: Optional[str] = None
        try:
            if never_created_delete:
                self._apply_never_created_delete(csid, portal_change_id)
            elif op == "create" and is_stream_change(change):
                self._apply_stream_create(csid, change, portal_change_id)
            elif op == "create":
                self._apply_create(csid, change, portal_change_id)
            elif op == "update" and is_stream_change(change):
                self._apply_stream_update(csid, change, portal_change_id)
            elif op == "update":
                self._apply_update(csid, change, portal_change_id)
            elif op == "delete":
                self._apply_delete(csid, portal_change_id)
            else:
                self._record_failure(
                    csid, "unsupported operation '{}'".format(op), portal_change_id
                )
        except CredentialFetchError as err:
            # Requirement 5.6: a denial is retried; any other failure fails
            # the change with a reason that holds no secret ("credential
            # retrieval failed: ResourceNotFoundException"), and the device
            # is left unchanged.
            return self._credential_fetch_failed(
                csid, change, portal_change_id, attempt, err
            )
        except (ValidationError, HTTPException) as err:
            # Accessor validation rejected the change: the message travels
            # verbatim as the failure reason (Requirement 5.4).
            self._record_failure(csid, _error_reason(err), portal_change_id)
            failure_code = type(err).__name__
        except Exception as err:  # noqa: BLE001 - apply isolation (11.2)
            logger.exception("Applying portal change for %s failed", csid)
            self._record_failure(csid, str(err), portal_change_id)
            failure_code = type(err).__name__
        if attempt > 0 and failure_code is None:
            logger.info(
                "Portal change %s for %s applied after %d credential retries",
                portal_change_id, csid, attempt,
            )
        elif attempt > 0:
            logger.warning(
                "Portal change %s for %s failed on credential retry %d (%s)",
                portal_change_id, csid, attempt, failure_code,
            )
        return None

    # --- denied credential fetches (Requirement 5.6, design component 12) ---

    def _redelivers_parked_change(
        self, csid: str, portal_change_id: Optional[str]
    ) -> bool:
        """A change was delivered for ``csid``, from a delta or the
        start-time catch-up (the caller holds ``_apply_lock``).

        True when it is the parked change redelivered: same
        ``portalChangeId``, both present. It is not applied again, and the
        parked change keeps its schedule. Any other change, of any op, is
        newer: the parked change is dropped, and its timer finds nothing."""
        parked = self._credential_retries.get(csid)
        if parked is None:
            return False
        parked_change_id = parked.get("portalChangeId")
        if portal_change_id and parked_change_id and portal_change_id == parked_change_id:
            logger.info(
                "Portal change %s for %s is already waiting for a credential "
                "retry; not applying it again", portal_change_id, csid,
            )
            return True
        del self._credential_retries[csid]
        logger.info(
            "Portal change %s for %s supersedes change %s, which was waiting "
            "for a credential retry", portal_change_id, csid, parked_change_id,
        )
        return False

    def _credential_fetch_failed(
        self,
        csid: str,
        change: Mapping[str, Any],
        portal_change_id: Optional[str],
        attempt: int,
        err,
    ) -> Optional[Tuple[str, float, int]]:
        """A credential fetch failed (the caller holds ``_apply_lock``). A
        denial parks the change for its next retry while one is left, and
        returns that retry. Any other failure fails the change at once, and
        so does a denial of the last retry, with the retry note. The log
        lines name only the id, the change id and the error code."""
        if err.retryable and attempt < len(CREDENTIAL_RETRY_DELAYS_S):
            token = next(self._retry_tokens)
            self._credential_retries[csid] = {
                "change": dict(change),
                "portalChangeId": portal_change_id,
                "attempt": attempt + 1,
                "token": token,
            }
            delay_s = CREDENTIAL_RETRY_DELAYS_S[attempt]
            log = logger.warning if attempt == 0 else logger.info
            log("Credential retrieval for Portal change %s for %s was denied "
                "(%s); retry %d of %d in %g s", portal_change_id, csid, err.reason,
                attempt + 1, len(CREDENTIAL_RETRY_DELAYS_S), delay_s)
            return csid, delay_s, token
        if err.retryable:
            logger.warning(
                "Credential retrieval for Portal change %s for %s was still "
                "denied (%s) after retrying for 60 s; the change failed",
                portal_change_id, csid, err.reason,
            )
            self._record_failure(
                csid, "{} ({})".format(err, CREDENTIAL_RETRY_NOTE), portal_change_id
            )
            return None
        if attempt > 0:
            logger.warning(
                "Credential retrieval for Portal change %s for %s failed on "
                "retry %d (%s); the change failed",
                portal_change_id, csid, attempt, err.reason,
            )
        self._record_failure(csid, str(err), portal_change_id)
        return None

    def _retry_parked_change(self, csid: str, token: int) -> None:
        """A credential retry timer fired, on the timer's thread.

        Does nothing unless the agent is running and ``csid``'s parked
        change still holds ``token``, so a timer left by a change that was
        superseded, retried again or dropped never runs a newer change's
        retry early. The attempt runs under ``_apply_lock``; the next
        retry's timer, or the report request, after it is released. Every
        exception is caught, so the timer thread never dies with one."""
        try:
            with self._apply_lock:
                parked = self._credential_retries.get(csid)
                if (
                    self._stop_event.is_set()
                    or parked is None
                    or parked.get("token") != token
                ):
                    return
                del self._credential_retries[csid]
                retry = self._apply_one_change(
                    csid, parked["change"], attempt=parked["attempt"]
                )
            if retry is not None:
                self._start_retry_timer(*retry)
            else:
                self.report_inventory()
        except Exception:  # noqa: BLE001 - timer thread isolation (11.2)
            logger.exception("The credential retry of the Portal change for %s failed", csid)

    def _start_retry_timer(self, csid: str, delay_s: float, token: int) -> None:
        """Start a parked change's retry timer. Never called with
        ``_apply_lock`` held, so even a timer that runs its action at once
        cannot deadlock. A timer that cannot start is logged; the change
        stays parked until a newer change for the id supersedes it."""
        try:
            self._change_retry_timer(
                delay_s, functools.partial(self._retry_parked_change, csid, token)
            )
        except Exception:  # noqa: BLE001 - timer start isolation (11.2)
            logger.exception("Could not start the credential retry timer for %s", csid)

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

    # --- stream cameras (rtsp-rtmp-stream-cameras Requirements 5.5, 5.6) ----

    def _fetch_credentials(self, credential_ref: Mapping[str, Any]) -> Dict[str, str]:
        if self._credential_fetcher is not None:
            return self._credential_fetcher(credential_ref)
        from stream_ingest.credential_fetch import fetch

        return fetch(credential_ref)

    def _credentials(self):
        if self._credential_store is not None:
            return self._credential_store
        from stream_ingest.credentials import get_credential_store

        return get_credential_store()

    def _apply_stream_create(
        self, csid: str, change: Mapping[str, Any], portal_change_id: Optional[str]
    ) -> None:
        """Create a stream camera from a Portal change (design component 12):

        1. fetch the credentials a Credential_Reference names, failing fast
           with a reason that holds no secret;
        2. create the Image_Source, recording the reference and its time;
        3. write the Credential_Store — the accessor deletes the just-created
           Image_Source again when this fails, so no half-configured camera
           remains.
        """
        data, credential_ref, _clear, updated_at = stream_change_parts(change)
        managed: Dict[str, Any] = {}
        if credential_ref is not None:
            data["credentials"] = self._fetch_credentials(credential_ref)
            managed = _managed_settings(credential_ref, updated_at)
        with self._make_session() as session:
            result = self._image_source_accessor.create_image_source(
                data, session, managed_stream_settings=managed)
        new_csid = configured_camera_source_id(str(result["imageSourceId"]))
        with self._lock:
            self._apply_failures.pop(csid, None)
            if portal_change_id:
                self._pending_acks[new_csid] = str(portal_change_id)
                if csid != new_csid:
                    self._create_aliases[csid] = new_csid

    def _apply_stream_update(
        self, csid: str, change: Mapping[str, Any], portal_change_id: Optional[str]
    ) -> None:
        """Update a stream camera from a Portal change. A delivered
        Credential_Reference is fetched only when it is not the one the
        device already holds; a delivered clear removes the credentials.
        The accessor writes the Credential_Store before the Image_Source and
        restores it if the Image_Source write fails."""
        image_source_id = csid[len(_CONFIGURED_PREFIX):]
        data, credential_ref, clear, updated_at = stream_change_parts(change)
        data.pop("type", None)  # the stored type is authoritative
        managed: Dict[str, Any] = {}
        with self._make_session() as session:
            if credential_ref is not None:
                current = self._image_source_accessor.get_image_source(image_source_id, session)
                configuration = getattr(current, "imageSourceConfiguration", None)
                stored = dict(getattr(configuration, "streamSettings", None) or {})
                if (stored.get("credentialRef") != credential_ref
                        or not self._credentials().configured(image_source_id)):
                    data["credentials"] = self._fetch_credentials(credential_ref)
                managed = _managed_settings(credential_ref, updated_at)
            elif clear:
                data["clearCredentials"] = True
                managed = _managed_settings(None, updated_at)
            self._image_source_accessor.update_image_source(
                image_source_id, data, session, managed_stream_settings=managed)
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

    def _apply_never_created_delete(
        self, csid: str, portal_change_id: Optional[str]
    ) -> None:
        """A Portal delete of an id that is neither a ``cfg-`` id nor
        discovery-managed (rule 4 of design component 12, Requirement 5.11),
        such as a Portal create that failed or never reached the device. The
        caller holds ``_apply_lock``.

        When the id is a create alias the agent has not reported yet, the
        create made a camera the Portal does not know yet, and the delete
        deletes it through :meth:`_apply_delete`. A 404 means it is already
        gone; any other error fails the delete and keeps the alias.
        Otherwise the device holds nothing for the id, and the delete is
        acknowledged. Either way the id's parked retry, recorded failure,
        pending ack and alias go, and its failure key is retired from the
        shadow when the shadow holds one (see ``_build_current_document``)."""
        with self._lock:
            created_csid = self._create_aliases.get(csid)
        deleted = False
        if created_csid is not None:
            try:
                self._apply_delete(created_csid, portal_change_id)
                deleted = True
            except HTTPException as err:
                if err.status_code != 404:
                    raise
        self._credential_retries.pop(csid, None)
        with self._lock:
            self._apply_failures.pop(csid, None)
            self._pending_acks.pop(csid, None)
            self._create_aliases.pop(csid, None)
            self._pending_failure_retirements.add(csid)
        logger.info(
            "Acknowledged Portal delete %s for %s, which the device never "
            "created%s", portal_change_id, csid,
            "; deleted {}, which its create made".format(created_csid) if deleted else "",
        )

    def _record_failure(
        self, csid: str, reason: str, portal_change_id: Optional[str]
    ) -> None:
        failure: Dict[str, Any] = {"reason": reason}
        if portal_change_id:
            failure["portalChangeId"] = str(portal_change_id)
        with self._lock:
            self._apply_failures[csid] = failure
            self._pending_acks.pop(csid, None)

    def _clear_desired_entries(self, csids: Iterable[str]) -> bool:
        """Clear applied or failed desired entries by writing ``null``
        (standard shadow discipline: the delta must not re-fire). Returns
        whether the write succeeded. A failed clear no longer waits for a
        delta redelivery: :meth:`apply_desired_changes` keeps its entries
        pending, and :meth:`pump` retries them (Requirement 5.13)."""
        payload = {"desired": {"changes": {csid: None for csid in csids}}}
        try:
            self._shadow.update_thing_shadow_state_request(
                self.thing_name, self.shadow_name, payload
            )
        except Exception:  # noqa: BLE001 - a failed clear is retried from pump()
            logger.exception(
                "Could not clear applied desired changes from the "
                "camera-registry shadow"
            )
            return False
        return True

    def _retry_pending_clears(self) -> Optional[float]:
        """Retry the failed clears when one is due (Requirement 5.13, design
        component 12, **Clear retry**). Returns the seconds until the next
        retry, or None when no clear is pending.

        The retry reads the shadow before it writes, and nulls only an entry
        that still holds the identity of the change it is for: a missing
        entry, or a newer change, needs nothing. Completion is
        compare-and-remove, so a newer pending clear added meanwhile stays.
        A failed retry (an unreadable shadow, or a GET or UPDATE that
        raises) keeps every entry and backs off, 1 s doubling up to 30 s.
        Nothing raised escapes."""
        with self._lock:
            if self._stop_event.is_set() or not self._pending_clears:
                return None
            now = self._clock()
            if now < self._clear_not_before:
                return self._clear_not_before - now
            snapshot = dict(self._pending_clears)
            self._clear_retries += 1
            retry_number = self._clear_retries

        done: Dict[str, Any] = {}
        nulls: List[str] = []
        error: Optional[str] = None
        try:
            state = self._shadow.get_thing_shadow_state_request(
                self.thing_name, self.shadow_name
            )
        except Exception as err:  # noqa: BLE001 - counts as a failed retry
            state, error = None, "{}: {}".format(type(err).__name__, err)
        if state is False:
            done = snapshot  # no shadow: nothing left to clear
        elif not isinstance(state, Mapping):
            error = error or "the shadow could not be read"
        else:
            desired = state.get("desired")
            entries = desired.get("changes") if isinstance(desired, Mapping) else None
            entries = entries if isinstance(entries, Mapping) else {}
            for csid, identity in snapshot.items():
                entry = entries.get(csid)
                if isinstance(entry, Mapping) and _change_identity(entry) == identity:
                    nulls.append(csid)
                else:
                    done[csid] = identity  # gone, or a newer change: left in place
            if nulls:
                try:
                    self._shadow.update_thing_shadow_state_request(
                        self.thing_name, self.shadow_name,
                        {"desired": {"changes": {csid: None for csid in nulls}}},
                    )
                except Exception as err:  # noqa: BLE001 - counts as a failed retry
                    error = "{}: {}".format(type(err).__name__, err)
                else:
                    done.update((csid, snapshot[csid]) for csid in nulls)

        with self._lock:
            for csid, identity in done.items():
                if self._pending_clears.get(csid) == identity:
                    del self._pending_clears[csid]
            now = self._clock()
            if error is not None:
                delay = self._clear_retry_delay
                self._clear_not_before = now + delay
                self._clear_retry_delay = min(delay * 2.0, CLEAR_RETRY_MAX_SECONDS)
            else:
                self._clear_retry_delay = CLEAR_RETRY_INITIAL_SECONDS
                self._clear_retries = 0
            pending = bool(self._pending_clears)
            next_delay = max(0.0, self._clear_not_before - now) if pending else None
        if error is not None:
            logger.warning(
                "Could not clear %d applied desired change(s) from the "
                "camera-registry shadow (retry %d): %s; retrying in %g s",
                len(nulls) if nulls else len(snapshot), retry_number, error, delay,
            )
        elif nulls:
            logger.info(
                "Cleared %d applied desired change(s) from the camera-registry "
                "shadow on retry %d", len(nulls), retry_number,
            )
        return next_delay

    # --- scheduling core ---------------------------------------------------

    def pump(self) -> Optional[float]:
        """Run one scheduling step; the worker thread's loop body.

        Returns ``None`` when idle (nothing pending), or the number of
        seconds until the next actionable moment (debounce expiry, backoff
        retry or clear retry). A due clear retry runs first, then the report
        step. Fake-clock tests call this directly to drive the agent
        deterministically.
        """
        clear_delay = self._retry_pending_clears()
        report_delay = self._pump_report()
        if clear_delay is None:
            return report_delay
        if report_delay is None:
            return clear_delay
        return min(clear_delay, report_delay)

    def _pump_report(self) -> Optional[float]:
        """The report step of :meth:`pump`: write the report when one is
        requested and due. Returns as :meth:`pump` does for reports alone."""
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
            self._wakeup.wait(delay)  # None: until woken
            self._wakeup.clear()

    # --- report construction ----------------------------------------------

    def _refresh_reported_versions(self) -> Optional[Mapping]:
        """Version floor for state-file loss: the shadow's current
        reported versions (never lowers a version, Requirement 3.5).
        Returns the shadow state so ``start()`` can also inspect
        ``desired.staticImagePin`` from the same GET (Requirement 5.2)."""
        try:
            state = self._shadow.get_thing_shadow_state_request(
                self.thing_name, self.shadow_name
            )
        except Exception:  # noqa: BLE001 - offline start must not crash (11.2)
            logger.exception("Could not read the camera-registry shadow at start")
            state = None
        reported = state.get("reported") if isinstance(state, Mapping) else None
        self._reported_versions = versions_from_reported(reported)
        self._seed_static_absence(reported)
        self._seed_static_video_absence(reported)
        if isinstance(state, Mapping):
            # The shadow's own record of the camera keys it holds (for the
            # deleted-source retirement); without a readable shadow the
            # first report seeds a fallback instead.
            cameras = reported.get("cameras") if isinstance(reported, Mapping) else None
            if not isinstance(cameras, Mapping):
                cameras = {}
            # And of the failure keys it holds, so a delete of a camera the
            # device never created can remove one an earlier process or
            # build left (design component 12).
            failures = reported.get("failures") if isinstance(reported, Mapping) else None
            if not isinstance(failures, Mapping):
                failures = {}
            with self._lock:
                self._published_keys = {
                    str(key) for key, entry in cameras.items() if isinstance(entry, Mapping)
                }
                self._published_failure_keys = {
                    str(key) for key, failure in failures.items() if isinstance(failure, Mapping)
                }
        return state if isinstance(state, Mapping) else None

    def _seed_static_absence(self, reported: Optional[Mapping]) -> None:
        """Restart stability for the Static_Image_Camera's absence
        timestamp (Requirement 6.2): when the shadow's current reported
        state already carries the entry absent, adopt its ``absentSince``
        so a restart neither invents a new timestamp nor version-churns
        the entry. Ignored once the store is pinned again (the pinned
        branch of ``_load_inventory`` clears it)."""
        cameras = (reported or {}).get("cameras") if isinstance(reported, Mapping) else None
        entry = cameras.get(STATIC_IMAGE_CAMERA_ID) if isinstance(cameras, Mapping) else None
        if not isinstance(entry, Mapping) or not entry.get("absent"):
            return
        absent_since = entry.get("absentSince")
        if isinstance(absent_since, (int, float)):
            self._static_absent_since_ms = int(absent_since)

    def _seed_static_video_absence(self, reported: Optional[Mapping]) -> None:
        """The same restart stability for the Static_Video_Camera's
        absence timestamp (static-camera-video-loop, Requirement 4.7)."""
        cameras = (reported or {}).get("cameras") if isinstance(reported, Mapping) else None
        entry = cameras.get(STATIC_VIDEO_CAMERA_ID) if isinstance(cameras, Mapping) else None
        if not isinstance(entry, Mapping) or not entry.get("absent"):
            return
        absent_since = entry.get("absentSince")
        if isinstance(absent_since, (int, float)):
            self._video_absent_since_ms = int(absent_since)

    def _build_current_document(self) -> Dict[str, Any]:
        snapshot = (
            self._discovery.latest_snapshot if self._discovery is not None else None
        )
        # Collected BEFORE the state store is advanced: `advance` rewrites
        # the state file from the current inventory, which is one of the
        # two records answering "previously reported". Both collections
        # add to the pending retirements this document carries.
        self._collect_retirements()
        inventory = self._load_inventory(snapshot)
        previously_published = self._previously_published_keys()
        versions = self._state_store.advance(inventory, self._reported_versions)
        discovery_errors = [
            {"devicePath": f.get("device_path"), "error": f.get("error")}
            for f in (snapshot.failures if snapshot is not None else ())
        ]
        with self._lock:
            failures = {k: dict(v) for k, v in self._apply_failures.items()}
            acks = dict(self._pending_acks)
            aliases = dict(self._create_aliases)
            retirements = self._collect_deleted_sources(
                previously_published,
                [entry.camera_source_id for entry in inventory] + list(aliases),
            )
            # The failure keys deletes of never-created cameras retired: a
            # null only for a key the shadow holds, and only while the id has
            # no live failure again (the new failure wins). The rest have
            # nothing to remove and are dropped.
            failure_retirements = {
                csid for csid in self._pending_failure_retirements
                if csid in self._published_failure_keys and csid not in failures
            }
            self._pending_failure_retirements = set(failure_retirements)
            # Acks, aliases, and failures are one-shot: remember what this
            # document carries so a successful write clears exactly that.
            self._consumed_acks = dict(acks)
            self._consumed_aliases = dict(aliases)
            self._consumed_failures = {k: dict(v) for k, v in failures.items()}
            self._consumed_retirements = set(retirements)
            self._consumed_failure_retirements = set(failure_retirements)
        # A source with an outstanding apply failure reports through the
        # `failures` map, not `cameras` (design reported-document shape):
        # the Portal keeps its recorded entry marked failed (Req 5.4).
        reported_inventory = [
            entry for entry in inventory
            if entry.camera_source_id not in failures
        ]
        cap = self.report_cap()
        self._last_report_cap = cap
        return build_report_document(
            reported_inventory,
            versions,
            reported_at_ms=int(self._wall_clock() * 1000),
            failures=failures,
            discovery_errors=discovery_errors,
            acks=acks,
            aliases=aliases,
            retirements=retirements,
            max_bytes=cap,
            device_capabilities=self._device_stream_capabilities(),
            failure_retirements=failure_retirements,
        )

    # --- report cap (static-camera-video-loop design Decision 7) ------------

    def report_cap(self) -> int:
        """The size cap for the next report: derived from the shadow
        document size limit in effect (re-read at most every
        ``limit_refresh_seconds``), lowered by any size-rejection back-off."""
        self._refresh_shadow_limit()
        cap = report_cap_for_shadow_limit(self._shadow_limit_bytes)
        if self._cap_override is not None:
            cap = min(cap, self._cap_override)
        return cap

    def _refresh_shadow_limit(self) -> None:
        provider = self._size_limit_provider
        if provider is None:
            return
        now = self._clock()
        if (self._limit_read_at is not None
                and now - self._limit_read_at < self._limit_refresh):
            return
        self._limit_read_at = now
        try:
            limit = provider()
        except Exception:  # noqa: BLE001 - keep the last known limit
            logger.exception(
                "Could not read the ShadowManager document size limit; "
                "keeping the last known limit"
            )
            return
        if limit != self._shadow_limit_bytes:
            logger.info(
                "Shadow document size limit: %s; camera report cap %d bytes",
                "{} bytes".format(limit) if limit is not None
                else "ShadowManager default ({} bytes)".format(
                    DEFAULT_SHADOW_DOCUMENT_LIMIT_BYTES),
                report_cap_for_shadow_limit(limit),
            )
            self._shadow_limit_bytes = limit
            # A new limit supersedes an earlier size back-off.
            self._cap_override = None

    def _back_off_report_cap(self) -> None:
        """After a size rejection: halve the cap (down to MIN_REPORT_BYTES)
        for the retry and later reports, until the limit changes."""
        current = self._last_report_cap or self.report_cap()
        self._cap_override = max(MIN_REPORT_BYTES, current // 2)
        logger.warning(
            "Camera report rejected for its size at a %d-byte cap; retrying "
            "with a %d-byte cap", current, self._cap_override,
        )

    def _collect_retirements(self) -> Tuple[str, ...]:
        """The already-published camera keys this report must RETIRE with
        an explicit ``null`` (Requirement 2.9).

        A key qualifies exactly once, and only when the device really
        published it: shadow updates MERGE nested maps, so a key this
        build no longer reports would otherwise stay alive in the shadow
        forever and the Portal's missing-from-report deletion path would
        never fire (bugfix.md 1.11). A device that never published the key
        gets NO retirement write at all — deleting a key that was never
        there is a pointless shadow write.

        "Previously reported" is answered from the same two independent
        records :meth:`_static_previously_reported` uses (see
        :meth:`_previously_reported`), never from a third mechanism.
        """
        newly = [
            key
            for key in RETIRED_CAMERA_SOURCE_IDS
            if key not in self._retired_registrations
            and key not in self._pending_retirements
            and self._previously_reported(key)
        ]
        if newly:
            logger.info(
                "Retiring already-published camera registration(s) %s from "
                "the camera-registry shadow (explicit null delete)",
                ", ".join(newly),
            )
        self._pending_retirements.update(newly)
        return tuple(sorted(self._pending_retirements))

    def _previously_published_keys(self) -> Set[str]:
        """The camera keys the shadow holds from this device. Seeded, when
        no start-time shadow GET did it, from the start-time reported
        versions and the version state store (the inventory of the last
        report built) — read BEFORE the state store is advanced."""
        with self._lock:
            if self._published_keys is not None:
                return set(self._published_keys)
        keys = set(self._reported_versions)
        try:
            state = self._state_store.load()
        except Exception:  # noqa: BLE001 - state read must not break reports
            logger.exception("Camera sync state store read failed")
            state = None
        if state:
            keys.update(str(key) for key in state)
        with self._lock:
            if self._published_keys is None:
                self._published_keys = keys
            return set(self._published_keys)

    def _collect_deleted_sources(
        self, previously_published: Iterable[str], live_keys: Iterable[str]
    ) -> Tuple[str, ...]:
        """Add the deleted sources' keys to the pending retirements and
        return every retirement the next document carries (called with
        ``self._lock`` held). A pending retirement whose key is live again
        is dropped: a live entry always wins."""
        live = set(live_keys)
        newly = deleted_source_retirements(previously_published, live) - self._pending_retirements
        if newly:
            logger.info(
                "Retiring deleted camera source(s) %s from the camera-registry "
                "shadow (explicit null delete)", ", ".join(sorted(newly)),
            )
        self._pending_retirements.update(newly)
        self._pending_retirements.difference_update(live)
        return tuple(sorted(self._pending_retirements))

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
        runs inside the session so relationship attributes resolve.

        The static-image pin state (cloud-static-camera-provisioning,
        Requirement 6.1) gates the virtual `static-image-camera` entry;
        a store failure never breaks camera reporting. An unpinned,
        previously reported camera is reported explicitly ABSENT (with a
        stable ``absentSince``) rather than omitted — shadow updates
        MERGE nested maps, so an omitted key would persist in the shadow
        document and the Portal would keep seeing the stale entry as
        present forever (Requirement 6.2, second hardware finding)."""
        static_image_pinned = False
        static_image_metadata = None
        try:
            status = get_store().status()
            static_image_pinned = bool(status.get("pinned"))
            static_image_metadata = status.get("metadata")
        except Exception:  # noqa: BLE001 - store failure must not break reports
            logger.exception(
                "Static image pin state could not be read; reporting the "
                "inventory without the static camera entry"
            )
        static_image_absent_since: Optional[int] = None
        if static_image_pinned:
            self._static_absent_since_ms = None  # absence episode over
        else:
            static_image_absent_since = self._static_image_absent_since()
        # The Static_Video_Camera (static-camera-video-loop, Requirements
        # 4.6, 4.7): read and guarded separately, so a video store failure
        # never drops the image entry or the rest of the report, with its
        # own absence episode.
        static_video_pinned = False
        static_video_metadata = None
        try:
            video_status = get_video_store().status()
            static_video_pinned = bool(video_status.get("pinned"))
            static_video_metadata = video_status.get("metadata")
        except Exception:  # noqa: BLE001 - store failure must not break reports
            logger.exception(
                "Static video pin state could not be read; reporting the "
                "inventory without the static video camera entry"
            )
        static_video_absent_since: Optional[int] = None
        if static_video_pinned:
            self._video_absent_since_ms = None  # absence episode over
        else:
            static_video_absent_since = self._static_video_absent_since()
        with self._make_session() as session:
            image_sources = list(self._image_source_accessor.list_image_sources(
                None, session
            ))
            stream_health, credentials_configured = self._stream_inventory_inputs(
                image_sources
            )
            return build_inventory(
                image_sources,
                snapshot,
                static_image_pinned=static_image_pinned,
                static_image_metadata=static_image_metadata,
                static_image_absent_since=static_image_absent_since,
                static_video_pinned=static_video_pinned,
                static_video_metadata=static_video_metadata,
                static_video_absent_since=static_video_absent_since,
                stream_health=stream_health,
                stream_credentials_configured=credentials_configured,
            )

    def _stream_inventory_inputs(self, image_sources):
        """The published stream health and ``credentialsConfigured`` of each
        stream Image_Source (Requirement 4.5); both empty, and nothing
        consulted, when there is none."""
        from model.stream_source import is_stream_source_type

        stream_ids = [
            str(_record_field(source, "imageSourceId"))
            for source in image_sources
            if is_stream_source_type(_record_field(source, "type"))
        ]
        self._stream_reports.forget_except(stream_ids)
        if not stream_ids:
            return {}, {}
        health: Dict[str, Any] = {}
        configured: Dict[str, bool] = {}
        store = None
        try:
            store = self._credentials()
        except Exception:  # noqa: BLE001 - reported as not configured
            logger.exception("The stream Credential_Store could not be opened")
        for image_source_id in stream_ids:
            live = None
            if self._stream_ingest is not None:
                try:
                    live = self._stream_ingest.health_for_image_source(image_source_id)
                except Exception:  # noqa: BLE001 - reported as idle
                    live = None
            health[image_source_id] = self._stream_reports.published(image_source_id, live)["stream"]
            try:
                configured[image_source_id] = bool(store is not None and store.configured(image_source_id))
            except Exception:  # noqa: BLE001
                configured[image_source_id] = False
        return health, configured

    def _device_stream_capabilities(self) -> Optional[Dict[str, Any]]:
        """``deviceCapabilities.streamIngest`` once the probe finished."""
        if self._stream_ingest is None:
            return None
        try:
            return stream_ingest_section(self._stream_ingest.capabilities(wait_s=0))
        except Exception:  # noqa: BLE001 - reported without the section
            logger.exception("Device stream capabilities could not be read")
            return None

    def _static_image_absent_since(self) -> Optional[int]:
        """The ``absentSince`` to report for the unpinned
        Static_Image_Camera, or ``None`` when it was never reported (no
        entry belongs in the report then).

        "Previously reported" is answered from two independent records —
        the version state store (persisted on every successful report,
        so it tracks entries first reported at runtime, after the
        start-time shadow GET) and the start-time shadow reported
        versions floor (which survives state-file loss/corruption); the
        two cover each other's failure modes, and either one knowing the
        entry means the Portal has seen it.

        The timestamp itself is derived at most once per absence episode
        and cached (``_static_absent_since_ms`` — also seeded from the
        shadow's reported entry at start), so it never churns between
        reports: the pin worker marker's ``completedAtEpochMs`` when the
        marker records an applied ``remove`` (cloud-initiated removal —
        the exact removal instant, stable across restarts), else the
        wall clock at the first absent observation."""
        if not self._static_previously_reported():
            return None
        if self._static_absent_since_ms is None:
            self._static_absent_since_ms = self._derive_static_absent_since()
        return self._static_absent_since_ms

    def _static_previously_reported(self) -> bool:
        return self._previously_reported(STATIC_IMAGE_CAMERA_ID)

    def _static_video_absent_since(self) -> Optional[int]:
        """The ``absentSince`` to report for the unpinned
        Static_Video_Camera, or ``None`` when it was never reported —
        :meth:`_static_image_absent_since` for the video camera, with its
        own cached episode timestamp and its own worker's marker
        (Requirement 4.7)."""
        if not self._previously_reported(STATIC_VIDEO_CAMERA_ID):
            return None
        if self._video_absent_since_ms is None:
            self._video_absent_since_ms = self._derive_absent_since(
                self.video_pin_worker, "static-video"
            )
        return self._video_absent_since_ms

    def _previously_reported(self, camera_source_id: str) -> bool:
        """Whether the Portal has already seen ``camera_source_id`` in a
        report, from two independent records (Requirements 6.2, 2.9).

        The start-time shadow reported-versions floor survives state-file
        loss or corruption; the version state store, persisted on every
        successful report, tracks entries first reported at runtime after
        that GET (and covers a GET that failed because the device started
        offline). Either record knowing the id means the Portal has seen
        it. Generalized from the Static_Image_Camera absence gate so the
        duplicate-key retirement reuses exactly this answer rather than
        inventing a third mechanism.
        """
        if camera_source_id in self._reported_versions:
            return True
        try:
            state = self._state_store.load()
        except Exception:  # noqa: BLE001 - state read must not break reports
            logger.exception("Camera sync state store read failed")
            state = None
        return bool(state) and camera_source_id in state

    def _derive_static_absent_since(self) -> int:
        return self._derive_absent_since(self.pin_worker, "static-image")

    def _derive_absent_since(self, worker, label: str) -> int:
        """The absence timestamp for one virtual camera: its pin worker
        marker's ``completedAtEpochMs`` when the marker records an applied
        ``remove``, else the wall clock now."""
        marker = None
        try:
            marker = worker.applied_marker()
        except Exception:  # noqa: BLE001 - marker read must not break reports
            logger.exception(
                "Could not read the %s pin marker for the absence timestamp",
                label,
            )
        if (
            isinstance(marker, Mapping)
            and marker.get("op") == OP_REMOVE
            and marker.get("status") == STATUS_APPLIED
            and isinstance(marker.get("completedAtEpochMs"), (int, float))
        ):
            return int(marker["completedAtEpochMs"])
        return int(self._wall_clock() * 1000)

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
                # What the shadow now holds: every key written with an
                # entry, minus every key written with a null.
                if self._published_keys is None:
                    self._published_keys = set()
                for csid, entry in (document.get("cameras") or {}).items():
                    if isinstance(entry, Mapping):
                        self._published_keys.add(csid)
                    else:
                        self._published_keys.discard(csid)
                for csid, failure in self._consumed_failures.items():
                    if self._apply_failures.get(csid) == failure:
                        del self._apply_failures[csid]
                # The failure keys the shadow now holds: every key written
                # with a failure, minus every key written with a null; the
                # nulls this document carried are done.
                for csid, failure in (document.get("failures") or {}).items():
                    if isinstance(failure, Mapping):
                        self._published_failure_keys.add(csid)
                    else:
                        self._published_failure_keys.discard(csid)
                self._pending_failure_retirements.difference_update(
                    self._consumed_failure_retirements
                )
                # The retirement this document carried is now applied: the
                # shadow key is gone. Prune it from the version floor so
                # nothing re-derives "previously reported" from it — the
                # state store prunes itself, since `advance` rewrites the
                # file from the current inventory, which no longer carries
                # the key (Requirement 2.9's one-shot, no-churn rule).
                for csid in self._consumed_retirements:
                    self._pending_retirements.discard(csid)
                    self._retired_registrations.add(csid)
                    self._reported_versions.pop(csid, None)
                self._consumed_acks = {}
                self._consumed_aliases = {}
                self._consumed_failures = {}
                self._consumed_retirements = set()
                self._consumed_failure_retirements = set()
            return True
        except Exception as exc:  # noqa: BLE001 - offline/transport errors retry
            logger.exception(
                "Camera-registry shadow report failed; retrying with backoff"
            )
            if _is_size_rejection(exc):
                # The limit in effect is lower than the one the cap was
                # derived from (or the pin slots outgrew their reserve):
                # the retry goes out with half the cap.
                self._back_off_report_cap()
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
