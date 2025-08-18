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
import os
from token import OP
from typing import List
import time
from utils.capture_task_manager import CaptureTaskParameters
from utils import utils
from sqlalchemy.orm import Session
from dao.sqlite_db.sqlite_db_operations import SessionLocal

# Fast api
from fastapi import HTTPException, APIRouter, Query, Depends, BackgroundTasks, Depends
from starlette.status import HTTP_400_BAD_REQUEST

# Custom Modules
from utils import inference_results_utils
from utils.constants import ANOMALY, FRAME_CAPTURE_TIMESTAMP, MAX_IMAGE_CAPTURE_COUNT, MAX_IMAGE_CAPTURE_TIME_INTERVAL, MIN_IMAGE_CAPTURE_COUNT, MIN_IMAGE_CAPTURE_TIME_INTERVAL, NORMAL, TRIGGER_TIMESTAMP, INFERENCE_RECEIVED_TIMESTAMP, CAPTURE, INFERENCE
from utils.server_setup import workflow_accessor, latency_time_accessor, gst_pipeline_executor, inference_result_accessor, image_source_accessor, capture_task_manager
from data_models.common import ImageSourceIdModel, WorkflowModel, RunWorkflowModel
import logging
logger = logging.getLogger(__name__)
from model.workflow import Workflow
from pydantic import BaseModel, validator, RootModel, Field
from typing import List, Optional
from metrics.collector import Timer
from utils.camera_manager import get_camera_frame
from utils.common import DIOProcessHealthStatusEnum
from resources.accessors.image_source_accessor import ImageSourceAccessor
from model.image_source import ImageSourceType
from metrics.latency_metrics import LatencyMetrics
from endpoints.route.access_log_router import get_api_router
from utils.server_setup import workflow_metadata_accessor
from utils import utils

router = get_api_router()

# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def validate_workflow_requirements(workflow):
    if not workflow.get("imageSources"):
        raise HTTPException(
            status_code=503,
            detail=f"Server cannot run workflow. Error: 'Workflow doesnt have imageSources configured'. Configure image sources for the workflow",
        )


class RunWorkflowRequest(BaseModel):
    captureImageCount: Optional[int] = Field(None, ge=MIN_IMAGE_CAPTURE_COUNT, le=MAX_IMAGE_CAPTURE_COUNT)
    captureTimeInterval: Optional[int] = Field(None, ge=MIN_IMAGE_CAPTURE_TIME_INTERVAL, le=MAX_IMAGE_CAPTURE_TIME_INTERVAL)
    capturePrefix: Optional[str] = None
    returnImageString: Optional[bool] = True
    # If specified, the value of returnImageString is ignored
    returnPartialResultsEarly: Optional[bool] = False


class RunWorkflowResponse(RunWorkflowModel):
    processingTime: Optional[float] = None


def get_frame(image_source_dict):
    camera_config = utils.convert_sqlalchemy_object_to_dict(image_source_dict.get("imageSourceConfiguration"))
    camera_id = image_source_dict.get('cameraId')
    try:
        return get_camera_frame(camera_id, camera_config)
    except Exception as err:
        raise HTTPException(
                status_code=500,
                detail=f"Server cannot fetch camera frame, Error {err}. Check camera connection and try again",
            )


def configure_image_source_and_run_pipeline(workflow:Workflow, db: Session, latency_metrics):
    image_source_accessor = ImageSourceAccessor()
    image_source_id = workflow.get('imageSourceId')
 
    image_source_db = image_source_accessor.get_image_source(image_source_id, db)
    image_source_dict = utils.convert_sqlalchemy_object_to_dict(image_source_db)

    if image_source_dict.get("type") == ImageSourceType.FOLDER:
        return gst_pipeline_executor.execute_workflow_pipeline(workflow, db, latency_metrics=latency_metrics)
    elif image_source_dict.get("type")  == ImageSourceType.CAMERA:
        frame = get_frame(image_source_dict)
        latency_metrics.add_timestamp(FRAME_CAPTURE_TIMESTAMP)
        return gst_pipeline_executor.execute_workflow_pipeline(workflow, db, frame, latency_metrics=latency_metrics)
    ## DD-18130: Add support for smart cameras
    elif image_source_dict.get("type") == ImageSourceType.ICAM:
        return gst_pipeline_executor.execute_workflow_pipeline(workflow, db, latency_metrics=latency_metrics)
    return "", {}

