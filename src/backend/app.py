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
import asyncio
import logging
import os
import time
import traceback
import structlog
from alembic import command
from alembic.config import Config
# Fast api
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from asgi_correlation_id import CorrelationIdMiddleware
from panorama import trace as emltriton_trace
from dda_triton.triton_setup import create_virtual_env, cp_model_conversion_files
from exceptions.api.base_types.validation_exception import ValidationException
import os
from dda_triton.triton_edge_client import TritonEdgeClient
        
 
triton_instance = None
'''
    Logging needs to be setup FIRST because the main module is the "parent" for all logging singletons to be intialized
    elsewhere. Basically this allows us to not pass around logging objects for no reason because it'll all use whats
    defined below
'''
from dda_logging.custom_logging import setup_logging

# Make this True to format all logs to JSON, for easier consumption ie: cloudwatch. For now, this favors readability
LOG_JSON_FORMAT = False
LOG_LEVEL = "INFO"
setup_logging(json_logs=LOG_JSON_FORMAT, log_level=LOG_LEVEL)

access_logger = structlog.stdlib.get_logger("api.access")
logger = logging.getLogger(__name__)
import uvicorn
# Custom Modules

from utils.digital_input_process_manager import terminate_digital_input_task, create_digital_input_process
from utils.digital_input_thread_manager import create_digital_input_thread, terminate_digital_input_task_thread
from utils.server_setup import (
    workflow_accessor, 
    workflow_metadata_accessor, 
    image_source_accessor,
    capture_task_manager
)
from dao.sqlite_db import db_migration, workflow_dao, db_backfill
from exceptions.handlers.middleware import context_var_middleware
from utils import dda_user_management_utils, constants, utils

from exceptions.api.gst_pipeline_exception import (
    PipelineExecutionException,
    PipelineSyntaxException,
)
from exceptions.api.captured_images_exception import (
    CapturedImageException,
    ImageNotFoundException
)
from exceptions.api.grpc_exceptions import GrpcException
from exceptions.api.aravis_camera_exception import AravisCameraException

from exceptions.handlers.exception_handlers import (
    request_validation_exception_handler,
    http_exception_handler,
    unhandled_exception_handler,
    pipeline_execution_exception_handler,
    pipeline_syntax_exception_handler,
    captured_image_exception_handler,
    image_not_found_exception_handler,
    grpc_exception_handler, 
    validation_exception_handler,
    aravis_camera_exception_handler
)
from endpoints import (
    camera,
    feature_config,
    system,
    workflow,
    image_source,
    auth_info,
    download_file,
    inference_result
)

import dao.sqlite_db.models as models
from dao.sqlite_db.sqlite_db_operations import SessionLocal, engine
from utils.camera_manager import disconnect_all_cameras, connect_camera
from utils.get_is_triton import get_is_triton
app = FastAPI()

app.middleware("http")(context_var_middleware)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)
app.add_exception_handler(ValidationException, validation_exception_handler)
app.add_exception_handler(PipelineExecutionException, pipeline_execution_exception_handler)
app.add_exception_handler(PipelineSyntaxException, pipeline_syntax_exception_handler)
app.add_exception_handler(CapturedImageException, captured_image_exception_handler)
app.add_exception_handler(ImageNotFoundException, image_not_found_exception_handler)
app.add_exception_handler(GrpcException, grpc_exception_handler)
app.add_exception_handler(AravisCameraException, aravis_camera_exception_handler)

app.include_router(image_source.router)
app.include_router(camera.router)
app.include_router(system.router)
app.include_router(feature_config.router)
app.include_router(workflow.router)
app.include_router(auth_info.router)
app.include_router(download_file.unauthenticated_router)
app.include_router(inference_result.router)


def cleanup_workflow_digital_inputs():
    logger.info("Cleaning up digital input workflows")
    with SessionLocal() as session:
        for workflow in workflow_accessor.list_workflows_with_image_sources(session):
            if workflow.get("inputConfigurations"):
                terminate_digital_input_task(workflow)


@app.on_event("shutdown")
async def shutdown_event():
    cleanup_workflow_digital_inputs()

    # On exit disconnect all cameras
    disconnect_all_cameras()

def alembic_schema_migration():
    '''
        NOTE: ALEMBIC SPECIFIC LOGGING HAS BEEN DISABLED as it overrides the base logger for some reason. It's not meant
        to be a live-application process. It still logs to the main logger as the root logger will handle everything as 
        a catch-all dump. We can re-enable it if needed by uncommenting lines in alembic/env.py
    '''
    try:
         # # Create/Update Configuration DB
        alembic_cfg_cp = Config(constants.ALEMBIC_CONFIG_PATH, ini_section=constants.ALEMBIC_CP_DATABASE_INIT_SECTION)
        command.upgrade(alembic_cfg_cp, "head")
         # # Create/Update Inference Result Metadata DB
        alembic_cfg_metadata = Config(constants.ALEMBIC_CONFIG_PATH, ini_section=constants.ALEMBIC_METADATA_DATABASE_INIT_SECTION)
        command.upgrade(alembic_cfg_metadata, "head")
    except:
        logger.error("[UPGRADE FAILED]")
        logger.error(traceback.format_exc())

