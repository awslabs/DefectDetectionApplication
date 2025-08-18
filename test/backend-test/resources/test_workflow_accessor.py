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
import os
from unittest.mock import patch, Mock

import logging

from local_server_base_test_case import LocalServerBaseTestCase
from dao.iotshadow.IoTShadowAccessor import IoTShadowAccessor
from fastapi import HTTPException
from sqlalchemy.orm import Session
from constants import FAKE_TIME_STAMP
from utils.constants import GPIO_FALLING
import pytest
from testfixtures import LogCapture

image_source = {
    "description": "folder test initial",
    "location": "/tmp/ddatests",
    "name": "fake_folder0",
    "type": "Folder"
}

image_source_camera = {
    "description": "camera test initial",
    "name": "fake_camera0",
    "cameraId": "Fake_1",
    "type": "Camera"
}

feature_configuration = {
    "type": "LFVModel",
    "modelName": "LFVModelName", 
}

feature_configuration_w_default_config = {
    "type": "LFVModel",
    "modelName": "LFVModelName", 
    "defaultConfiguration": {"modelAlias": "friendly name", "modelMetaData": "model description"}
}

input_configuration = {
    "inputConfigurationId": "fake_input_config_id",
    "creationTime": 0,
    "pin": "3",
    "triggerState": GPIO_FALLING,
    "debounceTime": 100
}

output_configuration = {
    "outputConfigurationId": "fake_output_config_id",
    "pin": "3",
    "signalType": GPIO_FALLING,
    "pulseWidth": 1,
    "creationTime": 0,
    "rule": "Normal"
}

test_workflow = {
    "workflowId": "12345",
    "name": "workflow_12345",
    "description": "test_workflow",
    "creationTime": FAKE_TIME_STAMP,
    "lastUpdatedTime": FAKE_TIME_STAMP,
    "workflowOutputPath": "/aws_dda/inference-results/12345",
    "imageSourceId": "fake_image_src_id",
    "featureConfigurations": [feature_configuration],
    "inputConfigurations": [input_configuration],
    "outputConfigurations": [output_configuration]
}

test_workflow2 = {
    "workflowId": "67890",
    "name": "workflow_67890",
    "creationTime": FAKE_TIME_STAMP,
    "lastUpdatedTime": FAKE_TIME_STAMP,
    "workflowOutputPath": "/aws_dda/inference-results/67890",
    "imageSourceId": "fake_image_src_id",
    "inputConfigurations": []
}

