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
import json

from local_server_base_test_case import LocalServerBaseTestCase
from unittest.mock import patch
from utils import utils
from utils.constants import EM_AGENT_CONFIG_PATH, DDA_LOGO_FOLDER
from constants import EMPTY_EM_AGENT_CONFIG

class TestUtils(LocalServerBaseTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.component_work_path_patcher = patch.dict(os.environ, {"KERNEL_ROOT_PATH": "./test/backend-test/utils", 
                                                                  "INFERENCE_COMPONENT_DECOMPRESED_PATH": "fake_infer_path", 
                                                                  "LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": "fake_component_path"})
        cls.component_work_path_patcher.start()
    
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.component_work_path_patcher.stop()

    def setUp(self):
        super().setUp()
        self.edge_manager_agent_config = os.path.join(os.environ['KERNEL_ROOT_PATH'], EM_AGENT_CONFIG_PATH)
        with open(self.edge_manager_agent_config, "w") as jsonFile:
            json.dump(EMPTY_EM_AGENT_CONFIG, jsonFile)
        self.workflow_output_path = "fake_output_path"

    def tearDown(self):
        super().tearDown()
        with open(self.edge_manager_agent_config, "w") as jsonFile:
            json.dump(EMPTY_EM_AGENT_CONFIG, jsonFile)

    def test_get_opcua_config_non_opcua_type(self):
        outputs = [{"path": "/usr/output", "type": "file"}, ]
        result = utils.get_opcua_config(outputs)
        self.assertEqual(result, (-1, []))

    def test_get_opcua_config_happy_path(self):
        outputs = [{"path": "/usr/output", "type": "file"}, 
                   {"opcuaConfig": {"scriptPath": "test-scriptPath"}, "type": "opcua"}]
        result = utils.get_opcua_config(outputs)
        self.assertEqual(result[0], 1)

    @patch("platform.uname", return_value=["","","","","aarch64"])
    def test_create_em_agent_config_aarch64(self, mock_platform_uname):
        workflow_config = {"workflowId": "id-test-utils",
                           "workflowOutputPath": self.workflow_output_path}
        workflow_em_agent_config_path = utils.create_em_agent_config(workflow_config)
        with open(workflow_em_agent_config_path, "r") as jsonFile:
            result = json.load(jsonFile)
        self.assertEqual(result["sagemaker_edge_core_capture_data_disk_path"], self.workflow_output_path)
        self.assertIn("certificates", result["sagemaker_edge_core_root_certs_path"])
        self.assertIn("fake_infer_path", result["sagemaker_edge_core_root_certs_path"])
        self.assertEqual(result["sagemaker_edge_core_capture_data_batch_size"], 1)

    @patch("platform.uname", return_value=["","","","",""])
    def test_create_em_agent_config_other(self, mock_platform_uname):
        workflow_config = {"workflowId": "id-test-utils",
                           "workflowOutputPath": self.workflow_output_path}
        workflow_em_agent_config_path = utils.create_em_agent_config(workflow_config)
        with open(workflow_em_agent_config_path, "r") as jsonFile:
            result = json.load(jsonFile)
        self.assertEqual(result["sagemaker_edge_core_capture_data_disk_path"], self.workflow_output_path)
        self.assertIn("fake_infer_path", result["sagemaker_edge_core_root_certs_path"])
        self.assertIn("certificates", result["sagemaker_edge_core_root_certs_path"])
        self.assertEqual(result["sagemaker_edge_core_capture_data_batch_size"], 1)

    @patch("platform.uname", return_value=["","","","","aarch64"])
    def test_get_gst_plugins_path_aarch64(self, mock_platform_uname):
        self.assertEqual("fake_infer_path/aarch64:/usr/lib/panoramagst", 
                         utils.get_gst_plugins_path())    
    
    @patch("platform.uname", return_value=["","","","",""])
    def test_get_gst_plugins_path_other(self, mock_platform_uname):
        self.assertEqual("fake_infer_path/amd64:/usr/lib/panoramagst", 
                         utils.get_gst_plugins_path())

    def test_generate_capture_id(self):
        result = utils.generate_capture_id("fake_id")
        assert result.split("-")[0] == "fake_id"
        assert result.split("'")[1] == "mock.correlation_id.get()"

    def test_get_dio_script_path(self):
        self.assertEqual("fake_component_path/script/dio.py", 
                         utils.get_dio_script_path())

    def test_remove_prefix(self):
        result = utils.remove_prefix("file://img.png", "file://")
        self.assertEqual(result, "img.png")

    def test_remove_prefix_invalid(self):
        result = utils.remove_prefix("file://img.png", "img")
        self.assertEqual(result, "file://img.png")

    def test_gen_uuid(self):
        result = utils.gen_uuid()
        assert result.isalnum()
        assert not any(x.isupper() for x in result)

    def test_split_file_name_and_path(self):
        file_path = "/tmp/path/image.jpg"
        folder_path, image_name = utils.split_file_name_and_path(file_path)
        self.assertEqual(folder_path, "/tmp/path")
        self.assertEqual(image_name, "image.jpg")

    def test_split_file_name_and_path_invalid(self):
        file_path = ""
        folder_path, image_name = utils.split_file_name_and_path(file_path)
        self.assertEqual(folder_path, "")
        self.assertEqual(image_name, "")

    def test_get_station_logo_returns_logo(self):
        from utils.utils import get_station_logo

        with patch('os.path.exists', return_value=True) as mock_exists:
            with patch('os.listdir', return_value=["aws-logo.png"]) as mock_listdir:  
                with patch('builtins.open', return_value=open("test/backend-test/test_logo/aws-logo.png","rb")):
                    with patch('mimetypes.guess_type', return_value=["image/png"]) as mock_mimetypes:
                        result = get_station_logo()        
            self.assertEqual(result[:22], 'data:image/png;base64,')
            mock_exists.assert_called_once_with(DDA_LOGO_FOLDER)
            mock_listdir.assert_called_with(DDA_LOGO_FOLDER)

    def test_get_station_logo_no_logo_folder(self):
        from utils.utils import get_station_logo
        
        with patch('os.path.exists', return_value=False) as mock_exists:
            with patch('os.listdir') as mock_listdir:  
                mock_listdir.return_value = []
                result = get_station_logo()
                self.assertEqual(result, None)
                mock_exists.assert_called_once_with(DDA_LOGO_FOLDER)