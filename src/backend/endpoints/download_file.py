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
import io
import json
import os
import zipfile
import time
from datetime import datetime
from fastapi import APIRouter, Depends, Path, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List
from starlette.responses import FileResponse
from fastapi.responses import StreamingResponse
from typing_extensions import Annotated
from sqlalchemy.orm import Session
from dao.sqlite_db.sqlite_db_operations import SessionLocal
from starlette.status import HTTP_404_NOT_FOUND

# Custom Modules
from utils.auth import validate_token
from utils.constants import SNAPSHOT_FILE_PATTERN, PREDICTION, CAPTURED_IDS_PATH_PATTERN, DDA_SYSTEM_FOLDER
from utils import utils, inference_results_utils
import logging
from model.workflow import Workflow
from utils.server_setup import workflow_accessor, inference_result_accessor
from utils.get_is_triton import get_is_triton

unauthenticated_router = APIRouter()

logger = logging.getLogger(__name__)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Following function is to handle token added in url parameter 
def validate_token_in_query_param(token: str):
    if utils.is_authorization_enabled_on_station():
        # Validate token only if auth is enabled on station
        validate_token(token)


### Following APIs will need separate Auth check instead of in API router, 
### Since download opens a new link, and would require token to send as url parameter.
### All APIs returning downloadable object will need separate Auth check.
@unauthenticated_router.get("/snapshotfile/{fileName}")
def get_snapshotfile(
    fileName: Annotated[
        str,
        Path(title="File Name of Snapshot", pattern=SNAPSHOT_FILE_PATTERN),
    ],
    token: str = None
):
    validate_token_in_query_param(token)
    
    # Sanitize filename to prevent path traversal
    safe_filename = os.path.basename(fileName)
    if safe_filename != fileName or '..' in fileName:
        raise HTTPException(
            status_code=400,
            detail="Invalid filename"
        )
    
    file_path = os.path.join(DDA_SYSTEM_FOLDER, safe_filename)
    
    # Ensure the resolved path is within the allowed directory
    if not os.path.abspath(file_path).startswith(os.path.abspath(DDA_SYSTEM_FOLDER)):
        raise HTTPException(
            status_code=400,
            detail="Access denied"
        )
    
    if os.path.exists(file_path):
        return FileResponse(file_path)

    raise HTTPException(
        status_code=HTTP_404_NOT_FOUND,
        detail=f"The server can't get the snapshot file. Error: 'The path {file_path} couldn't be found'. Check the path and try again.",
    )

@unauthenticated_router.get("/workflows/{workflowId}/capture-details/{captureId}/input-image")
def load_input_image_from_worflow_by_capture_id(workflowId: str, captureId: str, token: str = None, db: Session = Depends(get_db)):
    validate_token_in_query_param(token)
    # Retrieve input image from output path, image might arrive later then results.
    image_path = ""
    if get_is_triton():
        # Triton jsonl file writes are faster than DB writes
        # As DB writes are done after Gstreamer pipeline is fully completed
        # So DB entry is not garanteed be there when this API gets called from UI

        capture_details = inference_result_accessor.get_inference_result(db, captureId)
        if capture_details is None:
            logger.info("Getting image from jsonl file")
            jsonl_file = f"/aws_dda/inference-results/{workflowId}/{captureId}.jsonl"
            try:
                with open(jsonl_file, 'r') as f:
                    json_str = f.read()
                    json_dict = json.loads(json_str)
                    image_path = json_dict["deviceGroundTruthData"][0]["source-ref"]
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Server cannot find the input image path, Error {e}.",
                )
        else:
            image_path = capture_details.inputImageFilePath
    else:
        for _ in range(20):
            capture_details = inference_result_accessor.get_inference_result(db, captureId)
            if capture_details is None:
                logger.info("wait for image...")
                time.sleep(0.1)
            else:
                image_path = capture_details.inputImageFilePath
                break
    if image_path and os.path.exists(image_path):
        logger.info("image exist")
        return FileResponse(image_path, media_type="image/jpg")

    raise HTTPException(
        status_code=HTTP_404_NOT_FOUND,
        detail=f"Server unable to load input image for Workflow id: {workflowId} and Capture id: {captureId}. Error: 'Image not found'. Check workflow id and try again",
    )


