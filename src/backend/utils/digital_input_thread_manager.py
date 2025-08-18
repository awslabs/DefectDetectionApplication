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
from threading import Thread, active_count, Event, Lock
from threading import enumerate as threading_enumerate
import time
import traceback
from gstreamer.gst_pipeline_executor import GstPipelineExecutor


from model.workflow import Workflow
from model.image_source import ImageSource
from dao.sqlite_db.sqlite_db_operations import SessionLocal
import utils.utils as utils
import utils.inference_results_utils as inference_results_utils
from resources.accessors.image_source_accessor import ImageSourceAccessor

from utils.captured_images_utils import convert_captured_data_to_db
from utils.common import DIOProcessHealthStatusEnum
from utils import constants


import logging

from utils.constants import (
    GPIO_RISING,
    TRIGGER_TIMESTAMP,
    FRAME_CAPTURE_TIMESTAMP,
    INFERENCE_RECEIVED_TIMESTAMP,
    CAPTURE,
    INFERENCE,
)
from utils.camera_manager import get_camera_frame
from model.image_source import ImageSourceType
import concurrent.futures
from metrics.collector import Timer

logger = logging.getLogger(__name__)
try:
    import periphery
    from periphery import GPIO
except Exception as e:
    logger.error("periphery library could not be imported")

