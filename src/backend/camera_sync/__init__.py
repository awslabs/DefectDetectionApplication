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
"""Edge_Sync_Agent support: the pure ``build_inventory`` merge of configured
Image_Sources with Camera_Discovery results, the local per-source version
state persisted in ``/aws_dda/camera_sync_state.json``, and the
``EdgeSyncAgent`` report path over the ``dda-camera-registry`` named shadow.

Public surface for the camera-registry-sync feature (Requirements 1.1, 2.5,
3.1, 3.3, 3.4, 11.3, 12.4).
"""
from camera_sync.agent import (
    BACKOFF_INITIAL_SECONDS,
    BACKOFF_MAX_SECONDS,
    DEBOUNCE_SECONDS,
    MAX_REPORT_BYTES,
    MAX_REPORT_DELAY_SECONDS,
    REASON_DISCOVERY_MANAGED,
    SCHEMA_VERSION,
    SHADOW_NAME,
    EdgeSyncAgent,
    build_report_document,
    change_to_image_source_data,
    delta_topic_prefix,
    make_shadow_stream_handler,
)
from camera_sync.hooks import (
    clear_active_agent,
    get_active_agent,
    notify_image_source_changed,
    set_active_agent,
)
from camera_sync.inventory import (
    ORIGIN_EDGE_CONFIGURED,
    ORIGIN_EDGE_DISCOVERED,
    TYPE_V4L2_DISCOVERED,
    CameraSourceState,
    build_inventory,
    configured_camera_source_id,
)
from camera_sync.version_state import (
    DEFAULT_STATE_PATH,
    CameraSyncStateStore,
    assign_versions,
    compute_content_hash,
    versions_from_reported,
)

__all__ = [
    "BACKOFF_INITIAL_SECONDS",
    "BACKOFF_MAX_SECONDS",
    "DEBOUNCE_SECONDS",
    "MAX_REPORT_BYTES",
    "MAX_REPORT_DELAY_SECONDS",
    "REASON_DISCOVERY_MANAGED",
    "SCHEMA_VERSION",
    "SHADOW_NAME",
    "EdgeSyncAgent",
    "build_report_document",
    "change_to_image_source_data",
    "delta_topic_prefix",
    "make_shadow_stream_handler",
    "clear_active_agent",
    "get_active_agent",
    "notify_image_source_changed",
    "set_active_agent",
    "ORIGIN_EDGE_CONFIGURED",
    "ORIGIN_EDGE_DISCOVERED",
    "TYPE_V4L2_DISCOVERED",
    "CameraSourceState",
    "build_inventory",
    "configured_camera_source_id",
    "DEFAULT_STATE_PATH",
    "CameraSyncStateStore",
    "assign_versions",
    "compute_content_hash",
    "versions_from_reported",
]
