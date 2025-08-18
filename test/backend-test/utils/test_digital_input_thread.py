# Copyright 2025 Amazon Web Services, Inc.
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
import unittest
from unittest.mock import Mock, patch
import os
import threading
import time
from local_server_base_test_case import LocalServerBaseTestCase
from model.image_source import ImageSourceType
from utils.constants import GPIO_RISING

class TestDigitalInputThread(LocalServerBaseTestCase):

    def setUp(self):
        super().setUp()
        os_environment_patcher = patch.dict(
            os.environ,
            {
                "COMPONENT_WORK_PATH": "test/backend-test/resources",
                "LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": "test/",
                "INFERENCE_COMPONENT_DECOMPRESED_PATH": "test/",
            },
        )
        os_environment_patcher.start()
        self.workflow = {
            "workflowId": "test_workflow",
            "inputConfigurations": [
                {"pin": 17, "debounceTime": 100, "triggerState": GPIO_RISING}
            ],
        }
       
       

    """
    Test Description: Test init for Digital Input thread class
    """
    @patch("utils.digital_input_thread_manager.DigitalInputThread.gpio_init")
    @patch("utils.digital_input_thread_manager.DigitalInputThread.gpio_read")
    def test_init(self,mock_gpio_read, mock_gpio_init):

        from utils.digital_input_thread_manager import DigitalInputThread
        
        DigitalInputThread.get_camera_id = Mock()
        DigitalInputThread.get_camera_id.return_value = "Fake_1"

        DigitalInputThread._get_image_source = Mock()
        DigitalInputThread._get_image_source.return_value = {"type" : ImageSourceType.CAMERA}
        
        thread = DigitalInputThread(self.workflow)
        self.assertEqual(thread.workflow, self.workflow)
        self.assertEqual(thread.workflow_id, "test_workflow")
        self.assertEqual(thread.pin, 17)
        self.assertEqual(thread.debounce_time, 100)
        self.assertEqual(thread.trigger_edge, 1)
        self.assertEqual(thread.polling_frequency, 0.001)
        thread.exit_event.set()

    """
    Test Description: Test workflow is not called based on initial value of digital pin
    """
    @patch("utils.digital_input_thread_manager.DigitalInputThread.gpio_read", return_value = 1)
    @patch("utils.digital_input_thread_manager.DigitalInputThread.trigger_workflow_and_debounce")
    def test_monitor_digital_input_events_workflow_not_called_based_on_init_digital_level(
        self, mock_trigger_and_debounce, mock_gpio_read
    ):
        from utils.digital_input_thread_manager import DigitalInputThread

        DigitalInputThread.get_camera_id = Mock()
        DigitalInputThread.get_camera_id.return_value = "Fake_1"

        thread = DigitalInputThread(self.workflow)
        thread.trigger_edge = 1

        monitor_thread = threading.Thread(target=thread.monitor_digital_input_events, args=())
        monitor_thread.daemon = True
        monitor_thread.start()

        # Wait for the thread to complete init
        time.sleep(0.1)
        mock_trigger_and_debounce.assert_not_called()
        thread.exit_event.set()


    """
    Test Description: Test workflow trigger is in ready mode
    """
    @patch("utils.digital_input_thread_manager.DigitalInputThread.gpio_read", return_value = 0)
    @patch("utils.digital_input_thread_manager.DigitalInputThread.trigger_workflow_and_debounce")
    def test_monitor_digital_input_events_workflow_ready_mode(
        self, mock_trigger_and_debounce, mock_gpio_read
    ):
        from utils.digital_input_thread_manager import DigitalInputThread

        DigitalInputThread.get_camera_id = Mock()
        DigitalInputThread.get_camera_id.return_value = "Fake_1"

        thread = DigitalInputThread(self.workflow)
        thread.trigger_edge = 1  # Set trigger edge to 1 for testing

        monitor_thread = threading.Thread(target=thread.monitor_digital_input_events, args=())
        monitor_thread.daemon = True
        monitor_thread.start()

        # Wait for the thread to complete init
        time.sleep(0.1)
        mock_trigger_and_debounce.assert_not_called()
        assert thread.ready_event.is_set() == True
        thread.exit_event.set()
    


    """
    Test Description: Test workflow trigger not in ready mode
    """
    @patch("utils.digital_input_thread_manager.DigitalInputThread.gpio_read", return_value = 1)
    @patch("utils.digital_input_thread_manager.DigitalInputThread.trigger_workflow_and_debounce")
    def test_monitor_digital_input_events_workflow_trigger_not_ready(
        self, mock_trigger_and_debounce, mock_gpio_read
    ):
        from utils.digital_input_thread_manager import DigitalInputThread
        thread = DigitalInputThread(self.workflow)
        thread.trigger_edge = 1  # Set trigger edge to 1 for testing

        monitor_thread = threading.Thread(target=thread.monitor_digital_input_events, args=())
        monitor_thread.daemon = True
        monitor_thread.start()

        # Wait for the thread to complete init
        time.sleep(0.1)
        mock_trigger_and_debounce.assert_not_called()
        assert thread.ready_event.is_set() == False
        thread.exit_event.set()
    
    """
    Test Description: Test workflow triggered when a change of state occurs
    """
    @patch("utils.digital_input_thread_manager.DigitalInputThread.gpio_read", return_value = 0)
    @patch("utils.digital_input_thread_manager.DigitalInputThread.trigger_workflow_and_debounce")
    def test_monitor_digital_input_events_workflow_trigger_called(
        self, mock_trigger_and_debounce, mock_gpio_read

    ):
        from utils.digital_input_thread_manager import DigitalInputThread

        DigitalInputThread.get_camera_id = Mock()
        DigitalInputThread.get_camera_id.return_value = "Fake_1"

        thread = DigitalInputThread(self.workflow)
        thread.trigger_edge = 1  # Set trigger edge to 1 for testing

        monitor_thread = threading.Thread(target=thread.monitor_digital_input_events, args=())
        monitor_thread.daemon = True
        monitor_thread.start()
        
        # Wait for the thread to complete init
        time.sleep(0.1)
        assert thread.ready_event.is_set() == True

        mock_gpio_read.return_value = 1

        # wait to check the states
        time.sleep(0.1)

        mock_trigger_and_debounce.assert_called()
        time.sleep(0.1)
        assert thread.ready_event.is_set() == False
        thread.exit_event.set()


    """
    Test Description: Test debounce logic
    """
    @patch("utils.digital_input_thread_manager.DigitalInputThread.gst_pipeline_executor")
    @patch("time.sleep")
    def check_debounce(self, mock_gst_pipeline_executor, mock_sleep):
        from utils.digital_input_thread_manager import DigitalInputThread

        DigitalInputThread.get_camera_id = Mock()
        DigitalInputThread.get_camera_id.return_value = "Fake_1"

        thread = DigitalInputThread(self.workflow)

        self.thread.trigger_workflow_and_debounce()
        mock_sleep.assert_not_called()
        mock_gst_pipeline_executor.assert_called()

        self.thread.debounce_time = 1000
        self.thread.trigger_workflow_and_debounce()
        mock_sleep.assert_called_once()
        mock_gst_pipeline_executor.assert_called()
        thread.exit_event.set()


    """
    Test Description: Test health reporting logic
    """
    @patch("utils.digital_input_thread_manager.DigitalInputThread.gpio_init")
    @patch("utils.digital_input_thread_manager.DigitalInputThread.gpio_read", return_value = 0)
    @patch("utils.digital_input_thread_manager.DigitalInputThread.trigger_workflow_and_debounce")
    @patch("utils.camera_manager.disconnect_camera")
    def test_check_health_reporting(self, mock_gpio_init, mock_gpio_read, mock_trigger_and_debounce, mock_disconnect_camera):
        from utils.digital_input_thread_manager import (
            DigitalInputThread,
            DIOProcessHealthStatusEnum,
            create_digital_input_thread,
            terminate_digital_input_task_thread,
            get_dio_thread_health_report,
        )

        DigitalInputThread._get_image_source = Mock()
        DigitalInputThread._get_image_source.return_value = {"type" : ImageSourceType.CAMERA}

        DigitalInputThread.get_camera_id = Mock()
        DigitalInputThread.get_camera_id.return_value = "Fake_1"

        create_digital_input_thread(self.workflow)
        time.sleep(3)
        report = get_dio_thread_health_report(self.workflow["workflowId"])
        terminate_digital_input_task_thread(self.workflow)

        assert report is not None
        assert report.get("status") == DIOProcessHealthStatusEnum.STARTING
        assert report.get("error_type") is None
        assert report.get("last_updated") is not None


if __name__ == "__main__":
    unittest.main()
