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

from sqlalchemy.orm import Session
from sqlalchemy import select, func, between, update
from typing import List

from data_models.common import InferenceResultHistoryModel
from .models import InferenceResult
from utils.constants import ANOMALY, NORMAL


def get_inference_result_by_capture_id(db: Session, capture_id: str):
    return db.get(InferenceResult, capture_id)

def list_inference_results_by_workflow_id(db: Session, workflow_id: str):
    return db.scalars(select(InferenceResult).filter_by(workflowId=workflow_id)).all()

def list_inference_results_by_capture_task_id(db: Session, capture_task_id: str):
    stmt = (
        select(InferenceResult.inputImageFilePath)
        .where(InferenceResult.captureId.startswith(capture_task_id))
    )
    return db.execute(stmt).all()

def list_inference_result_data_by_capture_id_list(db: Session, capture_id_list: List[str]):
    stmt = (
        select(InferenceResult.inputImageFilePath, InferenceResult.prediction, InferenceResult.confidence, InferenceResult.inferenceCreationTime, InferenceResult.humanClassification, InferenceResult.textNote)
        .where(InferenceResult.captureId.in_(capture_id_list))
    )
    return db.execute(stmt).all()

def list_inference_result_data(
        db: Session, workflow_id: str, start_time: int, end_time: int,
        model_id: str, input_image_limit: int, prediction_res: str):
    # TODO: add filter for model id
    if prediction_res:
        stmt = (
            select(InferenceResult.inputImageFilePath, InferenceResult.prediction, InferenceResult.confidence, InferenceResult.inferenceCreationTime, InferenceResult.humanClassification, InferenceResult.textNote)
            .where(InferenceResult.workflowId == workflow_id)
            .where(InferenceResult.prediction == prediction_res)
            .where(between(InferenceResult.inferenceCreationTime, start_time, end_time))
            .order_by(func.abs(InferenceResult.anomalyScore - InferenceResult.anomalyThreshod))
            .limit(input_image_limit)
        )
    else:
        stmt = (
            select(InferenceResult.inputImageFilePath, InferenceResult.prediction, InferenceResult.confidence, InferenceResult.inferenceCreationTime, InferenceResult.humanClassification, InferenceResult.textNote)
            .where(InferenceResult.workflowId == workflow_id)
            .where(between(InferenceResult.inferenceCreationTime, start_time, end_time))
            .order_by(func.abs(InferenceResult.anomalyScore - InferenceResult.anomalyThreshod))
            .limit(input_image_limit)
        )
    return db.execute(stmt).all()

def store_inference_result(db: Session, inference_res):
    db_inference_res = InferenceResult(**inference_res)
    db.add(db_inference_res)
    db.commit()
    db.refresh(db_inference_res)
    return db_inference_res

def delete_inference_result_by_capture_id(db: Session, capture_id: str):
    inference_result = db.get(InferenceResult, capture_id)
    if not inference_result:
        raise ValueError(f"Inference Result with id {capture_id} does not exist.")
    db.delete(inference_result)
    db.commit()

def bulk_update_inference_result(db: Session, inference_result_list: List[InferenceResultHistoryModel]):
    db.execute(update(InferenceResult), inference_result_list)
    db.commit()

def get_inference_result_summary(db: Session, workflow_id, summaryStartTime):
    stmt2 = (
        select(func.count())
        .where(InferenceResult.workflowId == workflow_id)
        .where(InferenceResult.inferenceCreationTime >= summaryStartTime)
        .where(InferenceResult.prediction == NORMAL)
    )
    stmt3 = (
        select(func.count())
        .where(InferenceResult.workflowId == workflow_id)
        .where(InferenceResult.inferenceCreationTime >= summaryStartTime)
        .where(InferenceResult.prediction == ANOMALY)
    )
    normal_count = db.scalar(stmt2)
    anomaly_count = db.scalar(stmt3)
    
    return {"totalInference": normal_count + anomaly_count, "normal": normal_count, "anomaly": anomaly_count}
