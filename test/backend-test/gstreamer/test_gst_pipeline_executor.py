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
import os
import time
import unittest
from unittest.mock import patch, mock_open
from sqlalchemy.orm import Session
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR, HTTP_422_UNPROCESSABLE_ENTITY

from exceptions.api.captured_images_exception import CapturedImageException
from exceptions.api.unexpected_type_exception import UnexpectedTypeException
from local_server_base_test_case import LocalServerBaseTestCase
from model.image_source import ImageSource, ImageSourceType, ImageSourceSchema
from model.image_source_configuration import ImageSourceConfiguration, ImageSourceConfigurationSchema
from model.workflow import Workflow
from model.feature_configuration import FeatureConfiguration


class TestPipelineExecutor(LocalServerBaseTestCase):

    def setUp(self):
        super().setUp()
        self.session = Session(self.engine)
        # set to false by default, specific test will set it to True.
        os.environ["is_triton"] = "False"

    def tearDown(self):
        super().tearDown()

    @patch('utils.utils.convert_sqlalchemy_object_to_dict')
    @patch('resources.accessors.image_source_accessor.ImageSourceAccessor.get_image_source')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._run_pipeline')
    def test_execute_workflow_pipeline_camera(self, run_pipeline_mock, get_image_source_mock, image_source_dict_mock):
        image_source_dict_mock.return_value = {
            "type": ImageSourceType.CAMERA,
            "imageSourceConfiguration": {
                "processingPipeline": "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
            }
        }

        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()

        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]
        workflow_config = Workflow(
            workflowId = "fakeWorkflowId",
            name = "fakeWorkflowName",
            workflowOutputPath = "/tmp/workflow/output/path",
            creationTime = int(time.time()),
            lastUpdatedTime = int(time.time()),
            featureConfigurations = feature_configs,
            description="",
            imageSourceId="fakeImageSourceId",
            imageSources = [],
            inputConfigurations=[],
            outputConfigurations=[]
        )

        self.pipeline_executor.execute_workflow_pipeline(workflow_config, self.session)
        get_image_source_mock.assert_called_once()
        image_source_dict_mock.assert_called_once()
        run_pipeline_mock.assert_called_once()

    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._move_bad_folder_image_source')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._cleanup_file_after_processing')
    @patch('utils.utils.convert_sqlalchemy_object_to_dict')
    @patch('resources.accessors.image_source_accessor.ImageSourceAccessor.get_image_source')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._run_pipeline', side_effect = Exception("placeholder exception"))
    def test_execute_workflow_pipeline_camera_runtime_failure(self, run_pipeline_mock, get_image_source_mock, image_source_dict_mock, cleanup_file_mock, move_mock):
        image_source_dict_mock.return_value = {
            "type": ImageSourceType.CAMERA,
            "imageSourceConfiguration": {
                "processingPipeline": "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
            }
        }

        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()

        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]
        workflow_config = Workflow(
            workflowId = "fakeWorkflowId",
            name = "fakeWorkflowName",
            workflowOutputPath = "/tmp/workflow/output/path",
            creationTime = int(time.time()),
            lastUpdatedTime = int(time.time()),
            featureConfigurations = feature_configs,
            description="",
            imageSourceId="fakeImageSourceId",
            imageSources = [],
            inputConfigurations=[],
            outputConfigurations=[]
        )

        try:
            self.pipeline_executor.execute_workflow_pipeline(workflow_config, self.session)
        except Exception:
            get_image_source_mock.assert_called_once()
            image_source_dict_mock.assert_called_once()
            run_pipeline_mock.assert_called_once()
            cleanup_file_mock.assert_not_called()
            move_mock.assert_not_called()
        else:
            assert False

    @patch('utils.utils.convert_sqlalchemy_object_to_dict')
    @patch('resources.accessors.image_source_accessor.ImageSourceAccessor.get_image_source')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._run_pipeline')
    def test_execute_workflow_pipeline_icam(self, run_pipeline_mock, get_image_source_mock, image_source_dict_mock):
        image_source_dict_mock.return_value = {
            "type": ImageSourceType.ICAM,
            "imageSourceConfiguration": {
                "processingPipeline": "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
            }
        }

        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()

        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]
        workflow_config = Workflow(
            workflowId = "fakeWorkflowId",
            name = "fakeWorkflowName",
            workflowOutputPath = "/tmp/workflow/output/path",
            creationTime = int(time.time()),
            lastUpdatedTime = int(time.time()),
            featureConfigurations = feature_configs,
            description="",
            imageSourceId="fakeImageSourceId",
            imageSources = [],
            inputConfigurations=[],
            outputConfigurations=[]
        )

        self.pipeline_executor.execute_workflow_pipeline(workflow_config, self.session)
        get_image_source_mock.assert_called_once()
        image_source_dict_mock.assert_called_once()
        run_pipeline_mock.assert_called_once()

    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._move_bad_folder_image_source')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._cleanup_file_after_processing')
    @patch('utils.utils.convert_sqlalchemy_object_to_dict')
    @patch('resources.accessors.image_source_accessor.ImageSourceAccessor.get_image_source')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._run_pipeline', side_effect = Exception("placeholder exception"))
    def test_execute_workflow_pipeline_icam_runtime_failure(self, run_pipeline_mock, get_image_source_mock, image_source_dict_mock, cleanup_file_mock, move_mock):
        image_source_dict_mock.return_value = {
            "type": ImageSourceType.ICAM,
            "imageSourceConfiguration": {
                "processingPipeline": "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
            }
        }

        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()

        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]
        workflow_config = Workflow(
            workflowId = "fakeWorkflowId",
            name = "fakeWorkflowName",
            workflowOutputPath = "/tmp/workflow/output/path",
            creationTime = int(time.time()),
            lastUpdatedTime = int(time.time()),
            featureConfigurations = feature_configs,
            description="",
            imageSourceId="fakeImageSourceId",
            imageSources = [],
            inputConfigurations=[],
            outputConfigurations=[]
        )

        try:
            self.pipeline_executor.execute_workflow_pipeline(workflow_config, self.session)
        except Exception:
            get_image_source_mock.assert_called_once()
            image_source_dict_mock.assert_called_once()
            run_pipeline_mock.assert_called_once()
            cleanup_file_mock.assert_not_called()
            move_mock.assert_not_called()
        else:
            assert False

    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._update_file_permissions')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._cleanup_file_after_processing')
    @patch('os.path.getsize', return_value = 1)
    @patch('utils.captured_images_utils.get_oldest_image_file_path', return_value = "fake_filepath")
    @patch('utils.utils.convert_sqlalchemy_object_to_dict')
    @patch('resources.accessors.image_source_accessor.ImageSourceAccessor.get_image_source')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._run_pipeline')
    def test_execute_workflow_pipeline_folder(self, run_pipeline_mock, get_image_source_mock, image_source_dict_mock, oldest_image_fp_mock, getsize_mock, cleanup_mock, update_file_perm_mock):
        image_source_dict_mock.return_value = {
            "type": ImageSourceType.FOLDER,
            "location": "placeholder/folder"
        }

        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()

        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]
        workflow_config = Workflow(
            workflowId = "fakeWorkflowId",
            name = "fakeWorkflowName",
            workflowOutputPath = "/tmp/workflow/output/path",
            creationTime = int(time.time()),
            lastUpdatedTime = int(time.time()),
            featureConfigurations = feature_configs,
            description="",
            imageSourceId="fakeImageSourceId",
            imageSources = [],
            inputConfigurations=[],
            outputConfigurations=[]
        )

        self.pipeline_executor.execute_workflow_pipeline(workflow_config, self.session)
        get_image_source_mock.assert_called_once()
        image_source_dict_mock.assert_called_once()
        run_pipeline_mock.assert_called_once()
        oldest_image_fp_mock.assert_called_once()
        getsize_mock.assert_called_once()
        cleanup_mock.assert_called_once()
        update_file_perm_mock.assert_called_once()

    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._update_file_permissions')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._cleanup_file_after_processing')
    @patch('os.path.getsize', return_value = 1)
    @patch('utils.captured_images_utils.get_oldest_image_file_path', return_value = "fake_filepath")
    @patch('utils.utils.convert_sqlalchemy_object_to_dict')
    @patch('resources.accessors.image_source_accessor.ImageSourceAccessor.get_image_source')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._run_pipeline')
    @patch("builtins.open", new_callable=mock_open, read_data="{\"sagemaker_edge_core_capture_data_disk_path\": \"/tmp/workflow_id_test/\",\"sagemaker_edge_core_device_fleet_name\": \"dda_fleet\"}")
    def test_execute_workflow_pipeline_folder_triton(self,mock_file, run_pipeline_mock, get_image_source_mock, image_source_dict_mock, oldest_image_fp_mock, getsize_mock, cleanup_mock, update_file_perm_mock):
        os.environ["is_triton"] = "True"
        image_source_dict_mock.return_value = {
            "type": ImageSourceType.FOLDER,
            "location": "placeholder/folder"
        }

        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()

        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]
        workflow_config = Workflow(
            workflowId = "fakeWorkflowId",
            name = "fakeWorkflowName",
            workflowOutputPath = "/tmp/workflow/output/path",
            creationTime = int(time.time()),
            lastUpdatedTime = int(time.time()),
            featureConfigurations = feature_configs,
            description="",
            imageSourceId="fakeImageSourceId",
            imageSources = [],
            inputConfigurations=[],
            outputConfigurations=[]
        )

        self.pipeline_executor.execute_workflow_pipeline(workflow_config, self.session)
        get_image_source_mock.assert_called_once()
        image_source_dict_mock.assert_called_once()
        run_pipeline_mock.assert_called_once()
        oldest_image_fp_mock.assert_called_once()
        getsize_mock.assert_called_once()
        cleanup_mock.assert_called_once()
        update_file_perm_mock.assert_called_once()
        os.environ["is_triton"] = "False"

    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._move_bad_folder_image_source')
    @patch('os.path.getsize', return_value = 0)
    @patch('utils.captured_images_utils.get_oldest_image_file_path', return_value = "fake_filepath")
    @patch('utils.utils.convert_sqlalchemy_object_to_dict')
    @patch('resources.accessors.image_source_accessor.ImageSourceAccessor.get_image_source')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._run_pipeline')
    def test_execute_workflow_pipeline_folder_bad_file(self, run_pipeline_mock, get_image_source_mock, image_source_dict_mock, oldest_image_fp_mock, getsize_mock, move_mock):
        image_source_dict_mock.return_value = {
            "type": ImageSourceType.FOLDER,
            "location": "placeholder/folder"
        }

        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()

        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]
        workflow_config = Workflow(
            workflowId = "fakeWorkflowId",
            name = "fakeWorkflowName",
            workflowOutputPath = "/tmp/workflow/output/path",
            creationTime = int(time.time()),
            lastUpdatedTime = int(time.time()),
            featureConfigurations = feature_configs,
            description="",
            imageSourceId="fakeImageSourceId",
            imageSources = [],
            inputConfigurations=[],
            outputConfigurations=[]
        )

        try:
            self.pipeline_executor.execute_workflow_pipeline(workflow_config, self.session)
        except CapturedImageException as e:
            assert e.status_code == HTTP_422_UNPROCESSABLE_ENTITY
            get_image_source_mock.assert_called_once()
            image_source_dict_mock.assert_called_once()
            oldest_image_fp_mock.assert_called_once()
            getsize_mock.assert_called_once()
            move_mock.assert_called_once()
            run_pipeline_mock.assert_not_called()
        else:
            assert False

    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._move_bad_folder_image_source')
    @patch('os.path.getsize', return_value = 1)
    @patch('utils.captured_images_utils.get_oldest_image_file_path', return_value = "fake_filepath")
    @patch('utils.utils.convert_sqlalchemy_object_to_dict')
    @patch('resources.accessors.image_source_accessor.ImageSourceAccessor.get_image_source')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._run_pipeline', side_effect = Exception("placeholder exception"))
    def test_execute_workflow_pipeline_folder_runtime_failure(self, run_pipeline_mock, get_image_source_mock, image_source_dict_mock, oldest_image_fp_mock, getsize_mock, move_mock):
        image_source_dict_mock.return_value = {
            "type": ImageSourceType.FOLDER,
            "location": "placeholder/folder"
        }

        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()

        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]
        workflow_config = Workflow(
            workflowId = "fakeWorkflowId",
            name = "fakeWorkflowName",
            workflowOutputPath = "/tmp/workflow/output/path",
            creationTime = int(time.time()),
            lastUpdatedTime = int(time.time()),
            featureConfigurations = feature_configs,
            description="",
            imageSourceId="fakeImageSourceId",
            imageSources = [],
            inputConfigurations=[],
            outputConfigurations=[]
        )

        try:
            self.pipeline_executor.execute_workflow_pipeline(workflow_config, self.session)
        except Exception:
            get_image_source_mock.assert_called_once()
            image_source_dict_mock.assert_called_once()
            oldest_image_fp_mock.assert_called_once()
            getsize_mock.assert_called_once()
            move_mock.assert_called_once()
            run_pipeline_mock.assert_called_once()
        else:
            assert False

    @patch('utils.utils.get_image_bytes_from_file', return_value="fake_image_data")
    @patch('os.path.getsize', return_value = 1)
    @patch('utils.dda_user_management_utils.update_dda_user_file_permissions')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._run_pipeline')
    def test_execute_image_source_pipeline_camera_no_overrides(self, run_pipeline_mock, update_file_perm_mock, getsize_mock, image_bytes_mock):
        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline, int(time.time()))
        image_source = ImageSource(
            imageSourceId = "imageSourceId",
            name = "fakeName",
            type = ImageSourceType.CAMERA,
            location = "",
            cameraId = "Fake_1",
            description = "",
            creationTime = int(time.time()),
            lastUpdateTime = int(time.time()),
            imageCapturePath = "fake_image_capture_path",
            imageSourceConfiguration = image_source_configuration
        )

        self.pipeline_executor.execute_image_source_pipeline(image_source, is_preview=True)
        run_pipeline_mock.assert_called_once()
        getsize_mock.assert_called_once()
        image_bytes_mock.assert_called_once()
        update_file_perm_mock.assert_called_once()

    @patch('utils.utils.get_image_bytes_from_file', return_value="fake_image_data")
    @patch('os.path.getsize', return_value = 1)
    @patch('utils.dda_user_management_utils.update_dda_user_file_permissions')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._run_pipeline')
    def test_execute_image_source_pipeline_camera_with_overrides(self, run_pipeline_mock, update_file_perm_mock, getsize_mock, image_bytes_mock):
        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline, int(time.time()))
        image_source = ImageSource(
            imageSourceId = "imageSourceId",
            name = "fakeName",
            type = ImageSourceType.CAMERA,
            location = "",
            cameraId = "Fake_1",
            description = "",
            creationTime = int(time.time()),
            lastUpdateTime = int(time.time()),
            imageCapturePath = "fake_image_capture_path",
            imageSourceConfiguration = image_source_configuration
        )

        image_source_config_schema = ImageSourceConfigurationSchema()
        self.pipeline_executor.execute_image_source_pipeline(image_source,
                                                             image_source_config_schema.dump(image_source_configuration),
                                                             is_preview=True)
        run_pipeline_mock.assert_called_once()
        getsize_mock.assert_called_once()
        image_bytes_mock.assert_called_once()
        update_file_perm_mock.assert_called_once()

    @patch('utils.utils.get_image_bytes_from_file', return_value="fake_image_data")
    @patch('os.path.getsize', return_value = 1)
    @patch('utils.dda_user_management_utils.update_dda_user_file_permissions')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._run_pipeline')
    def test_execute_image_source_pipeline_icam_no_overrides(self, run_pipeline_mock, update_file_perm_mock, getsize_mock, image_bytes_mock):
        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline, int(time.time()))
        image_source = ImageSource(
            imageSourceId = "imageSourceId",
            name = "fakeName",
            type = ImageSourceType.ICAM,
            location = "",
            cameraId = "Fake_1",
            description = "",
            creationTime = int(time.time()),
            lastUpdateTime = int(time.time()),
            imageCapturePath = "fake_image_capture_path",
            imageSourceConfiguration = image_source_configuration
        )

        self.pipeline_executor.execute_image_source_pipeline(image_source, is_preview=True)
        run_pipeline_mock.assert_called_once()
        getsize_mock.assert_called_once()
        image_bytes_mock.assert_called_once()
        update_file_perm_mock.assert_called_once()

    @patch('utils.utils.get_image_bytes_from_file', return_value="fake_image_data")
    @patch('os.path.getsize', return_value = 1)
    @patch('utils.dda_user_management_utils.update_dda_user_file_permissions')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._run_pipeline')
    def test_execute_image_source_pipeline_icam_with_overrides(self, run_pipeline_mock, update_file_perm_mock, getsize_mock, image_bytes_mock):
        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline, int(time.time()))
        image_source = ImageSource(
            imageSourceId = "imageSourceId",
            name = "fakeName",
            type = ImageSourceType.ICAM,
            location = "",
            cameraId = "Fake_1",
            description = "",
            creationTime = int(time.time()),
            lastUpdateTime = int(time.time()),
            imageCapturePath = "fake_image_capture_path",
            imageSourceConfiguration = image_source_configuration
        )

        image_source_config_schema = ImageSourceConfigurationSchema()
        self.pipeline_executor.execute_image_source_pipeline(image_source,
                                                             image_source_config_schema.dump(image_source_configuration),
                                                             is_preview=True)
        run_pipeline_mock.assert_called_once()
        getsize_mock.assert_called_once()
        image_bytes_mock.assert_called_once()
        update_file_perm_mock.assert_called_once()

    @patch('utils.captured_images_utils.get_oldest_image_file_path', return_value="fake_filepath")
    @patch('utils.utils.get_image_bytes_from_file', return_value="fake_image_data")
    @patch('os.path.getsize', return_value = 1)
    @patch('utils.dda_user_management_utils.update_dda_user_file_permissions')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._run_pipeline')
    def test_execute_image_source_pipeline_folder_no_overrides(self, run_pipeline_mock, update_file_perm_mock, getsize_mock, image_bytes_mock, get_oldest_fp_mock):
        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline, int(time.time()))
        image_source = ImageSource(
            imageSourceId = "imageSourceId",
            name = "fakeName",
            type = ImageSourceType.FOLDER,
            location = "fake/location",
            cameraId = "Fake_1",
            description = "",
            creationTime = int(time.time()),
            lastUpdateTime = int(time.time()),
            imageCapturePath = "fake_image_capture_path",
            imageSourceConfiguration = image_source_configuration
        )

        self.pipeline_executor.execute_image_source_pipeline(image_source, is_preview=True)
        run_pipeline_mock.assert_called_once()
        get_oldest_fp_mock.assert_called_once()
        getsize_mock.assert_called_once()
        image_bytes_mock.assert_called_once()
        update_file_perm_mock.assert_called_once()

    @patch('utils.captured_images_utils.get_oldest_image_file_path', return_value="fake_filepath")
    @patch('utils.utils.get_image_bytes_from_file', return_value="fake_image_data")
    @patch('os.path.getsize', return_value = 1)
    @patch('utils.dda_user_management_utils.update_dda_user_file_permissions')
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._run_pipeline')
    def test_execute_image_source_pipeline_folder_with_overrides(self, run_pipeline_mock, update_file_perm_mock, getsize_mock, image_bytes_mock, get_oldest_fp_mock):
        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline, int(time.time()))
        image_source = ImageSource(
            imageSourceId = "imageSourceId",
            name = "fakeName",
            type = ImageSourceType.FOLDER,
            location = "fake/location",
            cameraId = "Fake_1",
            description = "",
            creationTime = int(time.time()),
            lastUpdateTime = int(time.time()),
            imageCapturePath = "fake_image_capture_path",
            imageSourceConfiguration = image_source_configuration
        )

        image_source_config_schema = ImageSourceConfigurationSchema()
        self.pipeline_executor.execute_image_source_pipeline(image_source,
                                                             image_source_config_schema.dump(image_source_configuration),
                                                             is_preview=True)
        run_pipeline_mock.assert_called_once()
        get_oldest_fp_mock.assert_called_once()
        getsize_mock.assert_called_once()
        image_bytes_mock.assert_called_once()
        update_file_perm_mock.assert_called_once()

    def test_execute_image_source_pipeline_invalid_type(self):
        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline, int(time.time()))
        image_source = ImageSource(
            imageSourceId = "imageSourceId",
            name = "fakeName",
            type = "not_a_type",
            location = "",
            cameraId = "Fake_1",
            description = "",
            creationTime = int(time.time()),
            lastUpdateTime = int(time.time()),
            imageCapturePath = "fake_image_capture_path",
            imageSourceConfiguration = image_source_configuration
        )

        try:
            self.pipeline_executor.execute_image_source_pipeline(image_source, is_preview=True)
        except UnexpectedTypeException as e:
            assert e.status_code == HTTP_500_INTERNAL_SERVER_ERROR
        else:
            # Expect an exception when wrong type, fail if we got here without an exception (or wrong exception)
            assert False

    @patch('utils.captured_images_utils.delete_image')
    @patch('os.path.getsize', return_value = 0)
    @patch('gstreamer.gst_pipeline_executor.GstPipelineExecutor._run_pipeline')
    def test_execute_image_source_pipeline_bad_file(self, run_pipeline_mock, getsize_mock, delete_image_mock):
        from gstreamer.gst_pipeline_executor import GstPipelineExecutor
        self.pipeline_executor = GstPipelineExecutor()
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline, int(time.time()))
        image_source = ImageSource(
            imageSourceId = "imageSourceId",
            name = "fakeName",
            type = ImageSourceType.CAMERA,
            location = "",
            cameraId = "Fake_1",
            description = "",
            creationTime = int(time.time()),
            lastUpdateTime = int(time.time()),
            imageCapturePath = "fake_image_capture_path",
            imageSourceConfiguration = image_source_configuration
        )

        try:
            self.pipeline_executor.execute_image_source_pipeline(image_source, is_preview=True)
        except CapturedImageException:
            run_pipeline_mock.assert_called_once()
            getsize_mock.assert_called_once()
            delete_image_mock.assert_called_once()
        else:
            # Expect an exception when bad file captured, fail if we got here without an exception (or wrong exception)
            assert False
