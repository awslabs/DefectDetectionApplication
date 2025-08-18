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
import traceback
from multiprocessing import active_children, Process, Event, shared_memory
import signal
import time
import pickle

from gstreamer.gst_pipeline_executor import GstPipelineExecutor

from model.workflow import Workflow
from model.image_source import ImageSource
from dao.sqlite_db.sqlite_db_operations import SessionLocal
import utils.utils as utils
import utils.inference_results_utils as inference_results_utils
from resources.accessors.image_source_accessor import ImageSourceAccessor

from utils.server_setup import inference_result_accessor
from utils.captured_images_utils import convert_captured_data_to_db
from utils.common import DIOProcessHealthStatusEnum
from utils import constants
from metrics.latency_metrics import LatencyMetrics

try:
    import periphery
    from periphery import GPIO
except Exception as e:
    print("periphery library could not be imported")
import logging

from utils.constants import GPIO_RISING, TRIGGER_TIMESTAMP, FRAME_CAPTURE_TIMESTAMP, INFERENCE_RECEIVED_TIMESTAMP, CAPTURE, INFERENCE
from utils.camera_manager import get_camera_frame
from model.image_source import ImageSourceType
import concurrent.futures
from metrics.collector import Timer

SHARED_MEMORY_NAME_PREFIX = "dda_dio_mem_block_process_"
SHARED_MEMORY_DEFAULT_SIZE_IN_BYTES = 1024 * 1024 # 1 MB
HEALTH_REPORT_CUTOFF_DURATION_IN_SECONDS = 60

logger = logging.getLogger(__name__)

