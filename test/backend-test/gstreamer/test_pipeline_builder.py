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

import inspect
import os
import unittest
import logging
import time
from unittest.mock import patch,mock_open

from model.image_source import ImageSource, ImageSourceType
from model.image_source_configuration import ImageSourceConfiguration
from model.workflow import Workflow
from model.feature_configuration import FeatureConfiguration
from model.output_configuration import OutputConfiguration
from utils.constants import GPIO_RISING
from edge_ml1_p_camera_management.aravis_functions import get_input_file_from_pipeline

#: Legacy goldens of jpeg-folder-source-decode-ubuntu2204 (task 2): captured
#: once from the unfixed tree, then frozen. Resolved from this file, never
#: from the working directory.
_JPEGDEC_GOLDEN_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "goldens", "jpegdec_native_caps")


def _capture_or_assert_text(name, current_text):
    """Capture ``current_text`` as the golden ``name`` when it is absent (the
    first run, on the unfixed tree), else assert byte equality with it. The
    ``_cmake_preservation_support.capture_or_assert_text`` pattern."""
    path = os.path.join(_JPEGDEC_GOLDEN_DIR, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(current_text)
        return current_text
    with open(path, encoding="utf-8") as fh:
        recorded = fh.read()
    assert current_text == recorded, (
        f"frozen golden '{name}' changed: the legacy pipeline string differs "
        f"from the unfixed-tree capture")
    return recorded


class TestPipelineBuilder(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.env_patcher = patch.dict(os.environ, {"COMPONENT_WORK_PATH": "test/backend-test/resources",
                                                  "LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": "test/"})
        cls.env_patcher.start()

    def setUp(self):
        from gstreamer.pipeline_builder import GstPipelineBuilder
        self.pipeline_builder = GstPipelineBuilder()
        os.environ["is_triton"] = "False"

    def test_pipeline_definition_with_image_source(self):
        expected_pipeline = "appsrc name=appsrc ! capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert ! jpegenc idct-method=2 quality=100 ! filesink location=fake_image_capture_path"
        image_capture_path = "fake_image_capture_path"
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline, int(time.time()))
        image_source = ImageSource("imageSourceId", "fakeName", ImageSourceType.CAMERA, int(time.time()),
                                   int(time.time()), image_capture_path, cameraId="Fake_1",
                                   description="", imageSourceConfiguration=image_source_configuration)
        result, _ = self.pipeline_builder.add_image_source(image_source).build()
        self.assertTrue(result.startswith(expected_pipeline),
                        f"Excepted pipeline to start with {expected_pipeline}, found {result}")

    def test_pipeline_preview_definition_with_image_source(self):
        expected_pipeline = "appsrc name=appsrc ! capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert ! jpegenc idct-method=2 quality=100 ! filesink location=/aws_dda/image-capture/preview"
        image_capture_path = "fake_image_capture_path"
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline,int(time.time()))
        image_source = ImageSource("imageSourceId", "fakeName", ImageSourceType.CAMERA, int(time.time()),
                                   int(time.time()), image_capture_path, cameraId="Fake_1",
                                   description="", imageSourceConfiguration=image_source_configuration)
        result, _ = self.pipeline_builder.add_image_source(image_source).build(is_preview=True)
        self.assertTrue(result.startswith(expected_pipeline),
                        f"Excepted pipeline to start with {expected_pipeline}, found {result}")

    def test_pipeline_definition_with_image_source_overrides(self):
        expected_pipeline = "appsrc name=appsrc ! capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert ! jpegenc idct-method=2 quality=100 ! filesink location=fake_image_capture_path"
        image_capture_path = "fake_image_capture_path"
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline,int(time.time()))
        image_source = ImageSource("imageSourceId", "fakeName", ImageSourceType.CAMERA, int(time.time()),
                                   int(time.time()), image_capture_path, cameraId="Fake_1",
                                   description="", imageSourceConfiguration=image_source_configuration)
        result, _ = self.pipeline_builder.add_image_source(image_source).build()
        self.assertTrue(result.startswith(expected_pipeline),
                        f"Excepted pipeline to start with {expected_pipeline}, found {result}")

    def test_pipeline_definition_with_image_source_and_workflow(self):
        expected_pipeline = "appsrc name=appsrc ! capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert ! capsfilter caps=video/x-raw,format=RGB ! eminfer name=eminferX mode=2 tensor-source=1 config=test/backend-test/resources/em-agent-fakeWorkflowId.json model-component=fakemodel-1 confidence-watermark=1 ! jpegenc idct-method=2 quality=100 ! emdatacapture config=test/backend-test/resources/em-agent-fakeWorkflowId.json aws-cred-source=0 target=eminferX file-extension=jpg capture-folder=/tmp/workflow/output/path capture-id=my-capture-id ! fakesink"
        image_capture_path = "fake_image_capture_path"
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline, int(time.time()))
        image_source = ImageSource("imageSourceId", "fakeName", ImageSourceType.CAMERA, int(time.time()),
                                   int(time.time()), image_capture_path, cameraId="Fake_1",
                                   description="", imageSourceConfiguration=image_source_configuration)

        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]

        workflow_config = Workflow("fakeWorkflowId", "fakeWorkflowName", "/tmp/workflow/output/path",
                                   int(time.time()), int(time.time()), feature_configs, description="",
                                   imageSourceId="imageSourceId", inputConfigurations=[], outputConfigurations=[])

        result, _ = self.pipeline_builder.add_image_source(image_source).add_inference(workflow_config, "my-capture-id").build()
        self.assertEqual(expected_pipeline, result)

    @patch("builtins.open", new_callable=mock_open, read_data="{\"sagemaker_edge_core_capture_data_disk_path\": \"/tmp/workflow_id_test/\",\"sagemaker_edge_core_device_fleet_name\": \"dda_fleet\"}")
    def test_pipeline_definition_with_triton(self, mock_file):
        os.environ["is_triton"] = "True"
        expected_pipeline = 'appsrc name=appsrc ! capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert ! capsfilter caps=video/x-raw,format=RGB ! emltriton model-repo=/aws_dda/dda_triton/triton_model_repo server-path=/opt/tritonserver model=fakemodel-1 metadata=\"{\\"sagemaker_edge_core_capture_data_disk_path\\": \\"/tmp/workflow_id_test/\\", \\"sagemaker_edge_core_device_fleet_name\\": \\"dda_fleet\\", \\"capture_id\\": \\"my-capture-id\\"}\" correlation-id=my-capture-id ! jpegenc idct-method=2 quality=100 ! emlcapture buffer-message-id=file-target_/tmp/workflow/output/path-jpg interval=0 meta=triton_inference_output_overlay:file-target_/tmp/workflow/output/path-overlay.jpg,triton_inference_output_mask:file-target_/tmp/workflow/output/path-mask.png,triton_inference_output_capture:file-target_/tmp/workflow/output/path-jsonl,triton_inference_output_anomalous:/tmp/workflow/output/path_is-anomalous,triton_inference_output_confidence:/tmp/workflow/output/path_confidence ! fakesink'
        image_capture_path = "fake_image_capture_path"
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline, int(time.time()))
        image_source = ImageSource("imageSourceId", "fakeName", ImageSourceType.CAMERA, int(time.time()),
                                   int(time.time()), image_capture_path, cameraId="Fake_1",
                                   description="", imageSourceConfiguration=image_source_configuration)

        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]

        workflow_config = Workflow("fakeWorkflowId", "fakeWorkflowName", "/tmp/workflow/output/path",
                                   int(time.time()), int(time.time()), feature_configs, description="",
                                   imageSourceId="imageSourceId", inputConfigurations=[], outputConfigurations=[])

        result, _ = self.pipeline_builder.add_image_source(image_source).add_inference(workflow_config, "my-capture-id").build()
        self.assertEqual(expected_pipeline, result)
        mock_file.assert_called_once_with('test/backend-test/resources/em-agent-fakeWorkflowId.json', 'r')

        os.environ["is_triton"] = "False"

    @patch("builtins.open", new_callable=mock_open, read_data="{\"sagemaker_edge_core_capture_data_disk_path\": \"/tmp/workflow_id_test/\",\"sagemaker_edge_core_device_fleet_name\": \"dda_fleet\"}")
    def test_pipeline_definition_with_triton_output_configs(self, mock_file):
        os.environ["is_triton"] = "True"
        expected_pipeline = 'appsrc name=appsrc ! capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert ! capsfilter caps=video/x-raw,format=RGB ! emltriton model-repo=/aws_dda/dda_triton/triton_model_repo server-path=/opt/tritonserver model=fakemodel-1 metadata=\"{\\"sagemaker_edge_core_capture_data_disk_path\\": \\"/tmp/workflow_id_test/\\", \\"sagemaker_edge_core_device_fleet_name\\": \\"dda_fleet\\", \\"capture_id\\": \\"my-capture-id\\"}\" correlation-id=my-capture-id ! jpegenc idct-method=2 quality=100 ! emlcapture buffer-message-id=file-target_/tmp/workflow/output/path-jpg interval=0 meta=triton_inference_output_overlay:file-target_/tmp/workflow/output/path-overlay.jpg,triton_inference_output_mask:file-target_/tmp/workflow/output/path-mask.png,triton_inference_output_capture:file-target_/tmp/workflow/output/path-jsonl,triton_inference_output_anomalous:gpio-target_Normal_GPIO.RISING_245_500 ! fakesink'
        image_capture_path = "fake_image_capture_path"
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline, int(time.time()))
        image_source = ImageSource("imageSourceId", "fakeName", ImageSourceType.CAMERA, int(time.time()),
                                   int(time.time()), image_capture_path, cameraId="Fake_1",
                                   description="", imageSourceConfiguration=image_source_configuration)

        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]
        output_configs = [OutputConfiguration("fakeOutputConfig1", "245", GPIO_RISING, 500, int(time.time()), "Normal")]

        workflow_config = Workflow("fakeWorkflowId", "fakeWorkflowName", "/tmp/workflow/output/path",
                                   int(time.time()), int(time.time()), feature_configs, description="",
                                   imageSourceId="imageSourceId", inputConfigurations=[], outputConfigurations=output_configs)

        result, _ = self.pipeline_builder.add_image_source(image_source).add_inference(workflow_config, "my-capture-id").build()
        self.assertEqual(expected_pipeline, result)

        mock_file.assert_called_once_with('test/backend-test/resources/em-agent-fakeWorkflowId.json', 'r')

        os.environ["is_triton"] = "False"

    @patch("builtins.open", new_callable=mock_open, read_data="{\"sagemaker_edge_core_capture_data_disk_path\": \"/tmp/workflow_id_test/\",\"sagemaker_edge_core_device_fleet_name\": \"dda_fleet\"}")
    def test_pipeline_definition_with_triton_output_configs_multiple(self, mock_file):
        os.environ["is_triton"] = "True"
        expected_pipeline = 'appsrc name=appsrc ! capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert ! capsfilter caps=video/x-raw,format=RGB ! emltriton model-repo=/aws_dda/dda_triton/triton_model_repo server-path=/opt/tritonserver model=fakemodel-1 metadata=\"{\\"sagemaker_edge_core_capture_data_disk_path\\": \\"/tmp/workflow_id_test/\\", \\"sagemaker_edge_core_device_fleet_name\\": \\"dda_fleet\\", \\"capture_id\\": \\"my-capture-id\\"}\" correlation-id=my-capture-id ! jpegenc idct-method=2 quality=100 ! emlcapture buffer-message-id=file-target_/tmp/workflow/output/path-jpg interval=0 meta=triton_inference_output_overlay:file-target_/tmp/workflow/output/path-overlay.jpg,triton_inference_output_mask:file-target_/tmp/workflow/output/path-mask.png,triton_inference_output_capture:file-target_/tmp/workflow/output/path-jsonl,triton_inference_output_anomalous:gpio-target_Normal;All_GPIO.RISING;GPIO.RISING_245;255_500;500 ! fakesink'
        image_capture_path = "fake_image_capture_path"
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline, int(time.time()))
        image_source = ImageSource("imageSourceId", "fakeName", ImageSourceType.CAMERA, int(time.time()),
                                   int(time.time()), image_capture_path, cameraId="Fake_1",
                                   description="", imageSourceConfiguration=image_source_configuration)

        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]
        output_configs = [OutputConfiguration("fakeOutputConfig1", "245", GPIO_RISING, 500, int(time.time()), "Normal"),OutputConfiguration("fakeOutputConfig1", "255", GPIO_RISING, 500, int(time.time()), "All")]

        workflow_config = Workflow("fakeWorkflowId", "fakeWorkflowName", "/tmp/workflow/output/path",
                                   int(time.time()), int(time.time()), feature_configs, description="",
                                   imageSourceId="imageSourceId", inputConfigurations=[], outputConfigurations=output_configs)

        result, _ = self.pipeline_builder.add_image_source(image_source).add_inference(workflow_config, "my-capture-id").build()
        self.assertEqual(expected_pipeline, result)
        
        mock_file.assert_called_once_with('test/backend-test/resources/em-agent-fakeWorkflowId.json', 'r')

        os.environ["is_triton"] = "False"

    def test_pipeline_definition_with_output_configuration(self):
        image_capture_path = "fake_image_capture_path"
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline, int(time.time()))
        image_source = ImageSource("imageSourceId", "fakeName", ImageSourceType.CAMERA, int(time.time()),
                                   int(time.time()), image_capture_path, cameraId="Fake_1",
                                   description="", imageSourceConfiguration=image_source_configuration)

        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]
        output_configs = [OutputConfiguration("fakeOutputConfig1", "245", GPIO_RISING, 500, int(time.time()), "Normal")]

        workflow_config = Workflow("fakeWorkflowId", "fakeWorkflowName", "/tmp/workflow/output/path",
                                   int(time.time()), int(time.time()), feature_configs, description="",
                                   imageSourceId="imageSourceId", inputConfigurations=[], outputConfigurations=output_configs)

        result, _ = self.pipeline_builder.add_image_source(image_source).add_inference(workflow_config).build()
        self.assertTrue("emoutputevent" in result, "Expected emoutputevent gstreamer plugin to be present in pipeline")
        self.assertTrue(GPIO_RISING in result, "Expected GPIO.RISING config to be present in pipeline")

    @patch("workflow_engine.environment.device_arch", return_value="x86_64")
    @patch("utils.captured_images_utils.get_oldest_image_file_path", return_value="test/backend-test/captured_images_for_test/test-1.jpg")
    def test_pipeline_definition_with_folder_input(self, mock_get_oldest_image, mock_device_arch):
        expected_result = "filesrc blocksize=-1 location=\"test/backend-test/captured_images_for_test/test-1.jpg\" ! emexifextract ! jpegparse ! jpegdec idct-method=2 ! capsfilter caps=\"video/x-raw,format=I420;video/x-raw,format=Y42B;video/x-raw,format=Y444;video/x-raw,format=GRAY8;video/x-raw,format=RGB\" ! videoconvert ! videoflip method=automatic ! capsfilter caps=video/x-raw,format=RGB ! eminfer name=eminferX mode=2 tensor-source=1 config=test/backend-test/resources/em-agent-fakeWorkflowId.json model-component=fakemodel-1 confidence-watermark=1 ! jpegenc idct-method=2 quality=100 ! emdatacapture config=test/backend-test/resources/em-agent-fakeWorkflowId.json aws-cred-source=0 target=eminferX file-extension=jpg capture-folder=/tmp/workflow/output/path capture-id=my-capture-id ! fakesink"
        image_capture_path = "fake_image_capture_path"
        image_source = ImageSource("imageSourceId", "fakeName", ImageSourceType.FOLDER, int(time.time()),
                                   int(time.time()), image_capture_path, location="test/backend-test/captured_images_for_test/")
        
        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]
        workflow_config = Workflow("fakeWorkflowId", "fakeWorkflowName", "/tmp/workflow/output/path",
                                   int(time.time()), int(time.time()), feature_configs, description="",
                                   imageSourceId="imageSourceId", inputConfigurations=[], outputConfigurations=[])
        
        result, _ = self.pipeline_builder.add_image_source(image_source).add_inference(workflow_config, "my-capture-id").build()
        self.assertEqual(expected_result, result)

    # jpeg-folder-source-decode-ubuntu2204: the legacy folder-input chain pins
    # jpegdec to its native formats on the Ubuntu 22.04 targets (design C4).
    # Each case first checks that it tests the tree's builder, not a
    # LocalServer image's baked /gstreamer/pipeline_builder.py.

    def _assert_tests_the_tree_builder(self):
        assert inspect.getsourcefile(type(self.pipeline_builder)) == os.path.join(
            os.getcwd(), "src", "backend", "gstreamer", "pipeline_builder.py"), \
            "the legacy cases must test the tree's builder"

    def _folder_input_pipeline(self):
        image_source = ImageSource("imageSourceId", "fakeName", ImageSourceType.FOLDER, int(time.time()),
                                   int(time.time()), "fake_image_capture_path",
                                   location="test/backend-test/captured_images_for_test/")
        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]
        workflow_config = Workflow("fakeWorkflowId", "fakeWorkflowName", "/tmp/workflow/output/path",
                                   int(time.time()), int(time.time()), feature_configs, description="",
                                   imageSourceId="imageSourceId", inputConfigurations=[], outputConfigurations=[])
        with patch("utils.captured_images_utils.get_oldest_image_file_path",
                   return_value="test/backend-test/captured_images_for_test/test-1.jpg"):
            result, _ = self.pipeline_builder.add_image_source(image_source).add_inference(
                workflow_config, "my-capture-id").build()
        return result

    def _assert_folder_input_pins_jpegdec(self, arch):
        from workflow_engine.vendor.workflow_core.catalog import nodes as vendored_nodes
        caps = getattr(vendored_nodes, "JPEGDEC_NATIVE_CAPS", None)
        self.assertIsNotNone(caps, "the vendored JPEGDEC_NATIVE_CAPS is missing")
        with patch("workflow_engine.environment.device_arch", return_value=arch):
            result = self._folder_input_pipeline()
        self.assertIn(f'jpegdec idct-method=2 ! capsfilter caps="{caps}" ! videoconvert', result)

    def test_folder_input_pins_jpegdec_on_x86_64(self):
        self._assert_tests_the_tree_builder()
        self._assert_folder_input_pins_jpegdec("x86_64")

    def test_folder_input_pins_jpegdec_on_arm64_cpu(self):
        self._assert_tests_the_tree_builder()
        self._assert_folder_input_pins_jpegdec("arm64_cpu")

    def test_folder_input_on_arm64_jp5_matches_the_frozen_golden(self):
        self._assert_tests_the_tree_builder()
        with patch("workflow_engine.environment.device_arch", return_value="arm64_jp5"):
            result = self._folder_input_pipeline()
        _capture_or_assert_text("folder_input_arm64_jp5.txt", result + "\n")

    def test_folder_input_jp6_png_branch_matches_the_frozen_golden(self):
        self._assert_tests_the_tree_builder()
        with patch("workflow_engine.environment.device_arch", return_value="arm64_jp6"), \
                patch.object(self.pipeline_builder, "_is_jp6", return_value=True), \
                patch.object(self.pipeline_builder, "_stage_decoded_png",
                             side_effect=lambda path: path + ".dda_decoded.png"):
            result = self._folder_input_pipeline()
        _capture_or_assert_text("folder_input_jp6_png.txt", result + "\n")

    def test_input_filename_extracted_from_pipeline_definition(self):
        pipeline_string = "filesrc blocksize=-1 location=\"test/backend-test/captured_images_for_test/test-1.jpg\" ! emexifextract ! jpegdec idct-method=2 ! videoconvert ! videoflip method=automatic ! capsfilter caps=video/x-raw,format=RGB ! eminfer name=eminferX mode=2 tensor-source=1 config=test/backend-test/resources/em-agent-fakeWorkflowId.json model-component=fakemodel-1 confidence-watermark=1 ! jpegenc idct-method=2 quality=100 ! emdatacapture config=test/backend-test/resources/em-agent-fakeWorkflowId.json aws-cred-source=0 target=eminferX file-extension=jpg ! fakesink"
        expected_result = "test/backend-test/captured_images_for_test/test-1.jpg"
        result = get_input_file_from_pipeline(pipeline_string)
        self.assertEqual(expected_result, result)


    def test_pipeline_definition_with_capture_id(self):
        image_capture_path = "fake_image_capture_path"
        image_processing_pipeline = "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        image_source_configuration = ImageSourceConfiguration("imageSourceConfigId", 40, 1000,
                                                              image_processing_pipeline, int(time.time()))
        image_source = ImageSource("imageSourceId", "fakeName", ImageSourceType.CAMERA, int(time.time()),
                                   int(time.time()), image_capture_path, cameraId="Fake_1",description="",
                                   imageSourceConfiguration=image_source_configuration)

        feature_configs = [FeatureConfiguration("LFVModel", "fakemodel-1")]

        workflow_config = Workflow("fakeWorkflowId", "fakeWorkflowName", "/tmp/workflow/output/path",
                                   int(time.time()), int(time.time()), feature_configs, description="",
                                   imageSourceId="imageSourceId", inputConfigurations=[], outputConfigurations=[])

        result, _ = self.pipeline_builder\
            .add_image_source(image_source)\
            .add_inference(workflow_config, capture_id="random-capture-id").build()
        self.assertTrue("capture-id=random-capture-id" in result, "Expected capture id to be present in pipeline")

    def test_pipeline_build_without_img_src_workflow_config(self):
        self.assertEqual(self.pipeline_builder.build(), None)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.env_patcher.stop()

