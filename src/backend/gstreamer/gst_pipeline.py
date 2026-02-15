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
import os
from utils import utils
from utils.constants import INFERENCE_RECEIVED_TIMESTAMP
from exceptions.api.gst_pipeline_exception import PipelineExecutionException, PipelineSyntaxException
from resources.accessors.latency_time_accessor import LatencyTimeAccessor

#  Aravis API reference:
#  list of functions which is super helpful https://lazka.github.io/pgi-docs/Aravis-0.8/functions.html
import gi
import time
gi.require_version("Aravis", "0.8")
gi.require_version("Gst", "1.0")
gi.require_version('GstVideo', '1.0')
gi.require_version('GstApp', '1.0')
from gi.repository import Aravis, Gst, GObject, GLib
from gi.repository.GLib import GError
import re
# https://stackoverflow.com/questions/3782962/why-does-python-gstreamer-crash-without-gobject-threads-init-at-the-top-of-m
GObject.threads_init()

import logging
logger = logging.getLogger(__name__)

class GstPipelineManager:

    def __init__(self):
        self.latency_time_accessor = LatencyTimeAccessor()

    def create_buffer(self, pipeline_str, pipeline, frame_data):
        Aravis.enable_interface("Fake")
        data = frame_data['data']
        ht = frame_data['height']
        wd = frame_data['width']

        pattern = r'caps=([^!]+)'
        match = re.search(pattern, pipeline_str)
        first_caps = match.group(1)

        source = pipeline.get_by_name("appsrc")
        source.set_property("caps", Gst.Caps.from_string(f"{first_caps} ,width={wd} , height={ht}"))
        source.set_property("block", True)
        source.set_property("format", Gst.Format.TIME)

        return source, Gst.Buffer.new_wrapped(data)

    def run_pipeline(self, pipeline_str, frame_data = None, latency_metrics = None) -> dict:
        logger.warning("Initializing GStreamer pipeline")
        parsed_tag_values = {}
        os.environ["GST_PLUGIN_PATH"] = utils.get_gst_plugins_path()
        os.environ["GST_DEBUG_FILE"] = os.path.join(os.environ['COMPONENT_WORK_PATH'], "gst-debug.log")
        # https://gstreamer.freedesktop.org/documentation/tutorials/basic/debugging-tools.html?gi-language=c
        os.environ["GST_DEBUG"] = "4"  # Logs all informational messages.
        os.environ["GST_DEBUG_NO_COLOR"] = "1"  # No colors, https://stackoverflow.com/a/56551269
        
        # Set DISPLAY for Argus camera daemon (nvarguscamerasrc)
        if "DISPLAY" not in os.environ:
            os.environ["DISPLAY"] = ":0"
        
        pipeline = None
        loop = None
        def on_message(bus, message):
            acceptable_messages = [Gst.MessageType.ERROR, Gst.MessageType.EOS, Gst.MessageType.TAG]
            if message.type not in acceptable_messages:
                return
            parsed_tag_values.update(self.parse_msg(message, latency_metrics=latency_metrics))
            if message.type != Gst.MessageType.TAG:
                logger.info("Quitting loop")
                loop.quit()
        try:
            Gst.init(None)
            # Create a GStreamer pipeline from the pipeline string
            pipeline = Gst.parse_launch(pipeline_str)
            loop = GLib.MainLoop()
            if frame_data:
                source, gst_buffer = self.create_buffer(pipeline_str, pipeline, frame_data)

            bus = pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", on_message)

            logger.warning("Setting pipeline to PLAYING state")
            ret = pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error("Pipeline failed to start")
                raise PipelineExecutionException("Pipeline failed to change state to PLAYING, check logs above this.")
            logger.warning("Pipeline started, waiting for Triton inference")
            if frame_data:
                source.emit("push-buffer", gst_buffer)
                source.emit("end-of-stream")
            logger.warning("Running pipeline main loop")
            loop.run()
            logger.warning("Pipeline main loop completed")
        except GError as e:
            logger.error("PipelineSyntaxException:" + str(e))
            raise PipelineSyntaxException(str(e))
        except PipelineExecutionException as pe:
            logger.error("PipelineExecutionException: " + str(pe))
            raise pe
        except Exception as exception:
            logger.error("Unknown exception:" + str(exception), exception)
            raise exception
        finally:
            # Stop the pipeline
            if pipeline:
                pipeline.set_state(Gst.State.NULL)
                logger.info("Pipeline set to NULL state")
        return parsed_tag_values

    def parse_msg(self, msg, latency_metrics = None) -> dict:
        tag_values = {}
        t = msg.type
        if t == Gst.MessageType.ERROR:
            # err.message: main error, dbg: detail error message
            err, dbg = msg.parse_error()
            logger.error("Pipeline ERROR - {} : {}".format(msg.src.get_name(), err.message))
            if dbg:
                logger.debug(f"Debug information: {dbg}")
            raise PipelineExecutionException("Pipeline failed with: {}. {}".format(err.message, dbg if dbg else ""))
        elif t == Gst.MessageType.EOS:
            logger.info("End of stream")
        elif t == Gst.MessageType.TAG:
            try: 
                taglist = msg.parse_tag()

                # validate tag came from eminfer plugin
                # tag names should match https://code.amazon.com/packages/NeoAgentSmith/blobs/4169508c22ef7094f34c807c8aeea9e169d7b5a4/--/gst_eminfer/plugin/library/sources/eminfer.cc#L844,L845,L847
                is_anomaly = taglist.get_value_index("is_anomalous", 0)
                confidence = taglist.get_value_index("confidence", 0)
                if is_anomaly is not None:
                    logger.warning(f"Triton inference result received: is_anomalous={is_anomaly}")
                    tag_values["is_anomalous"] = is_anomaly
                    latency_metrics.add_timestamp(INFERENCE_RECEIVED_TIMESTAMP)
                if confidence is not None:
                    logger.warning(f"Triton confidence score: {confidence}")
                    tag_values["confidence"] = confidence

            except Exception as exception: 
                logger.error("Unable to parse tag message from pipeline. " + str(exception))
        return tag_values