def on_startup():
    # # # [DDS-141] Permissions on writing to /aws_dda folder on the station
    dda_user_management_utils.setup_dda_users_and_groups()
  
    # # # DD-16305: Create image preview directory /aws_dda/image-capture/preview
    dda_user_management_utils.create_dda_user_directory(constants.IMAGE_CAPTURE_DIR)
    dda_user_management_utils.create_dda_user_directory(constants.DEFAULT_IMAGE_SAVE_DIR_PATH)
    try:
        db_migration.migrate()
    except:
        logger.error("[MIGRATION FAILED]")
        logger.error(traceback.format_exc())

    try:
        db_backfill.backfill()
    except:
        logger.error("[BACKFILL FAILED]")
        logger.error(traceback.format_exc())

    with SessionLocal() as session:
        workflows = workflow_accessor.list_workflows_with_image_sources(session)
        for workflow in workflows:
            # Update all configured workflow em config
            # Need this when there is em config change for backward compatibility
            if "imageSources" in workflow:
                utils.create_em_agent_config(workflow)

    # Create an entry in workflow metadata for each workflow if it does not already exist
    with SessionLocal() as session:
        all_workflow_ids = [workflow.workflowId for workflow in workflow_accessor.list_workflows(session)]
        workflow_metadata_ids = [entry.workflowId for entry in workflow_metadata_accessor.list_workflow_metadatas(session)]

        for workflow_id in all_workflow_ids:
            if workflow_id not in workflow_metadata_ids:
                workflow_metadata_entry = {"workflowId": workflow_id, "summaryStartTime": int(time.time())}
                workflow_metadata_accessor.create_workflow_metadata(session, workflow_metadata_entry)

    return None

def setup_triton():
    """
     Sets the env variable value for Triton after reading the value from file and stops lfv components if Triton is running
    """
    try:
        create_virtual_env()
        cp_model_conversion_files()
        os.environ["is_triton"] = "True"  # True by default
        from utils.edgemlsdk_trace_listener import EdgeMLSdkLoggingTraceListener

        logging_trace_listener = EdgeMLSdkLoggingTraceListener()
        emltriton_trace.add_trace_listener(logging_trace_listener)      
    except Exception as e:
        logger.error("[TRITON SETUP during startup FAILED]")
        logger.error(traceback.format_exc())

def setup_workflow_digital_inputs():
    logger.info("Setting up digital input workflows")
    with SessionLocal() as session:
        for workflow in workflow_accessor.list_workflows_with_image_sources(session):
            if workflow.get("inputConfigurations"):
                try:
                    if not get_is_triton():
                        create_digital_input_process(workflow)
                    else:
                        create_digital_input_thread(workflow)
                except Exception as err:
                    logger.error(f"Unable to start digital IO task {err}")

def connect_all_saved_cameras():
    logger.info("Establishing connection to saved cameras")
    with SessionLocal() as session:
        for camera_id in image_source_accessor.list_cameras_used_by_image_sources(session):
            try:
                connect_camera(camera_id)
            except AravisCameraException as e:
                logger.error(f"Unable to connect to camera {camera_id} {e}")


async def main():
    # Start capture task manager and FastAPI server
    loop = asyncio.get_event_loop()
    loop.create_task(capture_task_manager.run())

    if utils.is_authorization_enabled_on_station():
        logger.info("Local server starting up using SSL...")
        config = uvicorn.Config(app, host="0.0.0.0", port=5443, loop="asyncio", log_config="dda_logging/uvicorn_disable_logging.json", ssl_certfile=constants.DDA_LOCAL_SERVER_SSL_CERT, ssl_keyfile=constants.DDA_LOCAL_SERVER_SSL_KEY)
    else:
        logger.info("Local server starting up...")
        config = uvicorn.Config(app, host="0.0.0.0", port=5000, loop="asyncio", log_config="dda_logging/uvicorn_disable_logging.json")
    server = uvicorn.Server(config)
    loop.create_task(server.serve())

if __name__ == "__main__":  # pragma: no cover
    triton_instance = TritonEdgeClient.get_instance()
    setup_triton()
    # Start schema migration using alembic tool
    alembic_schema_migration()

    # set up interrupts for digital inputs
    setup_workflow_digital_inputs()
    
    # connect to all stored cameras on app startup
    connect_all_saved_cameras()

    # add cleanup shutdown code to this function
    logger.info("Local server init.")
    on_startup()

    # The event loop should start running continously after both FastAPI server and capture task manager
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
        loop.run_forever()
    finally:
        # Close loop if any exception to prevent the resource leak.
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()

