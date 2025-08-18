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

class TestDigitalInputProcess(LocalServerBaseTestCase):

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
    Test Description: Test init for Digital Input Process class
    """
    @patch("utils.digital_input_process_manager.DigitalInputProcess.gpio_init")
    @patch("utils.digital_input_process_manager.DigitalInputProcess.gpio_read")
    def test_init(self,mock_gpio_read, mock_gpio_init):

        from utils.digital_input_process_manager import DigitalInputProcess
        
        DigitalInputProcess.get_camera_id = Mock()
        DigitalInputProcess.get_camera_id.return_value = "Fake_1"

        DigitalInputProcess._get_image_source = Mock()
        DigitalInputProcess._get_image_source.return_value = {"type" : ImageSourceType.CAMERA}
        
        process = DigitalInputProcess(self.workflow)
        self.assertEqual(process.workflow, self.workflow)
        self.assertEqual(process.workflow_id, "test_workflow")
        self.assertEqual(process.pin, 17)
        self.assertEqual(process.debounce_time, 100)
        self.assertEqual(process.trigger_edge, 1)
        self.assertEqual(process.polling_frequency, 0.001)
        process.exit_event.set()

    """
    Test Description: Test workflow is not called based on initial value of digital pin
    """
    @patch("utils.digital_input_process_manager.DigitalInputProcess.gpio_read", return_value = 1)
    @patch("utils.digital_input_process_manager.DigitalInputProcess.trigger_workflow_and_debounce")
    def test_monitor_digital_input_events_workflow_not_called_based_on_init_digital_level(
        self, mock_trigger_and_debounce, mock_gpio_read
    ):
        from utils.digital_input_process_manager import DigitalInputProcess, setup_health_reporting, terminate_health_reporting

        DigitalInputProcess.get_camera_id = Mock()
        DigitalInputProcess.get_camera_id.return_value = "Fake_1"

        process = DigitalInputProcess(self.workflow)
        process.trigger_edge = 1
        setup_health_reporting(self.workflow["workflowId"])

        monitor_thread = threading.Thread(target=process.monitor_digital_input_events, args=())
        monitor_thread.daemon = True
        monitor_thread.start()

        # Wait for the thread to complete init
        time.sleep(0.1)
        mock_trigger_and_debounce.assert_not_called()
        process.exit_event.set()
        terminate_health_reporting(self.workflow["workflowId"])


    """
    Test Description: Test workflow trigger is in ready mode
    """
    @patch("utils.digital_input_process_manager.DigitalInputProcess.gpio_read", return_value = 0)
    @patch("utils.digital_input_process_manager.DigitalInputProcess.trigger_workflow_and_debounce")
    def test_monitor_digital_input_events_workflow_ready_mode(
        self, mock_trigger_and_debounce, mock_gpio_read
    ):
        from utils.digital_input_process_manager import DigitalInputProcess, setup_health_reporting, terminate_health_reporting

        DigitalInputProcess.get_camera_id = Mock()
        DigitalInputProcess.get_camera_id.return_value = "Fake_1"

        process = DigitalInputProcess(self.workflow)
        process.trigger_edge = 1  # Set trigger edge to 1 for testing
        setup_health_reporting(self.workflow["workflowId"])

        monitor_thread = threading.Thread(target=process.monitor_digital_input_events, args=())
        monitor_thread.daemon = True
        monitor_thread.start()

        # Wait for the thread to complete init
        time.sleep(0.1)
        mock_trigger_and_debounce.assert_not_called()
        assert process.ready_event.is_set() == True
        process.exit_event.set()
        terminate_health_reporting(self.workflow["workflowId"])
    


    """
    Test Description: Test workflow trigger not in ready mode
    """
    @patch("utils.digital_input_process_manager.DigitalInputProcess.gpio_read", return_value = 1)
    @patch("utils.digital_input_process_manager.DigitalInputProcess.trigger_workflow_and_debounce")
    def test_monitor_digital_input_events_workflow_trigger_not_ready(
        self, mock_trigger_and_debounce, mock_gpio_read
    ):
        from utils.digital_input_process_manager import DigitalInputProcess, setup_health_reporting, terminate_health_reporting
        process = DigitalInputProcess(self.workflow)
        process.trigger_edge = 1  # Set trigger edge to 1 for testing

        setup_health_reporting(self.workflow["workflowId"])
        monitor_thread = threading.Thread(target=process.monitor_digital_input_events, args=())
        monitor_thread.daemon = True
        monitor_thread.start()

        # Wait for the thread to complete init
        time.sleep(0.1)
        mock_trigger_and_debounce.assert_not_called()
        assert process.ready_event.is_set() == False
        process.exit_event.set()
        terminate_health_reporting(self.workflow["workflowId"])

    
    """
    Test Description: Test workflow triggered when a change of state occurs
    """
    @patch("utils.digital_input_process_manager.DigitalInputProcess.gpio_read", return_value = 0)
    @patch("utils.digital_input_process_manager.DigitalInputProcess.trigger_workflow_and_debounce")
    def test_monitor_digital_input_events_workflow_trigger_called(
        self, mock_trigger_and_debounce, mock_gpio_read

    ):
        from utils.digital_input_process_manager import DigitalInputProcess, setup_health_reporting, terminate_health_reporting

        DigitalInputProcess.get_camera_id = Mock()
        DigitalInputProcess.get_camera_id.return_value = "Fake_1"

        process = DigitalInputProcess(self.workflow)
        process.trigger_edge = 1  # Set trigger edge to 1 for testing

        setup_health_reporting(self.workflow["workflowId"])
        monitor_thread = threading.Thread(target=process.monitor_digital_input_events, args=())
        monitor_thread.daemon = True
        monitor_thread.start()
        
        # Wait for the thread to complete init
        time.sleep(0.1)
        assert process.ready_event.is_set() == True

        mock_gpio_read.return_value = 1

        # wait to check the states
        time.sleep(0.1)

        mock_trigger_and_debounce.assert_called()
        time.sleep(0.1)
        assert process.ready_event.is_set() == False
        process.exit_event.set()
        terminate_health_reporting(self.workflow["workflowId"])


    """
    Test Description: Test debounce logic
    """
    @patch("utils.digital_input_process_manager.DigitalInputProcess.gst_pipeline_executor")
    @patch("time.sleep")
    def check_debounce(self, mock_gst_pipeline_executor, mock_sleep):
        from utils.digital_input_process_manager import DigitalInputProcess

        DigitalInputProcess.get_camera_id = Mock()
        DigitalInputProcess.get_camera_id.return_value = "Fake_1"

        process = DigitalInputProcess(self.workflow)

        self.process.trigger_workflow_and_debounce()
        mock_sleep.assert_not_called()
        mock_gst_pipeline_executor.assert_called()

        self.process.debounce_time = 1000
        self.process.trigger_workflow_and_debounce()
        mock_sleep.assert_called_once()
        mock_gst_pipeline_executor.assert_called()
        process.exit_event.set()


    """
    Test Description: Test health reporting logic
    """
    @patch("utils.digital_input_process_manager.DigitalInputProcess.gpio_init")
    @patch("utils.digital_input_process_manager.DigitalInputProcess.gpio_read", return_value = 0)
    @patch("utils.digital_input_process_manager.DigitalInputProcess.trigger_workflow_and_debounce")
    @patch("utils.camera_manager.disconnect_camera")
    def test_check_health_reporting(self, mock_gpio_init, mock_gpio_read, mock_trigger_and_debounce, mock_disconnect_camera):
        from utils.digital_input_process_manager import (
            DigitalInputProcess,
            DIOProcessHealthStatusEnum,
            create_digital_input_process,
            terminate_digital_input_task,
            get_dio_process_health_report,
        )

        DigitalInputProcess._get_image_source = Mock()
        DigitalInputProcess._get_image_source.return_value = {"type" : ImageSourceType.CAMERA}

        DigitalInputProcess.get_camera_id = Mock()
        DigitalInputProcess.get_camera_id.return_value = "Fake_1"

        create_digital_input_process(self.workflow)
        time.sleep(3)
        report = get_dio_process_health_report(self.workflow["workflowId"])
        terminate_digital_input_task(self.workflow)

        assert report is not None
        assert report.get("status") == DIOProcessHealthStatusEnum.STARTING
        assert report.get("error_type") is None
        assert report.get("last_updated") is not None


if __name__ == "__main__":
    unittest.main()