class DigitalInputProcess(Process):
    def __init__(self,workflow, polling_frequency = 0.001):
        '''
        Fast API introduces a async signal handling mechanism. Instead of using a traditional signal handler,
        a socket is used to capture an event. When a digital input process is created, it inherits this file descriptor
        and signaling mechanism. When a terminate signal is sent, like while updating the workflow, the uvicorn server
        also sees the same termination signal and terminates the server as well. The solution is to override the digital
        io signaling mechanism to not use the fd from uvicorn.
        '''

        self.workflow = workflow
        self.workflow_id = workflow.get('workflowId')
        self.image_source_id = workflow.get('imageSourceId')
        self.__update_health_status(DIOProcessHealthStatusEnum.STARTING)
    
        self.image_source = self._get_image_source(self.workflow)
        self.camera_id = None
        if self.image_source.get("type") == ImageSourceType.CAMERA:
            self.camera_id = self.get_camera_id()

        self.input_cfg = self.workflow.get('inputConfigurations')[0]

        self.pin = int(self.input_cfg.get('pin'))
        self.debounce_time = self.input_cfg.get('debounceTime')
        self.trigger_edge = 1 if self.input_cfg.get('triggerState') == GPIO_RISING else 0
        self.gst_pipeline_executor = GstPipelineExecutor()

        self.exit_event = Event()
        self.ready_event = Event()
        self.polling_frequency = polling_frequency
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.future_list = []

        super().__init__(name=self.workflow_id, daemon=True)


    def __del__(self):
        try:
            self.executor.shutdown(wait=False)
        except Exception as err:
            logger.error(f"Error in shutdown {err}")


    def get_camera_id(self):
        self.image_source_accessor = ImageSourceAccessor()
        with SessionLocal() as session:
            image_source_db = self.image_source_accessor.get_image_source(self.image_source_id, session)
        self.image_source = utils.convert_sqlalchemy_object_to_dict(image_source_db)

        return self.image_source.get('cameraId')


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
            raise Exception(
                f"Image capture is not available because the image source is set to retrieve images from a folder"
                )
        elif self.image_source.get("type") == ImageSourceType.CAMERA:
            r = self.gst_pipeline_executor.execute_image_source_pipeline(
                ImageSource(**self.image_source), is_preview=False, frame_data=frame, 
                file_prefix=prefix, workflow_output_path=workflow_output_path
            )
            return r.get("captureLocation")
        elif self.image_source.get("type") == ImageSourceType.ICAM:
            r = self.gst_pipeline_executor.execute_image_source_pipeline(
                ImageSource(**self.image_source), is_preview=False, 
                file_prefix=prefix, workflow_output_path=workflow_output_path
            )
            return r.get("captureLocation")
        
    def execute_workflow(self, latency_metrics, frame = None):
        '''
        Function to handle workflow pipeline execution. This function is submitted to the thread pool.
        '''
        try: 
            inference_capture_id = ""
            # Run inference if workflow configures a model
            if self.workflow.get("featureConfigurations"):
                with SessionLocal() as session:
                    inference_capture_id, _ = self.gst_pipeline_executor.execute_workflow_pipeline(self.workflow, session, frame, latency_metrics=latency_metrics)
                    with Timer(metric_name="InferenceResultStoringTime"):
                        query = inference_results_utils.GetInferenceResults(self.workflow.get('workflowId'), None, 0, 1)
                        inference_res = query.get_infer_res_with_capture_id(inference_capture_id, self.workflow.get('workflowOutputPath'))
                        inference_res["captureType"] = INFERENCE
                        db_inference_res = inference_results_utils.convert_inference_res_to_save_in_db(inference_res, self.workflow)
                        inference_result_accessor.store_inference_result(session, db_inference_res)
                
                    latency_metrics.commit_timestamps(session, inference_capture_id)
            # Capture if workflow doesn't have a model
            else:
                with SessionLocal() as session:
                    inference_capture_id = utils.generate_capture_id(self.workflow.get('workflowId'))
                    captured_location = self.run_image_capture_pipeline(frame, prefix=inference_capture_id)

                    captured_data = {
                        "capturedLocation": captured_location,
                        "captureTaskId": inference_capture_id
                    }
                    db_captured_data = convert_captured_data_to_db(captured_data=captured_data, workflow_id=self.workflow.get('workflowId'))
                    inference_result_accessor.store_captured_data(session, db_captured_data)
            
            self.__update_health_status(DIOProcessHealthStatusEnum.RUNNING)
        except Exception as err:
            self.__update_health_status(DIOProcessHealthStatusEnum.ERROR, err)
            logger.error(f"Inference failed for id {inference_capture_id} error: {err}.")


    def monitor_digital_input_events(self):
        '''
        Function to monitor input events, we check the default level is same as trigger level,
        if its different we wait for a change in state of the gpio pin in the wait_for_trigger
        function. if same we wait until it has changed.

        Polling of GPIO pin is needed as advantech boxes don't support GPIO
        File based interrupt solution don't work as /sys/class/gpio is a virtual fs.
        '''
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


    def check_thread_status(self):
        for future in self.future_list:
            if not future.running():
                self.future_list.remove(future)
        size_of_pool = len(self.future_list)
        logger.info(f"Current thread pool in use {size_of_pool}")
        return size_of_pool

    def _get_image_source(self, workflow:Workflow):
        image_source_accessor = ImageSourceAccessor()
        image_source_id = workflow.get('imageSourceId')
        with SessionLocal() as session:
                image_source_db = image_source_accessor.get_image_source(image_source_id, session)
        image_source = utils.convert_sqlalchemy_object_to_dict(image_source_db)
        return image_source

    def _get_camera_config(self, image_source):
        logger.info(f"in cam config {image_source}")
        camera_config = utils.convert_sqlalchemy_object_to_dict(image_source.get("imageSourceConfiguration", None))

        return camera_config

    def trigger_workflow_and_debounce(self):
        '''
        Function to trigger workflow with a debounce mechanism
        '''
        latency_metrics = LatencyMetrics()
        latency_metrics.add_timestamp(TRIGGER_TIMESTAMP)

        size_of_pool = self.check_thread_status()
        if size_of_pool < 2:
            frame = None
            self.image_source = self._get_image_source(self.workflow)
            if self.image_source.get("type") == ImageSourceType.CAMERA:
                camera_config = self._get_camera_config(self.image_source)
                frame = get_camera_frame(self.camera_id, camera_config)
                latency_metrics.add_timestamp(FRAME_CAPTURE_TIMESTAMP)

            future = self.executor.submit(self.execute_workflow, latency_metrics, frame)
            
            self.future_list.append(future)
        else:
            logger.error(f"Pipeline is full, unable to fulfill request, Increase latency")

        end_time = time.time()
        execution_time_ms = (end_time - latency_metrics.get_timestamp(TRIGGER_TIMESTAMP)) * 1000
        logger.info(f"debounce time {self.debounce_time}")
        remaining_debounce_time_ms = self.debounce_time - execution_time_ms
        if remaining_debounce_time_ms > 0:
            time.sleep(remaining_debounce_time_ms / 1000)


    def __update_health_status(self, status: DIOProcessHealthStatusEnum, error: Exception = None):
        '''
        Load the current status of the digital input process into a shared memory buffer.
            The process has 4 state transitions
             STARTING: When the process is starting and no inference is run
             STARTING -> RUNNING: When inference ran successfully (inference needs to run atleast once to change state from STARTING to RUNNING)
             RUNNING -> ERROR: When workflow failed (camera or inference errors)
             ERROR -> RUNNING: When inference ran successfully and recovered from error state.
        '''
        try:
            existing_shm = get_shm_object(self.workflow_id)
            message = {
                "status" : status,
                "error_type" : error,
                "last_updated" : time.time()
            }
            pickled_message = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
            existing_shm.buf[:len(pickled_message)] = pickled_message
            existing_shm.close()
        except Exception as err:
            logger.error(f"Error while updating health status for {self.workflow_id}: {err}")


    def run(self):
        signal.set_wakeup_fd(-1)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        logger.info(f"Digital input task is running for workflow {self.workflow_id} and pin {self.pin}")

        while not self.exit_event.is_set():
            try:
                self.gpio_init()
                self.monitor_digital_input_events()
            except Exception as err:
                self.__update_health_status(DIOProcessHealthStatusEnum.ERROR, err)
                traceback.print_exc()
                logger.error(f"Digital Input process for workflow {self.workflow_id} error: {err}.")
            finally:
                logger.info(f"Closing gpio: {self.pin}")
                self.gpio_close()
                self.ready_event.clear()

                # Wait before recovering
                time.sleep(10)

