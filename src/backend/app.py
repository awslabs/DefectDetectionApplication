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
import importlib.util
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
from fastapi.staticfiles import StaticFiles
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
    inference_result,
    streams,
    local_auth,
    health
)

# Workflow Manager engine (additive subsystem, Requirement 13)
from workflow_engine import api as workflow_engine_api
from workflow_engine import runtime as workflow_engine_runtime

# vLLM capability probe (vllm-triton-inference, Requirements 4.1, 4.2, 4.3,
# 8.3): the companion Triton_vLLM_Runtime and the Text_Generation_API router
# exist only on images whose build installed the vllm wheel (Dockerfile.jp6's
# VLLM_ENABLE layer). Images without vLLM (jp4, jp5-default, x86 variants)
# skip the import, the router registration, and the manager startup below —
# exactly the pre-feature startup sequence.
VLLM_AVAILABLE = importlib.util.find_spec("vllm") is not None
if VLLM_AVAILABLE:
    from endpoints import text_generation

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

# Registered before the legacy routers so the workflow engine's fixed
# /workflows/registrations and /workflows/executions paths take precedence
# over the legacy /workflows/{workflowId} parameter route; behavior of the
# existing endpoints for real workflow ids is unchanged (Requirement 13.6).
app.include_router(workflow_engine_api.router)

app.include_router(image_source.router)
app.include_router(camera.router)
app.include_router(system.router)
app.include_router(feature_config.router)
app.include_router(workflow.router)
app.include_router(auth_info.router)
app.include_router(download_file.unauthenticated_router)
app.include_router(inference_result.router)
app.include_router(streams.router)
# Local_Login endpoints (portal-user-manager): intentionally carry no auth
# dependency — /local-auth/login and /local-auth/status must be reachable
# without credentials (exempt from authorize_request, task 9.4).
app.include_router(local_auth.router)
# Health endpoint (edge-deploy-reliability, Defect B): unauthenticated like
# local_auth's router (exempt from authorize_request) — the docker healthcheck
# (healthcheck.py) probes GET /health without credentials, and the Greengrass
# Startup `compose up -d --wait` gates RUNNING on the resulting health.
app.include_router(health.router)
# Text_Generation_API: registered beside the existing routers only when the
# capability probe found the vllm wheel; vLLM-free images keep the
# pre-feature router set (Requirements 4.1, 8.3).
if VLLM_AVAILABLE:
    app.include_router(text_generation.router)

# Quality Station HMI static bundle (quality-station-hmi, Requirement 6.7):
# serve the pre-built HMI single-page app same-origin with the API it
# consumes, so the kiosk browser needs nothing beyond static assets. The
# mount carries no auth dependency — static assets hold no secrets, and
# every data route the HMI calls still requires the Session_Token. Guarded
# by directory existence so devices without the HMI bundle behave
# byte-identically to today (no mount, no route changes).
HMI_DIST_DIR = os.environ.get(
    "HMI_DIST_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "hmi", "dist"),
)
if os.path.isdir(HMI_DIST_DIR):
    app.mount("/hmi", StaticFiles(directory=HMI_DIST_DIR, html=True), name="hmi")


def cleanup_workflow_digital_inputs():
    logger.info("Cleaning up digital input workflows")
    with SessionLocal() as session:
        for workflow in workflow_accessor.list_workflows_with_image_sources(session):
            if workflow.get("inputConfigurations"):
                terminate_digital_input_task(workflow)


# Bound on the SIGTERM shutdown cleanup (edge-deploy-reliability, Defect A):
# strictly below the compose stop_grace_period (120s) so the backend is never
# SIGKILLed mid-cleanup by Docker's stop timeout.
SHUTDOWN_CLEANUP_BUDGET_SECONDS = 20


@app.on_event("shutdown")
async def shutdown_event():
    def _cleanup():
        cleanup_workflow_digital_inputs()

        # On exit disconnect all cameras
        disconnect_all_cameras()

    try:
        await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _cleanup),
            timeout=SHUTDOWN_CLEANUP_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        logger.warning(
            "Shutdown cleanup exceeded %ss budget; proceeding with shutdown "
            "(remaining cleanup is abandoned — the container is being torn "
            "down)", SHUTDOWN_CLEANUP_BUDGET_SECONDS)

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

