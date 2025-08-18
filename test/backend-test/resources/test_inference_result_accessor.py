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
import copy
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.exc import InvalidRequestError
import pytest

from local_server_base_test_case import LocalServerBaseTestCase


class TestInferenceResultAccessor(LocalServerBaseTestCase):

    def setUp(self):
        super().setUp()

        from resources.accessors.inference_result_accessor import InferenceResultAccessor
        from dao.sqlite_db import inference_result_dao as dao
        from dao.sqlite_db.models import InferenceResult
        self.session = Session(self.metadata_engine)
        self.test_inference_res_data0 = {
            "captureId": "5c602574fa5e450c820df6f9b5af8c2f",
            "captureType": "Inference",
            "workflowId": "fake-wf-id",
            "inferenceCreationTime": 12345,
            "prediction": "Normal",
            "confidence": 0.8370160460472107,
            "anomalyScore": 0.8629839539527893,
            "anomalyThreshod": 0.916666567325592,
            "inputImageFilePath": "hi.jpg",
            "outputImageFilePath": "path",
            "modelId": "model-123",
            "modelName": "fake-model",
            "flagForReview": False,
            "downloaded": False
        }
        self.test_inference_res_data1 = {
            "captureId": "d38dbdf7b44c406eae81c34e070b2ea3",
            "captureType": "Inference",
            "workflowId": "fake-wf-id",
            "inferenceCreationTime": 12345,
            "prediction": "Anomaly",
            "confidence": 0.8370160460472107,
            "anomalyScore": 0.8629839539527893,
            "anomalyThreshod": 0.916666567325592,
            "inputImageFilePath": "foo/bar.jpg",
            "outputImageFilePath": "path",
            "modelId": "model-123",
            "modelName": "fake-model",
            "flagForReview": False,
            "downloaded": False,
            "humanClassification": "Anomaly",
            "textNote": "this_text_note_is_exactly_50_chars_which_is_maxlen"
        }
        self.session.add(InferenceResult(**self.test_inference_res_data0))
        self.session.add(InferenceResult(**self.test_inference_res_data1))
        self.session.commit()
        self.accessor = InferenceResultAccessor()

    def tearDown(self):
        super().tearDown()

    def test_store_inference_res_happy_path(self):
        from dao.sqlite_db.models import InferenceResult
        test_inference_res_data = {
            "captureId": "dummy",
            "captureType": "Inference",
            "workflowId": "fake-wf-id",
            "inferenceCreationTime": 123456,
            "prediction": "Normal",
            "confidence": 0.8370160460472107,
            "anomalyScore": 0.1629839539527893,
            "anomalyThreshod": 0.916666567325592,
            "inputImageFilePath": "path",
            "outputImageFilePath": "path",
            "modelId": "model-456",
            "modelName": "fake-model",
            "flagForReview": False,
            "downloaded": False
        }
        p_key = self.accessor.store_inference_result(self.session, test_inference_res_data)
        result_data = self.session.get(InferenceResult, p_key)
        self.assertEqual(result_data.workflowId, "fake-wf-id")
        self.assertEqual(result_data.inferenceCreationTime, 123456)
        self.assertEqual(result_data.prediction, "Normal")
        self.assertEqual(result_data.confidence, 0.8370160460472107)
        self.assertEqual(result_data.anomalyScore, 0.1629839539527893)
        self.assertEqual(result_data.anomalyThreshod, 0.916666567325592)
        self.assertEqual(result_data.inputImageFilePath, "path")
        self.assertEqual(result_data.outputImageFilePath, "path")
        self.assertEqual(result_data.modelId, "model-456")
        self.assertEqual(result_data.modelName, "fake-model")
        self.assertEqual(result_data.flagForReview, False)
        self.assertEqual(result_data.downloaded, False)
        self.assertEqual(result_data.humanClassification, None)
        self.assertEqual(result_data.textNote, None)

    def test_get_inference_res_happy_path(self):
        result_data = self.accessor.get_inference_result(self.session, "5c602574fa5e450c820df6f9b5af8c2f")
        self.assertEqual(result_data.workflowId, "fake-wf-id")
        self.assertEqual(result_data.inferenceCreationTime, 12345)
        self.assertEqual(result_data.prediction, "Normal")
        self.assertEqual(result_data.confidence, 0.8370160460472107)
        self.assertEqual(result_data.anomalyScore, 0.8629839539527893)
        self.assertEqual(result_data.anomalyThreshod, 0.916666567325592)
        self.assertEqual(result_data.inputImageFilePath, "hi.jpg")
        self.assertEqual(result_data.outputImageFilePath, "path")
        self.assertEqual(result_data.modelId, "model-123")
        self.assertEqual(result_data.modelName, "fake-model")
        self.assertEqual(result_data.flagForReview, False)
        self.assertEqual(result_data.downloaded, False)
        self.assertEqual(result_data.humanClassification, None)
        self.assertEqual(result_data.textNote, None)

    def test_store_inference_res_happy_path_with_human_classification_and_text_note(self):
        from dao.sqlite_db.models import InferenceResult
        test_inference_res_data = {
            "captureId": "dummy1",
            "captureType": "Inference",
            "workflowId": "fake-wf-id",
            "inferenceCreationTime": 123456,
            "prediction": "Normal",
            "confidence": 0.8370160460472107,
            "anomalyScore": 0.1629839539527893,
            "anomalyThreshod": 0.916666567325592,
            "inputImageFilePath": "path",
            "outputImageFilePath": "path",
            "modelId": "model-456",
            "modelName": "fake-model",
            "flagForReview": False,
            "downloaded": False,
            "humanClassification": "Anomaly",
            "textNote": "this_text_note_is_exactly_50_chars_which_is_maxlen"
        }
        p_key = self.accessor.store_inference_result(self.session, test_inference_res_data)
        result_data = self.session.get(InferenceResult, p_key)
        self.assertEqual(result_data.workflowId, "fake-wf-id")
        self.assertEqual(result_data.inferenceCreationTime, 123456)
        self.assertEqual(result_data.prediction, "Normal")
        self.assertEqual(result_data.confidence, 0.8370160460472107)
        self.assertEqual(result_data.anomalyScore, 0.1629839539527893)
        self.assertEqual(result_data.anomalyThreshod, 0.916666567325592)
        self.assertEqual(result_data.inputImageFilePath, "path")
        self.assertEqual(result_data.outputImageFilePath, "path")
        self.assertEqual(result_data.modelId, "model-456")
        self.assertEqual(result_data.modelName, "fake-model")
        self.assertEqual(result_data.flagForReview, False)
        self.assertEqual(result_data.downloaded, False)
        self.assertEqual(result_data.humanClassification, "Anomaly")
        self.assertEqual(result_data.textNote, "this_text_note_is_exactly_50_chars_which_is_maxlen")

    def test_get_inference_res_happy_path_with_human_classification_and_text_note(self):
        result_data = self.accessor.get_inference_result(self.session, "d38dbdf7b44c406eae81c34e070b2ea3")
        self.assertEqual(result_data.workflowId, "fake-wf-id")
        self.assertEqual(result_data.inferenceCreationTime, 12345)
        self.assertEqual(result_data.prediction, "Anomaly")
        self.assertEqual(result_data.confidence, 0.8370160460472107)
        self.assertEqual(result_data.anomalyScore, 0.8629839539527893)
        self.assertEqual(result_data.anomalyThreshod, 0.916666567325592)
        self.assertEqual(result_data.inputImageFilePath, "foo/bar.jpg")
        self.assertEqual(result_data.outputImageFilePath, "path")
        self.assertEqual(result_data.modelId, "model-123")
        self.assertEqual(result_data.modelName, "fake-model")
        self.assertEqual(result_data.flagForReview, False)
        self.assertEqual(result_data.downloaded, False)
        self.assertEqual(result_data.humanClassification, "Anomaly")
        self.assertEqual(result_data.textNote, "this_text_note_is_exactly_50_chars_which_is_maxlen")

    def test_store_inference_res_missing_attr(self):
        test_inference_res_data = {
            "captureId": "234134",
            "captureType": "Inference",
            "workflowId": "fake-wf-id",
            "inferenceCreationTime": 123456,
        }
        with pytest.raises(HTTPException):
            response = self.accessor.store_inference_result(self.session, test_inference_res_data)
            self.assertIn("Invalid data provided: ", response.description)

    def test_store_inference_res_invalid_prediction(self):
        test_inference_res_data = {
            "captureId": "1341134",
            "workflowId": "fake-wf-id",
            "prediction": "NONE"
        }
        with pytest.raises(HTTPException):
            response = self.accessor.store_inference_result(self.session, test_inference_res_data)
            self.assertIn("Invalid data provided: ", response.description)

    def test_store_inference_res_invalid_human_classification(self):
        test_inference_res_data = {
            "captureId": "12345",
            "captureType": "Inference",
            "workflowId": "fake-wf-id",
            "inferenceCreationTime": 123456,
            "prediction": "Normal",
            "confidence": 0.8370160460472107,
            "anomalyScore": 0.1629839539527893,
            "anomalyThreshod": 0.916666567325592,
            "inputImageFilePath": "path",
            "outputImageFilePath": "path",
            "modelId": "model-456",
            "modelName": "fake-model",
            "flagForReview": False,
            "downloaded": False,
            "humanClassification": "INVALID"
        }
        with pytest.raises(HTTPException):
            response = self.accessor.store_inference_result(self.session, test_inference_res_data)
            self.assertIn("Invalid data provided: ", response.description)

    def test_store_inference_res_invalid_text_note(self):
        test_inference_res_data = {
            "captureId": "12345",
            "captureType": "Inference",
            "workflowId": "fake-wf-id",
            "inferenceCreationTime": 123456,
            "prediction": "Normal",
            "confidence": 0.8370160460472107,
            "anomalyScore": 0.1629839539527893,
            "anomalyThreshod": 0.916666567325592,
            "inputImageFilePath": "path",
            "outputImageFilePath": "path",
            "modelId": "model-456",
            "modelName": "fake-model",
            "flagForReview": False,
            "downloaded": False,
            "humanClassification": "Anomaly",
            "textNote": "this_text_note_is_more_than_50_chars_which_is_maxlen"
        }
        with pytest.raises(HTTPException):
            response = self.accessor.store_inference_result(self.session, test_inference_res_data)
            self.assertIn("Invalid data provided: ", response.description)

    def test_list_inference_results(self):
        result_data = self.accessor.list_inference_results(self.session, "fake-wf-id")
        self.assertEqual(len(result_data), 2)
        self.assertEqual(result_data[0].captureId, "5c602574fa5e450c820df6f9b5af8c2f")
        self.assertEqual(result_data[1].captureId, "d38dbdf7b44c406eae81c34e070b2ea3")

    def test_list_input_images_path_for_retrain_get_most_confusing_inference(self):
        test_inference_res_data = {
            "captureId": "324145",
            "captureType": "Inference",
            "workflowId": "fake-wf-id",
            "inferenceCreationTime": 123456,
            "prediction": "Normal",
            "confidence": 0.8370160460472107,
            "anomalyScore": 0.9029839539527893,
            "anomalyThreshod": 0.916666567325592,
            "inputImageFilePath": "path-324145",
            "outputImageFilePath": "path",
            "modelId": "model-123",
            "modelName": "fake-model",
            "flagForReview": False,
            "downloaded": False
        }
        p_key = self.accessor.store_inference_result(self.session, test_inference_res_data)
        query_data = {
            "inputImageLimit": 1
        }
        result_data = self.accessor.list_inference_result_data_for_retraining(self.session, "fake-wf-id", query_data)
        self.assertEqual(len(result_data), 1)
        self.assertEqual(result_data[0], {
            "inputImageFilePath": "path-324145",
            "prediction": "Normal",
            "confidence": 0.8370160460472107,
            "inferenceCreationTime": 123456,
            "humanClassification": None,
            "textNote": None
        })

    def test_list_input_images_path_for_retrain_prediction_filter(self):
        query_data0 = {
            "inputImageLimit": 10,
            "predictionResult": "Anomaly"
        }
        result_data0 = self.accessor.list_inference_result_data_for_retraining(self.session, "fake-wf-id", query_data0)
        self.assertEqual(len(result_data0), 1)
        self.assertEqual(result_data0[0], {
            "inputImageFilePath": "foo/bar.jpg",
            "prediction": "Anomaly",
            "confidence": 0.8370160460472107,
            "inferenceCreationTime": 12345,
            "humanClassification": "Anomaly",
            "textNote": "this_text_note_is_exactly_50_chars_which_is_maxlen"
        })

        query_data1 = {
            "inputImageLimit": 10,
            "predictionResult": "Normal"
        }
        result_data1 = self.accessor.list_inference_result_data_for_retraining(self.session, "fake-wf-id", query_data1)
        self.assertEqual(len(result_data1), 1)
        self.assertEqual(result_data1[0], {
            "inputImageFilePath": "hi.jpg",
            "prediction": "Normal",
            "confidence": 0.8370160460472107,
            "inferenceCreationTime": 12345,
            "humanClassification": None,
            "textNote": None
        })

    def test_input_images_path_from_capture_id_list_happy_path(self):
        query_data = ["5c602574fa5e450c820df6f9b5af8c2f", "d38dbdf7b44c406eae81c34e070b2ea3"]
        result_data = self.accessor.list_inference_result_data_from_capture_id_list(self.session, query_data)
        self.assertEqual(result_data, [{
            "inputImageFilePath": self.test_inference_res_data0["inputImageFilePath"],
            "prediction": self.test_inference_res_data0["prediction"],
            "confidence": self.test_inference_res_data0["confidence"],
            "inferenceCreationTime": self.test_inference_res_data0["inferenceCreationTime"],
            "humanClassification": None,
            "textNote": None
        }, {
            "inputImageFilePath": self.test_inference_res_data1["inputImageFilePath"],
            "prediction": self.test_inference_res_data1["prediction"],
            "confidence": self.test_inference_res_data1["confidence"],
            "inferenceCreationTime": self.test_inference_res_data1["inferenceCreationTime"],
            "humanClassification": self.test_inference_res_data1["humanClassification"],
            "textNote": self.test_inference_res_data1["textNote"]
        }])

    def test_input_images_path_from_capture_id_list_non_exist(self):
        query_data = ["non-exist-id"]
        result_data = self.accessor.list_inference_result_data_from_capture_id_list(self.session, query_data)
        self.assertEqual(result_data, [])

    def test_get_inference_result_summary(self):
        result_data = self.accessor.get_inference_result_summary(self.session, "fake-wf-id", 0)
        summary = {'totalInference': 2, 'normal': 1, 'anomaly': 1}
        self.assertEqual(result_data['stats'], summary)
        self.assertEqual(result_data['lastResetTime'], 0)

    def test_delete_inference_result(self):
        from dao.sqlite_db.models import InferenceResult
        self.test_inference_res_delete = copy.deepcopy(self.test_inference_res_data0)
        self.test_inference_res_delete["captureId"] = "b03a8eb5ac1f4ac09554ed5f3557cc05"
        self.session.add(InferenceResult(**self.test_inference_res_delete))
        self.session.commit()

        p_key = "b03a8eb5ac1f4ac09554ed5f3557cc05"
        result_data = self.session.get(InferenceResult, p_key)
        self.assertEqual(result_data.captureId, p_key)

        self.accessor.delete_inference_result_by_capture_id(self.session, p_key)
        result_data_after_delete = self.session.get(InferenceResult, p_key)
        self.assertIsNone(result_data_after_delete)

    def test_update_inference_results_happy_path(self):
        from dao.sqlite_db.models import InferenceResult
        # Verify original entries before modification for safety
        result_data0 = self.session.get(InferenceResult, "5c602574fa5e450c820df6f9b5af8c2f")
        self.assertEqual(result_data0.flagForReview, False)
        self.assertEqual(result_data0.downloaded, False)
        self.assertEqual(result_data0.humanClassification, None)
        self.assertEqual(result_data0.textNote, None)

        result_data1 = self.session.get(InferenceResult, "d38dbdf7b44c406eae81c34e070b2ea3")
        self.assertEqual(result_data1.flagForReview, False)
        self.assertEqual(result_data1.downloaded, False)
        self.assertEqual(result_data1.humanClassification, "Anomaly")
        self.assertEqual(result_data1.textNote, "this_text_note_is_exactly_50_chars_which_is_maxlen")

        # Update the entries using some combination of optional parameters
        test_update_data = [{"captureId": "5c602574fa5e450c820df6f9b5af8c2f", "flagForReview": True, "downloaded": True},
                            {"captureId": "d38dbdf7b44c406eae81c34e070b2ea3", "humanClassification": "Normal"}]
        update_res = self.accessor.update_inference_results(self.session, test_update_data)
        # order doesn't matter
        self.assertCountEqual(update_res, ["5c602574fa5e450c820df6f9b5af8c2f", "d38dbdf7b44c406eae81c34e070b2ea3"])

        result_data0 = self.session.get(InferenceResult, "5c602574fa5e450c820df6f9b5af8c2f")
        self.assertEqual(result_data0.flagForReview, True)
        self.assertEqual(result_data0.downloaded, True)
        self.assertEqual(result_data0.humanClassification, None)
        self.assertEqual(result_data0.textNote, None)

        result_data1 = self.session.get(InferenceResult, "d38dbdf7b44c406eae81c34e070b2ea3")
        self.assertEqual(result_data1.flagForReview, False)
        self.assertEqual(result_data1.downloaded, False)
        self.assertEqual(result_data1.humanClassification, "Normal")
        self.assertEqual(result_data1.textNote, "this_text_note_is_exactly_50_chars_which_is_maxlen")

        # Update the entries using some combination of optional parameters, and test clearing
        test_update_data = [{"captureId": "5c602574fa5e450c820df6f9b5af8c2f", "humanClassification": "Anomaly", "textNote": "hello there"},
                            {"captureId": "d38dbdf7b44c406eae81c34e070b2ea3", "flagForReview": True, "downloaded": True, "humanClassification": None, "textNote": None}]
        update_res = self.accessor.update_inference_results(self.session, test_update_data)
        # order doesn't matter
        self.assertCountEqual(update_res, ["5c602574fa5e450c820df6f9b5af8c2f", "d38dbdf7b44c406eae81c34e070b2ea3"])

        result_data0 = self.session.get(InferenceResult, "5c602574fa5e450c820df6f9b5af8c2f")
        self.assertEqual(result_data0.flagForReview, True)
        self.assertEqual(result_data0.downloaded, True)
        self.assertEqual(result_data0.humanClassification, "Anomaly")
        self.assertEqual(result_data0.textNote, "hello there")

        result_data1 = self.session.get(InferenceResult, "d38dbdf7b44c406eae81c34e070b2ea3")
        self.assertEqual(result_data1.flagForReview, True)
        self.assertEqual(result_data1.downloaded, True)
        self.assertEqual(result_data1.humanClassification, None)
        self.assertEqual(result_data1.textNote, None)

    def test_update_inference_results_no_primary_key(self):
        test_update_data = [{"humanClassification": "Anomaly"}]
        # I would use with InvalidRequestError, but seems like it doesn't implement __enter__.
        try:
            self.accessor.update_inference_results(self.session, test_update_data)
        except InvalidRequestError:
            pass
        else:
            assert False

    def test_list_inference_result_data_by_capture_id_prefix(self):
        result_data = self.accessor.list_inference_result_data_with_capture_task_id(self.session, "5c60257")
        self.assertEqual(len(result_data), 1)