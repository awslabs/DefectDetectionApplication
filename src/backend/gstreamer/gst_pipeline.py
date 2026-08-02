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

# Hard upper bound on how long a single pipeline run may take before the
# watchdog force-quits the GLib main loop. Prevents a stalled pipeline (no
# EOS/ERROR) from hanging the flask worker indefinitely.
PIPELINE_TIMEOUT_SEC = 120

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

    def run_pipeline(self, pipeline_str, frame_data = None, latency_metrics = None, status_sink = None) -> dict:
        # ``status_sink`` is an OPTIONAL callable ``sink(element_name, kind,
        # detail)`` the deployed-workflow executor threads in to collect
        # per-node run status (deployed-workflow-run-observability R3). When
        # None (every Pipeline_Configuration caller), behavior is EXACTLY as
        # before: no extra bus messages are handled and nothing is forwarded
        # (R8.1). Sink calls are wrapped so a sink error can never disrupt the
        # pipeline (R8.5).
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
        # ERROR messages arrive on the GStreamer bus inside a GLib signal
        # callback. Raising a Python exception from within that C callback does
        # NOT propagate out to loop.run() — GLib prints the traceback to stderr
        # and keeps the loop running, so loop.run() would block forever and the
        # whole flask-app hangs. Instead, capture the error here, quit the loop,
        # and raise AFTER loop.run() returns (back on this thread).
        pipeline_error = {}

        def _notify_sink(element_name, kind, detail):
            # Forward a per-node status signal to the executor's collector.
            # Inert when no sink was passed; a sink error is swallowed so it
            # can never disrupt the pipeline (R8.5).
            if status_sink is None:
                return
            try:
                status_sink(element_name, kind, detail)
            except Exception:  # noqa: BLE001 - sink is best-effort, contained
                logger.debug("status_sink raised; ignoring", exc_info=True)

        def on_message(bus, message):
            acceptable_messages = [Gst.MessageType.ERROR, Gst.MessageType.EOS, Gst.MessageType.TAG]
            if status_sink is not None:
                # Additive, sink-only bus messages: WARNING (non-fatal, drives
                # a node's 'warning' status) and STATE_CHANGED (drives
                # 'running'). Existing ERROR/EOS/TAG handling is unchanged.
                acceptable_messages = acceptable_messages + [
                    Gst.MessageType.WARNING,
                    Gst.MessageType.STATE_CHANGED,
                ]
            if message.type not in acceptable_messages:
                return
            if message.type == Gst.MessageType.WARNING:
                # Non-fatal: never quit the loop on a warning — just log it and
                # forward it so the collector can mark the node 'warning'.
                warn, dbg = message.parse_warning()
                src_name = message.src.get_name() if message.src else "unknown"
                logger.warning("Pipeline WARNING - {} : {}".format(src_name, warn.message))
                if dbg:
                    logger.debug(f"Warning debug information: {dbg}")
                _notify_sink(src_name, "warning", warn.message)
                return
            if message.type == Gst.MessageType.STATE_CHANGED:
                # An element reaching PLAYING means its node is running. The
                # pipeline/bin's own transitions map to no node and are ignored
                # by the collector. Never affects control flow.
                _, new_state, _ = message.parse_state_changed()
                if new_state == Gst.State.PLAYING:
                    src_name = message.src.get_name() if message.src else "unknown"
                    _notify_sink(src_name, "running", None)
                return
            if message.type == Gst.MessageType.ERROR:
                err, dbg = message.parse_error()
                src_name = message.src.get_name() if message.src else "unknown"
                logger.error("Pipeline ERROR - {} : {}".format(src_name, err.message))
                if dbg:
                    logger.debug(f"Debug information: {dbg}")
                pipeline_error["message"] = "Pipeline failed with: {}. {}".format(
                    err.message, dbg if dbg else ""
                )
                logger.info("Quitting loop")
                loop.quit()
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

            # Safety watchdog: guarantee the loop terminates even if the
            # pipeline stalls without ever posting EOS or ERROR (otherwise
            # loop.run() blocks forever and the flask worker hangs).
            def _watchdog():
                if loop.is_running():
                    logger.error(
                        f"Pipeline watchdog timeout after {PIPELINE_TIMEOUT_SEC}s; "
                        "forcing loop quit."
                    )
                    pipeline_error.setdefault(
                        "message",
                        f"Pipeline timed out after {PIPELINE_TIMEOUT_SEC}s without "
                        "completing (no EOS/ERROR received).",
                    )
                    loop.quit()
                return False  # one-shot
            watchdog_id = GLib.timeout_add_seconds(PIPELINE_TIMEOUT_SEC, _watchdog)

            logger.warning("Setting pipeline to PLAYING state")
            ret = pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.error("Pipeline failed to start")
                # The state change failed synchronously, so the main loop never
                # ran and on_message never fired. Drain any ERROR the failing
                # element already posted to the bus (e.g. a Triton "failed to
                # load '<model>'" / "model name is valid?" error) and surface it,
                # so the caller gets an actionable reason instead of the opaque
                # "check logs above this".
                detail = ""
                try:
                    err_msg = bus.timed_pop_filtered(0, Gst.MessageType.ERROR)
                    if err_msg is not None:
                        gerror, dbg = err_msg.parse_error()
                        src = err_msg.src.get_name() if err_msg.src else "pipeline"
                        detail = " {}: {}{}".format(
                            src, gerror.message,
                            " ({})".format(dbg) if dbg else "")
                except Exception:  # noqa: BLE001 - best-effort error enrichment
                    pass
                try:
                    GLib.source_remove(watchdog_id)
                except Exception:
                    pass
                if detail:
                    raise PipelineExecutionException(
                        "Pipeline failed to change state to PLAYING -{}".format(detail))
                raise PipelineExecutionException(
                    "Pipeline failed to change state to PLAYING, check logs above this.")
            logger.warning("Pipeline started, waiting for Triton inference")
            if frame_data:
                source.emit("push-buffer", gst_buffer)
                source.emit("end-of-stream")
            logger.warning("Running pipeline main loop")
            loop.run()
            logger.warning("Pipeline main loop completed")
            # Cancel the watchdog if it didn't fire (ignore if already removed).
            try:
                GLib.source_remove(watchdog_id)
            except Exception:
                pass
            # If the bus reported an ERROR, raise it now that the loop has
            # cleanly exited (raising inside the callback would hang the loop).
            if pipeline_error:
                raise PipelineExecutionException(pipeline_error["message"])
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
