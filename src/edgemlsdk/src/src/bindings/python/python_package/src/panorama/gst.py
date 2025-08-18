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

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

from panorama.gst_modules import enums
from panorama.gst_modules import pipeline
from panorama.gst_modules import pipeline_manager

from panorama import panorama_projections

def initialize(log_level: int = 2):
    """
    Initializes the GStreamer framework.  Logs from GStreamer will be forwarded to the Panorama SDKv2 tracing framework.

    Args:
        log_level (int):  Sets the GST_DEBUG level.  See https://gstreamer.freedesktop.org/documentation/tutorials/basic/debugging-tools.html?gi-language=c for more information
    """
    Gst.init(None)
    panorama_projections.InitializeGStreamerWithArgs(0, None, log_level)

def shutdown():
    """
    Shuts down the GStreamer framework
    """
    panorama_projections.ShutdownGStreamer()