def save_full_inference_result(inference_result, workflow):
    with Timer(metric_name="InferenceResultStoringTime"):
        if inference_result.get("image"):
            del inference_result["image"]
        inference_result["captureType"] = INFERENCE
        db_inference_res = inference_results_utils.convert_inference_res_to_save_in_db(inference_result, workflow)
        with SessionLocal() as session:
            inference_result_accessor.store_inference_result(session, db_inference_res)

def read_full_results_and_save(workflow, db, trigger_timestamp, capture_id, latency_metrics):
    result = read_inference_result(workflow, capture_id)
    latency_metrics.commit_timestamps(db, result["captureId"])
    total_processing_time = (latency_metrics.get_timestamp(INFERENCE_RECEIVED_TIMESTAMP) - trigger_timestamp) * 1000
    result["processingTime"] = total_processing_time
    save_full_inference_result(result, workflow)

@router.post("/workflows/{workflow_id}/run")
async def run_inference_for_stream(
    workflow_id: str,
    background_tasks: BackgroundTasks,
    request: Optional[RunWorkflowRequest] = RunWorkflowRequest(returnImageString = True, returnPartialResultsEarly = False),
    db: Session = Depends(get_db)
    ):
    # TODO: redefine response model
    data = request.dict(exclude_unset=True)

    latency_metrics = LatencyMetrics()
    trigger_timestamp = latency_metrics.add_timestamp(TRIGGER_TIMESTAMP)

    workflow = workflow_accessor.get_workflow_by_id(workflow_id, db)
    validate_workflow_requirements(workflow)

    # Run inference if workflow configures a model
    if workflow.get("featureConfigurations"):
        with Timer(metric_name="WorkflowTotalTime"):
            capture_id, parsed_tags_dict = configure_image_source_and_run_pipeline(workflow, db, latency_metrics)
            if not data.get('returnPartialResultsEarly'):
                result = read_inference_result(workflow, capture_id)

        if data.get('returnPartialResultsEarly'):
            background_tasks.add_task(read_full_results_and_save, workflow, db, trigger_timestamp, capture_id, latency_metrics)
            return { "captureId": capture_id, "inferenceResult": { "confidence": parsed_tags_dict["confidence"], "inference_result": ANOMALY if parsed_tags_dict["is_anomalous"] else NORMAL } }
        else:
            latency_metrics.commit_timestamps(db, result["captureId"])
            total_processing_time = (latency_metrics.get_timestamp(INFERENCE_RECEIVED_TIMESTAMP) - trigger_timestamp) * 1000
            result["processingTime"] = total_processing_time

            # Background tasks to be run after returning response
            background_tasks.add_task(save_full_inference_result, result, workflow)

            # Delete image base64 string if request not to return it
            if not data.get("returnImageString", True):
                del result["image"]
        
            return result
    # Capture if workflow doesn't have a model
    else:
        # One workflow can only have one running capture task
        tasks = capture_task_manager.get_tasks()
        for task in tasks:
            if task.get("workflowId") == workflow_id:
                raise HTTPException(
                    status_code=HTTP_400_BAD_REQUEST,
                    detail=f"Server cannot start capture task. Error: 'This workflow already has capture task running'. Please wait for existing capture task to complete",
                )

        image_source_id = workflow.get("imageSources")[0].imageSourceId
        image_source = image_source_accessor.get_image_source(image_source_id, db)
        image_source_dict = utils.convert_sqlalchemy_object_to_dict(image_source)
        capture_task_id = utils.generate_capture_id(workflow.get('workflowId'))

        if image_source_dict.get("type") == ImageSourceType.FOLDER:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Server cannot start capture task for folder type image source",
            )

        # Add capture task
        image_count = data.get("captureImageCount")
        time_interval = data.get("captureTimeInterval") if image_count and image_count > 1 else 0.01
        if image_count and time_interval:
            capture_task_parameter = CaptureTaskParameters(
                capture_task_id=capture_task_id,
                interval=time_interval,
                count=image_count,
                workflow_id=workflow_id,
                workflow_output_path=workflow.get("workflowOutputPath"),
                image_source_dict=image_source_dict,
                capture_prefix=data.get("capturePrefix")
            )
            capture_task_manager.add_task(capture_task_parameter)
            return {"captureTaskId": capture_task_id}
        else:
            raise HTTPException(
                    status_code=HTTP_400_BAD_REQUEST,
                    detail=f"Please provide captureImageCount and captureTimeInterval to start capture task",
                )

