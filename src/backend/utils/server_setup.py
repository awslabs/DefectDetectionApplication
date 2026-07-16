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
import logging
import threading

import awsiot.greengrasscoreipc

from gstreamer.gst_pipeline_executor import GstPipelineExecutor
from dao.iotshadow.IoTShadowAccessor import IoTShadowAccessor
from mqtt.PublishHandler import PublishHandler
from edgeagent.lfv_edge_agent import LFVEdgeAgent
from resources.accessors.image_source_configuration_accessor import ImageSourceConfigurationAccessor
from resources.accessors.input_configuration_accessor import InputConfigurationAccessor
from resources.accessors.output_configuration_accessor import OutputConfigurationAccessor
from resources.accessors.workflow_accessor import WorkflowAccessor
from resources.accessors.workflow_metadata_accessor import WorkflowMetadataAccessor
from resources.accessors.latency_time_accessor import LatencyTimeAccessor
from defect_detection_config.defect_detection_config import DefectDetectionConfig
from resources.accessors.image_source_accessor import ImageSourceAccessor
from resources.accessors.inference_result_accessor import InferenceResultAccessor
from utils.capture_task_manager import CaptureTaskManager

logger = logging.getLogger(__name__)

# set up IPC client to connect to the IPC server
ipc_client = awsiot.greengrasscoreipc.connect()
iot_shadow_accessor = IoTShadowAccessor(ipc_client)
image_source_accessor = ImageSourceAccessor()
image_src_cfg_accessor = ImageSourceConfigurationAccessor()
input_cfg_accessor = InputConfigurationAccessor()
output_cfg_accessor = OutputConfigurationAccessor()
latency_time_accessor = LatencyTimeAccessor()
inference_result_accessor = InferenceResultAccessor()
workflow_accessor = WorkflowAccessor(iot_shadow_accessor)
workflow_metadata_accessor = WorkflowMetadataAccessor()
publish_handler = PublishHandler(ipc_client)
gst_pipeline_executor = GstPipelineExecutor()
capture_task_manager = CaptureTaskManager(inference_result_accessor, gst_pipeline_executor)

# set up LFV edge agent gRPC client
lfv_edge_agent = LFVEdgeAgent()

# set up local server config
defect_detection_config = DefectDetectionConfig(ipc_client)

# --- camera registry sync (camera-registry-sync feature, task 2.8) -----------
#
# Camera_Discovery + Edge_Sync_Agent run entirely on daemon threads; every
# step of their construction and start sits behind one exception guard so a
# failure is logged and leaves the rest of LocalServer startup untouched
# (Requirement 11.2). No migration is involved: the agent only reads the
# existing tables through the existing accessors (Requirement 11.4).

camera_discovery = None
camera_sync_agent = None

# Additional Camera_Discovery on_change consumers beyond the Edge_Sync_Agent
# (e.g. the WorkflowWatcher's Camera_Binding re-resolution, task 14.4 /
# Requirement 10.4). Consulted at dispatch time so listeners registered
# after the discovery loop started still receive changes; every listener
# is individually isolated (11.2).
_discovery_change_listeners = []
_discovery_listeners_lock = threading.Lock()


def add_discovery_change_listener(callback):
    """Register an extra Camera_Discovery ``on_change`` consumer.

    The callback receives the :class:`InventorySnapshot`; exceptions are
    contained per listener so one consumer can never starve another (11.2).
    """
    with _discovery_listeners_lock:
        _discovery_change_listeners.append(callback)


def _make_discovery_on_change(agent):
    """The single ``on_change`` callback handed to Camera_Discovery: the
    Edge_Sync_Agent first, then every registered extra listener."""

    def _dispatch(snapshot):
        try:
            agent.on_discovery_change(snapshot)
        except Exception:  # noqa: BLE001 - consumer isolation (11.2)
            logger.exception("Edge sync agent discovery on_change failed")
        with _discovery_listeners_lock:
            listeners = list(_discovery_change_listeners)
        for listener in listeners:
            try:
                listener(snapshot)
            except Exception:  # noqa: BLE001 - consumer isolation (11.2)
                logger.exception("Discovery change listener failed")

    return _dispatch


