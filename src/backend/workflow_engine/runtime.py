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

"""Workflow engine runtime wiring.

Holds the process-wide WorkflowWatcher instance and the startup entry
point called from app.py. Startup is wrapped so that any failure in the
workflow subsystem is logged and contained — it must never affect the
existing Pipeline_Configuration path (Requirement 13.6).
"""

import logging
import threading
from typing import Optional

from workflow_engine.watcher import WorkflowWatcher

logger = logging.getLogger(__name__)

_watcher: Optional[WorkflowWatcher] = None
_executor_instance = None
_lock = threading.Lock()


def start_workflow_engine() -> Optional[WorkflowWatcher]:
    """Start the WorkflowWatcher and register the WorkflowExecutor
    (idempotent). Returns the watcher or None.

    Called from LocalServer startup. Failures are contained: the rest of
    LocalServer starts normally and devices without Workflow_Components
    behave exactly as before.
    """
    global _watcher, _executor_instance
    with _lock:
        if _watcher is not None:
            return _watcher
        # Camera_Binding resolution wiring (camera-registry-sync task
        # 14.4). Contained separately: without it the watcher runs exactly
        # as before — documents register with their compiled-in values.
        binding_store = None
        inventory_provider = None
        try:
            binding_store, inventory_provider = _camera_binding_dependencies()
        except Exception:  # noqa: BLE001 - feature isolation (11.2)
            logger.exception(
                "Camera-binding wiring unavailable; workflows register "
                "without binding resolution"
            )
        try:
            watcher = WorkflowWatcher(
                binding_store=binding_store,
                inventory_provider=inventory_provider,
            )
            watcher.start()
            _watcher = watcher
        except Exception:  # noqa: BLE001 - never take LocalServer down
            logger.exception(
                "Workflow engine failed to start; continuing without it"
            )
            return None
        # Re-resolution hooks: discovery on_change and bindings-shadow
        # delta flip invalid registrations to registered when everything
        # resolves (camera-registry-sync Requirement 10.4). Contained: a
        # hook failure leaves the running watcher untouched (11.2).
        try:
            _wire_camera_binding_hooks(watcher)
        except Exception:  # noqa: BLE001 - post-start isolation (11.2)
            logger.exception(
                "Camera-binding re-resolution hooks could not be wired; "
                "invalid registrations re-resolve on the watch cycle only"
            )
        # Register the pipeline executor so triggered runs execute instead
        # of staying pending. Contained separately: a broken executor still
        # leaves discovery/registration/status reporting functional.
        try:
            from workflow_engine.output_bindings import OutputBindingProcessor
            from workflow_engine.pipeline_executor import register_workflow_executor

            # Post-run output bindings: digital output / MQTT / OPC UA
            # (Requirements 9.4, 9.5, 9.6; task 12.4).
            _executor_instance = register_workflow_executor(
                post_run_handler=OutputBindingProcessor()
            )
        except Exception:  # noqa: BLE001 - never take LocalServer down
            logger.exception(
                "WorkflowExecutor failed to register; triggered runs will "
                "stay pending"
            )
    return _watcher


def _camera_binding_dependencies():
    """The CameraBindingStore and local-inventory provider for the watcher
    (camera-registry-sync Requirements 10.1, 10.2, 12.4).

    Imports are deferred: ``utils.server_setup`` connects Greengrass IPC at
    import time, which only exists on-device — in every other environment
    this raises and the caller degrades to an unwired watcher.
    """
    from utils import server_setup
    from workflow_engine.camera_binding_store import CameraBindingStore

    store = CameraBindingStore(server_setup.iot_shadow_accessor)

    def inventory_provider():
        """The ``build_inventory`` merge of Image_Source records (through
        the existing accessor, read-only — 11.3) and the latest discovery
        snapshot; the discovery global may still be starting up, in which
        case configured sources alone form the inventory."""
        from camera_sync.inventory import build_inventory
        from dao.sqlite_db.sqlite_db_operations import SessionLocal

        camera_discovery = getattr(server_setup, "camera_discovery", None)
        snapshot = (
            camera_discovery.latest_snapshot if camera_discovery is not None else None
        )
        with SessionLocal() as session:
            image_sources = server_setup.image_source_accessor.list_image_sources(
                None, session
            )
            return build_inventory(image_sources, snapshot)

    return store, inventory_provider


def _wire_camera_binding_hooks(watcher: WorkflowWatcher) -> None:
    """Hook the running watcher's re-resolution into discovery ``on_change``
    and the bindings-shadow delta (Requirement 10.4)."""
    if watcher.binding_store is None:
        return

    from utils import server_setup
    from workflow_engine.camera_binding_store import (
        bindings_delta_topic_prefix,
        make_bindings_shadow_handler,
    )

    # Discovery inventory changes re-resolve invalid registrations; the
    # listener runs after (and isolated from) the Edge_Sync_Agent's own
    # on_change consumer (11.2).
    server_setup.add_discovery_change_listener(watcher.on_discovery_change)

    # Bindings-shadow delta subscription, following the existing MQTT
    # SubscriptionHandler pattern. subscribe() blocks to keep its stream
    # alive, so it gets its own daemon thread.
    from mqtt.SubscriptionHandler import SubscriptionHandler

    store = watcher.binding_store
    subscription = SubscriptionHandler(
        bindings_delta_topic_prefix(store.thing_name, store.shadow_name),
        make_bindings_shadow_handler(watcher),
        server_setup.publish_handler,
    )

    def _subscribe():
        try:
            subscription.subscribe()
        except Exception:  # noqa: BLE001 - subscription isolation (11.2)
            logger.exception(
                "Camera-bindings shadow subscription failed; binding changes "
                "apply on the next watch cycle or restart"
            )

    threading.Thread(
        target=_subscribe, name="camera-bindings-shadow-subscription", daemon=True
    ).start()


def get_watcher() -> Optional[WorkflowWatcher]:
    return _watcher


def invalid_reason(registration_id: str) -> Optional[str]:
    """Reported reason for an invalid registration, when the watcher knows it."""
    watcher = get_watcher()
    if watcher is None:
        return None
    return watcher.invalid_reason(registration_id)
