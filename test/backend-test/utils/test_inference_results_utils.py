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
import time
import glob
import json
import pytest
from local_server_base_test_case import LocalServerBaseTestCase
from utils.inference_results_utils import GetInferenceResults,convert_hex_color_to_rgb,convert_inference_res_to_save_in_db,generate_smgt_format_manifest
from constants import EXAMPLE_IMAGE_LIST
from unittest.mock import patch, call
import shutil

class TestInferenceResultsUtils(LocalServerBaseTestCase):
    def replace_path_infer_out(self, old_path, new_path, setup):
        for jsonl_filepath in self.sorted_jsonl_files:
            with open(jsonl_filepath, 'r') as jsonl_file:
                json_list = list(jsonl_file)
                output_list = []

            for json_line in json_list:
                infer_result = json.loads(json_line)
                infer_result["deviceFleetAuxiliaryInputs"][0]["data-ref"] = \
                    infer_result["deviceFleetAuxiliaryInputs"][0]["data-ref"].replace(old_path, new_path)
                if len(infer_result["deviceFleetAuxiliaryOutputs"]) == 4:
                    infer_result["deviceFleetAuxiliaryOutputs"][1]["data-ref"] = \
                        infer_result["deviceFleetAuxiliaryOutputs"][1]["data-ref"].replace(old_path, new_path)
                    infer_result["deviceFleetAuxiliaryOutputs"][0]["data-ref"] = \
                        infer_result["deviceFleetAuxiliaryOutputs"][0]["data-ref"].replace(old_path, new_path)
                output_list.append(infer_result)
            with open(jsonl_filepath, 'w') as jsonl_file:
                for infer_result in output_list:
                    json.dump(infer_result, jsonl_file)
                    jsonl_file.write('\n')
            if setup:
                time.sleep(0.01)

    def setUp(self):
        super().setUp()

        # update_jsonl_time_order
        self.sorted_jsonl_files = sorted(list(filter(os.path.isfile, glob.glob(pytest.infer_out_path + "*.jsonl"))))
        self.replace_path_infer_out("<INFER_OUT>", pytest.infer_out_path, setup=True)
        # mock em-config path
        self.get_em_agent_config_patcher = patch("utils.utils.get_em_agent_config_path_for_stream",
                                                 return_value=f"test/backend-test/utils/em-agent-id-testListImages.json")
        self.mock_get_em_agent_config = self.get_em_agent_config_patcher.start()
        self.workflow_output_path = os.getcwd() + "/test/backend-test/utils/tmp/"


    def tearDown(self):
        super().tearDown()
        self.replace_path_infer_out(pytest.infer_out_path, "<INFER_OUT>", setup=False)
        self.get_em_agent_config_patcher.stop()

    @patch("utils.inference_results_utils.GetInferenceResults.human_review_required", return_value=True)
    def test_get_inference_results_default(self, human_review_required):
        # default settings
        query = GetInferenceResults(stream_id="testListImages", sort="desc", starting_point=0, max_results=2)
        images = query.get_inference_results(self.workflow_output_path)
        assert {"images", "nextStartingPoint"} <= set(images)
        assert len(images["images"]) == 2
        assert images["nextStartingPoint"] == 2

        # classification model - img#1
        cls_img = images["images"][1]
        assert {"creationTime", "imageDataFilePath", "inferenceFilePath", "inferenceResult"} <= set(cls_img)
        assert EXAMPLE_IMAGE_LIST["1"] in cls_img["imageDataFilePath"]
        assert {"confidence", "inference_result"} <= set(cls_img["inferenceResult"])

        # segmentation model - img#0
        seg_img = images["images"][0]
        assert {"creationTime", "image", "imageDataFilePath", "inferenceFilePath", "inferenceResult", "inputImageFilePath"} <= set(seg_img)
        assert EXAMPLE_IMAGE_LIST["0"] in seg_img["imageDataFilePath"]
        assert {"confidence", "inference_result", "anomalies"} <= set(seg_img["inferenceResult"])
        assert "0" not in seg_img["inferenceResult"]["anomalies"]
        assert "1" in seg_img["inferenceResult"]["anomalies"]
        assert {"class-name", "hex-color", "total-percentage-area"} <= set(seg_img["inferenceResult"]["anomalies"]["1"])

    @patch("utils.inference_results_utils.GetInferenceResults.human_review_required", return_value=True)
    def test_get_inference_results_pagination_maxresults_startingpoint(self, human_review_required):
        # startingPoint=3, maxResults=2, sort=desc: img#3, img#4
        query = GetInferenceResults(stream_id="testListImages", sort="desc", starting_point=3, max_results=2)
        images = query.get_inference_results(self.workflow_output_path)
        assert len(images["images"]) == 2
        assert EXAMPLE_IMAGE_LIST["3"] in images["images"][0]["imageDataFilePath"]
        assert EXAMPLE_IMAGE_LIST["4"] in images["images"][1]["imageDataFilePath"]

    @patch("utils.inference_results_utils.GetInferenceResults.human_review_required", return_value=True)
    def test_get_inference_results_pagination_startingpoint(self, human_review_required):
        # startingPoint=4: img#4, img#5 (last two)
        query = GetInferenceResults(stream_id="testListImages", sort="desc", starting_point=4, max_results=2)
        images = query.get_inference_results(self.workflow_output_path)
        assert len(images["images"]) == 2
        assert EXAMPLE_IMAGE_LIST["4"] in images["images"][0]["imageDataFilePath"]
        assert EXAMPLE_IMAGE_LIST["5"] in images["images"][1]["imageDataFilePath"]

    @patch("utils.inference_results_utils.GetInferenceResults.human_review_required", return_value=True)
    def test_get_inference_results_pagination_sort_asc(self, human_review_required):
        # sort=asc: img#5, img#4 (first two in ascending order)
        query = GetInferenceResults(stream_id="testListImages", sort="asc", starting_point=0, max_results=2)
        images = query.get_inference_results(self.workflow_output_path)
        assert len(images["images"]) == 2
        assert EXAMPLE_IMAGE_LIST["5"] in images["images"][0]["imageDataFilePath"]
        assert EXAMPLE_IMAGE_LIST["4"] in images["images"][1]["imageDataFilePath"]

    @patch("utils.inference_results_utils.GetInferenceResults.human_review_required", return_value=True)
    def test_get_inference_results_pagination_sort_desc(self, human_review_required):
        # sort=asc: img#0, img#1 (first two in ascending order)
        query = GetInferenceResults(stream_id="testListImages", sort="desc", starting_point=0, max_results=2)
        images = query.get_inference_results(self.workflow_output_path)
        assert len(images["images"]) == 2
        assert EXAMPLE_IMAGE_LIST["0"] in images["images"][0]["imageDataFilePath"]
        assert EXAMPLE_IMAGE_LIST["1"] in images["images"][1]["imageDataFilePath"]

    def test_get_inference_results_invalid_startingpoint_exceeded(self):
        query = GetInferenceResults(stream_id="testListImages", sort="desc", starting_point=1000, max_results=2)
        images = query.get_inference_results(self.workflow_output_path)
        assert len(images["images"]) == 0

    @patch("time.sleep")
    @patch("utils.utils.remove_prefix", return_value = "non_exist_path")
    def test_read_raw_data_exception(self, mock_remove_prefix, mock_sleep):
        query = GetInferenceResults(stream_id="testListImages", sort="desc", starting_point=0, max_results=2)
        with self.assertRaises(Exception):
            query.read_raw_data("fake_path")
        mock_sleep.assert_has_calls([call(1) for i in range(10)])
    
    def test_save_image_object_exception(self):
        query = GetInferenceResults(stream_id="testListImages", sort="desc", starting_point=0, max_results=2)
        with self.assertRaises(Exception):
            query.save_image_object({"deviceFleetAuxiliaryOutputs": [{}, {}]}, "fake_path")

    @patch("utils.inference_results_utils.GetInferenceResults.human_review_required", return_value=True)
    def test_read_inference_result_from_jsonl(self, human_review_required):
        with open(self.sorted_jsonl_files[0], 'r') as jsonl:
            jsonl_content = list(jsonl)
            query = GetInferenceResults(stream_id="testListImages", sort="desc", starting_point=0, max_results=2)
            images = query.read_inference_result_from_jsonl(jsonl_content, self.sorted_jsonl_files[0])
            assert len(images) == 2


    @patch("utils.inference_results_utils.GetInferenceResults.human_review_required", return_value=True)
    def test_get_infer_res_with_capture_id(self, human_review_required):
        query = GetInferenceResults(stream_id="testListImages", sort="desc", starting_point=0, max_results=2)
        output_image = query.get_infer_res_with_capture_id("A_jsonl_img0-1_0314-161130_161132", self.workflow_output_path)
        assert "A_img1_03-14T16:11:32_2-1.jpg" in output_image['imageDataFilePath']

    def test_convert_hex_color_to_rgb(self):
        background = {"hex-color": "#ffffff"}
        rgb_background = convert_hex_color_to_rgb(background)
        assert rgb_background["rgb-color"] == [255, 255, 255]

    @patch("utils.feature_configs_utils.get_default_configs_lfv", autospec=True)
    def test_convert_inference_res_to_save_in_db(self, default_configs):
        default_configs.return_value = {
            "modelAlias": "cookie-model",
            "modelMetaData":"someMetadata",
            "modelVersion": "1",
            "modelConfidenceThresholds": {"AnomalyThreshold": 0.9, "NormalThreshold": 0.1}
        }
        workflow = {
            "name":"test-workflow",
            "creationTime":1684398714640,
            "lastUpdatedTime":1699317326918,
            "featureConfigurations":[
                {"type":"LFVModel","defaultConfiguration":{"modelAlias":"cookie-model"},"modelName":"model-123"}],
            "outputConfigurations":[],
            "description":"",
            "workflowId":"id-489314100",
            "workflowOutputPath":"/tmp",
            "inputConfigurations":[],
            "imageSourceId":"1vjwykbj",
        }
        inference_res = {
            "creationTime":"2023-11-10T22:00:21",
            "imageDataFilePath":"fake-path",
            "inferenceResult":{
                "anomalies":None,
                "confidence":0.83,
                "anomaly_score":0.16,
                "anomaly_threshold":0.91,
                "inference_result":"Normal",
                "mask_background":None,
                "mask_image":None
            },
            "inferenceFilePath":"fake-path",
            "captureId": "id-489314104-1699653620856",
            "captureType": "Inference",
            "inputImageFilePath":"fake-path",
            "processingTime": 100,
            "humanReviewRequired": True
        }
        res = convert_inference_res_to_save_in_db(inference_res, workflow)
        expected_res = {'anomalyScore': 0.16,
                       'anomalyThreshod': 0.91,
                       'captureId': 'id-489314104-1699653620856',
                       'captureType': 'Inference',
                       'confidence': 0.83,
                       'inferenceCreationTime': 1699653621,
                       'inputImageFilePath': 'fake-path',
                       'modelId': 'model-123',
                       'outputImageFilePath': 'fake-path',
                       'prediction': 'Normal',
                       'workflowId': 'id-489314100',
                       'flagForReview': False,
                       "downloaded": False,
                       "humanReviewRequired": True,
                       }
        
        # modelName and modelConfidenceThresholds are magic mock here, we check that it is present and remove it so we can comapre the rest of the items
        assert "modelName" in res.keys()
        del res["modelName"]

        assert "modelConfidenceThresholds" in res.keys()
        del res['modelConfidenceThresholds']

        assert expected_res.items() == res.items()
        
        
    def test_generate_smgt_format_multiple_entries(self):
        multi_input = [
        {
            "inputImageFilePath": "foo.jpg",
            "prediction": "Anomaly",
            "confidence": 0.6,
            "inferenceCreationTime": 1712785191,
            "humanClassification": None,
            "textNote": "bar"
        },
        {
            "inputImageFilePath": "my/path/foobar.jpg",
            "prediction": "Normal",
            "confidence": 0.5,
            "inferenceCreationTime": 1712784822,
            "humanClassification": "Normal",
            "textNote": None
        }]
        manifest_data = generate_smgt_format_manifest(multi_input)
        assert manifest_data == [
            {
                "source-ref": "foo.jpg",
                "source-ref-metadata": { "notes": "bar" },
                "anomaly-label": 1,
                "anomaly-label-metadata": {
                    "class-name": "Anomaly",
                    "confidence": 0.6,
                    "type": "groundtruth/image-classification",
                    "human-annotated": "no",
                    "creation-date": "2024-04-10T21:39:51"
                }
            },
            {
                "source-ref": "foobar.jpg",
                "source-ref-metadata": { "notes": None },
                "anomaly-label": 0,
                "anomaly-label-metadata": {
                    "class-name": "Normal",
                    "confidence": 0.5,
                    "type": "groundtruth/image-classification",
                    "human-annotated": "yes",
                    "creation-date": "2024-04-10T21:33:42"
                }
            }
        ]

    def test_generate_smgt_format_empty(self):
        empty_input = []
        manifest_data = generate_smgt_format_manifest(empty_input)
        assert manifest_data == []

    def test_generate_smgt_format_optional_params_none(self):
        multi_input = [
        {
            "inputImageFilePath": "foo.jpg",
            "prediction": "Anomaly",
            "confidence": 0.6,
            "inferenceCreationTime": 1712785191,
            "humanClassification": None,
            "textNote": None
        }]
        manifest_data = generate_smgt_format_manifest(multi_input)
        assert manifest_data == [
            {
                "source-ref": "foo.jpg",
                "source-ref-metadata": { "notes": None },
                "anomaly-label": 1,
                "anomaly-label-metadata": {
                    "class-name": "Anomaly",
                    "confidence": 0.6,
                    "type": "groundtruth/image-classification",
                    "human-annotated": "no",
                    "creation-date": "2024-04-10T21:39:51"
                }
            }
        ]

    def test_generate_smgt_format_human_machine_prediction_conflict(self):
        multi_input = [
        {
            "inputImageFilePath": "foo.jpg",
            "prediction": "Anomaly",
            "confidence": 0.6,
            "inferenceCreationTime": 1712785191,
            "humanClassification": "Normal",
            "textNote": "bar"
        }]
        manifest_data = generate_smgt_format_manifest(multi_input)
        assert manifest_data == [
            {
                "source-ref": "foo.jpg",
                "source-ref-metadata": { "notes": "bar" },
                "anomaly-label": 0,
                "anomaly-label-metadata": {
                    "class-name": "Normal",
                    "confidence": 0.6,
                    "type": "groundtruth/image-classification",
                    "human-annotated": "yes",
                    "creation-date": "2024-04-10T21:39:51"
                }
            }
        ]
    def test_generate_smgt_format_human_machine_prediction_conflict_reversed(self):
        multi_input = [
        {
            "inputImageFilePath": "foo.jpg",
            "prediction": "Normal",
            "confidence": 0.6,
            "inferenceCreationTime": 1712785191,
            "humanClassification": "Anomaly",
            "textNote": "bar"
        }]
        manifest_data = generate_smgt_format_manifest(multi_input)
        assert manifest_data == [
            {
                "source-ref": "foo.jpg",
                "source-ref-metadata": { "notes": "bar" },
                "anomaly-label": 1,
                "anomaly-label-metadata": {
                    "class-name": "Anomaly",
                    "confidence": 0.6,
                    "type": "groundtruth/image-classification",
                    "human-annotated": "yes",
                    "creation-date": "2024-04-10T21:39:51"
                }
            }
        ]

    @patch("utils.inference_results_utils.get_default_configs_lfv", autospec=True)
    def test_human_review_required(self, default_config_patch):
        default_config_patch.return_value = {
            "modelConfidenceThresholds": {"AnomalyThreshold": "0.9", "NormalThreshold": "0.8"}
        }
        query = GetInferenceResults(stream_id="testListImages", sort="desc", starting_point=0, max_results=2)
        assert query.human_review_required({"modelName": "cookie-model"}, 0.85, "Anomaly") == True
        assert query.human_review_required({"modelName": "cookie-model"}, 0.85, "Normal") == False
        assert query.human_review_required({"modelName": "cookie-model"}, 0.75, "Normal") == True
        assert query.human_review_required({"modelName": "cookie-model"}, 0.95, "Anomaly") == False

        default_config_patch.return_value = {}
        assert query.human_review_required({"modelName": "cookie-model"}, 0.05, "Anomaly") == False