def read_inference_result(workflow, inference_capture_id):
    for _ in range(20):
        try:
            with Timer(metric_name="InferenceResultProcessingTime"):
                query = inference_results_utils.GetInferenceResults(workflow.get('workflowId'), None, 0, 1)
                return query.get_infer_res_with_capture_id(inference_capture_id, workflow.get('workflowOutputPath'))
        except:
            logger.warning(f"File Not Found with inference capture id {inference_capture_id}")
            time.sleep(0.2)
            continue

@router.get("/workflows/{workflow_id}/capture-task")
def get_capture_task_status(workflow_id: str, db: Session = Depends(get_db)):
    # Workflow and capture task is one-to-one mapping
    tasks = capture_task_manager.get_tasks()
    for task in tasks:
        if task.get("workflowId") == workflow_id:
            capture_task_id = task.get("captureTaskId")
            captured_images = inference_result_accessor.list_inference_result_data_with_capture_task_id(db, capture_task_id)
            task["capturedCount"] = len(captured_images)
            return task

    logger.info(f"Server cannot find capture tasks associated with workflow {workflow_id}.")
    return {}


class ListWorkflowImagesResponse(BaseModel):
    images: List[RunWorkflowResponse]
    nextStartingPoint: Optional[int] = None


class SortMethodValidator(BaseModel):
    sort: str

    @validator("sort")
    def validate_sort(cls, sorting_method):
        if sorting_method not in ["desc", "asc"]:
            raise HTTPException(
                status_code=400,
                detail=f"The server can't get the analysis results from the workflow. Error: 'Invalid sorting method provided: '{sorting_method}'. Valid sorting methods are 'desc' or 'asc''. Check the error message and try again.",
            )


class StartingPointValueValidator(BaseModel):
    startingPoint: int

    @validator("startingPoint")
    def validate_starting_point(cls, starting_point_value):
        if starting_point_value < 0:
            raise HTTPException(
                status_code=400,
                detail=f"The server can't get the analysis results from the workflow. Error: 'Invalid starting point value provided: '{starting_point_value}'', Expected non-negative integer. Check the error message and try again.",
            )


class MaxNumberOfResultsValidator(BaseModel):
    maxResults: int

    @validator("maxResults")
    def validate_max_results(cls, max_results_value):
        if max_results_value <= 0 or max_results_value > 2:
            raise HTTPException(
                status_code=400,
                detail=f"The server can't get the analysis results from the workflow. Error: 'Invalid input for maxResults: '{max_results_value}', valid values are (0, 2]'. Check the error message and try again.",
            )
        return max_results_value


