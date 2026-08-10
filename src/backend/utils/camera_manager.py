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
import gi
gi.require_version('Gst', '1.0')
gi.require_version('Aravis', '0.8')
from gi.repository import Aravis

from metrics.collector import Timer
import logging
import logging
logger = logging.getLogger(__name__)
import multiprocessing
from multiprocessing.managers import BaseManager
import json
import struct
from utils.namespace_lock import NamespaceLock
import queue
import concurrent.futures
from utils.common import CameraStatusEnum
from data_models.common import CameraStatusModel
import time
from exceptions.api.aravis_camera_exception import AravisCameraException

from threading import Lock

get_frame_lock = Lock()

# Upper bound for how long to wait for a single frame before giving up, on top
# of the configured exposure time. Prevents a stalled USB/GenICam transfer from
# blocking pop_buffer (and thus every preview) indefinitely.
FRAME_POP_TIMEOUT_MARGIN_US = 5_000_000  # 5s beyond the exposure time


# ---------------------------------------------------------------------------
# Frame transport (security fix #6, Req 2.6)
#
# Historically the frame moved across the in-process ``BaseManager``-hosted
# ``Camera`` via an executable object serializer (dumps/loads of the
# ``{'data','height','width'}`` dict). Even though the producer and consumer
# share the same trust domain, an executable-object deserializer is an
# unnecessary code-execution primitive on a live hot path, so we replace it
# with a NON-EXECUTABLE serialization that keeps the exact
# ``{'data','height','width'}`` dict shape (and ``None`` on timeout/failure).
#
# Wire format: a 4-byte big-endian length prefix, then a UTF-8 JSON header, then
# the raw ``data`` bytes.
#   * frame  -> header {"null": false, "height": <int>, "width": <int>}
#               followed by the raw image bytes.
#   * None    -> header {"null": true} with no trailing bytes.
# ``get_frame`` still returns ``bytes``, so the ``BaseManager`` proxy contract
# (which transports the return value) is unchanged; only the explicit
# (de)serializer on this call site is replaced.
# ---------------------------------------------------------------------------
def encode_frame(frame):
    """Serialize a ``{'data','height','width'[,'pixel_format']}`` frame dict
    (or ``None``) into the non-executable length-prefixed JSON-header +
    raw-bytes transport. ``pixel_format`` is the optional camera pixel
    format tag (see :func:`gst_pixel_format`); absent for older callers."""
    if frame is None:
        header = json.dumps({"null": True}).encode("utf-8")
        return struct.pack(">I", len(header)) + header
    header_fields = {
        "null": False, "height": frame["height"], "width": frame["width"],
    }
    if frame.get("pixel_format"):
        header_fields["pixel_format"] = frame["pixel_format"]
    header = json.dumps(header_fields).encode("utf-8")
    return struct.pack(">I", len(header)) + header + bytes(frame["data"])


def decode_frame(buf):
    """Inverse of :func:`encode_frame`. Returns the identical
    ``{'data','height','width'[,'pixel_format']}`` dict, or ``None`` for the
    null/timeout frame."""
    (hlen,) = struct.unpack(">I", buf[:4])
    header = json.loads(buf[4:4 + hlen].decode("utf-8"))
    if header.get("null"):
        return None
    data = buf[4 + hlen:]
    frame = {"data": data, "height": header["height"], "width": header["width"]}
    if header.get("pixel_format"):
        frame["pixel_format"] = header["pixel_format"]
    return frame


# ---------------------------------------------------------------------------
# Camera pixel format -> portable tag (aravis workflow feed correctness).
#
# The workflow Frame_Feed path pushes the RAW grabbed bytes into an appsrc;
# without the camera's actual pixel format the executor can only guess from
# bytes-per-pixel, and a Bayer mosaic (1 byte/pixel, like Mono8) gets
# mislabeled GRAY8 — downstream then treats the un-demosaiced mosaic as a
# grayscale image (garbage frames). The classic Image_Source pipeline avoids
# this only because its configured conversion chain (e.g.
# `video/x-bayer,format=bggr ! bayer2rgb`) carries the knowledge; the feed
# path needs it from the buffer itself.
# ---------------------------------------------------------------------------