@unauthenticated_router.get("/workflows/{workflowId}/capture-details/{captureId}/output-image")
def load_output_image_from_worflow_by_capture_id(workflowId: str, captureId: str, token: str = None, db: Session = Depends(get_db)):
    validate_token_in_query_param(token)
    
    workflow: Workflow = workflow_accessor.get_workflow_by_id(workflowId, db)
    output_path = workflow.get("workflowOutputPath")
    query = inference_results_utils.GetInferenceResults(output_path, None, 0, 1)
    capture_details = query.get_infer_res_with_capture_id(
        capture_id=captureId, workflow_output_path=output_path
    )
    image_path = capture_details.get("imageDataFilePath")
    if image_path and os.path.exists(image_path):
        logger.info("image exist")
        return FileResponse(image_path, media_type="image/jpg")

    raise HTTPException(
        status_code=HTTP_404_NOT_FOUND,
        detail=f"Server unable to load output image for Workflow id: {workflowId} and Capture id: {captureId}. Error: 'Image not found'. Check error message and try again",
    )

class RetrainInputImagesRequest(BaseModel):
    startTime: Optional[int] = None
    endTime: Optional[int] = None
    modelId: Optional[str] = None
    inputImageLimit: Optional[int] = None
    predictionResult: Optional[str] = None
    captureIdPath: Optional[str] = None

 
@unauthenticated_router.get("/workflows/{workflow_id}/results/export")
def get_inference_result_data_for_retraining(
    workflow_id: str,
    startTime: int = None,
    endTime: int = None,
    modelId: str = None,
    inputImageLimit: int = 10,
    predictionResult: str = None,
    captureIdPath: str = Query(default=None, pattern=CAPTURED_IDS_PATH_PATTERN),
    token: str = None,
    db: Session = Depends(get_db)):

    validate_token_in_query_param(token)

    inference_result_data_list = []

    # Special case for UI:
    # First, POST to /workflows/{workflow_id}/results/export, which writes out the data to a file on disk
    # Then, GET from /workflows/{workflow_id}/results/export (here), which loads the data from said file on disk
    if captureIdPath:
        if not os.path.exists(captureIdPath):
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail=f"The server can't get the capture id file. Error: 'The path {captureIdPath} couldn't be found'. Check the path and try again.",
            )
        with open(captureIdPath) as json_file:
            inference_result_data_list = json.load(json_file)
    else:
        # Regular case, query the data ourselves
        request = RetrainInputImagesRequest(startTime=startTime, endTime=endTime, modelId=modelId, 
                                        inputImageLimit=inputImageLimit, predictionResult=predictionResult)
        data = request.dict(exclude_none=True)
        inference_result_data_list = inference_result_accessor.list_inference_result_data_for_retraining(db, workflow_id, data)

    if len(inference_result_data_list) < 1:
        raise HTTPException(
                status_code=442,
                detail=f"The server can't find any inference results from the query. Check filters and try again",
            )

    now = datetime.now()
    manifest_data = inference_results_utils.generate_smgt_format_manifest(inference_result_data_list)
    manifest_file_name = "manifest-{}".format(now.strftime("%Y-%m-%d-%H-%M-%S"))
    manifest_file_path = os.path.join(DDA_SYSTEM_FOLDER, manifest_file_name)
    with open(manifest_file_path, "w") as json_file:
        json.dump(manifest_data, json_file, indent=4)

    if predictionResult in PREDICTION:
        # Only prefix the zip file name with prediction type if the contents have guaranteed prediction results (we filtered)
        zip_filename = "{}-images-{}-{}.zip".format(predictionResult, workflow_id, now.strftime("%Y-%m-%d-%H-%M-%S"))
    else:
        # We cannot guarantee if the contents are all Normal, all Anomaly, or a mix of both
        # We could search all of the selected images to figure this out, but the performance hit doesn't seem worth it just for a zip file name
        zip_filename = "images-{}-{}.zip".format(workflow_id, now.strftime("%Y-%m-%d-%H-%M-%S"))
    zip_io = io.BytesIO()
    with zipfile.ZipFile(zip_io, mode='w', compression=zipfile.ZIP_DEFLATED) as temp_zip:
        temp_zip.write(manifest_file_path, "manifest")
        for entry in inference_result_data_list:
            temp_zip.write(entry["inputImageFilePath"], os.path.basename(entry["inputImageFilePath"]))
    return StreamingResponse(
        iter([zip_io.getvalue()]), 
        media_type="application/x-zip-compressed", 
        headers = { "Content-Disposition": f"attachment; filename={zip_filename}"}
    )