@router.get("/workflows/{workflow_id}/images")
def list_images(
    workflow_id: str,
    sort: str = Query(default="desc", description="Sorting order"),
    startingPoint: int = Query(default=0, description="Starting Point value"),
    maxResults: int = Query(default=2, description="Max Results value"),
    db: Session = Depends(get_db),
) -> ListWorkflowImagesResponse:
    SortMethodValidator(sort=sort)
    StartingPointValueValidator(startingPoint=startingPoint)
    MaxNumberOfResultsValidator(maxResults=maxResults)

    emagent_config = utils.get_em_agent_config_path_for_stream(workflow_id)
    if not os.path.isfile(emagent_config):
        workflow_config_file = emagent_config.split("/")[-1]
        raise HTTPException(
            status_code=404,
            detail=f"The server can't get the analysis results from the workflow {workflow_id}. Error: 'Unable to find emagent config file: '{workflow_config_file}''.  Check the error message and try again.",
        )

    query = inference_results_utils.GetInferenceResults(
        workflow_id, sort, startingPoint, maxResults
    )
    workflow = workflow_accessor.get_workflow_by_id(workflow_id, db)

    # get health status, throws an exception if the workflow state is not healthy
    health_status = workflow_accessor.check_workflow_health(workflow)
    # get inference results
    output_path = workflow.get("workflowOutputPath")
    result = query.get_inference_results(output_path)

    if not result.get("images"):
        ## if health_status is RUNNING and no images
        #      then throw an error as this is an unexpected state
        if health_status == DIOProcessHealthStatusEnum.RUNNING:
            raise HTTPException(
                status_code=500,
                detail=f"The server can't get the analysis results from the workflow {workflow_id}. \
                    Error: 'Unable to find output images in the inference results directory"
            )
        ## if health_status is STARTING and no images
        #      then return empty values
        if health_status == DIOProcessHealthStatusEnum.STARTING:
            return result
    ## else, image is available and health_status is either STARTING or RUNNING
    #      then show the last available image
        
    for image in result.get("images"):
        file_path = image["inferenceFilePath"]
        inference_capture_id = file_path[file_path.rfind('/') + 1: -6]
        
        trigger_timestamp = latency_time_accessor.get_latency_time(db, inference_capture_id, TRIGGER_TIMESTAMP)
        inference_recieved_timestamp = latency_time_accessor.get_latency_time(db, inference_capture_id, INFERENCE_RECEIVED_TIMESTAMP)
        if trigger_timestamp and inference_recieved_timestamp:
            image["processingTime"] = (inference_recieved_timestamp - trigger_timestamp) * 1000
    return result


class GetWorkflowResponse(RootModel):
    root: WorkflowModel


class ListWorkflowResponse(RootModel):
    root: List[WorkflowModel]


@router.get("/workflows")
def list_workflows(db: Session = Depends(get_db), cameraId: str = Query(default=None, description="Camera Id")):
    workflows = []
    if cameraId:
        workflows = workflow_accessor.list_workflows_by_camera(cameraId, db)
    else: 
        workflows = workflow_accessor.list_workflows_with_image_sources(db)

    for workflow in workflows:
        image_sources = workflow.get("imageSources")
        if image_sources:
            workflow["imageSources"] = image_source_accessor.update_all_image_sources_with_camera_status(image_sources)
    return workflows


@router.get("/workflows/{workflowId}")
def get_workflow(workflowId: str, db: Session = Depends(get_db)):
    workflow = workflow_accessor.get_workflow_with_default_config(workflowId, db)
    image_sources = workflow.get("imageSources")
    if image_sources:
        workflow["imageSources"] = image_source_accessor.update_all_image_sources_with_camera_status(image_sources)
    return workflow


@router.post("/workflows")
def create_workflow(db: Session = Depends(get_db)):
    random_uuid = str(utils.gen_uuid())
    response = workflow_accessor.create_workflow({"workflowId": random_uuid}, db)
    # Add workflow to workflow metadata table 
    with SessionLocal() as metadata_session:
        workflow_metadata_entry = {"workflowId": random_uuid, "summaryStartTime": int(time.time())}
        workflow_metadata_accessor.create_workflow_metadata(db = metadata_session, data= workflow_metadata_entry)
    return response

@router.delete("/workflows/{workflow_id}")
def delete_workflow(workflow_id: str, db: Session = Depends(get_db)):
    return workflow_accessor.delete_workflow(workflow_id, db)

@router.get("/workflows/{workflow_id}/retry")
def retry_dio_workflow(workflow_id: str, db: Session = Depends(get_db)):
    return workflow_accessor.retry_dio_workflow(workflow_id, db)


class ModifyWorkflowRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    inputConfigurations: Optional[List[dict]] = None
    imageSources: Optional[List[ImageSourceIdModel]] = None
    featureConfigurations: Optional[List[dict]] = None
    outputConfigurations: Optional[List[dict]] = None


@router.patch("/workflows/{workflowId}")
def modify_workflows(workflowId: str, request: ModifyWorkflowRequest, db: Session = Depends(get_db)) -> str:
    data = request.dict(exclude_unset=True)
    data[workflow_accessor.primary_key] = workflowId

    return workflow_accessor.update_workflow(data, db)