#: GenICam PFNC pixel format codes -> portable tag. Raw formats map to the
#: GStreamer video/x-raw format name; Bayer mosaics map to "bayer:<pattern>"
#: (the video/x-raw vs video/x-bayer split is the consumer's concern).
#: Values are the PFNC constants Aravis.PIXEL_FORMAT_* resolve to, inlined so
#: the map is usable in tests without the Aravis runtime.
_PFNC_TO_TAG = {
    0x01080001: "GRAY8",         # Mono8
    0x01080008: "bayer:grbg",    # BayerGR8
    0x01080009: "bayer:rggb",    # BayerRG8
    0x0108000A: "bayer:gbrg",    # BayerGB8
    0x0108000B: "bayer:bggr",    # BayerBG8
    0x02180014: "RGB",           # RGB8Packed
    0x02180015: "BGR",           # BGR8Packed
    0x02200016: "RGBA",          # RGBA8Packed
    0x02200017: "BGRA",          # BGRA8Packed
}


def gst_pixel_format(pfnc_code):
    """The portable pixel-format tag for a GenICam PFNC code, or ``None``
    for unknown/unmapped formats (the caller falls back to the historic
    bytes-per-pixel guess)."""
    try:
        return _PFNC_TO_TAG.get(int(pfnc_code))
    except (TypeError, ValueError):
        return None

# Tier 2 GenICam controls surfaced under "Advanced settings". Scoped to the
# "safe" controls that are persisted and don't affect the stream payload /
# pipeline. (Pixel format and ROI are intentionally excluded for now: they need
# the GStreamer caps to track them, so they are a separate follow-up.)
# (response_key, GenICam feature name, kind, unit).
ADVANCED_DEVICE_FEATURES = [
    ("balanceWhiteAuto", "BalanceWhiteAuto", "enumeration", None),
    ("reverseX", "ReverseX", "boolean", None),
    ("reverseY", "ReverseY", "boolean", None),
]

# Image-source-config advancedSettings key -> (GenICam feature, kind) for the
# controls applied inside start_acquisition (the same path gain/exposure use),
# so they reliably affect the captured frame and the persisted profile applies
# in preview, capture and workflow alike.
CONFIG_FEATURE_MAP = {
    "reverseX": ("ReverseX", "boolean"),
    "reverseY": ("ReverseY", "boolean"),
    "balanceWhiteAuto": ("BalanceWhiteAuto", "enumeration"),
}
# None of the currently-supported safe controls change the payload; kept for
# when pixel format / ROI are added back.
PAYLOAD_AFFECTING_FEATURES = {"Width", "Height", "OffsetX", "OffsetY", "PixelFormat"}

