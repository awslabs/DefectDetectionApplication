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
"""The pure ``build_inventory`` merge (Requirements 1.1, 2.5, 11.3).

Merges the device's configured Image_Sources (read through the existing
``ImageSourceAccessor`` — this module never touches SQLite itself) with the
latest Camera_Discovery result into the Camera_Source inventory the
Edge_Sync_Agent reports to the Portal:

- An Image_Source whose resolved device path equals a discovered camera's
  path yields ONE entry: configured id ``cfg-{imageSourceId}``, the
  configured parameters, the discovered capability metadata, origin
  ``edge-configured`` and ``discovered: True`` (Requirement 2.5).
- Discovered hardware not referenced by any Image_Source yields an entry
  with origin ``edge-discovered`` under its discovery stable id.
- Every discovered camera contributes to exactly one entry — a device path
  never appears both merged into a configured entry and as a separate
  discovered entry.

Aravis branch (feature aravis-camera-input, Requirements 2.1, 2.3, 2.4,
7.2), structurally parallel to the device-path merge:

- A configured Image_Source of type ``Camera`` whose ``cameraId`` equals a
  tracked Aravis camera's ``camera_id`` merges into ONE entry under
  ``cfg-{imageSourceId}``: configured params, ``capabilities.aravis``
  identity metadata, ``discovered: True``, and the tracked absent state
  (Requirement 2.4).
- Unmerged Aravis cameras yield ``AravisDiscovered`` / ``edge-discovered``
  entries under their ``arv-`` stable ids (Requirements 2.1, 2.3).
- Families are distinguished by the tracked entry's camera object type
  (:class:`camera_discovery.aravis.DiscoveredAravisCamera` vs
  :class:`camera_discovery.DiscoveredCamera`); inputs containing no Aravis
  cameras produce output identical to the pre-feature merge
  (Requirement 7.2).

``build_inventory`` is pure: it accepts plain data (Image_Source model or
ORM objects — anything attribute- or dict-shaped — and a
``DiscoveryResult`` or ``InventorySnapshot``) and returns a deterministic,
sorted list of :class:`CameraSourceState`.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from camera_discovery.aravis import DiscoveredAravisCamera

ORIGIN_EDGE_CONFIGURED = "edge-configured"
ORIGIN_EDGE_DISCOVERED = "edge-discovered"

#: Reported ``type`` for discovered-only hardware (design: ImageSourceType
#: values + "V4L2Discovered").
TYPE_V4L2_DISCOVERED = "V4L2Discovered"

#: Reported ``type`` for discovered-only Aravis (GenICam) bus cameras
#: (aravis-camera-input Requirement 2.1).
TYPE_ARAVIS_DISCOVERED = "AravisDiscovered"

#: The configured Image_Source ``type`` whose ``cameraId`` references an
#: Aravis camera (merge key for Requirement 2.4).
_ARAVIS_BACKED_SOURCE_TYPE = "Camera"


@dataclass(frozen=True)
class CameraSourceState:
    """One Camera_Source in the device inventory (Requirement 1.1 shape,
    minus the version counter which is assigned by
    :mod:`camera_sync.version_state`)."""

    camera_source_id: str
    name: str
    type: str
    origin: str  # "edge-configured" | "edge-discovered"
    params: Dict[str, Any] = field(default_factory=dict)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    discovered: bool = False
    absent: bool = False
    absent_since: Optional[int] = None  # epoch ms


def configured_camera_source_id(image_source_id: str) -> str:
    """Stable id for a configured Image_Source: ``cfg-{imageSourceId}``."""
    return "cfg-" + str(image_source_id)


def build_inventory(image_sources, discovery_result) -> List[CameraSourceState]:
    """Pure merge of configured Image_Sources with discovered hardware.

    ``image_sources`` is an iterable of Image_Source records (model
    objects, ORM rows, or dicts). ``discovery_result`` is a
    ``camera_discovery.DiscoveryResult`` or ``InventorySnapshot`` (the
    latter carries absence tracking, Requirement 2.4).
    """
    tracked = _normalize_discovery(discovery_result)

    # Discovered V4L2 cameras indexed by device path, and Aravis cameras
    # indexed by camera id; a present camera wins over an absent one
    # claiming the same key (post-renumbering leftovers).
    by_path: Dict[str, Tuple[str, Any, bool, Optional[int]]] = {}
    by_camera_id: Dict[str, Tuple[str, Any, bool, Optional[int]]] = {}
    for stable_id in sorted(tracked):
        camera, absent, absent_since = tracked[stable_id]
        if isinstance(camera, DiscoveredAravisCamera):
            index, key = by_camera_id, camera.camera_id
        else:
            index, key = by_path, camera.device_path
        existing = index.get(key)
        if existing is not None and not existing[2]:
            continue  # keep the present camera already claiming this key
        if existing is None or (existing[2] and not absent):
            index[key] = (stable_id, camera, absent, absent_since)

    merged_stable_ids = set()
    entries: List[CameraSourceState] = []

    for source in sorted(image_sources, key=_image_source_sort_key):
        image_source_id = _get(source, "imageSourceId")
        device_path = _resolve_device_path(source)

        match = None
        if device_path is not None and device_path in by_path:
            candidate = by_path[device_path]
            if candidate[0] not in merged_stable_ids:
                match = candidate

        # Aravis merge by camera id (aravis-camera-input Requirement 2.4):
        # a configured Camera-type Image_Source whose cameraId equals a
        # tracked Aravis camera's id merges into this configured entry.
        aravis_match = None
        if _source_type(source) == _ARAVIS_BACKED_SOURCE_TYPE:
            camera_id = _get(source, "cameraId")
            if camera_id and str(camera_id) in by_camera_id:
                candidate = by_camera_id[str(camera_id)]
                if candidate[0] not in merged_stable_ids:
                    aravis_match = candidate

        params = _configured_params(source, device_path)
        if match is None and aravis_match is None:
            entries.append(
                CameraSourceState(
                    camera_source_id=configured_camera_source_id(image_source_id),
                    name=_get(source, "name") or "",
                    type=_source_type(source),
                    origin=ORIGIN_EDGE_CONFIGURED,
                    params=params,
                )
            )
            continue

        capabilities: Dict[str, Any] = {}
        absent = False
        absent_since: Optional[int] = None
        if match is not None:
            stable_id, camera, absent, absent_since = match
            merged_stable_ids.add(stable_id)
            capabilities = _capabilities(camera)
        if aravis_match is not None:
            aravis_stable_id, aravis_camera, aravis_absent, aravis_absent_since = (
                aravis_match
            )
            merged_stable_ids.add(aravis_stable_id)
            capabilities["aravis"] = _aravis_identity(aravis_camera)
            if match is None:
                absent = aravis_absent
                absent_since = aravis_absent_since
        entries.append(
            CameraSourceState(
                camera_source_id=configured_camera_source_id(image_source_id),
                name=_get(source, "name") or "",
                type=_source_type(source),
                origin=ORIGIN_EDGE_CONFIGURED,
                params=params,
                capabilities=capabilities,
                discovered=True,
                absent=absent,
                absent_since=absent_since,
            )
        )

    for stable_id in sorted(tracked):
        if stable_id in merged_stable_ids:
            continue
        camera, absent, absent_since = tracked[stable_id]
        # A camera whose merge key merged under a different stable id
        # (absent leftover displaced by a present camera) still reports
        # separately — its stable id is what bindings reference.
        if isinstance(camera, DiscoveredAravisCamera):
            entries.append(
                CameraSourceState(
                    camera_source_id=stable_id,
                    name=f"{camera.vendor} {camera.model}",
                    type=TYPE_ARAVIS_DISCOVERED,
                    origin=ORIGIN_EDGE_DISCOVERED,
                    params={
                        "cameraId": camera.camera_id,
                        "serial": camera.serial,
                        "protocol": camera.protocol,
                        "address": camera.address,
                    },
                    capabilities={"aravis": _aravis_identity(camera)},
                    discovered=True,
                    absent=absent,
                    absent_since=absent_since,
                )
            )
            continue
        entries.append(
            CameraSourceState(
                camera_source_id=stable_id,
                name=camera.card_name,
                type=TYPE_V4L2_DISCOVERED,
                origin=ORIGIN_EDGE_DISCOVERED,
                params={"devicePath": camera.device_path},
                capabilities=_capabilities(camera),
                discovered=True,
                absent=absent,
                absent_since=absent_since,
            )
        )

    return entries


# --- helpers -----------------------------------------------------------------


def _normalize_discovery(discovery_result) -> Dict[str, Tuple[Any, bool, Optional[int]]]:
    """Normalize a ``DiscoveryResult`` or ``InventorySnapshot`` into
    ``{stable_id: (camera, absent, absent_since)}``."""
    tracked: Dict[str, Tuple[Any, bool, Optional[int]]] = {}
    if discovery_result is None:
        return tracked

    cameras = getattr(discovery_result, "cameras", None)
    if isinstance(cameras, Mapping):  # InventorySnapshot
        for stable_id, entry in cameras.items():
            tracked[stable_id] = (entry.camera, entry.absent, entry.absent_since)
    elif cameras is not None:  # DiscoveryResult
        for camera in cameras:
            tracked[camera.stable_id] = (camera, False, None)
    return tracked


def _get(obj, key, default=None):
    """Dict- or attribute-style access, tolerant of both record shapes."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _image_source_sort_key(source) -> str:
    return str(_get(source, "imageSourceId") or "")