message_storage = {}
message_storage_lock = Lock()
thread_events = {}
thread_events_lock = Lock()
class DigitalInputThread(Thread):
    def __init__(self, workflow, polling_frequency=0.001):
        self.workflow = workflow
        self.workflow_id = workflow.get("workflowId")
        self.image_source_id = workflow.get("imageSourceId")
        self.__update_health_status(DIOProcessHealthStatusEnum.STARTING)
        self.image_source = self._get_image_source(self.workflow)
        self.camera_id = None
        if self.image_source.get("type") == ImageSourceType.CAMERA:
            self.camera_id = self.get_camera_id()

        self.input_cfg = self.workflow.get("inputConfigurations")[0]

        self.pin = int(self.input_cfg.get("pin"))
        self.debounce_time = self.input_cfg.get("debounceTime")
        self.trigger_edge = 1 if self.input_cfg.get("triggerState") == GPIO_RISING else 0
        self.gst_pipeline_executor = GstPipelineExecutor()
        self.exit_event = Event()
        global thread_events
        global thread_events_lock
        thread_events_lock.acquire()
        thread_events[self.workflow_id] = self.exit_event
        thread_events_lock.release()
        self.ready_event = Event()
        self.polling_frequency = polling_frequency
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.future_in_progress = None
        super().__init__(name=self.workflow_id, daemon=False)

    def __del__(self):
        try:
            self.executor.shutdown(wait=False)
        except Exception as err:
            logger.error(f"Error in shutdown {err}")

    def get_camera_id(self):
        self.image_source_accessor = ImageSourceAccessor()
        with SessionLocal() as session:
            image_source_db = self.image_source_accessor.get_image_source(
                self.image_source_id, session
            )
        self.image_source = utils.convert_sqlalchemy_object_to_dict(image_source_db)

        return self.image_source.get("cameraId","")

    def gpio_init(self):
        self.gpio_in = periphery.GPIO(self.pin, "in")
        logger.info(f"GPIO pin {self.pin} initialized")

    def gpio_read(self):
        value = self.gpio_in.read()
        return value

    def gpio_close(self):
        self.gpio_in.close()
        logger.info(f"GPIO pin {self.pin} un-initialized")

    def run_image_capture_pipeline(self, frame, prefix):
        workflow_output_path = constants.INFERENCE_RESULTS_DIR + "/" + self.workflow_id
        if self.image_source.get("type") == ImageSourceType.FOLDER:
            raise TypeError(
                f"Image capture is not available because the image source is set to retrieve images from a folder"
            )
        elif self.image_source.get("type") == ImageSourceType.CAMERA:
            r = self.gst_pipeline_executor.execute_image_source_pipeline(
                ImageSource(**self.image_source),
                is_preview=False,
                frame_data=frame,
                file_prefix=prefix,
                workflow_output_path=workflow_output_path,
            )
            return r.get("captureLocation")
        elif self.image_source.get("type") == ImageSourceType.ICAM:
            r = self.gst_pipeline_executor.execute_image_source_pipeline(
                ImageSource(**self.image_source),
                is_preview=False,
                file_prefix=prefix,
                workflow_output_path=workflow_output_path,
            )
            return r.get("captureLocation")

    def execute_workflow(self, latency_metrics, frame=None):
        from utils.server_setup import inference_result_accessor
        """
        Function to handle workflow pipeline execution. This function is submitted to the thread pool.
        """
        try:
            inference_capture_id = ""
            # Run inference if workflow configures a model
            if self.workflow.get("featureConfigurations"):
                with SessionLocal() as session:
                    inference_capture_id, _ = self.gst_pipeline_executor.execute_workflow_pipeline(
                        self.workflow, session, frame, latency_metrics=latency_metrics
                    )
                    with Timer(metric_name="InferenceResultStoringTime"):
                        query = inference_results_utils.GetInferenceResults(
                            self.workflow.get("workflowId"), None, 0, 1
                        )
                        inference_res = query.get_infer_res_with_capture_id(
                            inference_capture_id, self.workflow.get("workflowOutputPath")
                        )
                        inference_res["captureType"] = INFERENCE
                        db_inference_res = (
                            inference_results_utils.convert_inference_res_to_save_in_db(
                                inference_res, self.workflow
                            )
                        )
                        inference_result_accessor.store_inference_result(session, db_inference_res)

                    latency_metrics.commit_timestamps(session, inference_capture_id)
            # Capture if workflow doesn't have a model
            else:
                with SessionLocal() as session:
                    inference_capture_id = utils.generate_capture_id(
                        self.workflow.get("workflowId")
                    )
                    captured_location = self.run_image_capture_pipeline(
                        frame, prefix=inference_capture_id
                    )

                    captured_data = {
                        "capturedLocation": captured_location,
                        "captureTaskId": inference_capture_id,
                    }
                    db_captured_data = convert_captured_data_to_db(
                        captured_data=captured_data, workflow_id=self.workflow.get("workflowId")
                    )
                    inference_result_accessor.store_captured_data(session, db_captured_data)
            self.__update_health_status(DIOProcessHealthStatusEnum.RUNNING)
        except Exception as err:
            self.__update_health_status(DIOProcessHealthStatusEnum.ERROR, err)
            logger.error(f"Inference failed for id {inference_capture_id} error: {err}.")

    def monitor_digital_input_events(self):
        """
        Function to monitor input events, we check the default level is same as trigger level,
        if its different we wait for a change in state of the gpio pin in the wait_for_trigger
        function. if same we wait until it has changed.

        Polling of GPIO pin is needed as advantech boxes don't support GPIO
        File based interrupt solution don't work as /sys/class/gpio is a virtual fs.
        """
        while not self.exit_event.is_set():
            base_value = self.gpio_read()
            if not self.ready_event.is_set():
                if base_value != self.trigger_edge:
                    self.ready_event.set()
            else:
                if base_value == self.trigger_edge:
                    self.trigger_workflow_and_debounce()
                    self.ready_event.clear()
            # Reduce cpu cycles by 50%. Sleep timer accuracy is 1 microsecond.
            time.sleep(self.polling_frequency)

    def _get_image_source(self, workflow: Workflow):
        image_source_accessor = ImageSourceAccessor()
        image_source_id = workflow.get("imageSourceId")
        with SessionLocal() as session:
            image_source_db = image_source_accessor.get_image_source(image_source_id, session)
        image_source = utils.convert_sqlalchemy_object_to_dict(image_source_db)
        return image_source

    def _get_camera_config(self, image_source):
        logger.info(f"in cam config {image_source}")
        camera_config = utils.convert_sqlalchemy_object_to_dict(
            image_source.get("imageSourceConfiguration", None)
        )

        return camera_config

    def trigger_workflow_and_debounce(self):
        from metrics.latency_metrics import LatencyMetrics
        """
        Function to trigger workflow with a debounce mechanism
        """
        latency_metrics = LatencyMetrics()
        latency_metrics.add_timestamp(TRIGGER_TIMESTAMP)

        if self.future_in_progress is None or self.future_in_progress.done():
            frame = None
            self.image_source = self._get_image_source(self.workflow)
            if self.image_source.get("type") == ImageSourceType.CAMERA:
                camera_config = self._get_camera_config(self.image_source)
                frame = get_camera_frame(self.camera_id, camera_config)
                latency_metrics.add_timestamp(FRAME_CAPTURE_TIMESTAMP)

            self.future_in_progress = self.executor.submit(self.execute_workflow, latency_metrics, frame)

        else:
            logger.error(f"Pipeline is full, unable to fulfill request, Increase latency")

        end_time = time.time()
        execution_time_ms = (end_time - latency_metrics.get_timestamp(TRIGGER_TIMESTAMP)) * 1000
        logger.info(f"debounce time {self.debounce_time}")
        remaining_debounce_time_ms = self.debounce_time - execution_time_ms
        if remaining_debounce_time_ms > 0:
            time.sleep(remaining_debounce_time_ms / 1000)

    def __update_health_status(self, status: DIOProcessHealthStatusEnum, error: Exception = None):
        """
        Load the current status of the digital input process into a shared memory buffer.
            The process has 4 state transitions
             STARTING: When the process is starting and no inference is run
             STARTING -> RUNNING: When inference ran successfully (inference needs to run atleast once to change state from STARTING to RUNNING)
             RUNNING -> ERROR: When workflow failed (camera or inference errors)
             ERROR -> RUNNING: When inference ran successfully and recovered from error state.
        """
        global message_storage
        global message_storage_lock
        message_storage_lock.acquire()
        message_storage[self.workflow_id] = {
            "status": status,
            "error_type": error,
            "last_updated": time.time(),
        }
        message_storage_lock.release()

    def run(self):
        logger.info(
            f"Digital input task is running for workflow {self.workflow_id} and pin {self.pin}"
        )
        while not self.exit_event.is_set():
            try:
                self.gpio_init()
                self.monitor_digital_input_events()
            except Exception as err:
                self.__update_health_status(DIOProcessHealthStatusEnum.ERROR, err)
                traceback.print_exc()
                logger.error(f"Digital Input thread for workflow {self.workflow_id} error: {err}.")
            finally:
                logger.info(f"Closing gpio: {self.pin}")
                self.gpio_close()
                self.ready_event.clear()

                # Wait before recovering
                time.sleep(10)


