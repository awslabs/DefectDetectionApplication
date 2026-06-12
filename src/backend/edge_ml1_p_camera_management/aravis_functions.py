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

# Amazon L4V Camera Station Application Library
# this has the functions to grab an image from a camera and execute a gstreamer pipeline configured in l4v.ini
# See stream_camera.py flask app for usage
# Author @ryvan (Ryan Vanderwerf)
# Flask App that acts as a camera integration station glue
# is the /getDevices endpoint, and build your configuration from
# there in l4v.ini
#
#
#  If you have issues
#
#   export GI_TYPELIB_PATH=$GI_TYPELIB_PATH:/opt/bin/lib/girepositry-1.0/
#
#  You may also have to give the path to libaravis.so, using LD_PRELOAD or
#  LD_LIBRARY_PATH.
#  Aravis API reference:
#  list of functions which is super helpful https://lazka.github.io/pgi-docs/Aravis-0.8/functions.html
import gi
import numpy as np
import os
from string import Template
import threading
import time
import traceback
import logging
import logging.config

from exceptions.api.aravis_camera_not_found import AravisCameraNotFound
from exceptions.api.aravis_camera_open_error import AravisCameraOpenError

# LOG LEVELS
# CRITICAL 50
# ERROR 40
# WARNING 30
# INFO 20
# DEBUG 10
# NOTSET 0

gi.require_version("Aravis", "0.8")
gi.require_version("Gst", "1.0")

from gi.repository import Aravis, Gst
from gi.repository.GLib import GError

from model.Camera import Camera

# Initialize Aravis
log = logging.getLogger(__name__)

# Serializes full bus rescans so a context teardown (Aravis.shutdown) can never
# race with a concurrent enumeration in another request thread.
_rescan_lock = threading.Lock()


def getCameras():
    Aravis.enable_interface("Fake")
    Aravis.update_device_list()

    cameras = []

    n_devices = Aravis.get_n_devices()
    for i in range(n_devices):
        id = Aravis.get_device_id(i)
        model = Aravis.get_device_model(i)
        address = Aravis.get_device_address(i)
        physical_id = Aravis.get_device_physical_id(i)
        protocol = Aravis.get_device_protocol(i)
        serial = Aravis.get_device_serial_nbr(i)
        vendor = Aravis.get_device_vendor(i)

        camera = Camera(id, model, address, physical_id, protocol, serial, vendor)
        cameras.append(camera)
    return cameras


def rescan_cameras():
    """
    Force a fresh enumeration of the camera bus and return the discovered
    cameras.

    The backend runs inside a container that does not receive host udev hotplug
    events, and Aravis/libusb keeps a cached USB context for the life of the
    process. Because of this, a camera connected after the server started may
    not show up from a plain update_device_list(), and the only reliable way to
    pick it up used to be restarting the container.

    Aravis.shutdown() tears down the cached USB/GenICam context in this
    enumeration process; the following update_device_list() rebuilds it and
    reliably discovers devices that were hotplugged after startup. Camera
    acquisition objects live in a separate manager process with their own
    Aravis context (see utils.camera_manager), so resetting the context here
    does not disturb active acquisitions/streams.
    """
    with _rescan_lock:
        try:
            Aravis.shutdown()
        except Exception as err:
            # A failed teardown is non-fatal; the re-init below still runs.
            log.warning("Aravis.shutdown() during rescan failed, continuing: %s", err)
        return getCameras()


def getCamera(cameraId):
    # Enable Fake camera
    Aravis.enable_interface("Fake")
    # Refresh camera list and discover new cameras, if any
    Aravis.update_device_list()

    # Capture the set of devices currently enumerated on the bus so we can tell
    # apart two very different failure modes when Aravis.Camera.new() raises:
    #   1. The camera is not enumerated at all -> genuinely "not found".
    #   2. The camera is enumerated (it shows up when adding an image source)
    #      but cannot be opened/bootstrapped -> "detected but unavailable".
    # The second case is what users hit when a USB3/GenICam camera negotiates
    # only a USB2 link (non-SuperSpeed cable, USB2 port/hub, low bus power) or
    # the device is already held open by another process. Aravis reports it as
    # "Failed to bootstrap USB device ... ", which previously surfaced to the
    # user as a confusing "not found".
    discovered_ids = [Aravis.get_device_id(i) for i in range(Aravis.get_n_devices())]

    try:
        return Aravis.Camera.new(cameraId)
    except GError as err:
        error_detail = getattr(err, "message", None) or str(err)
        if cameraId not in discovered_ids:
            log.error(
                "Camera '%s' was not found on the bus. Discovered devices: %s. Error: %s",
                cameraId, discovered_ids, error_detail,
            )
            raise AravisCameraNotFound(
                f"Camera '{cameraId}' was not detected. Make sure it is connected "
                f"and not already in use by another device."
            )
        log.error(
            "Camera '%s' was detected but could not be opened. Error: %s",
            cameraId, error_detail,
        )
        raise AravisCameraOpenError(
            f"Camera '{cameraId}' was detected but could not be opened. This often "
            f"means a USB3 (GenICam) camera negotiated only a USB2 link, a faulty or "
            f"non-SuperSpeed cable, a USB2 port/hub, insufficient bus power, or the "
            f"camera is already in use by another process. Reconnect the camera "
            f"directly to a USB3 (SuperSpeed) port using a certified USB3 cable and "
            f"try again. Underlying error: {error_detail}"
        )


def get_input_file_from_pipeline(pipelinestr):
    input_file_plugin = pipelinestr.split("!")[0]
    input_file_plugin = input_file_plugin.strip()
    start = input_file_plugin.find('filesrc blocksize=-1 location=')
    if start == -1:
        return None
    else:
        pathStart = start + len('filesrc blocksize=-1 location=')
        outputfile = input_file_plugin[pathStart:]
        # Strip outer quotes.
        return outputfile[1:-1]