class Camera():
    def __init__(self,camera_id):
        self.status = None
        Aravis.enable_interface("Fake")
        self.camera_id = camera_id
        logger.info(f"Camera ID {self.camera_id} : init")
        self._lock = multiprocessing.Lock()
        self._lock.acquire()
        self.camera = self.connect_camera()
        if self.camera and self.set_camera():
            self.payload = self.camera.get_payload()
            self.stream = self.camera.create_stream(None, None)
            self.set_buffer()
            self.update_camera_status(CameraStatusEnum.CONNECTED)
        self._lock.release()

    def get_status(self):
        return self.status
    
    def update_camera_status(self, status, error = None):
        self.status = CameraStatusModel(status=status, lastUpdatedTime=time.time(), error=str(error))

    def disconnect(self):
        # Cleanup and release resources here
        logger.info(f"Camera ID {self.camera_id} : Disconnecting")
        self._lock.acquire()
        if self.camera:
            with Timer(metric_name="CameraDisconnectTime"):
                try:
                    self.camera.stop_acquisition()
                except Exception as e:
                    logger.error(f"Error while disconnecting camera {self.camera_id}: {e}")
                finally:
                    self.unset_camera()
                    if hasattr(self, "stream"):
                        del self.stream
                    if hasattr(self, "camera"):
                        del self.camera
                    Aravis.shutdown()
        self._lock.release()

    def get_camera_id(self):
        return self.camera_id

    def get_feature_bounds(self):
        """
        Read adjustable feature ranges directly from the camera's GenICam
        feature map (the device "XML"), so the UI can present limits that match
        the actual hardware instead of hard-coded defaults.

        Returns a dict keyed by feature name. Each entry describes the feature
        generically so new controls can be surfaced without changing the shape:
            {
              "type": "float" | "integer" | "enumeration" | "boolean",
              "min": <number|None>, "max": <number|None>,
              "increment": <number|None>,
              "current": <value|None>,
              "unit": <str|None>,
              "options": [<str>, ...],   # enumeration only
              "available": <bool>,       # supported by this device
              "advanced": <bool>         # belongs in the "Advanced settings" UI
            }
        Features the device does not implement are omitted entirely, so the UI
        can simply render whatever is returned. Exposure time is reported in
        microseconds (Aravis' native unit and the unit stored for USB3Vision
        cameras).
        """
        bounds = {}
        if not self.camera:
            return bounds

        # Serialize with acquisition (start/get_frame/stop all take this lock).
        # Reading GenICam registers on the shared camera concurrently with a
        # frame grab corrupts the libaravis/libusb channel and can wedge
        # pop_buffer, so feature reads must not overlap acquisition.
        self._lock.acquire()
        try:
            # Primary controls (always shown when supported).
            for key, feature, unit in [
                ("exposure", "ExposureTime", "us"),
                ("gain", "Gain", None),
            ]:
                entry = self._read_float_bounds(key, feature, unit)
                if entry:
                    entry["advanced"] = False
                    bounds[key] = entry

            # Tier 2 / Tier 3 controls, surfaced under "Advanced settings". These
            # are common across most GenICam devices but vary by model, so each is
            # included only when the device actually implements it.
            for key, feature, kind, unit in ADVANCED_DEVICE_FEATURES:
                entry = self._read_feature_entry(feature, kind, unit)
                if entry:
                    entry["advanced"] = True
                    bounds[key] = entry
        finally:
            self._lock.release()

        return bounds

    def _read_float_bounds(self, key, feature, unit):
        try:
            if not self.camera.get_device().is_feature_available(feature):
                return None
            if key == "exposure":
                fmin, fmax = self.camera.get_exposure_time_bounds()
                current = self.camera.get_exposure_time()
            else:
                fmin, fmax = self.camera.get_gain_bounds()
                current = self.camera.get_gain()
            return {
                "type": "float", "min": fmin, "max": fmax, "increment": None,
                "current": current, "unit": unit, "options": [], "available": True,
                "feature": feature,
            }
        except Exception as e:
            logger.warning(f"Camera ID {self.camera_id}: unable to read {key} bounds: {e}")
            return None

    def _read_feature_entry(self, feature, kind, unit):
        """Read a single GenICam feature generically. Returns None if the device
        does not implement it (or it cannot be read)."""
        try:
            device = self.camera.get_device()
            if not device.is_feature_available(feature):
                return None

            entry = {
                "type": kind, "min": None, "max": None, "increment": None,
                "current": None, "unit": unit, "options": [], "available": True,
                "feature": feature,
            }
            if kind == "enumeration":
                entry["current"] = device.get_string_feature_value(feature)
                entry["options"] = list(
                    device.dup_available_enumeration_feature_values_as_strings(feature) or []
                )
            elif kind == "boolean":
                entry["current"] = device.get_boolean_feature_value(feature)
            elif kind == "integer":
                entry["current"] = device.get_integer_feature_value(feature)
                try:
                    imin, imax = device.get_integer_feature_bounds(feature)
                    entry["min"], entry["max"] = imin, imax
                except Exception:
                    pass
            elif kind == "float":
                entry["current"] = device.get_float_feature_value(feature)
                try:
                    fmin, fmax = device.get_float_feature_bounds(feature)
                    entry["min"], entry["max"] = fmin, fmax
                except Exception:
                    pass
            return entry
        except Exception as e:
            logger.warning(f"Camera ID {self.camera_id}: unable to read feature {feature}: {e}")
            return None

    def apply_device_features(self, features):
        """
        Apply a batch of GenICam feature values to the live camera.

        `features` is a list of {"feature": <GenICam name>, "type": <kind>,
        "value": <value>}. Payload-affecting features (ROI / PixelFormat) cause
        the stream to be rebuilt so subsequent captures stay consistent. Returns
        the re-read current values so the UI can reflect what the device
        actually accepted (values may be coerced/clamped by the device).
        """
        applied = {}
        if not self.camera:
            return applied

        payload_affecting = {"Width", "Height", "OffsetX", "OffsetY", "PixelFormat"}
        self._lock.acquire()
        try:
            device = self.camera.get_device()
            # Stop acquisition before touching features that are locked while
            # streaming (ROI, PixelFormat, etc.). Best-effort.
            try:
                self.camera.stop_acquisition()
            except Exception:
                pass

            needs_stream_rebuild = False
            for item in features:
                feature = item.get("feature")
                kind = item.get("type")
                value = item.get("value")
                if not feature:
                    continue
                try:
                    if kind in ("enumeration", "string"):
                        device.set_string_feature_value(feature, str(value))
                    elif kind == "boolean":
                        device.set_boolean_feature_value(feature, bool(value))
                    elif kind == "integer":
                        device.set_integer_feature_value(feature, int(value))
                    elif kind == "float":
                        device.set_float_feature_value(feature, float(value))
                    else:
                        logger.warning(f"Camera ID {self.camera_id}: unknown feature kind '{kind}' for {feature}")
                        continue
                    if feature in payload_affecting:
                        needs_stream_rebuild = True
                except Exception as e:
                    logger.error(f"Camera ID {self.camera_id}: failed to set {feature}={value}: {e}")
                    raise AravisCameraException(f"Failed to set {feature}: {e}")

            if needs_stream_rebuild:
                self.payload = self.camera.get_payload()
                if hasattr(self, "stream"):
                    del self.stream
                self.stream = self.camera.create_stream(None, None)
                self.set_buffer()

            # Re-read so the caller sees the device-accepted values.
            for item in features:
                feature = item.get("feature")
                kind = item.get("type")
                try:
                    if kind in ("enumeration", "string"):
                        applied[feature] = device.get_string_feature_value(feature)
                    elif kind == "boolean":
                        applied[feature] = device.get_boolean_feature_value(feature)
                    elif kind == "integer":
                        applied[feature] = device.get_integer_feature_value(feature)
                    elif kind == "float":
                        applied[feature] = device.get_float_feature_value(feature)
                except Exception:
                    pass
            return applied
        finally:
            self._lock.release()

    def connect_camera(self):
        logger.info(f"Camera ID {self.camera_id} : Connecting")
        try:
            with Timer(metric_name="CameraConnectTime"):
                return Aravis.Camera.new(self.camera_id)
        except TypeError as e:
            logger.info(f"No camera found {self.camera_id}")
            self.update_camera_status(CameraStatusEnum.DISCONNECTED, e)
        except Exception as e:
            logger.error(f"Unable to connect camera {self.camera_id}. {e}")
            self.update_camera_status(CameraStatusEnum.DISCONNECTED, e)
    def set_camera(self):
        try: 
            logger.info(f"Camera ID {self.camera_id} : Setup camera")
            device = self.camera.get_device()

            # Write feature changes straight through to the device instead of
            # only updating Aravis' register cache. Without this, writes such as
            # ReverseX/ReverseY/PixelFormat can read back as "set" while the
            # sensor never actually applied them.
            try:
                self.camera.set_register_cache_policy(Aravis.RegisterCachePolicy.DISABLE)
            except Exception as e:
                logger.warning(f"Camera ID {self.camera_id} : could not disable register cache: {e}")

            # TODO: This is ideal settings but didnt work with zebra cameras.
            # device.set_string_feature_value("TriggerMode", "On")
            # device.set_string_feature_value("TriggerSelector", "FrameStart")
            # device.set_string_feature_value("AcquisitionMode", "SingleFrame")
            # device.set_string_feature_value("TriggerSource", "Software")

            # Worked with zebra, basler and omrom
            device.set_string_feature_value("TriggerMode", "On")
            device.set_string_feature_value("TriggerSource", "Software")
            device.set_string_feature_value("AcquisitionMode", "Continuous")
            logger.info("camera setup done")
            return True
        except Exception as e:
            logger.error(f"Unable to set camera {self.camera_id}: {e}")
            self.update_camera_status(CameraStatusEnum.DISCONNECTED, e)


    ## Use this function to unset any camera configuration changes that may impact 
    ## other 3rd party tools from accessing the camera
    def unset_camera(self):
        logger.info(f"Camera ID {self.camera_id} : Reverting camera settings")
        try:
            device = self.camera.get_device()
            # Disable trigger mode
            device.set_string_feature_value("TriggerMode", "Off")
            logger.info("Reverting camera settings done")
        except Exception as e:
            logger.error(f"Unable to unset camera trigger mode settings. {e}")
            self.update_camera_status(CameraStatusEnum.DISCONNECTED, e)

    def set_buffer(self):
        self.stream.push_buffer(Aravis.Buffer.new_allocate(self.payload))

    def _apply_config_features(self, config):
        """Apply advanced GenICam controls from config['advancedSettings'] to the
        device. Returns True if a payload-affecting feature changed (so the
        caller can rebuild the stream). Only writes a feature when its value
        actually changes, so per-frame previews don't rewrite each grab. Must be
        called with self._lock held and acquisition stopped."""
        if not self.camera or not config:
            return False
        advanced = config.get("advancedSettings") or {}
        if not advanced:
            return False
        applied = getattr(self, "_applied_features", None)
        if applied is None:
            applied = {}
            self._applied_features = applied
        device = self.camera.get_device()
        payload_changed = False
        for key, (feature, kind) in CONFIG_FEATURE_MAP.items():
            if key not in advanced or advanced.get(key) is None:
                continue
            value = advanced.get(key)
            if applied.get(key) == value:
                continue  # already applied; skip redundant write / stream rebuild
            try:
                if not device.is_feature_available(feature):
                    continue
                if kind in ("enumeration", "string"):
                    device.set_string_feature_value(feature, str(value))
                elif kind == "boolean":
                    device.set_boolean_feature_value(feature, bool(value))
                elif kind == "integer":
                    device.set_integer_feature_value(feature, int(value))
                applied[key] = value
                logger.info(f"Camera ID {self.camera_id} : set {feature}={value}")
                if feature in PAYLOAD_AFFECTING_FEATURES:
                    payload_changed = True
            except Exception as e:
                logger.error(f"Camera ID {self.camera_id} : failed to set {feature}={value}: {e}")
        return payload_changed

    def start_acquisition(self, image_source_config):
        # Made image_src_cfg optional for camera status check
        self._lock.acquire()
        if image_source_config:
            # TODO: For fake camera set pixel type. for real cameras its expected to setup externally. This need to be tested.
            self.gain = image_source_config.get("gain")
            self.exposure = image_source_config.get("exposure")
            logger.info(f"setting gain {self.gain}")
            self.camera.set_gain(self.gain)
            logger.info(f"setting exposure {self.exposure}")
            self.camera.set_exposure_time(self.exposure)
            # Apply advanced GenICam controls (flip, white balance, pixel
            # format, ROI) here, with acquisition stopped, so they actually take
            # effect on the frame about to be grabbed. Rebuild the stream if a
            # payload-affecting feature (ROI / pixel format) changed.
            if self._apply_config_features(image_source_config):
                self.payload = self.camera.get_payload()
                if hasattr(self, "stream"):
                    del self.stream
                self.stream = self.camera.create_stream(None, None)
                self.set_buffer()
            logger.info(f"Camera ID {self.camera_id} : camera setup done, start acquisition")
        with Timer(metric_name="CameraStartAcquisitionTime"):
            self.camera.start_acquisition()
        self._lock.release()

    def stop_acquisition(self):
        self._lock.acquire()
        logger.info(f"Camera ID {self.camera_id} : stop acquisition")
        with Timer(metric_name="CameraStopAcquisitionTime"):
            self.camera.stop_acquisition()
        self._lock.release()
        logger.info(f"Camera ID {self.camera_id} : stopped acquisition")

    def get_frame(self):
        logger.info(f"Camera ID {self.camera_id} : get frame acquisition")
        self._lock.acquire()
        with Timer(metric_name="CameraGetFrameTime"):
            self.camera.software_trigger()
            # Bound the wait so a stalled transfer can't hang the request (and,
            # via get_frame_lock, every subsequent preview) forever.
            timeout_us = int((getattr(self, "exposure", 0) or 0) + FRAME_POP_TIMEOUT_MARGIN_US)
            arv_buffer = self.stream.timeout_pop_buffer(timeout_us)
            if arv_buffer is None:
                logger.error(
                    f"Camera ID {self.camera_id} : Timed out after {timeout_us} us waiting for a frame"
                )
                self.update_camera_status(
                    CameraStatusEnum.DISCONNECTED,
                    "Timed out waiting for a frame from the camera",
                )
                self._lock.release()
                return encode_frame(None)
            self.set_buffer()
            if arv_buffer.get_status() != Aravis.BufferStatus.SUCCESS:
                logger.error(f"Camera ID {self.camera_id} : Failed to get frame")
                self.update_camera_status(CameraStatusEnum.DISCONNECTED, "Failed to get frame")
                self._lock.release()
                return encode_frame(None)
            else:
                data = arv_buffer.get_data()
                wd = arv_buffer.get_image_width()
                ht = arv_buffer.get_image_height()
                #update camera connection status 
                self.update_camera_status(CameraStatusEnum.CONNECTED)
                data_dict = {'data': data, 'height': ht, 'width': wd}
                # Tag the frame with the camera's actual pixel format so the
                # workflow Frame_Feed can set truthful appsrc caps (a Bayer
                # mosaic must not be mislabeled GRAY8). Best-effort: older
                # Aravis bindings without the getter just omit the tag.
                try:
                    pixel_format = gst_pixel_format(
                        arv_buffer.get_image_pixel_format())
                    if pixel_format:
                        data_dict['pixel_format'] = pixel_format
                except Exception:
                    logger.debug(
                        f"Camera ID {self.camera_id}: pixel format "
                        "unavailable on this Aravis buffer; frame untagged")
                encoded_data = encode_frame(data_dict)
                self._lock.release()
                return encoded_data