"""
Function to create a digital input thread
"""


def create_digital_input_thread(workflow: Workflow):
    workflow_id = workflow.get("workflowId")
    logger.info(f"Adding new digital input process for workflow {workflow_id}")
    thread = DigitalInputThread(workflow)
    thread.start()
    logger.info(f"Digital input process for workflow {workflow_id} started")
    threads = threading_enumerate()
    for thread in threads:
        logger.info(f"Active thread(s) Thread: {thread.name}")


"""
Function to terminate digital input task.
Get all active threads of the main local server process and terminate the thread name with workflowID.
WorkflowID is used to name the digital input thread.
"""


def terminate_digital_input_task_thread(workflow: Workflow):
    workflow_id = workflow.get("workflowId")
    threads = threading_enumerate()
    for thread in threads:
        if thread.name == workflow_id:
            try:
                logger.info(f"Terminating digital input thread for workflow {workflow_id}")
                global thread_events
                global thread_events_lock
                thread_events_lock.acquire()
                thread_events[workflow_id].set()
                thread_events_lock.release()
                thread.join(timeout=10)
                logger.info(f"Digital input thread for workflow {workflow_id} terminated.")
            except Exception as err:
                traceback.print_exc()
                logger.error(
                    f"Caught digital input thread for workflow {workflow_id} exception while termination: {err}"
                )
    return


def is_thread_running(workflow_id):
    threads = threading_enumerate()
    for thread in threads:
        if thread.name == workflow_id:
            return True
    return False


def get_dio_thread_health_report(workflow_id: str):
    global message_storage
    global message_storage_lock
    message_storage_lock.acquire()
    message = message_storage.get(workflow_id, "")
    message_storage_lock.release()
    if message:
        logger.info(f"Fetching health status for workflow_id: {workflow_id} message: {message}")
        return message
    else:
        logger.error(f"Error while fetching health report for workflow_id: {workflow_id}.")
        return None