def start_vllm_runtime():
    """
    Start the companion Triton_vLLM_Runtime on vLLM-capable images: the
    VllmRuntimeManager plus its loopback model-control/generate server, then
    install the manager into the Text_Generation_API router and the
    feature-config status merge (Requirements 4.1, 4.2). Failures are
    contained: the vision stack and every existing router start exactly as
    before (Requirement 4.3).
    """
    if not VLLM_AVAILABLE:
        return None
    try:
        from vllm_runtime.manager import VllmRuntimeManager
        from vllm_runtime.server import VllmRuntimeServer
        from utils import feature_configs_utils

        manager = VllmRuntimeManager()
        vllm_server = VllmRuntimeServer(manager)
        vllm_server.start()
        text_generation.set_runtime(manager)
        feature_configs_utils.set_vllm_manager(manager)
        # Re-drive staged-but-unloaded models after a backend restart
        # (vllm-model-reload-after-backend-restart, design File 4 /
        # Decision 7). The import lives INSIDE this try block, after the
        # VLLM_AVAILABLE early return, so vLLM-free images never import
        # or construct the reconciler; a reconciler construction/start
        # failure is caught by the existing containment below.
        from vllm_runtime.reconciler import VllmReconciler
        VllmReconciler(manager).start()
        logger.info(
            "vLLM reconciler started (staged-model reload after restart).")
        logger.info("vLLM runtime manager started.")
        return vllm_server
    except Exception:
        logger.error("[VLLM RUNTIME STARTUP FAILED]")
        logger.error(traceback.format_exc())
        return None


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
        # nosec B104 — intentional LAN bind for an on-device edge appliance operator UI.
        # Port 5443 is TLS-protected AND gated by station authorization; no plaintext
        # traffic reaches this listener and only authorized callers may proceed.
        config = uvicorn.Config(app, host="0.0.0.0", port=5443, loop="asyncio", log_config="dda_logging/uvicorn_disable_logging.json", ssl_certfile=constants.DDA_LOCAL_SERVER_SSL_CERT, ssl_keyfile=constants.DDA_LOCAL_SERVER_SSL_KEY)  # nosec B104
    else:
        logger.info("Local server starting up...")
        # nosec B104 — intentional LAN bind for an on-device edge appliance operator UI.
        # This plaintext path is reachable ONLY when station authorization is disabled;
        # it is intentional for an on-device edge appliance serving the LAN UI.
        config = uvicorn.Config(app, host="0.0.0.0", port=5000, loop="asyncio", log_config="dda_logging/uvicorn_disable_logging.json")  # nosec B104
    server = uvicorn.Server(config)
    # AWAIT the server instead of detaching it as a task (edge-deploy-
    # reliability, deterministic shutdown): uvicorn installs its own
    # SIGTERM/SIGINT handlers and serve() RETURNS after the graceful
    # shutdown sequence (connection drain + lifespan shutdown, i.e. our
    # bounded shutdown_event) completes. When serve() was detached and the
    # process parked in loop.run_forever(), nothing ever stopped the loop
    # after shutdown finished, so on `docker stop` the process idled in
    # epoll until the stop_grace_period SIGKILL (exit 137 at exactly 120s).
    # Awaiting serve() makes main() — and run_until_complete below — return
    # as soon as the graceful shutdown is done.
    await server.serve()

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

    # Start the Workflow Manager watcher (own daemon thread). Failures are
    # contained inside start_workflow_engine: LocalServer and every
    # Pipeline_Configuration continue exactly as before (Requirement 13.6).
    workflow_engine_runtime.start_workflow_engine()

    # Start the companion vLLM runtime (no-op on images without the vllm
    # wheel — jp4, jp5-default, x86 — which run exactly the pre-feature
    # startup sequence, Requirement 8.3). Install the result into the /health
    # endpoint (edge-deploy-reliability, Defect B): only a genuinely started
    # runtime server (non-None) arms the 8901 reachability gate — a contained
    # startup failure (None, Requirement 4.3) never flips the backend
    # unhealthy, while a started-then-dead runtime does.
    vllm_server = start_vllm_runtime()
    health.set_vllm_server(vllm_server)

    # Run the server to completion. main() awaits uvicorn's serve(), which
    # returns once the SIGTERM-triggered graceful shutdown (including the
    # bounded shutdown_event cleanup) has finished — no run_forever(): the
    # loop must stop when the server is done so the process can exit inside
    # the compose stop_grace_period instead of being SIGKILLed (exit 137).
    loop = asyncio.get_event_loop()
    exit_code = 0
    try:
        loop.run_until_complete(main())
    except Exception:
        logger.error("[LOCAL SERVER CRASHED]")
        logger.error(traceback.format_exc())
        exit_code = 1
    finally:
        # Stop the companion vLLM runtime server (bounded internally); the
        # loaded engine's worker threads/processes die with the process.
        if vllm_server is not None:
            try:
                vllm_server.stop()
            except Exception:
                logger.error(traceback.format_exc())
        # Close loop to prevent the resource leak.
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
        except Exception:
            logger.error(traceback.format_exc())
        logger.info("Local server shutdown complete; exiting.")
        # Deterministic exit (edge-deploy-reliability): every cleanup the app
        # owns has already run (bounded shutdown_event, uvicorn connection
        # drain, vLLM runtime stop, asyncgens, loop close). A plain return
        # would now hang in interpreter teardown: the loaded vLLM engine,
        # Triton client and torch/NCCL leave NON-DAEMON threads (140+
        # observed on JP6) and child processes that block threading/
        # multiprocessing atexit joins far past the 120s stop grace window —
        # the incident's exit-137-at-exactly-120s shape. os._exit skips that
        # teardown and ends the process immediately with the correct code.
        logging.shutdown()
        os._exit(exit_code)  # pylint: disable=protected-access

