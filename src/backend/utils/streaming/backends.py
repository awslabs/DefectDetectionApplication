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
"""Backend abstraction for the concurrent camera stream broadcaster.

A single ``CameraBackend`` interface lets the ``StreamBroadcaster`` /
``StreamSession`` treat both camera families (GenICam / USB3Vision via Aravis
and NVIDIA CSI / ICAM via GStreamer) identically (Req 2.8). Each backend owns
exactly one ``Device_Claim`` for its physical camera; the acquisition worker is
the only code that drives a backend, looping ``grab()`` -> publish.

``AravisBackend`` wraps the existing Aravis ``Camera`` acquisition path;
``GStreamerBackend`` wraps a persistent GStreamer pipeline that ends in an
``appsink`` pull loop (replacing the one-shot ``execute_image_source_pipeline``
for the live-view path). Both keep their ``gi`` imports lazy so this module stays
importable on hosts without the GenICam / GStreamer stack.
"""
import logging
import os
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional, Protocol, runtime_checkable

from utils.streaming.models import StreamConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawFrame:
    """A single frame as returned by a backend ``grab()``.

    Carries the raw image payload plus the dimensions needed to build a
    ``LatestFrame``. The monotonic sequence number and acquired-at timestamp
    that complete a ``LatestFrame`` are assigned by the session at publish time,
    not by the backend, so they are intentionally absent here.

    Attributes:
        data: Raw image payload bytes (same shape as today's ``get_frame``
            dict ``data`` field).
        width: Image width in pixels.
        height: Image height in pixels.
    """

    data: bytes
    width: int
    height: int


@runtime_checkable
class CameraBackend(Protocol):
    """Common interface over the Aravis and GStreamer acquisition paths.

    Implementations hold a single ``Device_Claim`` per physical camera. The
    broadcaster/session drives the lifecycle: ``open()`` -> ``start_stream()``
    -> repeated ``grab()`` -> ``stop_stream()`` -> ``close()``. ``apply_features``
    may be called on a running stream to adjust live controls.
    """

    def open(self) -> None:
        """Acquire the single ``Device_Claim`` for the camera.

        Implementations attempt the open at most ``max_open_attempts`` (3) times
        within the configured open timeout (Req 7.6). Raises on failure so the
        broadcaster can reject the subscribe with ``camera_unavailable``.
        """
        ...

    def start_stream(self) -> None:
        """Begin continuous acquisition.

        Aravis cameras switch from per-request software-trigger to a
        worker-driven continuous acquisition; GStreamer cameras start a
        persistent ``appsink`` pull loop.
        """
        ...

    def grab(self, timeout_ms: int) -> "RawFrame | None":
        """Grab the next frame, waiting at most ``timeout_ms`` milliseconds.

        Returns a ``RawFrame`` on success, or ``None`` on timeout / acquisition
        failure (which the worker treats as a disconnect signal, Req 7.1).
        """
        ...

    def apply_features(self, features: dict) -> dict:
        """Apply camera control values (gain / exposure / advanced GenICam
        features) to the live stream.

        Returns the device-accepted values so callers can reflect what the
        hardware actually applied (values may be coerced/clamped).
        """
        ...

    def stop_stream(self) -> None:
        """Stop continuous acquisition without releasing the device claim."""
        ...

    def close(self) -> None:
        """Release the single ``Device_Claim`` for the camera."""
        ...


# Default device-open retry budget (Req 7.6) used when a StreamConfig is absent.
_DEFAULT_MAX_OPEN_ATTEMPTS = 3


class CaptureConfigValidationError(ValueError):
    """Raised when a per-capture image-source config has an out-of-range value.

    Carries the name of the offending ``parameter`` so callers (and the
    inference/capture path) can surface exactly which control was invalid while
    rejecting the request (Req 6.5). Subclasses :class:`ValueError` so this stays
    a plain, import-safe exception with no ``gi`` / hardware dependency.
    """

    def __init__(self, parameter, message):
        self.parameter = parameter
        super().__init__(message)


# Top-level image-source-config keys that carry numeric controls; each is range
# checked against the same-named entry in a ``get_feature_bounds`` map.
_NUMERIC_TOP_LEVEL_KEYS = ("gain", "exposure")