def start_camera_registry_sync():
    """Construct and start Camera_Discovery and the Edge_Sync_Agent.

    Imports are deferred so this module section stays importable in
    environments without the feature's optional runtime pieces; awsiot
    usage inside ``make_shadow_stream_handler`` is likewise deferred by
    the camera_sync module itself.

    Returns ``(discovery, agent)``; raises on any construction or start
    failure — the caller isolates that from server startup (11.2).
    """
    from camera_discovery import CameraDiscovery
    from camera_sync import (
        EdgeSyncAgent,
        delta_topic_prefix,
        make_shadow_stream_handler,
        set_active_agent,
    )
    from mqtt.SubscriptionHandler import SubscriptionHandler

    # Discovery interval comes from the existing feature-config mechanism
    # (CameraDiscoveryIntervalSeconds, default 300 s — Requirement 2.3).
    discovery = CameraDiscovery(
        config_provider=defect_detection_config.get_local_server_config
    )

    # The agent reads Image_Sources only through the existing accessors
    # (11.3/11.4); shadow I/O goes through the existing IoTShadowAccessor
    # (device IoT identity, 12.4). Thing name defaults to AWS_IOT_THING_NAME.
    agent = EdgeSyncAgent(
        iot_shadow_accessor,
        image_source_accessor,
        input_configuration_accessor=input_cfg_accessor,
        camera_discovery=discovery,
    )

    # Build the delta subscription pieces before starting anything, so a
    # construction failure (e.g. the deferred awsiot import inside
    # make_shadow_stream_handler) never leaves half-started threads behind.
    subscription = SubscriptionHandler(
        delta_topic_prefix(agent.thing_name, agent.shadow_name),
        make_shadow_stream_handler(agent),
        publish_handler,
    )

    # Start the report worker (schedules the full startup report, 3.4),
    # then the periodic re-enumeration loop feeding it (2.3, 2.4). Both
    # run on their own daemon threads.
    agent.start()
    discovery.start(on_change=_make_discovery_on_change(agent))

    # Image_Source CRUD in the FastAPI route layer triggers reports through
    # the hooks module (3.1).
    set_active_agent(agent)

    # From here on the agent is running: post-start steps are individually
    # guarded so a hiccup in one never abandons the running agent (11.2).

    # Reconnect-time application of pending portal changes (5.5): desired
    # entries that queued up in the shadow while the device was offline are
    # applied now; a missing or unreadable shadow yields nothing to apply.
    try:
        state = iot_shadow_accessor.get_thing_shadow_state_request(
            agent.thing_name, agent.shadow_name
        )
        desired = state.get("desired") if isinstance(state, dict) else None
        changes = desired.get("changes") if isinstance(desired, dict) else None
        if isinstance(changes, dict) and changes:
            agent.apply_desired_changes(changes)
    except Exception:  # noqa: BLE001 - post-start isolation (11.2)
        logger.exception(
            "Could not apply pending portal camera changes at start; they "
            "will be applied when the next shadow delta arrives"
        )

    # Delta subscription for portal-originated changes (5.2), following the
    # existing MQTT SubscriptionHandler pattern. subscribe() blocks to keep
    # its stream alive, so it gets its own daemon thread.
    def _subscribe():
        try:
            subscription.subscribe()
        except Exception:  # noqa: BLE001 - subscription isolation (11.2)
            logger.exception(
                "Camera-registry shadow subscription failed; portal-originated "
                "changes will not be applied until the next restart"
            )

    try:
        threading.Thread(
            target=_subscribe, name="camera-sync-shadow-subscription", daemon=True
        ).start()
    except Exception:  # noqa: BLE001 - post-start isolation (11.2)
        logger.exception("Could not start the camera-registry shadow subscription")

    return discovery, agent


def _start_camera_registry_sync_isolated():
    """Daemon-thread entry point: any construction or start failure is
    logged and swallowed so LocalServer startup proceeds untouched (11.2)."""
    global camera_discovery, camera_sync_agent
    try:
        camera_discovery, camera_sync_agent = start_camera_registry_sync()
        logger.info("Camera registry sync started")
    except Exception:  # noqa: BLE001 - top-level failure isolation (11.2)
        logger.exception(
            "Camera registry sync failed to start; continuing LocalServer "
            "startup without it"
        )


try:
    threading.Thread(
        target=_start_camera_registry_sync_isolated,
        name="camera-sync-startup",
        daemon=True,
    ).start()
except Exception:  # noqa: BLE001 - even thread creation must not break startup
    logger.exception("Could not start the camera registry sync thread")