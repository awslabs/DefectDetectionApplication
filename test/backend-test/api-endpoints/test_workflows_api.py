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
from local_server_base_test_case import LocalServerBaseTestCase
from unittest.mock import patch, call, MagicMock
from html import unescape
from fastapi.testclient import TestClient
from typing import Union
from unittest.mock import ANY,mock_open
from starlette import status
from exceptions.api.captured_images_exception import CapturedImageException
from model.image_source import ImageSourceType, ImageSource
from dda_triton.triton_edge_client import TritonEdgeClient
class TestWorkflows(LocalServerBaseTestCase):
    def setUp(self):
        super().setUp()
        from app import app
        from endpoints.workflow import get_db
        # raise_server_exceptions=False uses our exception handlers rather than raising exception during testing
        self.client = TestClient(app, raise_server_exceptions = False)
        app.dependency_overrides[get_db] = self.override_dep

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

    @patch("utils.server_setup.workflow_accessor.list_workflows_with_image_sources", return_value=[])
    def test_list_workflows(self, list_workflows_mock):
        response = self.client.get("/workflows")
        list_workflows_mock.assert_called_once()
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("utils.server_setup.workflow_accessor.list_workflows_by_camera")
    def test_list_workflows_with_cameraId(self, list_workflows_mock):
        response = self.client.get("/workflows?cameraId=Fake_1")
        list_workflows_mock.assert_called_once()
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("utils.server_setup.image_source_accessor.update_all_image_sources_with_camera_status")
    @patch("utils.server_setup.workflow_accessor.get_workflow_with_default_config", return_value={"imageSources": []})
    def test_get_workflows(self, get_workflows_mock, mock_update_all_image_sources_with_camera_status):
        response = self.client.get("/workflows/fake-workflow-id")
        get_workflows_mock.assert_called_once()
        assert get_workflows_mock.mock_calls == [call('fake-workflow-id', 'fake-db-session')]
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("utils.server_setup.workflow_accessor.update_workflow", return_value="")
    def test_modify_workflows(self, modify_workflows_mock):
        response = self.client.patch("/workflows/fake-workflow-id", json={'imageSources': [{"imageSourceId":"testCamera"}]})
        modify_workflows_mock.assert_called_once()
        assert modify_workflows_mock.mock_calls == [call({'imageSources': [{"imageSourceId":"testCamera"}], 'workflowId': 'fake-workflow-id'}, 'fake-db-session')]
        assert response.status_code == 200, f"status_code: {response.status_code}"
        
    @patch("utils.server_setup.workflow_accessor.retry_dio_workflow")
    def retry_dio_workflow(self, retry_dio_workflow_mock):
        response = self.client.get("/workflows/fake-workflow-id/retry")
        retry_dio_workflow.assert_called_once()        
        assert retry_dio_workflow.mock_calls == [call('fake-workflow-id', 'fake-db-session'),call().keys(),call().keys().__iter__(),]
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("endpoints.workflow.get_camera_frame", return_value=None)
    @patch("utils.utils.convert_sqlalchemy_object_to_dict", return_value = {"type": ImageSourceType.CAMERA})
    @patch("resources.accessors.image_source_accessor.ImageSourceAccessor.get_image_source", return_value={"imageSourceConfiguration":{}})
    @patch("endpoints.workflow.read_inference_result", return_value={"captureId":"fake-id", "creationTime": "123", "imageDataFilePath": "fake-path", "inferenceFilePath": "fake-path", "inferenceResult": {"confidence": 0.123, "inference_result": ""}})
    @patch("utils.server_setup.gst_pipeline_executor.execute_workflow_pipeline", return_value=("fake-id", {}))
    @patch("utils.server_setup.workflow_accessor.get_workflow_by_id")
    @patch("metrics.latency_metrics.LatencyMetrics.commit_timestamps")
    @patch("metrics.latency_metrics.LatencyMetrics.get_timestamp")
    @patch("fastapi.BackgroundTasks.add_task")
    def test_run_inference_for_stream_happy_path(self, add_task_mock, get_timestamp_mock, commit_timestamps_mock, get_workflows_mock, exec_workflow_pipeline_mock, read_inference_result_mock, get_image_source_mock, convert_sqlalchemy_object_to_dict_mock, get_camera_frame_mock):
        test_workflow = {
            "workflowId": "12345", 
            "name": "workflow_12345", 
            "imageSources": [{"type": ImageSourceType.CAMERA}],
            "featureConfigurations": [{
                "type": "LFVModel", 
                "modelName": "LFVModelName"}]
            }
        get_workflows_mock.return_value = test_workflow
        response = self.client.post("/workflows/fake-workflow-id/run")
        get_workflows_mock.assert_called_once()
        add_task_mock.assert_called_once()
        assert get_workflows_mock.mock_calls == [call('fake-workflow-id', 'fake-db-session')]
        exec_workflow_pipeline_mock.assert_called_once()
        read_inference_result_mock.assert_called_once()
        assert exec_workflow_pipeline_mock.mock_calls[0].args == call(test_workflow, 'fake-db-session', ANY).args
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("endpoints.workflow.get_camera_frame", return_value=None)
    @patch("utils.utils.convert_sqlalchemy_object_to_dict", return_value = {"type": ImageSourceType.CAMERA})
    @patch("resources.accessors.image_source_accessor.ImageSourceAccessor.get_image_source", return_value={"imageSourceConfiguration":{}})
    @patch("endpoints.workflow.read_inference_result", return_value={"captureId":"fake-id", "creationTime": "123", "image":"fake-image-base64-string", "imageDataFilePath": "fake-path", "inferenceFilePath": "fake-path", "inferenceResult": {"confidence": 0.123, "inference_result": ""}})
    @patch("utils.server_setup.gst_pipeline_executor.execute_workflow_pipeline", return_value=("fake-id", {}))
    @patch("utils.server_setup.workflow_accessor.get_workflow_by_id")
    @patch("metrics.latency_metrics.LatencyMetrics.commit_timestamps")
    @patch("metrics.latency_metrics.LatencyMetrics.get_timestamp")
    @patch("fastapi.BackgroundTasks.add_task")
    def test_run_inference_for_stream_without_image_bytes(self, add_task_mock, get_timestamp_mock, commit_timestamps_mock, get_workflows_mock, exec_workflow_pipeline_mock, read_inference_result_mock, get_image_source_mock, convert_sqlalchemy_object_to_dict_mock, get_camera_frame_mock):
        test_workflow = {
            "workflowId": "12345",
            "name": "workflow_12345",
            "imageSources": [{"type": ImageSourceType.CAMERA}],
            "featureConfigurations": [{
                "type": "LFVModel",
                "modelName": "LFVModelName"}]
            }
        get_workflows_mock.return_value = test_workflow
        response = self.client.post("/workflows/fake-workflow-id/run", json={"returnImageString": False})
        get_workflows_mock.assert_called_once()
        add_task_mock.assert_called_once()
        assert get_workflows_mock.mock_calls == [call('fake-workflow-id', 'fake-db-session')]
        exec_workflow_pipeline_mock.assert_called_once()
        read_inference_result_mock.assert_called_once()
        assert exec_workflow_pipeline_mock.mock_calls[0].args == call(test_workflow, 'fake-db-session', ANY).args
        assert "image" not in response.json()
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("utils.utils.convert_sqlalchemy_object_to_dict", return_value = {"type": ImageSourceType.FOLDER})
    @patch("resources.accessors.image_source_accessor.ImageSourceAccessor.get_image_source", return_value={"imageSourceConfiguration":{}})
    @patch("utils.server_setup.gst_pipeline_executor.execute_workflow_pipeline", side_effect = CapturedImageException("placeholder message", status_code=status.HTTP_422_UNPROCESSABLE_ENTITY))
    @patch("utils.server_setup.workflow_accessor.get_workflow_by_id")
    def test_run_inference_folder_bad_file(self, get_workflows_mock, exec_workflow_pipeline_mock, get_image_source_mock, convert_sqlalchemy_object_to_dict_mock):
        test_workflow = {
            "workflowId": "12345", 
            "name": "workflow_12345", 
            "imageSources": [{"type": ImageSourceType.FOLDER}],
            "featureConfigurations": [{
                "type": "LFVModel", 
                "modelName": "LFVModelName"}]
            }
        get_workflows_mock.return_value = test_workflow
        response = self.client.post("/workflows/fake-workflow-id/run")
        get_workflows_mock.assert_called_once()
        assert get_workflows_mock.mock_calls == [call('fake-workflow-id', 'fake-db-session')]
        exec_workflow_pipeline_mock.assert_called_once()
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, f"status_code: {response.status_code}"

    @patch("utils.server_setup.gst_pipeline_executor.execute_workflow_pipeline")
    @patch("utils.server_setup.workflow_accessor.get_workflow_by_id")
    def test_run_inference_for_stream_without_img_src(self, get_workflows_mock, exec_workflow_pipeline_mock):
        test_workflow = {
            "workflowId": "12345", 
            "name": "workflow_12345", 
            "featureConfigurations": [{
                "type": "LFVModel", 
               "modelName": "LFVModelName"}]
            }
        get_workflows_mock.return_value = test_workflow

        response = self.client.post("/workflows/fake-workflow-id/run")
        get_workflows_mock.assert_called_once()
        assert get_workflows_mock.mock_calls == [call('fake-workflow-id', 'fake-db-session')]
        exec_workflow_pipeline_mock.assert_not_called()
        assert response.status_code == 503, f"status_code: {response.status_code}"  
        assert "Server cannot run workflow. Error: 'Workflow doesnt have imageSources configured'. Configure image sources for the workflow" in response.json()['message']

    @patch("utils.server_setup.capture_task_manager.add_task")
    @patch("utils.server_setup.workflow_accessor.get_workflow_by_id")
    @patch("utils.utils.convert_sqlalchemy_object_to_dict")
    @patch("utils.server_setup.image_source_accessor.get_image_source", return_value=[])
    def test_run_data_capture_for_stream_without_feature_configs(self, get_imgsrc_mock, convert_mock, get_workflows_mock, add_capture_task_mock):
        test_workflow = {
            "workflowId": "12345", 
            "name": "workflow_12345", 
            "imageSources": [ImageSource(
                name = "testImageSource",
                type = ImageSourceType.CAMERA,
                imageSourceId = "testCamera",
                imageCapturePath = "testPath",
                lastUpdateTime = "123",
                creationTime = "123"
                )],
            }
        get_workflows_mock.return_value = test_workflow
        get_imgsrc_mock.return_value = []
        convert_mock.return_value = {
            "name": "testImageSource",
            "type": ImageSourceType.CAMERA,
            "imageSourceId": "testCamera",
            "imageCapturePath": "testPath",
            "lastUpdateTime": "123",
            "creationTime": "123"
        }

        response = self.client.post("/workflows/fake-workflow-id/run", json={"capturePrefix":"test-prefix", "captureTimeInterval":2, "captureImageCount":2})
        get_workflows_mock.assert_called_once()
        assert get_workflows_mock.mock_calls == [call('fake-workflow-id', 'fake-db-session')]
        add_capture_task_mock.assert_called_once()
        assert response.status_code == 200, f"status_code: {response.status_code}"
    
    @patch("utils.server_setup.capture_task_manager.add_task")
    @patch("utils.server_setup.workflow_accessor.get_workflow_by_id")
    @patch("utils.utils.convert_sqlalchemy_object_to_dict")
    @patch("utils.server_setup.image_source_accessor.get_image_source", return_value=[])
    def test_run_data_capture_for_workflow_missing_capture_task_param(self, get_imgsrc_mock, convert_mock, get_workflows_mock, add_capture_task_mock):
        test_workflow = {
            "workflowId": "12345", 
            "name": "workflow_12345", 
            "imageSources": [ImageSource(
                name = "testImageSource",
                type = ImageSourceType.CAMERA,
                imageSourceId = "testCamera",
                imageCapturePath = "testPath",
                lastUpdateTime = "123",
                creationTime = "123"
                )],
            }
        get_workflows_mock.return_value = test_workflow
        get_imgsrc_mock.return_value = []
        convert_mock.return_value = {
            "name": "testImageSource",
            "type": ImageSourceType.CAMERA,
            "imageSourceId": "testCamera",
            "imageCapturePath": "testPath",
            "lastUpdateTime": "123",
            "creationTime": "123"
        }

        response = self.client.post("/workflows/fake-workflow-id/run", json={"capturePrefix":"test-prefix"})
        assert response.status_code == 400, f"status_code: {response.status_code}"

    @patch("utils.server_setup.workflow_accessor.get_workflow_by_id")
    @patch("utils.utils.convert_sqlalchemy_object_to_dict")
    @patch("utils.server_setup.image_source_accessor.get_image_source", return_value=[])
    def test_run_data_capture_for_stream_for_folder_image_source(self, get_imgsrc_mock, convert_mock, get_workflows_mock):
        test_workflow = {
            "workflowId": "fake-workflow-id", 
            "name": "workflow_12345", 
            "imageSources": [ImageSource(
                name = "testImageSource",
                type = ImageSourceType.FOLDER,
                imageSourceId = "testFolder",
                imageCapturePath = "testPath",
                lastUpdateTime = "123",
                creationTime = "123"
                )]
            }
        get_workflows_mock.return_value = test_workflow
        get_imgsrc_mock.return_value = []
        convert_mock.return_value = {
            "name": "testImageSource",
            "type": ImageSourceType.FOLDER,
            "location": "testPath",
            "lastUpdateTime": "123",
            "creationTime": "123"
        }
        response = self.client.post("/workflows/fake-workflow-id/run", json={"capturePrefix":"test-prefix", "captureTimeInterval":2, "captureImageCount":2})
        get_workflows_mock.assert_called_once()
        assert get_workflows_mock.mock_calls == [call('fake-workflow-id', 'fake-db-session')]
        assert response.status_code == 400

    @patch("endpoints.workflow.get_camera_frame", return_value=None)
    @patch("utils.utils.convert_sqlalchemy_object_to_dict", return_value = {"type": ImageSourceType.CAMERA})
    @patch("resources.accessors.image_source_accessor.ImageSourceAccessor.get_image_source", return_value={"imageSourceConfiguration":{}})
    @patch("utils.server_setup.gst_pipeline_executor.execute_workflow_pipeline", return_value=("fake-id", {"is_anomalous": True, "confidence": 0.123}))
    @patch("utils.server_setup.workflow_accessor.get_workflow_by_id")
    @patch("fastapi.BackgroundTasks.add_task")
    def test_run_inference_return_early_anomaly(self, add_task_mock, get_workflows_mock, exec_workflow_pipeline_mock, get_image_source_mock, convert_sqlalchemy_object_to_dict_mock, get_camera_frame_mock):
        test_workflow = {
            "workflowId": "12345",
            "name": "workflow_12345",
            "imageSources": [{"type": ImageSourceType.CAMERA}],
            "featureConfigurations": [{
                "type": "LFVModel",
                "modelName": "LFVModelName"}]
            }
        get_workflows_mock.return_value = test_workflow
        response = self.client.post("/workflows/fake-workflow-id/run", json={"returnPartialResultsEarly": True})
        get_workflows_mock.assert_called_once()
        add_task_mock.assert_called_once()
        assert get_workflows_mock.mock_calls == [call('fake-workflow-id', 'fake-db-session')]
        exec_workflow_pipeline_mock.assert_called_once()
        assert exec_workflow_pipeline_mock.mock_calls[0].args == call(test_workflow, 'fake-db-session', ANY).args
        assert response.json() == {
            "captureId": "fake-id",
            "inferenceResult": {
                "confidence": 0.123,
                "inference_result": "Anomaly",
            },
        }
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("endpoints.workflow.get_camera_frame", return_value=None)
    @patch("utils.utils.convert_sqlalchemy_object_to_dict", return_value = {"type": ImageSourceType.CAMERA})
    @patch("resources.accessors.image_source_accessor.ImageSourceAccessor.get_image_source", return_value={"imageSourceConfiguration":{}})
    @patch("utils.server_setup.gst_pipeline_executor.execute_workflow_pipeline", return_value=("fake-id", {"is_anomalous": False, "confidence": 0.123}))
    @patch("utils.server_setup.workflow_accessor.get_workflow_by_id")
    @patch("fastapi.BackgroundTasks.add_task")
    def test_run_inference_return_early_normal(self, add_task_mock, get_workflows_mock, exec_workflow_pipeline_mock, get_image_source_mock, convert_sqlalchemy_object_to_dict_mock, get_camera_frame_mock):
        test_workflow = {
            "workflowId": "12345",
            "name": "workflow_12345",
            "imageSources": [{"type": ImageSourceType.CAMERA}],
            "featureConfigurations": [{
                "type": "LFVModel",
                "modelName": "LFVModelName"}]
            }
        get_workflows_mock.return_value = test_workflow
        response = self.client.post("/workflows/fake-workflow-id/run", json={"returnPartialResultsEarly": True})
        get_workflows_mock.assert_called_once()
        add_task_mock.assert_called_once()
        assert get_workflows_mock.mock_calls == [call('fake-workflow-id', 'fake-db-session')]
        exec_workflow_pipeline_mock.assert_called_once()
        assert exec_workflow_pipeline_mock.mock_calls[0].args == call(test_workflow, 'fake-db-session', ANY).args
        assert response.json() == {
            "captureId": "fake-id",
            "inferenceResult": {
                "confidence": 0.123,
                "inference_result": "Normal",
            },
        }
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("os.path.exists", return_value=True)
    @patch("utils.server_setup.inference_result_accessor.get_inference_result")
    def test_load_input_image_from_worflow_by_capture_id_exist_path(self, get_inference_result_mock, os_path_exist_mock):
        get_inference_result_mock.return_value.inputImageFilePath = "/tmp"
        response = self.client.get("/workflows/fake-workflow-id/capture-details/fake-capture-id/input-image")
        os_path_exist_mock.assert_called_once()

    @patch.dict(os.environ, {'is_triton':'True'})
    @patch("utils.get_is_triton.get_is_triton")
    @patch("builtins.open",new_callable=mock_open,read_data="{\"deviceGroundTruthData\": [{\"source-ref\": \"/some/path/file.jpg\"}]}")
    @patch.object(TritonEdgeClient, 'get_instance')
    @patch("os.path.exists", return_value=True)
    def test_load_input_image_for_triton(self, mock_exist, mock_get_instance, mock_open, mock_get_is_triton):
        mock_get_instance = MagicMock()
        mock_get_is_triton.return_value = mock_get_instance
        response = self.client.get("/workflows/fake-workflow-id/capture-details/fake-capture-id/input-image")
        mock_open.assert_called_once()
        assert mock_exist.call_count == 1

    @patch("os.path.exists", return_value=False)
    @patch("utils.server_setup.inference_result_accessor.get_inference_result")
    def test_load_input_image_from_worflow_by_capture_id_nonexist_path(self, get_inference_result_mock, os_path_exist_mock):
        get_inference_result_mock.return_value.inputImageFilePath = "/tmp"
        response = self.client.get("/workflows/fake-workflow-id/capture-details/fake-capture-id/input-image")        
        os_path_exist_mock.assert_called_once()
        assert response.status_code == 404, f"status_code: {response.status_code}"

    @patch("utils.server_setup.workflow_accessor.get_workflow_by_id")
    def test_load_output_image_from_worflow_by_capture_id(self, get_workflows_mock):
        test_workflow = {
            "workflowId": "12345",
            "name": "workflow_12345",
            "workflowOutputPath": "/tmp",
        }
        get_workflows_mock.return_value = test_workflow
        response = self.client.get("/workflows/fake-workflow-id/capture-details/fake-capture-id/output-image")
        self.mock_get_inference_results_object.assert_called_once()
        object_mock_calls = [
            call('/tmp', None, 0, 1),
            call().get_infer_res_with_capture_id(capture_id='fake-capture-id', workflow_output_path='/tmp')
        ]
        assert self.mock_get_inference_results_object.mock_calls[0] == object_mock_calls[0]
        assert self.mock_get_inference_results_object.mock_calls[1] == object_mock_calls[1]

    @patch("utils.server_setup.inference_result_accessor.list_inference_result_data_with_capture_task_id")
    @patch("utils.server_setup.capture_task_manager.get_tasks", return_value=[{"captureTaskId": "fake-2d8b47fdadcb47859578be3e2c76e072","workflowId": "fake","interval": 5,"count": 5,"status": "Running"}])
    def test_get_capture_task_status(self, mock_get_tasks, mock_list_capture):
        mock_list_capture.return_value = ["fake-path"]
        response = self.client.get("/workflows/fake/capture-task")
        expected = {
            "captureTaskId": "fake-2d8b47fdadcb47859578be3e2c76e072",
            "workflowId": "fake",
            "interval": 5,
            "count": 5,
            "status": "Running",
            "capturedCount": 1
            }
        mock_list_capture.assert_called_once()
        assert response.json() == expected
        assert response.status_code == 200

    @patch("utils.server_setup.workflow_accessor.create_workflow")
    def test_create_workflow(self, create_workflow_test):
        from utils import utils
        from utils.server_setup import workflow_metadata_accessor
        with patch.object(workflow_metadata_accessor, 'create_workflow_metadata', return_value=None) as mock_create_metadata:
            mock_workflow_id = str(utils.gen_uuid())
            create_workflow_test.return_value = mock_workflow_id
            response = self.client.post("/workflows")
            mock_create_metadata.assert_called_once()
            create_workflow_test.assert_called_once()
            assert response.status_code == 200

    @patch("utils.server_setup.workflow_accessor.delete_workflow")
    def test_delete_workflow(self, mock_delete_workflow):
        mock_delete_workflow.return_value = True
        
        response = self.client.delete("/workflows/existing-workflow-id")
        
        mock_delete_workflow.assert_called_once_with("existing-workflow-id", "fake-db-session")
        assert response.status_code == 200