################
# Camera Manager

manager_base = BaseManager()
manager_base.register('Camera', Camera)  
manager_base.start()

# Create a dictionary to store the Camera objects by camera_id
manager = multiprocessing.Manager()
camera_objects = manager.dict()


def get_all_camera_statuses():
    status_objs = {}
    for camera_id in camera_objects:
        status = get_camera_status(camera_id)
        status_objs[camera_id] = status
    return status_objs

def get_camera_status(camera_id):
    camera = camera_objects.get(camera_id) 
    if camera:
        return camera.get_status()
    return CameraStatusModel(status=CameraStatusEnum.DISCONNECTED, lastUpdatedTime=time.time())

def connect_camera(camera_id):
    if not camera_id:
        raise AravisCameraException("Camera ID is required")

    if camera_id in camera_objects:
        disconnect_camera(camera_id)

    camera = manager_base.Camera(camera_id)
    camera_objects[camera_id] = camera
    camera_status = get_camera_status(camera_id)

    if camera_status.status == CameraStatusEnum.CONNECTED:
        return True
    else: # Connection Failed
        disconnect_camera(camera_id)
        raise AravisCameraException(camera_status.error)


def get_camera_feature_bounds(camera_id):
    """
    Return the adjustable feature ranges for a camera, read from its GenICam
    feature map. Reads only from an existing connection — it never opens/claims
    the camera itself, because doing so makes a subsequent connect (which opens
    the device to verify it) fail with LIBUSB_ERROR_BUSY. If the camera isn't
    connected, returns an empty dict so the UI falls back to defaults /
    read-only until the user connects.
    """
    if not camera_id:
        raise AravisCameraException("Camera ID is required")

    camera = camera_objects.get(camera_id)
    if camera is None:
        return {}

    return camera.get_feature_bounds()


