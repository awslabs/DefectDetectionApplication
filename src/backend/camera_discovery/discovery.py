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
"""Camera_Discovery enumeration core and re-enumeration loop
(Requirements 2.1, 2.2, 2.3, 2.4, 2.6, 11.2).

Enumerates the physical capture devices present on the Edge_Device by
globbing ``/dev/video*`` and issuing ``VIDIOC_QUERYCAP`` /
``VIDIOC_ENUM_FMT`` / ``VIDIOC_ENUM_FRAMESIZES`` ioctls through the
injectable :class:`camera_discovery.v4l2.V4l2Io` layer.

- Nodes without the ``VIDEO_CAPTURE`` capability (metadata nodes,
  encoders) are skipped silently.
- Tegra CSI drivers are classified ``kind="csi"`` so Jetson CSI sensors
  are distinguishable from ordinary V4L2 cameras.
- Per-node failures are recorded in :attr:`DiscoveryResult.failures` and
  enumeration continues (2.6). A total failure yields an empty result plus
  a logged error — never an exception into LocalServer startup (11.2).
- :meth:`CameraDiscovery.start` runs the periodic re-enumeration loop
  (default every 300 s, configurable through the LocalServer component
  configuration — the same feature-config mechanism ``StationName`` et al
  use, 2.3). Consecutive snapshots are diffed with the pure
  :func:`diff_snapshot`; previously seen stable ids missing from a new
  enumeration are marked absent with an absence timestamp and are never
  dropped from the tracked set (2.4).
"""
import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from camera_discovery import aravis, v4l2

logger = logging.getLogger(__name__)

#: V4L2 driver names that expose Jetson CSI sensors as video nodes
#: (the tegra-video family; JP4/JP5 report "tegra-video", newer stacks
#: report the camrtc capture driver).
TEGRA_CSI_DRIVERS = frozenset(
    {
        "tegra-video",
        "tegra_camera",
        "tegra-camrtc-capture-vi",
        "tegra-camrtc-ca",
    }
)

#: Defensive caps so a misbehaving driver (or fake) cannot spin the
#: enumeration loops forever.
_MAX_FORMATS_PER_NODE = 256
_MAX_FRAMESIZES_PER_FORMAT = 1024

KIND_V4L2 = "v4l2"
KIND_CSI = "csi"

#: Default re-enumeration interval (Requirement 2.3).
DEFAULT_INTERVAL_SECONDS = 300

#: LocalServer component-configuration key overriding the interval,
#: following the existing PascalCase feature-config key convention
#: (``StationName``, ``SoftwareVersion``, ...).
INTERVAL_CONFIG_KEY = "CameraDiscoveryIntervalSeconds"


@dataclass(frozen=True)
class DiscoveredCamera:
    """One enumerated capture device (Requirement 2.2)."""

    stable_id: str
    device_path: str
    card_name: str
    bus_info: str
    driver: str
    kind: str  # "v4l2" | "csi"
    formats: List[Dict[str, Any]]  # [{pixel_format, resolutions: [[w, h], ...]}]


