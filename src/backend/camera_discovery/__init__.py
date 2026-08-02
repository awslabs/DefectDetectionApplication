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
"""Camera_Discovery: enumeration of physical capture devices (V4L2 / CSI)
and the periodic re-enumeration loop with absence tracking.

Public surface for the camera-registry-sync feature (Requirements 2.1, 2.2,
2.3, 2.4, 2.6, 11.2).
"""
from camera_discovery.aravis import (
    AravisDiscoveryResult,
    DiscoveredAravisCamera,
    aravis_stable_id,
    enumerate_aravis,
)
from camera_discovery.discovery import (
    DEFAULT_INTERVAL_SECONDS,
    INTERVAL_CONFIG_KEY,
    CameraDiscovery,
    DiscoveredCamera,
    DiscoveryResult,
    InventorySnapshot,
    TrackedCamera,
    diff_snapshot,
)
from camera_discovery.v4l2 import V4l2Io

__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "INTERVAL_CONFIG_KEY",
    "AravisDiscoveryResult",
    "CameraDiscovery",
    "DiscoveredAravisCamera",
    "DiscoveredCamera",
    "DiscoveryResult",
    "InventorySnapshot",
    "TrackedCamera",
    "V4l2Io",
    "aravis_stable_id",
    "diff_snapshot",
    "enumerate_aravis",
]