def apply_camera_features(camera_id, features):
    """
    Apply a batch of advanced GenICam feature values to a live camera and return
    the device-accepted values. Connects on demand if the camera isn't already
    connected.

    `features` is a list of {"feature", "type", "value"}.
    """
    if not camera_id:
        raise AravisCameraException("Camera ID is required")
    if not features:
        return {}

    if camera_id not in camera_objects:
        connect_camera(camera_id)

    camera = camera_objects.get(camera_id)
    if camera is None:
        raise AravisCameraException(f"Unable to connect camera {camera_id}")

    return camera.apply_device_features(features)

def _disconnect_camera(camera_id):
    camera = camera_objects.get(camera_id)
    if camera:
        camera.disconnect()

def disconnect_camera(camera_id):
    logger.info(f'Deleting camera: {camera_id}')
    if camera_id in camera_objects:
        _disconnect_camera(camera_id)
        del camera_objects[camera_id]
        logger.info(f"Deleted camera {camera_id}")
    return True

def disconnect_all_cameras():
    if camera_objects:
        logger.info('Disconnecting all cameras')
        for camera_id in camera_objects:
            _disconnect_camera(camera_id)
        del camera_objects
        logger.info('Deleted all cameras')
    else:
        logger.info("No cameras found during disconnect process")