'''
Function to create a digital input process
'''
## TODO:: Refactor this portion into a separate digital input manager
def create_digital_input_process(workflow: Workflow):
    workflow_id = workflow.get('workflowId')
    logger.info(f"Adding new digital input process for workflow {workflow_id}")
    setup_health_reporting(workflow_id)
    process = DigitalInputProcess(workflow)
    process.start()
    logger.info(f"Digital input process for workflow {workflow_id} started")
    processes = active_children()
    for process in processes:
        logger.info(f"Active process process: {process.name}")

'''
Function to terminate digital input task.
Get all active children of the main local server process and terminate the process name with workflowID.
WorkflowID is used to name the digital input process.
'''
def terminate_digital_input_task(workflow: Workflow):
    workflow_id = workflow.get('workflowId')
    processes = active_children()
    for process in processes:
        if process.name == workflow_id:
            try:
                logger.info(f"Terminating digital input process for workflow {workflow_id}")
                process.terminate()
                process.join(timeout=10)
                terminate_health_reporting(workflow_id)
                logger.info(f"Digital input process for workflow {workflow_id} terminated.")
            except Exception as err:
                traceback.print_exc()
                logger.error(f"Caught digital input process for workflow {workflow_id} exception while termination: {err}")
    return


## TODO:: Refactor the health reporting portion (below) to a separate health reporting manager
def setup_health_reporting(workflow_id: str):
    logger.info(f"Starting health reporting for workflow_id: {workflow_id}")
    __shm_name = SHARED_MEMORY_NAME_PREFIX + workflow_id
    try:
        # Size specifies the requested number of bytes when creating a new shared memory block.
        # Some platforms choose to allocate chunks of memory based upon that platform’s memory page size.
        # The exact size of the shared memory block may be larger or equal to the size requested.
        # Default size is set at 1MB and will not automatically increase based on usage.
        # Child process will throw an error if message size exceeds the default size.
        __shm = shared_memory.SharedMemory(name=__shm_name, create=True, size=SHARED_MEMORY_DEFAULT_SIZE_IN_BYTES)
        return __shm
    except FileExistsError:
        logger.warning(f"Shared memory file already exists for workflow_id: {workflow_id}. Re-using the existing file.")
        __shm = shared_memory.SharedMemory(name=__shm_name, create=False)
        return __shm
    except Exception as shm_e:
        logger.error(f"Error while setting up shared memory file. Error: {str(shm_e)}")
        raise shm_e


def terminate_health_reporting(workflow_id: str):
    logger.info(f"Terminating health reporting for workflow_id: {workflow_id}")
    __shm = get_shm_object(workflow_id)
    __shm.close()
    __shm.unlink()


def get_shm_object(workflow_id):
    __shm_name = SHARED_MEMORY_NAME_PREFIX + workflow_id
    return shared_memory.SharedMemory(name=__shm_name)


def is_process_running(workflow_id):
    processes = active_children()
    for process in processes:
        if process.name == workflow_id:
            return True
    return False


def get_dio_process_health_report(workflow_id: str):
    try:
        __shm = get_shm_object(workflow_id)
        message = pickle.loads(__shm.buf)
        logger.info(f"Fetching health status for workflow_id: {workflow_id} message: {message}")
        return message
    except Exception as err:
        logger.error(f"Error while fetching health report for workflow_id: {workflow_id}. Error: {str(err)}")
        return None