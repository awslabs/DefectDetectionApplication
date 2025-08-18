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
import time
import traceback
import logging
import logging.config

from exceptions.api.aravis_camera_not_found import AravisCameraNotFound

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


def getCamera(cameraId):
    try:
        # Enable Fake camera
        Aravis.enable_interface("Fake")
        # Refresh camera list and discover new cameras, if any
        Aravis.update_device_list()
        return Aravis.Camera.new(cameraId)
    except GError:
        raise AravisCameraNotFound("Error fetching camera details")


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
