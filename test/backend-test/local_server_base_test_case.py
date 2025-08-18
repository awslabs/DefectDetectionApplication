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
import pytest
import logging
import json
import sys
import os
from unittest import TestCase
from unittest.mock import patch
from mock_gi import bogus_gi_module
from fastapi.testclient import TestClient
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


class LocalServerBaseTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        from mock_gi import bogus_gi_module
        from mock_edge_agent_pb2 import edge_agent_pb2_module
        from mock_edge_agent_pb2_grpc import edge_agent_pb2_grpc_module
        cls.EdgeAgentStub_patcher = patch('edge_agent_pb2_grpc.EdgeAgentStub', return_value=None, create=True)
        cls.patcher = patch('awsiot.greengrasscoreipc.connect')
        cls.component_work_path_patcher = patch.dict(os.environ,
                                                     {"COMPONENT_WORK_PATH": "/tmp",
                                                      "AWS_IOT_THING_NAME": "iot_thing_test",
                                                      "LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": "comp_decomp_path_test",
                                                      "KERNEL_ROOT_PATH": "."})
        cls.default_camera_config_patcher = patch('utils.constants.DEFAULT_CAMERA_CONFIG_FILE_PATH',"./src/backend/utils/config/default_camera_configurations.json")
        cls.EdgeAgentStub_patcher.start()
        cls.patcher.start()
        cls.component_work_path_patcher.start()
        cls.default_camera_config_patcher.start()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()

        cls.EdgeAgentStub_patcher.stop()
        cls.patcher.stop()
        cls.component_work_path_patcher.stop()
        cls.default_camera_config_patcher.stop()

    def setUp(self):
        super().setUp()

        self.test_dir = "test/backend-test/utils"
        infer_out_dir = "tmp/"
        self.em_agent_config = "em-agent-id-testListImages.json"
        pytest.infer_out_path = os.path.join(os.getcwd(), self.test_dir, infer_out_dir)

        filepath = os.path.join(self.test_dir, self.em_agent_config)
        with open(filepath, "r", encoding='utf-8') as jsonFile:
            stream_config = json.load(jsonFile)
        stream_config["sagemaker_edge_core_capture_data_disk_path"] = os.path.join(os.getcwd(), self.test_dir)
        stream_config["sagemaker_edge_core_folder_prefix"] = infer_out_dir
        with open(filepath, "w") as jsonFile:
            json.dump(stream_config, jsonFile)

        from app import app
        from dao.sqlite_db.sqlite_db_operations import engine, Base, metadata_engine, BaseMetadata

        self.client = TestClient(app)
        self.engine = engine
        self.metadata_engine = metadata_engine
        Base.metadata.create_all(self.engine)
        BaseMetadata.metadata.create_all(self.metadata_engine)

    def tearDown(self):
        super().tearDown()

        filepath = os.path.join(self.test_dir, self.em_agent_config)
        with open(filepath, "r", encoding='utf-8') as jsonFile:
            stream_config = json.load(jsonFile)
        stream_config["sagemaker_edge_core_capture_data_disk_path"] = \
            stream_config["sagemaker_edge_core_capture_data_disk_path"].replace(os.getcwd(), "<ROOT_PATH>")
        with open(filepath, "w") as jsonFile:
            json.dump(stream_config, jsonFile)

        from dao.sqlite_db.sqlite_db_operations import Base, BaseMetadata
        Base.metadata.drop_all(self.engine)
        BaseMetadata.metadata.drop_all(self.metadata_engine)
