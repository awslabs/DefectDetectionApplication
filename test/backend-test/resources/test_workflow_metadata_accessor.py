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
import logging
from unittest.mock import patch



from fastapi import HTTPException
from marshmallow import ValidationError
from sqlalchemy.orm import Session
import pytest

from local_server_base_test_case import LocalServerBaseTestCase


class TestWorkflowMetadataAccessor(LocalServerBaseTestCase):

    def setUp(self):
        super().setUp()

        from resources.accessors.workflow_metadata_accessor import WorkflowMetadataAccessor
        from dao.sqlite_db.models import WorkflowMetadata
        self.session = Session(self.metadata_engine)
        self.test_workflow_metadata_data = {
            "workflowId": "fake-wf-id", 
            "summaryStartTime": 123,
        }
        self.session.add(WorkflowMetadata(**self.test_workflow_metadata_data))
        self.session.commit()
        self.accessor = WorkflowMetadataAccessor()

    def tearDown(self):
        super().tearDown()

    def test_create_workflow_metadata(self):
        from dao.sqlite_db.models import WorkflowMetadata
        test_workflow_metadata_data = {
            "workflowId": "fake-wf-id-2", 
            "summaryStartTime": 123,
        }
        p_key = self.accessor.create_workflow_metadata(self.session, test_workflow_metadata_data)
        result_data = self.session.get(WorkflowMetadata, p_key)
        self.assertEqual(result_data.workflowId, "fake-wf-id-2")
        self.assertEqual(result_data.summaryStartTime, 123)

    def test_create_workflow_metadata_missing_attr(self):
        test_workflow_metadata_data = {
            "workflowId": "fake-wf-id-2", 
        }
        with pytest.raises(HTTPException) as err:
            response = self.accessor.create_workflow_metadata(self.session, test_workflow_metadata_data)
            self.assertIn("Invalid data provided: ", response.description)

    def test_create_workflow_metadata_invalid_param(self):
        test_workflow_metadata_data = {
            "workflowId": "fake-wf-id-3", 
            "summaryStartTime": "WRONG",
        }
        with pytest.raises(HTTPException) as err:
            response = self.accessor.create_workflow_metadata(self.session, test_workflow_metadata_data)
            self.assertIn("Invalid data provided: ", response.description)

    @patch("dao.sqlite_db.workflow_metadata_dao.get_workflow_metadata")
    def test_update_workflow_metadata(self, mock_get_workflow_metadata):
        from dao.sqlite_db.models import WorkflowMetadata
        test_workflow_metadata_data = {
            "workflowId": "fake-wf-id-3", 
            "summaryStartTime": 123,
        }
        p_key = self.accessor.create_workflow_metadata(self.session, test_workflow_metadata_data)
        result_data = self.session.get(WorkflowMetadata, p_key)
        mock_get_workflow_metadata.return_value = result_data

        test_workflow_metadata_data["summaryStartTime"] = 1234
        p_key2 = self.accessor.update_workflow_metadata(self.session, test_workflow_metadata_data)
        result_data = self.session.get(WorkflowMetadata, p_key2)
        self.assertEqual(result_data.workflowId, "fake-wf-id-3")
        self.assertEqual(result_data.summaryStartTime, 1234)

    def test_list_workflow_metadatas(self):
        NUM_WORKFLOWS = 3

        for i in range(NUM_WORKFLOWS):
            test_workflow = {
                "workflowId": str(i),
                "summaryStartTime": 0
            }
            self.accessor.create_workflow_metadata(self.session, test_workflow)

        result_data = self.accessor.list_workflow_metadatas(self.session)
        self.assertEqual(len(result_data), NUM_WORKFLOWS + 1)

    def test_get_workflow_metadata(self):
        result_data = self.accessor.get_workflow_metadata(self.session, "fake-wf-id")
        self.assertEqual(result_data.get("workflowId"), "fake-wf-id")
        self.assertEqual(result_data.get("summaryStartTime"), 123)