def _source_type(source) -> str:
    """The Image_Source's ``type`` as the design's ImageSourceType VALUE
    (e.g. "Camera", "Folder"). Real ORM rows carry an ``ImageSourceType``
    enum member (whose ``str()`` is the member repr, not its value);
    dict-shaped records already carry the plain string."""
    raw = _get(source, "type")
    if raw is None:
        return ""
    return str(getattr(raw, "value", raw) or "")


def _resolve_device_path(source) -> Optional[str]:
    """The Image_Source's resolved device path: the ``device`` value on its
    attached image-source configuration, when present."""
    configuration = _get(source, "imageSourceConfiguration")
    if not configuration:
        return None
    device = _get(configuration, "device")
    if device:
        return str(device)
    return None


def _configured_params(source, device_path: Optional[str]) -> Dict[str, Any]:
    """The configured, portal-relevant parameters, omitting empty values."""
    configuration = _get(source, "imageSourceConfiguration") or {}
    params: Dict[str, Any] = {}
    if device_path is not None:
        params["devicePath"] = device_path
    for source_key, param_key in (
        ("cameraId", "cameraId"),
        ("location", "location"),
        ("description", "description"),
    ):
        value = _get(source, source_key)
        if value:
            params[param_key] = value
    for config_key, param_key in (
        ("gain", "gain"),
        ("exposure", "exposure"),
        ("deviceName", "deviceName"),
    ):
        value = _get(configuration, config_key)
        if value is not None:
            params[param_key] = value
    return params


def _aravis_identity(camera) -> Dict[str, Any]:
    """The discovered Aravis identity metadata reported under
    ``capabilities.aravis`` (aravis-camera-input Requirement 2.3)."""
    return {
        "model": camera.model,
        "address": camera.address,
        "physicalId": camera.physical_id,
        "protocol": camera.protocol,
        "serial": camera.serial,
        "vendor": camera.vendor,
    }


def _capabilities(camera) -> Dict[str, Any]:
    """Discovered capability metadata in the reported-document shape."""
    return {
        "formats": [
            {
                "pixelFormat": fmt.get("pixel_format"),
                "resolutions": [list(r) for r in fmt.get("resolutions", [])],
            }
            for fmt in camera.formats
        ],
        "driver": camera.driver,
        "busInfo": camera.bus_info,
        "kind": camera.kind,
    }