def _get_camera_frame(camera_id, camera, camera_config):
    try:
        camera.start_acquisition(camera_config)
        camera_frame = decode_frame(camera.get_frame())
        camera.stop_acquisition()
        return camera_frame
    except Exception as err:
        disconnect_camera(camera_id)
        logger.error(f"Unable to grab frames. {err}")
        return None

def get_camera_frame(camera_id, camera_config=None):
    """Return a single inference/capture/preview frame for ``camera_id``.

    Uses the cached, persistent ``Camera`` connection model: the camera is opened
    once via ``connect_camera`` (stored in ``camera_objects``) and **reused** across
    calls — each call performs a per-request ``start_acquisition`` / ``get_frame`` /
    ``stop_acquisition`` on that already-claimed device, but does NOT release the USB
    claim between calls. This is what the live-preview poll (~2 Hz) and the
    capture/workflow callers depend on.

    NOTE (regression fix): an earlier revision routed this through
    ``StreamBroadcaster.get_inference_frame``, whose no-session path opened AND
    closed a fresh device claim on every call. On real USB3Vision hardware the
    ~500 ms preview poll then overlapped successive open/close cycles and the device
    rejected the next claim with ``LIBUSB_ERROR_BUSY``, breaking the live view. The
    broadcast (``/streams``) stack remains available for the viewer subscribe path,
    but the preview/capture/inference hot path reuses the single cached connection
    again to avoid that claim thrash.

    Returns the ``{'data', 'height', 'width'}`` dict produced by the camera, and
    raises ``Exception`` on no-frame/failure (the contract ``digital_input_*`` /
    ``workflow`` / capture / preview callers rely on to surface an HTTP error).
    """
    get_frame_lock.acquire()
    if camera_id not in camera_objects:
        logger.error("Attempting to create camera object")
        connect_camera(camera_id)

    camera = camera_objects.get(camera_id)
    if camera is None:
        logger.error(f"Camera not found for ID {camera_id}")
        raise Exception(f"Camera not able to connect for ID {camera_id}")
    try:
        frame = _get_camera_frame(camera_id, camera, camera_config)
        if frame is not None:
            return frame
        else:
            raise Exception(f"Unable to get camera frame for camera id: {camera_id}")
    except Exception:
        raise Exception(f"Unable to get camera frame for camera id: {camera_id}")
    finally:
        get_frame_lock.release()
