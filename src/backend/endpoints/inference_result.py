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
# System Modules
import json
import os
from datetime import datetime
from typing import List, Literal, Union
from sqlalchemy import or_
from sqlalchemy.orm import Session
from pathlib import Path
from dao.sqlite_db.sqlite_db_operations import SessionLocal
from dao.sqlite_db.models import InferenceResult

# Fast api
from fastapi import Depends, HTTPException, Query
from typing_extensions import Annotated

# Custom Modules
from utils.server_setup import workflow_accessor, inference_result_accessor, workflow_metadata_accessor
from utils.constants import ANOMALY, INFERENCE, INFERENCE_RESULT_MAX_DOWNLOAD, DDA_SYSTEM_FOLDER, DB_TEXT_NOTE_MAX_LENGTH, NORMAL, CAPTURE
from data_models.common import InferenceResultHistoryModel
from starlette.status import HTTP_413_REQUEST_ENTITY_TOO_LARGE, HTTP_404_NOT_FOUND
import logging
logger = logging.getLogger(__name__)
from resources.pagination.base_paginator import PageParams, paginate
from pydantic import BaseModel, Field
from typing import Optional
from endpoints.route.access_log_router import get_api_router
from utils import constants

router = get_api_router()

# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/workflows/{workflow_id}/results/summary")
def get_inference_result_summary(workflow_id: str, db: Session = Depends(get_db)):
    workflow_metadata = workflow_metadata_accessor.get_workflow_metadata(db, workflow_id)
    return inference_result_accessor.get_inference_result_summary(db, workflow_id, workflow_metadata['summaryStartTime'])


class ResetInferenceResultSummaryTimeRequest(BaseModel):
    resetTime: Optional[int] = None

@router.post("/workflows/{workflow_id}/results/reset")
def reset_inference_result_summary_start_time(workflow_id: str, request: ResetInferenceResultSummaryTimeRequest = ResetInferenceResultSummaryTimeRequest(), db: Session = Depends(get_db)):
    workflow_metadata_entry = {'workflowId': workflow_id, 'summaryStartTime': request.resetTime}
    return workflow_metadata_accessor.update_workflow_metadata(db, workflow_metadata_entry)


@router.get("/workflows/{workflow_id}/results")
def list_inference_results(
    workflow_id: str,
    prediction: Optional[Literal[NORMAL, ANOMALY]] = None,
    downloaded: Optional[bool] = None,
    textNoteFilter: Annotated[Union[str, None], Query(max_length=DB_TEXT_NOTE_MAX_LENGTH)] = None,
    humanClassificationProvided: Optional[bool] = None,
    humanReviewRequired: Optional[bool] = None,
    captureType: Optional[Literal[INFERENCE, CAPTURE]] = None,
    page_params: PageParams = Depends(),
    db: Session = Depends(get_db)
    ):
    workflow_accessor.get_workflow_by_id(workflow_id, db)
    query = db.query(InferenceResult).filter(InferenceResult.workflowId == workflow_id).order_by(InferenceResult.inferenceCreationTime.desc())
    # Query would add up depends on how many filters passing in
    if prediction:
        query = query.filter(InferenceResult.prediction == prediction)
    if downloaded is not None:
        query = query.filter(InferenceResult.downloaded == downloaded)
    if textNoteFilter and not textNoteFilter.isspace():
        query = query.filter(InferenceResult.textNote.contains(textNoteFilter))
    if humanClassificationProvided is not None:
        # NOTE: CAN'T use {xx is not None} here, it will just be True instead of filter expression
        if humanClassificationProvided:
            query = query.filter(InferenceResult.humanClassification != None)
        else:
            query = query.filter(InferenceResult.humanClassification == None)
    if humanReviewRequired is not None:
        # For null field, filter as humanReviewRequired is false, doesn't need human review
        if humanReviewRequired:
            query = query.filter(InferenceResult.humanReviewRequired == humanReviewRequired)
        else:
            query = query.filter(or_(InferenceResult.humanReviewRequired == humanReviewRequired, InferenceResult.humanReviewRequired == None))
    if captureType is not None:
        query = query.filter(InferenceResult.captureType == captureType)

    return paginate(page_params, query, InferenceResultHistoryModel)

@router.get("/workflows/{workflow_id}/results/{capture_id}")
def get_inference_result(
    workflow_id: str,
    capture_id: str,
    db: Session = Depends(get_db)
    ):
    # Check workflow id for validity
    workflow_accessor.get_workflow_by_id(workflow_id, db)

    result = inference_result_accessor.get_inference_result(db, capture_id)
    # Check capture id for validity
    if not result:
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=f"Capture id {capture_id} not found",
        )

    return result

@router.delete("/workflows/{workflow_id}/results/{capture_id}")
def delete_inference_result_by_capture_id(workflow_id: str, capture_id: str, db: Session = Depends(get_db)):
    inference_result_accessor.delete_inference_result_by_capture_id(db, capture_id)


class EditInferenceResult(BaseModel):
    captureId: str
    flagForReview: Optional[bool] = None
    downloaded: Optional[bool] = None
    humanClassification: Optional[Literal["Normal", "Anomaly"]] = None
    textNote: Optional[str] = Field(None, max_length = DB_TEXT_NOTE_MAX_LENGTH)

class UpdateInferenceResultsRequest(BaseModel):
    inferenceResults: List[EditInferenceResult]

@router.patch("/workflows/{workflow_id}/results")
def update_inference_results(workflow_id: str, request: UpdateInferenceResultsRequest, db: Session = Depends(get_db)):
    return inference_result_accessor.update_inference_results(db, request.dict(exclude_unset=True).get("inferenceResults"))


class DownloadInferenceResultsRequest(BaseModel):
    captureIds: List[str]

class DownloadInferenceResultsResponse(BaseModel):
    captureIdPath: str

@router.post("/workflows/{workflow_id}/results/export")
def save_capture_ids_for_inference_results_download(
    workflow_id: str,
    request: DownloadInferenceResultsRequest,
    db: Session = Depends(get_db)
    ) -> DownloadInferenceResultsResponse:

    capture_id_list = request.dict(exclude_unset=True).get("captureIds")
    if len(capture_id_list) > INFERENCE_RESULT_MAX_DOWNLOAD:
        raise HTTPException(
                status_code=HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"You cannot download more than {INFERENCE_RESULT_MAX_DOWNLOAD} images at a time",
            )

    inference_result_data_list = inference_result_accessor.list_inference_result_data_from_capture_id_list(db, capture_id_list)

    now = datetime.now()
    capture_id_path_file = "capture-id-path-{}".format(now.strftime("%Y-%m-%d-%H-%M-%S"))
    capture_id_path = os.path.join(DDA_SYSTEM_FOLDER, capture_id_path_file)
    # Create /aws_dda/system folder if not exists
    Path(DDA_SYSTEM_FOLDER).mkdir(parents=True, exist_ok=True)

    with open(capture_id_path, "w") as json_file:
        json.dump(inference_result_data_list, json_file)
    return DownloadInferenceResultsResponse(captureIdPath=capture_id_path)
