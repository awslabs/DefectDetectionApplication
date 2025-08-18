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
import asyncio
import logging
from enum import Enum
from typing import List, Dict, Any

from dao.sqlite_db.sqlite_db_operations import SessionLocal
from utils.captured_images_utils import convert_captured_data_to_db, run_image_capture_pipeline


class CaptureTaskStatus(Enum):
    RUNNING = "Running"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


# Paramters needed to run capture task continuously
class CaptureTaskParameters:
    def __init__(self, capture_task_id, interval, count, workflow_id, workflow_output_path, image_source_dict, capture_prefix=None):
        self.capture_task_id = capture_task_id
        self.interval = interval
        self.count = count
        self.workflow_id = workflow_id
        self.workflow_output_path = workflow_output_path
        self.image_source_dict = image_source_dict
        self.capture_prefix = capture_prefix

    def get(self, attr_name, default=None):
        return getattr(self, attr_name, default)

    def set(self, attr_name, value):
        return setattr(self, attr_name, value)


class CaptureTaskManager:
    def __init__(self, inference_result_accessor, gst_pipeline_executor):
        self.task_queue = []
        self.new_task_queue = asyncio.Queue()
        self.running = True
        self.inference_result_accessor = inference_result_accessor
        self.gst_pipeline_executor = gst_pipeline_executor

    async def capture_image_in_time_interval(self, capture_task: CaptureTaskParameters):
        try:
            for num in range(capture_task.count):
                logging.info(f"Capture task order[{num}] for total {capture_task.count}...")
                prefix = '{}-{}'.format(capture_task.capture_prefix, capture_task.capture_task_id) if capture_task.capture_prefix else capture_task.capture_task_id
                captured_location = run_image_capture_pipeline(
                    image_source=capture_task.image_source_dict,
                    workflow_output_path=capture_task.workflow_output_path,
                    gst_pipeline_executor=self.gst_pipeline_executor,
                    file_prefix=prefix
                    )
                captured_data = {
                    "capturedLocation": captured_location,
                    "captureTaskId": capture_task.capture_task_id
                }
                with SessionLocal() as session:
                    db_captured_data = convert_captured_data_to_db(captured_data=captured_data, workflow_id=capture_task.workflow_id)
                    self.inference_result_accessor.store_captured_data(session, db_captured_data)

                await asyncio.sleep(capture_task.interval)
        except Exception as e:
            logging.error(f"Exception in capture image: {e}")
            raise

    async def add_tasks_continuously(self):
        while self.running:
            try:
                # Wait for a new task to be added to the queue
                capture_task_parameter = await self.new_task_queue.get()
                new_task = asyncio.create_task(self.capture_image_in_time_interval(capture_task_parameter))
                self.task_queue.append(new_task)
                logging.info(f"Added new capture task {capture_task_parameter.capture_task_id} with interval {capture_task_parameter.interval} and count {capture_task_parameter.count}")
            except Exception as e:
                logging.error(f"Exception in add_tasks_continuously: {e}")
                self.running = False

    async def run(self):
        # Create a task for adding more capture tasks continuously
        task_adder = asyncio.create_task(self.add_tasks_continuously())

        try:
            # Wait for all tasks to complete or cancel them if an exception occurs
            await asyncio.gather(task_adder)
        except Exception as e:
            logging.error(f"Exception caught in main: {e}")
            self.running = False
            for task in self.task_queue:
                task.cancel()
            await asyncio.gather(*self.task_queue, return_exceptions=True)

    def add_task(self, capture_task: CaptureTaskParameters):
        try:
            asyncio.run_coroutine_threadsafe(
                self.new_task_queue.put((capture_task)),
                asyncio.get_event_loop()
            )
        except Exception as e:
            logging.error(f"Failed to add capture task: {e}")

    def get_tasks(self) -> List[Dict[str, Any]]:
        task_list = []
        for task in self.task_queue:
            if not task.done():
                task_info = {
                    "captureTaskId": task.get_coro().cr_frame.f_locals.get('capture_task').get('capture_task_id'),
                    "workflowId": task.get_coro().cr_frame.f_locals.get('capture_task').get('workflow_id'),
                    "interval": task.get_coro().cr_frame.f_locals.get('capture_task').get('interval'),
                    "count": task.get_coro().cr_frame.f_locals.get('capture_task').get('count'),
                    "prefix": task.get_coro().cr_frame.f_locals.get('capture_task').get('capture_prefix'),
                    "status": CaptureTaskStatus.RUNNING.value
                }
                task_list.append(task_info)
            else:
                # TODO: Remove done tasks
                task_info = {}
                if task.cancelled():
                    status = CaptureTaskStatus.CANCELLED
                    task_info["status"] = status
                elif task.exception() is not None:
                    status = CaptureTaskStatus.FAILED
                    status_message = f"Exception: {task.exception()}"
                    task_info["status"] = status
                    task_info["statusMessage"] = status_message
                else:
                    status = CaptureTaskStatus.COMPLETED
                    task_info["status"] = status

        return task_list