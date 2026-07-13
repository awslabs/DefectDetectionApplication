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
        try:
            watcher = WorkflowWatcher()
            watcher.start()
            _watcher = watcher
        except Exception:  # noqa: BLE001 - never take LocalServer down
            logger.exception(
                "Workflow engine failed to start; continuing without it"
            )
            return None
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


def get_watcher() -> Optional[WorkflowWatcher]:
    return _watcher


def invalid_reason(registration_id: str) -> Optional[str]:
    """Reported reason for an invalid registration, when the watcher knows it."""
    watcher = get_watcher()
    if watcher is None:
        return None
    return watcher.invalid_reason(registration_id)