class TestWorkflowAccessor(LocalServerBaseTestCase):

    def setUp(self):
        super().setUp()

        from resources.accessors import workflow_accessor, image_source_accessor
        self.session = Session(self.engine)
        self.iot_shadow_accessor = Mock(spec=IoTShadowAccessor)
        self.workflow_accessor = workflow_accessor.WorkflowAccessor(self.iot_shadow_accessor)
        self.image_source_accessor = image_source_accessor.ImageSourceAccessor()

        self.image_source_accessor._ImageSourceAccessor__create_folder = Mock()
        self.test_image_source_key = {'imageSourceId': self.image_source_accessor.create_image_source(image_source, self.session)["imageSourceId"]}
        self.test_image_source_camera_key = {'imageSourceId': self.create_image_source_camera_type(self.image_source_accessor)}
        self.workflow_accessor._WorkflowAccessor__create_folder = Mock(return_value="/aws_dda/inference-results/fake-workflow-id")
        self.workflow_accessor.create_workflow(test_workflow, self.session)
        self.workflow_accessor.create_workflow(test_workflow2, self.session)
        self.test_workflow_key = test_workflow["workflowId"]
        self.test_workflow_key2 = test_workflow2["workflowId"]
        self.workflow_accessor.get_camera_id = Mock()
        self.workflow_accessor.get_camera_id.return_value = "Fake"

    def tearDown(self):
        super().tearDown()

    def test_create_workflow_happy_path(self):
        from dao.sqlite_db.models import Workflow
        test_workflow_data = {"workflowId": "fake-workflow-id"}
        self.workflow_accessor.create_workflow(test_workflow_data, self.session)
        result_data = self.session.get(Workflow, "fake-workflow-id")
        self.assertEqual(result_data.name, "workflow_fake-workflow-id")
        self.assertEqual(result_data.workflowOutputPath, "/aws_dda/inference-results/fake-workflow-id")

    def test_create_workflow_existed_id(self):
        test_workflow_data = {
            "workflowId": "12345",
            "name": "workflow_12345",
            "creationTime": FAKE_TIME_STAMP,
            "lastUpdatedTime": FAKE_TIME_STAMP,
            "workflowOutputPath": "/aws_dda/inference-results/workflow_12345"
        }
        with pytest.raises(HTTPException) as err:
            with patch.object(self.workflow_accessor, '_WorkflowAccessor__create_folder', return_value='/aws_dda/inference-results/workflow_12345') as method:
                response = self.workflow_accessor.create_workflow(test_workflow_data, self.session)
                assert response.type == HTTPException

    def test_create_workflow_missing_id(self):
        test_workflow_data = {
            "name": "workflow_12345",
            "creationTime": FAKE_TIME_STAMP,
            "lastUpdatedTime": FAKE_TIME_STAMP,
            "workflowOutputPath": "/aws_dda/inference-results/workflow_12345"
        }
        with pytest.raises(HTTPException) as err:
            response = self.workflow_accessor.create_workflow(test_workflow_data, self.session)
            assert response.type == HTTPException
    @patch('utils.utils.gen_uuid', return_value='fake_image_source_id')
    def create_image_source_camera_type(self, image_source_accessor, mock_gen_uuid):
        image_source_accessor._ImageSourceAccessor__create_folder = Mock()
        image_source_accessor._ImageSourceAccessor__create_image_source_configuration = Mock(return_value="fakeid")
        return self.image_source_accessor.create_image_source(image_source_camera, self.session)["imageSourceId"]

    @patch('os.path.getmtime', return_value=1)
    @patch('os.listdir', return_value=["test-1.jpg"])
    @patch('utils.utils.create_em_agent_config')
    def test_update_workflow_minimum_options_happy_path(self, mock_create_agent_config,
                                                        mock_os_listir, moc_os_path_getatime):
        from dao.sqlite_db.models import Workflow
        test_workflow = {
            "workflowId": self.test_workflow_key,
            "name":  "fake_workflow_1",
            "imageSources": [self.test_image_source_key],
            "featureConfigurations": [feature_configuration]
        }

        primary_key = self.workflow_accessor.update_workflow(test_workflow, self.session)
        result_data = self.session.get(Workflow, primary_key)

        self.assertTrue(mock_create_agent_config.called)
        self.assertIsNotNone(result_data.workflowId)
        self.assertEqual(result_data.name, test_workflow["name"])
        self.assertEqual(len(result_data.description), 0)
        self.assertIsNotNone(result_data.workflowOutputPath)
        self.assertIsNotNone(result_data.imageSources)
        self.assertEqual(len(result_data.outputConfigurations), 0)
        self.assertEqual(len(result_data.featureConfigurations), len(test_workflow["featureConfigurations"]))
        self.assertIsNotNone(result_data.creationTime)
        self.assertIsNotNone(result_data.lastUpdatedTime)

    @patch('os.path.getmtime', return_value=1)
    @patch('os.listdir', return_value=["test-1.jpg"])
    @patch('utils.utils.create_em_agent_config')
    @patch('utils.digital_input_process_manager.create_digital_input_process')
    def test_get_workflow_happy_path(self, digital_input_create, mock_create_agent_config,
                                     mock_os_listir, moc_os_path_getatime):
        from dao.sqlite_db.models import Workflow
        test_workflow = {
            "workflowId": self.test_workflow_key,
            "name":  "fake_workflow_2",
            "description": "fake_description",
            "imageSources": [self.test_image_source_key],
            "inputConfigurations": [input_configuration],
            "outputConfigurations": [output_configuration],
            "featureConfigurations": [feature_configuration]
        }

        primary_key = self.workflow_accessor.update_workflow(test_workflow, self.session)
        result_data = self.session.get(Workflow, primary_key)

        self.assertTrue(digital_input_create.called)
        self.assertTrue(mock_create_agent_config.called)
        self.assertIsNotNone(result_data.workflowId)
        self.assertEqual(result_data.name, test_workflow["name"])
        self.assertEqual(result_data.description, test_workflow["description"])
        self.assertIsNotNone(result_data.workflowOutputPath)
        self.assertIsNotNone(result_data.imageSources)
        self.assertEqual(result_data.outputConfigurations, test_workflow["outputConfigurations"])
        self.assertEqual(result_data.featureConfigurations, test_workflow["featureConfigurations"])
        self.assertIsNotNone(result_data.creationTime)
        self.assertIsNotNone(result_data.lastUpdatedTime)

    def test_get_workflow_with_default_config_happy_path(self):
        result_data = self.workflow_accessor.get_workflow_with_default_config(self.test_workflow_key, self.session)
        self.assertIsNotNone(result_data)

    @patch('os.path.getmtime', return_value=1)
    @patch('os.listdir', return_value=["test-1.jpg"])
    @patch('utils.utils.create_em_agent_config')
    @patch('utils.digital_input_process_manager.create_digital_input_process')
    def test_update_workflow_all_options_happy_path(self, digital_input_create, mock_create_agent_config,
                                                    mock_os_listir, moc_os_path_getatime):
        from dao.sqlite_db.models import Workflow
        test_workflow = {
            "workflowId": self.test_workflow_key,
            "name":  "fake_workflow_2",
            "description": "fake_description",
            "imageSources": [self.test_image_source_key],
            "inputConfigurations": [input_configuration],
            "outputConfigurations": [output_configuration],
            "featureConfigurations": [feature_configuration]
        }

        primary_key = self.workflow_accessor.update_workflow(test_workflow, self.session)
        result_data = self.session.get(Workflow, primary_key)

        self.assertTrue(digital_input_create.called)
        self.assertTrue(mock_create_agent_config.called)
        self.assertIsNotNone(result_data.workflowId)
        self.assertEqual(result_data.name, test_workflow["name"])
        self.assertEqual(result_data.description, test_workflow["description"])
        self.assertIsNotNone(result_data.workflowOutputPath)
        self.assertIsNotNone(result_data.imageSources)
        self.assertEqual(len(result_data.inputConfigurations), len(test_workflow["inputConfigurations"]))
        self.assertEqual(len(result_data.outputConfigurations), len(test_workflow["outputConfigurations"]))
        self.assertEqual(len(result_data.featureConfigurations), len(test_workflow["featureConfigurations"]))
        self.assertIsNotNone(result_data.creationTime)
        self.assertIsNotNone(result_data.lastUpdatedTime)

    @patch('utils.utils.create_em_agent_config')
    def test_update_workflow_for_image_source_type_from_folder_to_camera(self, mock_create_agent_config):
        test_workflow2 = {
            "workflowId": self.test_workflow_key2,
            "name":  "fake_workflow_2",
            "imageSources": [self.test_image_source_key],
            "featureConfigurations": [feature_configuration]
        }

        self.workflow_accessor.update_workflow(test_workflow2, self.session)
        self.assertTrue(mock_create_agent_config.called)

        update_test_workflow2 = {
            "workflowId": self.test_workflow_key2,
            "name":  "fake_workflow_2",
            "imageSources": [self.test_image_source_camera_key],
            "featureConfigurations": [feature_configuration]
        }
        self.workflow_accessor.update_workflow(update_test_workflow2, self.session)
        self.assertTrue(mock_create_agent_config.called)

    def test_list_workflows_happy_path(self):

        NUM_WORKFLOWS = 3

        for i in range(NUM_WORKFLOWS):
            test_workflow = {
                "workflowId": str(i)
            }
            self.workflow_accessor.create_workflow(test_workflow, self.session)

        result_data = self.workflow_accessor.list_workflows(self.session)
        self.assertEqual(len(result_data), NUM_WORKFLOWS + 2)

    @patch('utils.utils.gen_uuid', return_value='fake_id')
    def test_list_workflows_by_camera_happy_path(self, mock_gen_uuid):

        NUM_WORKFLOWS = 3

        for i in range(NUM_WORKFLOWS):
            test_workflow = {
                "workflowId": str(i),
                "imageSourceId": 'fake_image_source_id'
            }
            self.workflow_accessor.create_workflow(test_workflow, self.session)
        
        result_data = self.workflow_accessor.list_workflows_by_camera("Fake_1", self.session)
        self.assertEqual(len(result_data), NUM_WORKFLOWS)

    def test_update_workflow_missing_attr(self):

        test_workflow = {
            "workflowId": self.test_workflow_key,
            "name":  "fake_workflow_4"
        }

        with pytest.raises(HTTPException) as err:
            response = self.workflow_accessor.update_workflow(test_workflow, self.session)
            self.assertIn("Invalid data provided: ", response.description)

    def test_get_workflow_non_exist(self):

        NONEXISTANT_ID = "nonexistant_id"

        with pytest.raises(HTTPException) as err:
            response = self.workflow_accessor.get_workflow_by_id(NONEXISTANT_ID, self.session)
            expected_err = f"Workflow {NONEXISTANT_ID} not found"
            self.assertIn(expected_err, response.description)

    def test_update_workflow_non_exist(self):

        NONEXISTANT_ID = "nonexistant_id"

        test_workflow = {
            "workflowId": NONEXISTANT_ID,
            "name":  "fake_workflow_6",
            "imageSources": [self.test_image_source_key]
        }

        with pytest.raises(HTTPException) as err:
            response = self.workflow_accessor.update_workflow(test_workflow, self.session)
            expected_err = f"Workflow {NONEXISTANT_ID} not found"
            self.assertIn(expected_err, response.description)

    def test_update_workflow_no_id(self):

        test_workflow = {
            "name":  "fake_workflow_6",
            "imageSources": [self.test_image_source_key]
        }
        with pytest.raises(HTTPException) as err:
            response = self.workflow_accessor.update_workflow(test_workflow, self.session)
            expected_err = f"No workflowId provided"
            self.assertIn(expected_err, response.description)

    def test_update_workflow_image_source_non_exist(self):

        NONEXISTANT_ID = "nonexistant_id"

        test_workflow = {
            "workflowId": self.test_workflow_key,
            "name":  "fake_workflow_6",
            "imageSources": [{'imageSourceId': NONEXISTANT_ID}]
        }

        with pytest.raises(HTTPException) as err:
            response = self.workflow_accessor.update_workflow(test_workflow, self.session)
            expected_err = f"Image source {NONEXISTANT_ID} does not exist"
            self.assertIn(expected_err, response.description)


    def test_delete_workflow(self):
        from dao.sqlite_db.models import Workflow
        data_init = self.session.get(Workflow, "12345")
        self.assertIsNotNone(data_init)
        self.workflow_accessor.delete_workflow(self.test_workflow_key, self.session)
        data_del = self.session.get(Workflow, "12345")
        self.assertIsNone(data_del)
        
    @patch('utils.digital_input_process_manager.create_digital_input_process')
    def test_retry_dio_workflow(self, digital_input_create):
        # Verify non api workflow cannot be retried
        with pytest.raises(HTTPException) as err:
            response = self.workflow_accessor.retry_dio_workflow(self.test_workflow_key2, self.session)
            expected_err = f"The workflow {self.test_workflow_key2} doesn't have any input configurations"
            self.assertIn(expected_err, response.description)
        
        # Verify dio workflow retried
        with LogCapture() as logs:
            self.workflow_accessor.retry_dio_workflow(self.test_workflow_key, self.session)
            
        self.assertTrue(digital_input_create.called)
        logs.check(
            ("resources.accessors.workflow_accessor", "INFO", f"Restart workflow {self.test_workflow_key} successfully.")
        )