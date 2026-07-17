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
"""Aravis (GenICam) enumeration layer for Camera_Discovery.

Feature: aravis-camera-input (Requirements 2.1, 2.3, 2.6, 2.7).

Mirrors the injectable pattern of :mod:`camera_discovery.v4l2`: the
enumeration source is a zero-argument callable returning the Aravis bus
cameras, defaulting to a lazy import of
``edge_ml1_p_camera_management.aravis_functions.getCameras()``. Tests supply
fake enumerators; the lazy import keeps :mod:`camera_discovery` importable
on hosts without the ``gi``/Aravis stack (Requirement 2.7).

- :func:`enumerate_aravis` never raises: an import or enumeration failure
  yields an empty result with a failure record (Requirement 2.6), and a
  camera with no usable identity fields is recorded in the failures list
  and skipped rather than crashing the pass (design Error Handling).
- :func:`aravis_stable_id` is a pure function of the bus-stable GenICam
  identity fields (vendor, model, serial) so the derived Camera_Source
  identifier survives reboots and bus re-enumerations; when the serial is
  empty it falls back to including ``physical_id`` so two serial-less
  cameras of the same model do not collide (Requirement 2.2).
"""
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Prefix of every discovered Aravis Camera_Source identifier.
STABLE_ID_PREFIX = "arv-"

#: The Aravis identity attributes mapped off each enumerated camera object
#: (the ``model.Camera`` shape ``aravis_functions.getCameras()`` returns).
_IDENTITY_ATTRIBUTES = (
    "id",
    "model",
    "address",
    "physical_id",
    "protocol",
    "serial",
    "vendor",
)


@dataclass(frozen=True)
class DiscoveredAravisCamera:
    """One enumerated Aravis bus camera (Requirements 2.1, 2.3)."""

    stable_id: str  # arv-{sha1(vendor|model|serial)[:12]}
    camera_id: str  # the Aravis runtime id camera_manager connects by
    model: str
    address: str
    physical_id: str
    protocol: str  # e.g. "GigEVision" | "USB3Vision" | "Fake"
    serial: str
    vendor: str


@dataclass
class AravisDiscoveryResult:
    """Outcome of one Aravis enumeration pass.

    ``failures`` entries are ``{"error": ...}`` — an enumeration-level
    failure (missing runtime, raising enumerator) or a skipped camera with
    no usable identity. A failure never aborts the pass or raises into the
    caller (Requirement 2.6).
    """

    cameras: List[DiscoveredAravisCamera] = field(default_factory=list)
    failures: List[Dict[str, str]] = field(default_factory=list)


def aravis_stable_id(
    vendor: str, model: str, serial: str, physical_id: str = ""
) -> str:
    """Derive the deterministic Camera_Source id for an Aravis identity.

    ``arv-{sha1(vendor|model|serial)[:12]}`` — a pure function of the
    bus-stable GenICam identity fields, invariant under enumeration order,
    runtime-id, and address changes (Requirement 2.2). When ``serial`` is
    empty the key falls back to including ``physical_id`` so serial-less
    cameras of the same model stay distinct.
    """
    vendor = vendor or ""
    model = model or ""
    serial = serial or ""
    physical_id = physical_id or ""
    if serial:
        key = "|".join((vendor, model, serial))
    else:
        key = "|".join((vendor, model, serial, physical_id))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return STABLE_ID_PREFIX + digest[:12]


def _default_enumerator() -> Sequence[Any]:
    """Lazy default: the edge application's own bus enumeration.

    Imported at call time (not module import time) so ``camera_discovery``
    stays importable where the ``gi``/Aravis stack is absent
    (Requirement 2.7).
    """
    from edge_ml1_p_camera_management import aravis_functions

    return aravis_functions.getCameras()


def _text(value: Any) -> str:
    """Coerce an identity attribute to a plain string ('' for None)."""
    if value is None:
        return ""
    return str(value)


def _map_camera(raw: Any) -> Optional[DiscoveredAravisCamera]:
    """Map one enumerated camera object to a DiscoveredAravisCamera.

    Returns ``None`` when the camera carries no usable identity fields —
    neither a runtime id to connect by nor any bus-stable identity to
    derive a stable id from (design Error Handling).
    """
    fields = {name: _text(getattr(raw, name, None)) for name in _IDENTITY_ATTRIBUTES}

    camera_id = fields["id"]
    has_stable_identity = any(
        fields[name] for name in ("vendor", "model", "serial", "physical_id")
    )
    if not camera_id or not has_stable_identity:
        return None

    return DiscoveredAravisCamera(
        stable_id=aravis_stable_id(
            fields["vendor"],
            fields["model"],
            fields["serial"],
            fields["physical_id"],
        ),
        camera_id=camera_id,
        model=fields["model"],
        address=fields["address"],
        physical_id=fields["physical_id"],
        protocol=fields["protocol"],
        serial=fields["serial"],
        vendor=fields["vendor"],
    )


def enumerate_aravis(
    enumerator: Optional[Callable[[], Sequence[Any]]] = None,
) -> AravisDiscoveryResult:
    """Enumerate the Aravis bus through an injectable enumeration layer.

    ``enumerator`` is a zero-argument callable returning the bus cameras
    (objects carrying the Aravis identity attributes: ``id``, ``model``,
    ``address``, ``physical_id``, ``protocol``, ``serial``, ``vendor``);
    the default lazily imports ``aravis_functions.getCameras()``
    (Requirements 2.1, 2.7).

    Never raises: an import or enumeration failure yields an empty result
    with a failure record, and a camera with no usable identity fields is
    recorded in ``failures`` and skipped (Requirement 2.6).
    """
    if enumerator is None:
        enumerator = _default_enumerator

    try:
        raw_cameras = enumerator()
    except Exception as error:  # noqa: BLE001 - isolation per 2.6
        logger.warning("Aravis enumeration unavailable: %s", error)
        return AravisDiscoveryResult(failures=[{"error": str(error)}])

    result = AravisDiscoveryResult()
    for raw in raw_cameras or ():
        try:
            camera = _map_camera(raw)
        except Exception as error:  # noqa: BLE001 - per-camera isolation
            logger.warning("Failed to map Aravis camera %r: %s", raw, error)
            result.failures.append({"error": str(error)})
            continue
        if camera is None:
            message = (
                "Skipped Aravis camera with no usable identity fields: "
                f"{raw!r}"
            )
            logger.warning(message)
            result.failures.append({"error": message})
            continue
        result.cameras.append(camera)
    return result
