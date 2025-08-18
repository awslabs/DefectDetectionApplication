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

from local_server_base_test_case import LocalServerBaseTestCase
from unittest.mock import patch, call
from fastapi.testclient import TestClient
from typing import Union
from unittest.mock import ANY
from unittest.mock import Mock

class TestInferenceResult(LocalServerBaseTestCase):
    def setUp(self):
        super().setUp()
        from app import app
        from endpoints.inference_result import get_db
        self.client = TestClient(app, raise_server_exceptions = False)
        self.mock_session = Mock()
        app.dependency_overrides[get_db] = lambda: self.mock_session

        self.get_inference_results_object_patcher = patch("utils.inference_results_utils.GetInferenceResults")
        self.mock_get_inference_results_object = self.get_inference_results_object_patcher.start()
        self.mock_get_inference_results_object.return_value.get_infer_res_with_capture_id.return_value = {
            "inputImageFilePath": "test/backend-test/captured_images_for_test/test-1.jpg"
        }

    def tearDown(self):
        super().tearDown()
        self.get_inference_results_object_patcher.stop()

        from app import app
        app.dependency_overrides = {}

    def override_dep(q: Union[str, None] = None):
        return "fake-db-session"
    
    @patch("utils.server_setup.inference_result_accessor.list_inference_result_data_for_retraining")
    def test_list_inference_input_images_for_retrain_happy_path(self, list_input_images_mock):
        list_input_images_mock.return_value = [{
            "inputImageFilePath": "/tmp",
            "prediction": "Anomaly",
            "confidence": 0.9,
            "inferenceCreationTime": 1234567890,
            "humanClassification": None,
            "textNote": None
        }]
        with patch('endpoints.download_file.DDA_SYSTEM_FOLDER', "/tmp"):
            response = self.client.get("/workflows/fake-workflow-id/results/export")
            assert response.status_code == 200

    @patch("utils.server_setup.inference_result_accessor.list_inference_result_data_for_retraining")
    def test_list_inference_input_images_for_retrain_no_file(self, list_input_images_mock):
        list_input_images_mock.return_value = []
        response = self.client.get("/workflows/fake-workflow-id/results/export")
        assert response.status_code == 442

    # @patch("utils.server_setup.inference_result_accessor.list_inference_result_data_for_retraining")
    def test_list_inference_input_images_for_retrain_extends_max_download_limit(self):
        capture_id_list = []
        for i in range(1005):
            capture_id_list.append(str(i))
        response = self.client.post("/workflows/fake-workflow-id/results/export", json={"captureIds":capture_id_list})
        assert response.status_code == 413

    @patch("utils.server_setup.inference_result_accessor.get_inference_result_summary")
    @patch("utils.server_setup.workflow_metadata_accessor.get_workflow_metadata")
    def test_get_inference_result_summary(self, get_inference_result_summary_mock, get_workflow_metadata_mock):
        response = self.client.get("/workflows/fake-workflow-id/results/summary")
        get_inference_result_summary_mock.assert_called_once()
        assert get_inference_result_summary_mock.mock_calls[0] == call(self.mock_session, 'fake-workflow-id')
        assert response.status_code == 200, f"status_code: {response.status_code}"
    
    @patch("utils.server_setup.workflow_metadata_accessor.update_workflow_metadata")
    def test_reset_inference_result_summary_start_time(self, update_workflow_metadata_mock):
        response = self.client.post("/workflows/fake-workflow-id/results/reset")
        update_workflow_metadata_mock.assert_called_once()
        assert update_workflow_metadata_mock.mock_calls[0] == call(self.mock_session, {'workflowId': 'fake-workflow-id', 'summaryStartTime': None})
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("dao.sqlite_db.workflow_dao.get_workflow", return_value=None)
    def test_get_inference_result_by_capture_id_workflow_not_found(self, get_workflow_mock):
        response = self.client.get("/workflows/fake-workflow-id/results/fake-capture-id")
        get_workflow_mock.assert_called_once()
        assert response.status_code == 404, f"status_code: {response.status_code}"

    @patch("utils.server_setup.inference_result_accessor.get_inference_result", return_value=None)
    @patch("utils.server_setup.workflow_accessor.get_workflow_by_id")
    def test_get_inference_result_by_capture_id_capture_id_not_found(self, get_workflow_mock, get_inference_result_mock):
        response = self.client.get("/workflows/fake-workflow-id/results/fake-capture-id")
        get_workflow_mock.assert_called_once()
        get_inference_result_mock.assert_called_once()
        assert get_inference_result_mock.mock_calls[0] == call(self.mock_session, 'fake-capture-id')
        assert response.status_code == 404, f"status_code: {response.status_code}"

    @patch("utils.server_setup.inference_result_accessor.get_inference_result")
    @patch("utils.server_setup.workflow_accessor.get_workflow_by_id")
    def test_get_inference_result_by_capture_id(self, get_workflow_mock, get_inference_result_mock):
        response = self.client.get("/workflows/fake-workflow-id/results/fake-capture-id")
        get_workflow_mock.assert_called_once()
        get_inference_result_mock.assert_called_once()
        assert get_inference_result_mock.mock_calls[0] == call(self.mock_session, 'fake-capture-id')
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("utils.server_setup.inference_result_accessor.delete_inference_result_by_capture_id", return_value=[])
    def test_delete_inference_result(self, delete_inference_result_by_capture_id_mock):
        response = self.client.delete("/workflows/fake-wf-id/results/fake-capture-id")
        delete_inference_result_by_capture_id_mock.assert_called_once()
        assert delete_inference_result_by_capture_id_mock.mock_calls == [call(self.mock_session, 'fake-capture-id')]
        assert response.status_code == 200, f"status_code: {response.status_code}" 

    @patch("utils.server_setup.inference_result_accessor.update_inference_results", return_value=[])
    def test_bulk_update_inference_results(self, update_inference_results_mock):
        response = self.client.patch("/workflows/fake-wf-id/results", json={"inferenceResults":[{"captureId":"fake-capture-id", "flagForReview": True, "downloaded": False}]})
        update_inference_results_mock.assert_called_once()
        assert update_inference_results_mock.mock_calls == [call(self.mock_session, [{"captureId":"fake-capture-id", "flagForReview": True, "downloaded": False}])]
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("utils.server_setup.inference_result_accessor.update_inference_results", return_value=[])
    def test_bulk_update_inference_results_with_human_classification_and_text_note(self, update_inference_results_mock):
        response = self.client.patch("/workflows/fake-wf-id/results", json={"inferenceResults":[{"captureId":"fake-capture-id", "flagForReview": True, "downloaded": False, "humanClassification": "Anomaly", "textNote": "hello there"}]})
        update_inference_results_mock.assert_called_once()
        assert update_inference_results_mock.mock_calls == [call(self.mock_session, [{"captureId":"fake-capture-id", "flagForReview": True, "downloaded": False, "humanClassification": "Anomaly", "textNote": "hello there"}])]
        assert response.status_code == 200, f"status_code: {response.status_code}"

    def test_bulk_update_inference_results_no_capture_id(self):
        response = self.client.patch("/workflows/fake-wf-id/results", json={"inferenceResults":[{"flagForReview": True, "downloaded": False}]})
        assert response.status_code == 400, f"status_code: {response.status_code}"

    def test_bulk_update_inference_results_invalid_human_classification(self):
        response = self.client.patch("/workflows/fake-wf-id/results", json={"inferenceResults":[{"captureId":"fake-capture-id", "flagForReview": True, "downloaded": False, "humanClassification": "whatisthisgarbage", "textNote": "hello there"}]})
        assert response.status_code == 400, f"status_code: {response.status_code}"

    def test_bulk_update_inference_results_invalid_text_note(self):
        response = self.client.patch("/workflows/fake-wf-id/results", json={"inferenceResults":[{"captureId":"fake-capture-id", "flagForReview": True, "downloaded": False, "humanClassification": "Normal", "textNote": "this_text_note_is_more_than_50_characters_which_is_maxlen"}]})
        assert response.status_code == 400, f"status_code: {response.status_code}"

    @patch("endpoints.inference_result.paginate", return_value={})
    @patch("utils.server_setup.workflow_accessor.get_workflow_by_id", return_value={})
    def test_list_inference_results(self, get_workflow_mock, paginate_mock):
        from resources.pagination.base_paginator import PageParams
        from data_models.common import InferenceResultHistoryModel
        
        mock_query = self.mock_session.query().filter().order_by
        response = self.client.get("/workflows/fake-wf-id/results")
        paginate_mock.assert_called_once()
        assert paginate_mock.mock_calls == [call(PageParams(page=1, size=12), mock_query.return_value, InferenceResultHistoryModel)]
        assert response.status_code == 200, f"status_code: {response.status_code}"

    def test_list_inference_results_invalid_input(self):
        response = self.client.get("/workflows/fake-wf-id/results?page=10&size=12&humanReviewRequired=invalid")
        assert response.status_code == 400, f"status_code: {response.status_code}"