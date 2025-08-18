#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
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
from typing import List
from marshmallow import ValidationError
from fastapi import HTTPException
import time

from dao.sqlite_db import inference_result_dao
from model.inference_result import CapturedDataSchema, InferenceResultSchema
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND
from utils import utils, constants

import logging
logger = logging.getLogger(__name__)

class InferenceResultAccessor:
    def __init__(self):
        self.primary_key = "captureId"
        self.schema = InferenceResultSchema()
        self.capture_schema = CapturedDataSchema()

    def store_inference_result(self, db: Session, data: dict) -> str:
        """
        Store inference result in metadata db

        :param data: The inference result.
        :return: Capture id.
        """
        try:
            capture_id = data[self.primary_key]
            result = self.schema.load(data)

            inference_result_dao.store_inference_result(db, self.schema.dump(result))
            logger.info("Stored inference result with capture id:" + str(capture_id))

            return getattr(result, self.primary_key)

        except ValidationError as err:
            logger.error(err.messages)
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="Unable to store inference result. {}'.".format(
                    err.messages
                ),
            )

    def store_captured_data(self, db: Session, data: dict) -> str:
        """
        Store captured data in metadata db

        :param data: Captured data.
        :return: Capture id.
        """
        try:
            capture_id = data[self.primary_key]
            result = self.capture_schema.load(data)

            inference_result_dao.store_inference_result(db, self.capture_schema.dump(result))
            logger.info("Stored capture data with capture id: " + str(capture_id))

            return getattr(result, self.primary_key)

        except ValidationError as err:
            logger.error(err.messages)
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="Unable to store capture data. {}'.".format(
                    err.messages
                ),
            )

    def list_inference_results(self, db: Session, workflow_id: str):
        return inference_result_dao.list_inference_results_by_workflow_id(db=db, workflow_id=workflow_id)
    
    def list_inference_result_data_with_capture_task_id(self, db: Session, capture_task_id: str):
        return inference_result_dao.list_inference_results_by_capture_task_id(db=db, capture_task_id=capture_task_id)

    def list_inference_result_data_from_capture_id_list(self, db: Session, capture_id_list: List[str]):
        # Convert the list of tuples to a list of dicts for JSON compatibility
        inference_result_list_tuples = inference_result_dao.list_inference_result_data_by_capture_id_list(db=db, capture_id_list=capture_id_list)
        return [{"inputImageFilePath": inputImageFilePath,
                 "prediction": prediction,
                 "confidence": confidence,
                 "inferenceCreationTime": inferenceCreationTime,
                 "humanClassification": humanClassification,
                 "textNote": textNote
                 } for inputImageFilePath, prediction, confidence, inferenceCreationTime, humanClassification, textNote in inference_result_list_tuples]
    
    def list_inference_result_data_for_retraining(self, db: Session, workflow_id: str, data: dict):
        start_time_range = data.get("startTime", 0)
        end_time_range = data.get("endTime", int(time.time() * 1000))
        model_id = data.get("modelId")
        input_image_limit = data.get("inputImageLimit")
        prediction_res = data.get("predictionResult")
        if prediction_res and prediction_res not in constants.PREDICTION:
            logger.info("Unsupported prediction type {} to filter.".format(prediction_res))
            prediction_res = None

        inference_result_list_tuples = inference_result_dao.list_inference_result_data(
            db, workflow_id, start_time_range, end_time_range, model_id, input_image_limit, prediction_res)
    
        # Convert the list of tuples to a list of dicts for JSON compatibility
        return [{"inputImageFilePath": inputImageFilePath,
                 "prediction": prediction,
                 "confidence": confidence,
                 "inferenceCreationTime": inferenceCreationTime,
                 "humanClassification": humanClassification,
                 "textNote": textNote
                 } for inputImageFilePath, prediction, confidence, inferenceCreationTime, humanClassification, textNote in inference_result_list_tuples]


    def get_inference_result(self, db: Session, capture_id: str):
        return inference_result_dao.get_inference_result_by_capture_id(db=db, capture_id=capture_id)

    def get_inference_result_summary(self, db: Session, workflow_id: str, summaryStartTime: int):
        summary = inference_result_dao.get_inference_result_summary(db, workflow_id, summaryStartTime)
        return {"stats": summary, "lastResetTime": summaryStartTime}
    
    def delete_inference_result_by_capture_id(self, db: Session, capture_id: str):
        # TODO: batch deletion and delete files on disk
        try:
            inference_res = inference_result_dao.get_inference_result_by_capture_id(db, capture_id)
            inference_result_dao.delete_inference_result_by_capture_id(db, capture_id)            
        except ValueError as err:
            logger.error(err)
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail=f"The server can't delete the inference result. Error: 'The inference result {id} doesn't exist'. Check the capture ID and try again.",
            )
        
    def update_inference_results(self, db: Session, inference_result_list):
        logger.info(f"Bulk update inference result: {inference_result_list}")
        try:
            inference_result_dao.bulk_update_inference_result(db, inference_result_list)
        except StaleDataError as err:
            # sqlalchemy StaleDataError indicates primary key in list cannot be found in DB
            logger.error(err)
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Unable to bulk update inference results. Error: 'Inference result doesn't exist'. Check the capture ID and try again.",
            )
        return list(inf.get("captureId") for inf in inference_result_list)