def _as_number(value):
    """Return ``value`` as a number, or ``None`` if it is not numeric.

    Booleans are intentionally rejected (returned as ``None``) so a boolean
    supplied for a numeric control (gain / exposure) is treated as invalid rather
    than silently coerced to 0/1.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_boolean_like(value):
    """True when ``value`` is a valid boolean control value (true/false)."""
    if isinstance(value, bool):
        return True
    if isinstance(value, int) and value in (0, 1):
        return True
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "0", "1"):
        return True
    return False


def _validate_against_entry(name, value, entry):
    """Validate a single control ``value`` against its bounds ``entry``.

    Raises :class:`CaptureConfigValidationError` (naming ``name``) when the value
    is outside the entry's accepted range: not one of an enumeration's options,
    not a valid boolean, non-numeric for a numeric control, or below ``min`` /
    above ``max``.
    """
    kind = entry.get("type")

    if kind == "enumeration":
        options = entry.get("options") or []
        # Only enforce membership when the device reported the allowed set.
        if options and str(value) not in options:
            raise CaptureConfigValidationError(
                name, f"{name} '{value}' is not one of {options}"
            )
        return

    if kind == "boolean":
        if not _is_boolean_like(value):
            raise CaptureConfigValidationError(
                name,
                f"{name} '{value}' is not a valid boolean (expected true or false)",
            )
        return

    # Numeric controls (float / integer): range-check against min/max when known.
    numeric = _as_number(value)
    if numeric is None:
        raise CaptureConfigValidationError(
            name, f"{name} '{value}' is not a valid number"
        )
    lo = entry.get("min")
    hi = entry.get("max")
    if (lo is not None and numeric < lo) or (hi is not None and numeric > hi):
        raise CaptureConfigValidationError(
            name, f"{name} {value} is outside the allowed range [{lo}, {hi}]"
        )


def validate_config_against_bounds(config, bounds):
    """Validate a per-capture image-source configuration against feature bounds.

    Pure validation logic (no device / ``gi`` access) so it is unit-testable in a
    bare checkout. Given an image-source-style ``config`` (``gain`` / ``exposure``
    floats plus an ``advancedSettings`` dict of enumeration / boolean / numeric
    controls) and a ``bounds`` map in the shape produced by
    ``Camera.get_feature_bounds`` (keyed by control, each entry carrying ``type``
    and ``min`` / ``max`` / ``options``), raise
    :class:`CaptureConfigValidationError` naming the first offending parameter
    when any supplied value falls outside its accepted range.

    Parameters absent from ``bounds`` (no known range for this device) are left
    unvalidated. ``None`` values are skipped (the control is simply not being
    set). Returns ``None`` on success and never mutates ``config`` or ``bounds``,
    so a rejected config leaves the previously active configuration unchanged
    (Req 6.5).
    """
    if not config or not bounds:
        return None

    # Top-level numeric controls (gain / exposure).
    for key in _NUMERIC_TOP_LEVEL_KEYS:
        value = config.get(key)
        if value is None:
            continue
        entry = bounds.get(key)
        if entry:
            _validate_against_entry(key, value, entry)

    # Advanced controls (enumeration / boolean / numeric), e.g. balanceWhiteAuto,
    # reverseX / reverseY.
    advanced = config.get("advancedSettings") or {}
    for key, value in advanced.items():
        if value is None:
            continue
        entry = bounds.get(key)
        if entry:
            _validate_against_entry(key, value, entry)

    return None


def _load_aravis_runtime() -> SimpleNamespace:
    """Lazily import the Aravis-backed camera runtime.

    The ``gi`` / Aravis bindings — and ``camera_manager`` itself, which imports
    them at module load and additionally spins up a multiprocessing manager — are
    imported here rather than at module top level. This keeps ``backends.py``
    importable in a bare checkout / on hosts without the GenICam stack, which is
    why the ``CameraBackend`` Protocol and ``GStreamerBackend`` can live alongside
    ``AravisBackend`` without forcing a hard dependency on Aravis. It mirrors the
    ``gi.require_version('Aravis', '0.8')`` usage already in ``camera_manager``.

    Returns:
        A namespace bundling ``Aravis``, the existing ``Camera`` class,
        ``CONFIG_FEATURE_MAP``, ``CameraStatusEnum``, ``Timer`` and
        ``AravisCameraException`` for use by :class:`AravisBackend`.
    """
    import gi

    gi.require_version("Aravis", "0.8")
    from gi.repository import Aravis  # noqa: E402

    from utils.camera_manager import Camera, CONFIG_FEATURE_MAP
    from utils.common import CameraStatusEnum
    from metrics.collector import Timer
    from exceptions.api.aravis_camera_exception import AravisCameraException

    return SimpleNamespace(
        Aravis=Aravis,
        Camera=Camera,
        CONFIG_FEATURE_MAP=CONFIG_FEATURE_MAP,
        CameraStatusEnum=CameraStatusEnum,
        Timer=Timer,
        AravisCameraException=AravisCameraException,
    )


class AravisBackend:
    """``CameraBackend`` adapter over the existing Aravis ``Camera`` path.

    Wraps the ``Camera`` class from ``camera_manager`` and converts its
    per-request software-trigger model into the broadcaster's worker-driven
    continuous acquisition:

    * :meth:`open` connects (acquiring the single ``Device_Claim``) reusing the
      ``Aravis.Camera.new`` connect plus the register-cache-disable + trigger
      setup from ``Camera.set_camera``; it retries at most
      ``max_open_attempts`` (3) times within ``open_timeout_ms`` (Req 7.6).
    * :meth:`start_stream` calls ``start_acquisition`` once (continuous mode).
    * :meth:`grab` issues a ``software_trigger`` then a bounded
      ``timeout_pop_buffer`` read, returning a :class:`RawFrame`.
    * :meth:`stop_stream` calls ``stop_acquisition`` (claim retained).
    * :meth:`close` disconnects, releasing the ``Device_Claim``.

    ``apply_features`` is routed through the existing ``apply_device_features`` /
    ``_apply_config_features`` logic so gain / exposure / advanced GenICam
    controls are applied exactly as the legacy path applies them.

    This adapter is intended to be driven by a single acquisition worker thread
    (the only code that touches the device handle), matching the broadcast
    design's single-producer model.
    """

    def __init__(self, camera_id, image_source_config=None, stream_config=None):
        """Create an adapter for one physical camera.

        Args:
            camera_id: Identifier passed to ``Aravis.Camera.new`` (e.g. a
                discovered device id or a ``Fake_*`` id).
            image_source_config: Optional image-source config (gain / exposure /
                ``advancedSettings``) applied when acquisition starts, reusing the
                ``Camera.start_acquisition`` path.
            stream_config: :class:`StreamConfig` governing open retry budget and
                open timeout; defaults are used when omitted.
        """
        self.camera_id = camera_id
        self._image_source_config = image_source_config
        self._stream_config = stream_config or StreamConfig()
        self._rt = None          # lazily-loaded Aravis runtime namespace
        self._camera = None      # the wrapped Camera instance == the Device_Claim
        self._streaming = False

    def open(self) -> None:
        """Acquire the single ``Device_Claim`` for the camera.

        Attempts to connect at most ``max_open_attempts`` (3) times and never past
        the ``open_timeout_ms`` deadline. Each attempt constructs a ``Camera``
        (which runs the ``Aravis.Camera.new`` connect plus the trigger /
        register-cache setup) and accepts it only when it reports
        ``CONNECTED``. Raises ``AravisCameraException`` when every attempt fails so
        the broadcaster can reject the subscribe with ``camera_unavailable``
        (Req 7.6).
        """
        rt = _load_aravis_runtime()
        self._rt = rt

        max_attempts = max(1, int(self._stream_config.max_open_attempts or _DEFAULT_MAX_OPEN_ATTEMPTS))
        deadline = time.monotonic() + (self._stream_config.open_timeout_ms / 1000.0)
        last_error = None
        attempt = 0
        for attempt in range(1, max_attempts + 1):
            try:
                camera = rt.Camera(self.camera_id)
                status = camera.get_status()
                if status is not None and status.status == rt.CameraStatusEnum.CONNECTED:
                    self._camera = camera
                    logger.info(
                        f"AravisBackend {self.camera_id}: opened device claim on attempt {attempt}"
                    )
                    return
                last_error = getattr(status, "error", None) or "camera did not reach CONNECTED state"
                logger.warning(
                    f"AravisBackend {self.camera_id}: open attempt {attempt} not connected: {last_error}"
                )
                self._safe_disconnect(camera)
            except Exception as e:
                last_error = str(e)
                logger.warning(f"AravisBackend {self.camera_id}: open attempt {attempt} failed: {e}")

            if time.monotonic() >= deadline:
                logger.error(
                    f"AravisBackend {self.camera_id}: open timeout exceeded after {attempt} attempt(s)"
                )
                break

        raise rt.AravisCameraException(
            f"Unable to open camera {self.camera_id} after {attempt} attempt(s): {last_error}"
        )

    def start_stream(self) -> None:
        """Begin continuous acquisition.

        Calls ``Camera.start_acquisition`` once with the configured image-source
        config (applying gain / exposure / advanced controls via the existing
        path), replacing the legacy per-request start/stop cycle.
        """
        self._require_open()
        self._camera.start_acquisition(self._image_source_config)
        self._streaming = True
        logger.info(f"AravisBackend {self.camera_id}: continuous acquisition started")

    def grab(self, timeout_ms: int) -> "RawFrame | None":
        """Grab the next frame, waiting at most ``timeout_ms`` milliseconds.

        Software-triggers the camera and pops a buffer with a bounded
        ``timeout_pop_buffer`` (reusing the legacy ``Camera.get_frame`` read
        logic). Returns a :class:`RawFrame` on success, or ``None`` on timeout or
        a non-success buffer status — which the acquisition worker treats as a
        disconnect signal (Req 7.1).
        """
        cam = self._camera
        if cam is None or getattr(cam, "camera", None) is None:
            return None
        rt = self._rt
        Aravis = rt.Aravis
        timeout_us = int(max(0, timeout_ms) * 1000)

        cam._lock.acquire()
        try:
            with rt.Timer(metric_name="CameraGetFrameTime"):
                cam.camera.software_trigger()
                arv_buffer = cam.stream.timeout_pop_buffer(timeout_us)
                if arv_buffer is None:
                    logger.error(
                        f"AravisBackend {self.camera_id}: timed out after {timeout_us} us waiting for a frame"
                    )
                    cam.update_camera_status(
                        rt.CameraStatusEnum.DISCONNECTED,
                        "Timed out waiting for a frame from the camera",
                    )
                    return None
                # Re-arm the stream with a fresh buffer for the next grab.
                cam.set_buffer()
                if arv_buffer.get_status() != Aravis.BufferStatus.SUCCESS:
                    logger.error(f"AravisBackend {self.camera_id}: failed to get frame")
                    cam.update_camera_status(rt.CameraStatusEnum.DISCONNECTED, "Failed to get frame")
                    return None
                data = arv_buffer.get_data()
                width = arv_buffer.get_image_width()
                height = arv_buffer.get_image_height()
                cam.update_camera_status(rt.CameraStatusEnum.CONNECTED)
                return RawFrame(data=data, width=width, height=height)
        finally:
            cam._lock.release()

    def apply_features(self, features: dict) -> dict:
        """Apply gain / exposure / advanced GenICam controls to the live stream.

        Routes through the existing ``Camera.apply_device_features`` logic (which
        also reuses the ``CONFIG_FEATURE_MAP`` used by ``_apply_config_features``).
        Accepts either a device-feature list (``[{"feature","type","value"}, ...]``)
        or an image-source-style dict with ``gain`` / ``exposure`` /
        ``advancedSettings`` keys, normalizing the latter into the device-feature
        list form. Because ``apply_device_features`` stops acquisition while it
        writes, continuous acquisition is resumed afterwards so the live session
        keeps producing frames. Returns the device-accepted values.

        A per-capture image-source-style config (dict) is validated against the
        device's feature bounds *before* anything is written, so an out-of-range
        value is rejected with :class:`CaptureConfigValidationError` naming the
        offending parameter and the already-applied device configuration is left
        unchanged (Req 6.5).
        """
        self._require_open()
        self._validate_capture_config(features)
        feature_list = self._normalize_features(features)
        applied = self._camera.apply_device_features(feature_list) if feature_list else {}
        if self._streaming:
            # apply_device_features stops acquisition before writing features;
            # resume the continuous stream for the live session.
            self._camera.start_acquisition(None)
        return applied

    def stop_stream(self) -> None:
        """Stop continuous acquisition without releasing the device claim."""
        if self._camera is None or not self._streaming:
            return
        self._camera.stop_acquisition()
        self._streaming = False
        logger.info(f"AravisBackend {self.camera_id}: continuous acquisition stopped")

    def close(self) -> None:
        """Release the single ``Device_Claim`` for the camera."""
        if self._camera is None:
            return
        try:
            self._camera.disconnect()
        finally:
            self._camera = None
            self._streaming = False
            logger.info(f"AravisBackend {self.camera_id}: device claim released")

    # --- internal helpers -------------------------------------------------

    def _require_open(self) -> None:
        """Raise if the device claim has not been acquired via :meth:`open`."""
        if self._camera is None:
            rt = self._rt or _load_aravis_runtime()
            raise rt.AravisCameraException(
                f"Camera {self.camera_id} is not open; call open() before this operation"
            )

    def _validate_capture_config(self, features) -> None:
        """Reject an out-of-range per-capture image-source config before applying.

        Only image-source-style dict configs (``gain`` / ``exposure`` /
        ``advancedSettings``) are validated; already-built device-feature lists are
        left to the existing apply path. Bounds are read from the live device via
        ``Camera.get_feature_bounds`` (the same shape ``get_camera_feature_bounds``
        exposes); when no bounds are readable the config is left unvalidated.
        Raises :class:`CaptureConfigValidationError` (naming the offending
        parameter) so the caller can reject the request with the active
        configuration untouched (Req 6.5).
        """
        if not isinstance(features, dict):
            return
        try:
            bounds = self._camera.get_feature_bounds()
        except Exception as e:  # pragma: no cover - bounds read best-effort
            logger.warning(
                f"AravisBackend {self.camera_id}: unable to read feature bounds for validation: {e}"
            )
            return
        validate_config_against_bounds(features, bounds)

    def _safe_disconnect(self, camera) -> None:
        """Best-effort disconnect of a failed/rejected ``Camera`` instance."""
        try:
            camera.disconnect()
        except Exception as e:  # pragma: no cover - best-effort cleanup
            logger.warning(f"AravisBackend {self.camera_id}: error cleaning up failed open: {e}")

    def _normalize_features(self, features) -> list:
        """Normalize an ``apply_features`` argument into a device-feature list.

        Accepts either an already-built device-feature list, or an image-source
        config dict with ``gain`` / ``exposure`` / ``advancedSettings`` (and/or an
        explicit ``features`` list), returning the
        ``[{"feature","type","value"}, ...]`` form expected by
        ``Camera.apply_device_features``. Advanced settings are mapped through the
        existing ``CONFIG_FEATURE_MAP`` so only supported safe controls are sent.
        """
        if not features:
            return []
        if isinstance(features, list):
            return features

        rt = self._rt or _load_aravis_runtime()
        feature_list = []

        explicit = features.get("features")
        if isinstance(explicit, list):
            feature_list.extend(explicit)

        if features.get("gain") is not None:
            feature_list.append({"feature": "Gain", "type": "float", "value": features["gain"]})
        if features.get("exposure") is not None:
            feature_list.append(
                {"feature": "ExposureTime", "type": "float", "value": features["exposure"]}
            )

        advanced = features.get("advancedSettings") or {}
        for key, value in advanced.items():
            if value is None:
                continue
            mapping = rt.CONFIG_FEATURE_MAP.get(key)
            if not mapping:
                continue
            genicam_feature, kind = mapping
            feature_list.append({"feature": genicam_feature, "type": kind, "value": value})

        return feature_list


def _load_gstreamer_runtime() -> SimpleNamespace:
    """Lazily import the GStreamer-backed live-view runtime.

    The ``gi`` / ``Gst`` / ``GstApp`` bindings — and the GStreamer pipeline
    helpers that import them transitively — are imported here rather than at
    module load. This keeps ``backends.py`` importable in a bare checkout / on
    hosts without the GStreamer stack, exactly as :func:`_load_aravis_runtime`
    does for Aravis. It mirrors the ``gi.require_version('Gst', '1.0')`` /
    ``GstApp`` usage already in ``gstreamer/gst_pipeline.py``.

    Returns:
        A namespace bundling ``Gst``, ``GstApp``, ``GLib``, the existing
        ``GstPipelineBuilder``, ``Timer``, ``PipelineExecutionException`` and the
        ``utils`` module for use by :class:`GStreamerBackend`.
    """
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstApp", "1.0")
    from gi.repository import Gst, GstApp, GLib  # noqa: E402

    from gstreamer.pipeline_builder import GstPipelineBuilder
    from metrics.collector import Timer
    from exceptions.api.gst_pipeline_exception import PipelineExecutionException
    from utils import utils

    return SimpleNamespace(
        Gst=Gst,
        GstApp=GstApp,
        GLib=GLib,
        GstPipelineBuilder=GstPipelineBuilder,
        Timer=Timer,
        PipelineExecutionException=PipelineExecutionException,
        utils=utils,
    )


class GStreamerBackend:
    """``CameraBackend`` adapter over a persistent GStreamer ``appsink`` pipeline.

    Replaces the one-shot ``execute_image_source_pipeline`` (build → run → tear
    down per request) used for the live-view path with a single long-lived
    pipeline that ends in an ``appsink``, which the acquisition worker pulls in a
    loop. NVIDIA CSI / ICAM cameras therefore switch from per-request capture to
    the broadcast design's single-producer continuous model, the GStreamer mirror
    of :class:`AravisBackend`'s continuous-acquisition switch.

    Lifecycle (driven by the broadcaster / acquisition worker):

    * :meth:`open` builds the live pipeline (reusing ``GstPipelineBuilder`` to add
      the configured image source, then appending
      ``videoconvert ! video/x-raw,format=RGB ! appsink``) and sets it to
      ``PAUSED`` — passing through ``READY`` — which opens the device and acquires
      the single source claim. It retries at most ``max_open_attempts`` (3) times
      within ``open_timeout_ms`` (Req 7.6).
    * :meth:`start_stream` sets the pipeline to ``PLAYING`` (begins acquisition).
    * :meth:`grab` pulls one sample from the ``appsink`` with a bounded timeout and
      returns a :class:`RawFrame`; a timeout / missing sample returns ``None`` so
      the worker can treat it as a disconnect signal (Req 7.1).
    * :meth:`stop_stream` sets the pipeline back to ``PAUSED`` (claim retained).
    * :meth:`close` sets the pipeline to ``NULL``, releasing the source claim.

    The ``appsink`` is configured ``sync=false max-buffers=1 drop=true`` so the
    pipeline always holds only the most recent frame (drop-to-latest), matching
    the broadcast model's no-backpressure latest-frame slot.

    This adapter is intended to be driven by a single acquisition worker thread
    (the only code that touches the pipeline), matching the single-producer model.
    """

    _APPSINK_NAME = "appsink"

    def __init__(self, camera_id, image_source=None, stream_config=None, pipeline_description=None):
        """Create an adapter for one GStreamer-driven camera.

        Args:
            camera_id: Identifier of the physical camera (used for logging /
                claim tracking).
            image_source: The image-source description understood by
                ``GstPipelineBuilder.add_image_source`` (``type`` +
                ``imageSourceConfiguration``), used to build the source portion of
                the live pipeline. Ignored when ``pipeline_description`` is given.
            stream_config: :class:`StreamConfig` governing open retry budget and
                open timeout; defaults are used when omitted.
            pipeline_description: Optional explicit GStreamer launch string for the
                source portion of the pipeline (everything before the appended
                ``appsink`` stage). When provided it is used verbatim instead of
                building from ``image_source`` — useful for ICAM/CSI variants or a
                simulated source in tests.
        """
        self.camera_id = camera_id
        self._image_source = image_source
        self._stream_config = stream_config or StreamConfig()
        self._pipeline_description = pipeline_description
        self._rt = None          # lazily-loaded GStreamer runtime namespace
        self._pipeline = None     # the persistent pipeline == the source claim
        self._appsink = None      # the appsink element pulled by grab()
        self._streaming = False

    def open(self) -> None:
        """Acquire the single source claim by building and prerolling the pipeline.

        Builds the live ``appsink`` pipeline and sets it to ``PAUSED`` (which
        transitions through ``READY``, opening the device). Attempts at most
        ``max_open_attempts`` (3) times and never past the ``open_timeout_ms``
        deadline, tearing a failed pipeline back down to ``NULL`` between attempts.
        Raises ``PipelineExecutionException`` when every attempt fails so the
        broadcaster can reject the subscribe with ``camera_unavailable`` (Req 7.6).
        """
        rt = _load_gstreamer_runtime()
        self._rt = rt
        Gst = rt.Gst

        # Mirror gst_pipeline.run_pipeline: ensure the DDA GStreamer plugins are
        # discoverable before initializing/parsing the pipeline.
        try:
            os.environ["GST_PLUGIN_PATH"] = rt.utils.get_gst_plugins_path()
        except Exception as e:  # pragma: no cover - env best-effort
            logger.warning(f"GStreamerBackend {self.camera_id}: could not set GST_PLUGIN_PATH: {e}")
        Gst.init(None)

        pipeline_str = self._build_pipeline_string()
        logger.info(f"GStreamerBackend {self.camera_id}: live pipeline = {pipeline_str}")

        max_attempts = max(1, int(self._stream_config.max_open_attempts or _DEFAULT_MAX_OPEN_ATTEMPTS))
        open_timeout_s = self._stream_config.open_timeout_ms / 1000.0
        deadline = time.monotonic() + open_timeout_s
        last_error = None
        attempt = 0
        for attempt in range(1, max_attempts + 1):
            pipeline = None
            try:
                pipeline = Gst.parse_launch(pipeline_str)
                appsink = pipeline.get_by_name(self._APPSINK_NAME)
                if appsink is None:
                    raise rt.PipelineExecutionException(
                        f"live pipeline for {self.camera_id} has no '{self._APPSINK_NAME}' element"
                    )
                self._configure_appsink(appsink)

                ret = pipeline.set_state(Gst.State.PAUSED)
                if ret == Gst.StateChangeReturn.FAILURE:
                    raise rt.PipelineExecutionException(
                        f"pipeline for {self.camera_id} failed to change state to PAUSED"
                    )
                # Bounded wait for the state change to settle (live sources report
                # NO_PREROLL rather than SUCCESS; only FAILURE is fatal).
                remaining_s = max(0.0, deadline - time.monotonic())
                state_ret, _state, _pending = pipeline.get_state(int(remaining_s * Gst.SECOND))
                if state_ret == Gst.StateChangeReturn.FAILURE:
                    raise rt.PipelineExecutionException(
                        f"pipeline for {self.camera_id} failed to reach PAUSED/READY"
                    )

                self._pipeline = pipeline
                self._appsink = appsink
                logger.info(
                    f"GStreamerBackend {self.camera_id}: opened source claim on attempt {attempt}"
                )
                return
            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"GStreamerBackend {self.camera_id}: open attempt {attempt} failed: {e}"
                )
                self._safe_null(pipeline)

            if time.monotonic() >= deadline:
                logger.error(
                    f"GStreamerBackend {self.camera_id}: open timeout exceeded after {attempt} attempt(s)"
                )
                break

        raise rt.PipelineExecutionException(
            f"Unable to open camera {self.camera_id} after {attempt} attempt(s): {last_error}"
        )

    def start_stream(self) -> None:
        """Begin continuous acquisition by setting the pipeline to ``PLAYING``."""
        self._require_open()
        Gst = self._rt.Gst
        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise self._rt.PipelineExecutionException(
                f"pipeline for {self.camera_id} failed to change state to PLAYING"
            )
        self._streaming = True
        logger.info(f"GStreamerBackend {self.camera_id}: pipeline PLAYING (acquisition started)")

    def grab(self, timeout_ms: int) -> "RawFrame | None":
        """Pull the next frame from the ``appsink``, waiting at most ``timeout_ms``.

        Uses ``appsink.try_pull_sample`` with a bounded (nanosecond) timeout. On a
        timeout or a missing/unmappable sample returns ``None`` — which the
        acquisition worker treats as a disconnect signal (Req 7.1). On success maps
        the buffer to copy out the raw payload and reads the width/height from the
        sample caps, returning a :class:`RawFrame`.
        """
        if self._pipeline is None or self._appsink is None:
            return None
        rt = self._rt
        Gst = rt.Gst
        timeout_ns = int(max(0, timeout_ms)) * 1_000_000

        with rt.Timer(metric_name="CameraGetFrameTime"):
            sample = self._appsink.try_pull_sample(timeout_ns)
            if sample is None:
                logger.error(
                    f"GStreamerBackend {self.camera_id}: timed out after {timeout_ms} ms waiting for a frame"
                )
                return None

            buffer = sample.get_buffer()
            if buffer is None:
                logger.error(f"GStreamerBackend {self.camera_id}: sample had no buffer")
                return None
            width, height = self._frame_dimensions(sample.get_caps())

            success, map_info = buffer.map(Gst.MapFlags.READ)
            if not success:
                logger.error(f"GStreamerBackend {self.camera_id}: failed to map frame buffer")
                return None
            try:
                # Copy the bytes out before unmapping so the payload outlives the
                # GStreamer buffer (which is recycled by the pipeline).
                data = bytes(map_info.data)
            finally:
                buffer.unmap(map_info)
            return RawFrame(data=data, width=width, height=height)

    def apply_features(self, features: dict) -> dict:
        """Apply gain / exposure / advanced controls to the live source.

        GStreamer sources (NVIDIA CSI host-capture service, v4l2 ICAM) take their
        gain / exposure / crop from the image-source configuration that
        ``GstPipelineBuilder`` writes when the source is added (e.g. the CSI host
        config file). This merges the supplied values into the adapter's stored
        image-source configuration and returns the merged values as the accepted
        set. The live session keeps running throughout.

        Accepts either an image-source-style dict (``gain`` / ``exposure`` /
        ``advancedSettings``) or a device-feature list (normalized into the same
        keys). Returns the accepted values.
        """
        self._require_open()
        accepted = self._merge_features(features)
        logger.info(f"GStreamerBackend {self.camera_id}: applied features {accepted}")
        return accepted

    def stop_stream(self) -> None:
        """Stop acquisition without releasing the claim (pipeline back to PAUSED)."""
        if self._pipeline is None or not self._streaming:
            return
        Gst = self._rt.Gst
        self._pipeline.set_state(Gst.State.PAUSED)
        self._streaming = False
        logger.info(f"GStreamerBackend {self.camera_id}: pipeline PAUSED (acquisition stopped)")

    def close(self) -> None:
        """Release the single source claim by tearing the pipeline down to NULL."""
        if self._pipeline is None:
            return
        try:
            self._pipeline.set_state(self._rt.Gst.State.NULL)
        finally:
            self._pipeline = None
            self._appsink = None
            self._streaming = False
            logger.info(f"GStreamerBackend {self.camera_id}: source claim released")

    # --- internal helpers -------------------------------------------------

    def _require_open(self) -> None:
        """Raise if the pipeline has not been built/opened via :meth:`open`."""
        if self._pipeline is None:
            rt = self._rt or _load_gstreamer_runtime()
            raise rt.PipelineExecutionException(
                f"Camera {self.camera_id} is not open; call open() before this operation"
            )

    def _build_pipeline_string(self) -> str:
        """Build the persistent live-view pipeline string ending in an ``appsink``.

        Uses ``pipeline_description`` verbatim for the source portion when provided;
        otherwise reuses ``GstPipelineBuilder.add_image_source`` (mirroring the
        existing live-view source construction) to assemble the source plugins.
        Appends a ``videoconvert ! video/x-raw,format=RGB ! appsink`` tail so the
        worker pulls raw RGB frames.
        """
        rt = self._rt
        if self._pipeline_description:
            source_str = self._pipeline_description.strip().rstrip("!").strip()
        else:
            if not self._image_source:
                raise rt.PipelineExecutionException(
                    f"GStreamerBackend {self.camera_id} requires an image source or pipeline_description"
                )
            builder = rt.GstPipelineBuilder()
            builder.add_image_source(self._image_source)
            source_str = builder.pipeline_config.build_pipeline_string()

        appsink_tail = (
            "videoconvert ! video/x-raw,format=RGB ! "
            f"appsink name={self._APPSINK_NAME} sync=false max-buffers=1 drop=true emit-signals=false"
        )
        return f"{source_str} ! {appsink_tail}"

    def _configure_appsink(self, appsink) -> None:
        """Configure the ``appsink`` for drop-to-latest, pull-based acquisition."""
        try:
            appsink.set_property("sync", False)
            appsink.set_property("max-buffers", 1)
            appsink.set_property("drop", True)
            appsink.set_property("emit-signals", False)
        except Exception as e:  # pragma: no cover - properties already set via launch string
            logger.warning(f"GStreamerBackend {self.camera_id}: could not set appsink properties: {e}")

    def _frame_dimensions(self, caps):
        """Read width/height from a sample's caps, defaulting to 0 when absent."""
        width = height = 0
        if caps is not None and caps.get_size() > 0:
            structure = caps.get_structure(0)
            ok_w, w = structure.get_int("width")
            ok_h, h = structure.get_int("height")
            if ok_w:
                width = w
            if ok_h:
                height = h
        return width, height

    def _merge_features(self, features) -> dict:
        """Merge an ``apply_features`` argument into the stored image-source config.

        Normalizes either an image-source-style dict or a device-feature list into
        ``gain`` / ``exposure`` / ``advancedSettings`` keys, updates the adapter's
        ``imageSourceConfiguration`` in place (so a future pipeline rebuild picks up
        the values), and returns the accepted values.
        """
        accepted: dict = {}
        if not features:
            return accepted

        if isinstance(features, list):
            for item in features:
                name = item.get("feature")
                value = item.get("value")
                if name in ("Gain", "gain"):
                    accepted["gain"] = value
                elif name in ("ExposureTime", "exposure"):
                    accepted["exposure"] = value
                elif name is not None:
                    accepted.setdefault("advancedSettings", {})[name] = value
        else:
            if features.get("gain") is not None:
                accepted["gain"] = features["gain"]
            if features.get("exposure") is not None:
                accepted["exposure"] = features["exposure"]
            advanced = features.get("advancedSettings") or {}
            if advanced:
                accepted["advancedSettings"] = dict(advanced)

        # Reflect accepted values into the stored image-source configuration so a
        # subsequent pipeline build (CSI host config / v4l2 properties) uses them.
        if accepted and isinstance(self._image_source, dict):
            config = self._image_source.get("imageSourceConfiguration")
            if not isinstance(config, dict):
                config = {}
                self._image_source["imageSourceConfiguration"] = config
            for key, value in accepted.items():
                if key == "advancedSettings":
                    config.setdefault("advancedSettings", {}).update(value)
                else:
                    config[key] = value

        return accepted

    def _safe_null(self, pipeline) -> None:
        """Best-effort teardown of a failed/rejected pipeline to ``NULL``."""
        if pipeline is None:
            return
        try:
            pipeline.set_state(self._rt.Gst.State.NULL)
        except Exception as e:  # pragma: no cover - best-effort cleanup
            logger.warning(f"GStreamerBackend {self.camera_id}: error cleaning up failed open: {e}")
