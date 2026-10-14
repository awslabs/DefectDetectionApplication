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
"""Image_Source CRUD report trigger (Requirement 3.1).

The existing FastAPI route layer calls
:func:`notify_image_source_changed` after a successful Image_Source
mutation; when an :class:`camera_sync.agent.EdgeSyncAgent` is active
(registered by the server wiring, task 2.8) this requests a debounced
inventory report. Without an active agent it is a no-op, so the route
layer carries no dependency on the sync feature being up — a failing or
absent agent never affects the API (Requirement 11.2).
"""
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_active_agent = None


def set_active_agent(agent) -> None:
    """Register the running Edge_Sync_Agent so route-layer mutations
    trigger inventory reports."""
    global _active_agent
    with _lock:
        _active_agent = agent


def clear_active_agent() -> None:
    global _active_agent
    with _lock:
        _active_agent = None


def get_active_agent() -> Optional[object]:
    with _lock:
        return _active_agent


def notify_image_source_changed() -> None:
    """Request a debounced inventory report from the active agent, if any.

    Never raises: a broken agent must not fail the Image_Source API call
    that triggered the notification (Requirement 11.2).
    """
    with _lock:
        agent = _active_agent
    if agent is None:
        return
    try:
        agent.report_inventory()
    except Exception:  # noqa: BLE001 - isolation from the route layer
        logger.exception("Camera sync report trigger failed")