@dataclass
class DiscoveryResult:
    """Outcome of one enumeration pass.

    ``failures`` entries are ``{"device_path": ..., "error": ...}`` — a
    failing node never aborts the pass (Requirement 2.6).
    """

    cameras: List[DiscoveredCamera] = field(default_factory=list)
    failures: List[Dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class TrackedCamera:
    """One camera in the tracked inventory (Requirement 2.4).

    ``absent`` cameras were seen by an earlier enumeration but are missing
    from the latest one; ``absent_since`` is the epoch-ms timestamp of the
    enumeration that first noticed the disappearance. Present cameras carry
    ``absent=False`` and ``absent_since=None``.
    """

    camera: DiscoveredCamera
    absent: bool = False
    absent_since: Optional[int] = None  # epoch ms


@dataclass(frozen=True)
class InventorySnapshot:
    """The tracked inventory handed to ``on_change`` after each diff.

    ``cameras`` maps stable id -> :class:`TrackedCamera` and includes every
    stable id ever seen (absent entries included — ids are never dropped,
    Requirement 2.4). ``failures`` is the latest enumeration's per-node
    failure list.
    """

    cameras: Mapping[str, TrackedCamera]
    failures: Tuple[Dict[str, str], ...] = ()


def diff_snapshot(
    previous: Mapping[str, TrackedCamera],
    result: DiscoveryResult,
    now_ms: int,
) -> Tuple[Dict[str, TrackedCamera], bool]:
    """Pure diff of one enumeration pass against the tracked inventory.

    Returns ``(new_tracked, changed)``:

    - Every camera in ``result`` is tracked as present (a returning camera
      loses its absence marking).
    - Every stable id in ``previous`` missing from ``result`` stays in the
      tracked set marked absent (Requirement 2.4); a camera that was
      already absent keeps its original ``absent_since`` so repeated
      identical enumerations produce identical inventories.
    - ``changed`` is True exactly when the tracked inventory differs from
      ``previous`` (new/updated/returned cameras or new absences).
    """
    tracked: Dict[str, TrackedCamera] = {}
    for camera in result.cameras:
        tracked[camera.stable_id] = TrackedCamera(camera=camera)

    for stable_id, entry in previous.items():
        if stable_id in tracked:
            continue
        if entry.absent:
            tracked[stable_id] = entry  # keep the original absence timestamp
        else:
            tracked[stable_id] = TrackedCamera(
                camera=entry.camera, absent=True, absent_since=now_ms
            )

    changed = tracked != dict(previous)
    return tracked, changed


def make_stable_id(bus_info: str, card_name: str) -> str:
    """``disc-{sha1(bus_info + card)[:12]}`` — stable across reboots and
    ``/dev/videoN`` renumbering, which V4L2 does not guarantee."""
    digest = hashlib.sha1((bus_info + card_name).encode("utf-8")).hexdigest()
    return "disc-" + digest[:12]


class CameraDiscovery:
    """Enumerates physical capture devices through an injectable V4L2 layer.

    ``v4l2_io`` must expose ``list_device_paths()``, ``open(path)``,
    ``close(fd)``, and ``ioctl(fd, request, buffer)``; the default is the
    real :class:`camera_discovery.v4l2.V4l2Io`.

    ``config_provider`` is an optional zero-argument callable returning the
    LocalServer component-configuration dict (the existing feature-config
    mechanism — ``DefectDetectionConfig.get_local_server_config`` on
    device); when it yields a valid ``CameraDiscoveryIntervalSeconds``
    value, that overrides the ``start()`` interval (Requirement 2.3).

    ``aravis_enumerator`` is an optional zero-argument callable returning
    the Aravis bus cameras, forwarded to
    :func:`camera_discovery.aravis.enumerate_aravis` on every periodic
    pass (``None`` uses that function's default lazy import of
    ``aravis_functions.getCameras()`` — aravis-camera-input Requirements
    2.1, 2.7). Aravis stable ids flow through the same tracked-snapshot
    diff as V4L2 ids: same present/absent semantics, same ``absent_since``
    timestamps, ``on_change`` only on change, one timer for both families
    (aravis-camera-input Requirement 2.5).

    ``clock`` returns the current time in seconds since the epoch
    (``time.time`` by default); injectable so tests control absence
    timestamps.
    """

    def __init__(
        self,
        v4l2_io=None,
        config_provider: Optional[Callable[[], Optional[Mapping[str, Any]]]] = None,
        clock: Callable[[], float] = time.time,
        aravis_enumerator: Optional[Callable[[], Sequence[Any]]] = None,
    ):
        self._io = v4l2_io if v4l2_io is not None else v4l2.V4l2Io()
        self._config_provider = config_provider
        self._aravis_enumerator = aravis_enumerator
        self._clock = clock
        self._tracked: Dict[str, TrackedCamera] = {}
        self._latest_failures: Tuple[Dict[str, str], ...] = ()
        self._on_change: Optional[Callable[[InventorySnapshot], None]] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._interval: float = float(DEFAULT_INTERVAL_SECONDS)

    def enumerate(self) -> DiscoveryResult:
        """Enumerate all capture devices currently present.

        Never raises: per-node failures land in ``result.failures`` and a
        total failure (e.g. the device listing itself blowing up) yields an
        empty result plus a logged error (Requirements 2.6, 11.2).
        """
        result = DiscoveryResult()
        try:
            device_paths = self._io.list_device_paths()
        except Exception:
            logger.exception("Camera discovery failed to list video devices")
            return result

        for device_path in device_paths:
            try:
                camera = self._enumerate_node(device_path)
            except Exception as error:  # noqa: BLE001 - isolation per 2.6
                logger.warning(
                    "Camera discovery failed for %s: %s", device_path, error
                )
                result.failures.append(
                    {"device_path": device_path, "error": str(error)}
                )
                continue
            if camera is not None:
                result.cameras.append(camera)
        return result

    # --- periodic re-enumeration loop (Requirements 2.3, 2.4) ------------

    def start(
        self,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        on_change: Optional[Callable[[InventorySnapshot], None]] = None,
    ) -> None:
        """Start the periodic re-enumeration loop on a daemon thread.

        The effective interval is the ``CameraDiscoveryIntervalSeconds``
        feature-config value when present and valid, else
        ``interval_seconds`` (default 300 s — Requirement 2.3). The first
        enumeration runs immediately; ``on_change`` is invoked with an
        :class:`InventorySnapshot` only when the tracked inventory changed.

        Idempotent: calling ``start`` while the loop is running logs a
        warning and returns.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Camera discovery loop already running")
            return

        self._on_change = on_change
        self._interval = self._resolve_interval(interval_seconds)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="camera-discovery",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the re-enumeration loop and wait for the thread to exit."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join()
        self._thread = None

    def run_once(self) -> InventorySnapshot:
        """Run one enumeration + diff pass, firing ``on_change`` if the
        inventory changed; returns the resulting tracked snapshot.

        Each pass runs the V4L2 enumeration and the Aravis enumeration
        (aravis-camera-input Requirement 2.1) and diffs the combined
        result against the tracked inventory in one step — Aravis stable
        ids get the identical absence semantics, and there is no second
        timer (aravis-camera-input Requirements 2.5, 2.7). Downstream
        consumers distinguish the families by the tracked entry's camera
        object type (:class:`DiscoveredCamera` vs
        :class:`camera_discovery.aravis.DiscoveredAravisCamera`).

        This is the loop body — callable directly by tests (and by callers
        wanting an immediate refresh). Never raises: enumeration failures
        are already absorbed by :meth:`enumerate` and
        :func:`camera_discovery.aravis.enumerate_aravis`, and an
        ``on_change`` callback error is logged without killing the loop
        (11.2).
        """
        result = self.enumerate()
        aravis_result = aravis.enumerate_aravis(self._aravis_enumerator)
        result.cameras.extend(aravis_result.cameras)
        result.failures.extend(aravis_result.failures)
        now_ms = int(self._clock() * 1000)

        with self._lock:
            self._tracked, changed = diff_snapshot(self._tracked, result, now_ms)
            self._latest_failures = tuple(result.failures)
            snapshot = InventorySnapshot(
                cameras=dict(self._tracked),
                failures=self._latest_failures,
            )

        if changed and self._on_change is not None:
            try:
                self._on_change(snapshot)
            except Exception:  # noqa: BLE001 - callback isolation per 11.2
                logger.exception("Camera discovery on_change callback failed")
        return snapshot

    @property
    def latest_snapshot(self) -> InventorySnapshot:
        """The tracked inventory as of the last enumeration pass."""
        with self._lock:
            return InventorySnapshot(
                cameras=dict(self._tracked),
                failures=self._latest_failures,
            )

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(self._interval)

    def _resolve_interval(self, interval_seconds: float) -> float:
        """Feature-config override when valid, else ``interval_seconds``."""
        fallback = float(interval_seconds)
        if self._config_provider is None:
            return fallback
        try:
            config = self._config_provider() or {}
            value = config.get(INTERVAL_CONFIG_KEY)
        except Exception:  # noqa: BLE001 - config read must not break startup
            logger.exception(
                "Failed to read camera discovery interval configuration"
            )
            return fallback
        if value is None:
            return fallback
        try:
            configured = float(value)
        except (TypeError, ValueError):
            logger.warning(
                "Ignoring non-numeric %s value %r", INTERVAL_CONFIG_KEY, value
            )
            return fallback
        if configured <= 0:
            logger.warning(
                "Ignoring non-positive %s value %r", INTERVAL_CONFIG_KEY, value
            )
            return fallback
        return configured

    # --- per-node enumeration -------------------------------------------

    def _enumerate_node(self, device_path):
        """Return a DiscoveredCamera, or None when the node is not a
        capture device (skipped, not a failure)."""
        fd = self._io.open(device_path)
        try:
            caps = v4l2.V4l2Capability()
            self._io.ioctl(fd, v4l2.VIDIOC_QUERYCAP, caps)

            if not self._is_capture_node(caps):
                return None

            driver = caps.driver.decode("utf-8", errors="replace")
            card_name = caps.card.decode("utf-8", errors="replace")
            bus_info = caps.bus_info.decode("utf-8", errors="replace")
            kind = KIND_CSI if driver in TEGRA_CSI_DRIVERS else KIND_V4L2

            return DiscoveredCamera(
                stable_id=make_stable_id(bus_info, card_name),
                device_path=device_path,
                card_name=card_name,
                bus_info=bus_info,
                driver=driver,
                kind=kind,
                formats=self._enumerate_formats(fd),
            )
        finally:
            try:
                self._io.close(fd)
            except Exception:  # noqa: BLE001 - close failures are non-fatal
                logger.warning("Failed to close %s", device_path)

    @staticmethod
    def _is_capture_node(caps) -> bool:
        """VIDEO_CAPTURE check against device_caps when the driver reports
        per-node capabilities, else the global capability word."""
        effective = (
            caps.device_caps
            if caps.capabilities & v4l2.V4L2_CAP_DEVICE_CAPS
            else caps.capabilities
        )
        return bool(effective & v4l2.V4L2_CAP_VIDEO_CAPTURE)

    def _enumerate_formats(self, fd):
        """VIDIOC_ENUM_FMT / VIDIOC_ENUM_FRAMESIZES loops; OSError (EINVAL)
        terminates each V4L2 enumeration, per the kernel contract."""
        formats = []
        for index in range(_MAX_FORMATS_PER_NODE):
            desc = v4l2.V4l2FmtDesc()
            desc.index = index
            desc.type = v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
            try:
                self._io.ioctl(fd, v4l2.VIDIOC_ENUM_FMT, desc)
            except OSError:
                break
            formats.append(
                {
                    "pixel_format": v4l2.fourcc_to_str(desc.pixelformat),
                    "resolutions": self._enumerate_framesizes(
                        fd, desc.pixelformat
                    ),
                }
            )
        return formats

    def _enumerate_framesizes(self, fd, pixelformat):
        resolutions = []
        for index in range(_MAX_FRAMESIZES_PER_FORMAT):
            frmsize = v4l2.V4l2FrmSizeEnum()
            frmsize.index = index
            frmsize.pixel_format = pixelformat
            try:
                self._io.ioctl(fd, v4l2.VIDIOC_ENUM_FRAMESIZES, frmsize)
            except OSError:
                break
            if frmsize.type == v4l2.V4L2_FRMSIZE_TYPE_DISCRETE:
                resolutions.append(
                    [int(frmsize.discrete.width), int(frmsize.discrete.height)]
                )
            else:
                # Continuous/stepwise ranges: record the min and max corners.
                resolutions.append(
                    [
                        int(frmsize.stepwise.min_width),
                        int(frmsize.stepwise.min_height),
                    ]
                )
                resolutions.append(
                    [
                        int(frmsize.stepwise.max_width),
                        int(frmsize.stepwise.max_height),
                    ]
                )
                break  # non-discrete types enumerate a single entry
        return resolutions
