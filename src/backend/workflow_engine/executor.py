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

"""Executor hook for triggered workflow runs.

The trigger endpoint creates a WorkflowExecution record with status
``pending`` and then hands the execution id to whatever executor is
registered here. The WorkflowExecutor (task 12.3) registers itself via
:func:`set_executor` at engine startup; until then triggered runs simply
stay ``pending``.

The callback runs on a dedicated daemon thread so a slow or failing
pipeline can never block the API or touch Pipeline_Configuration
execution (Requirements 13.4, 13.7).
"""

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: Signature: execute(execution_id: str) -> None. Implementations load the
#: WorkflowExecution + WorkflowRegistration rows themselves and update the
#: execution status/failing_node_id/error columns as the run progresses.
ExecutorCallback = Callable[[str], None]

_executor: Optional[ExecutorCallback] = None
_lock = threading.Lock()


def set_executor(callback: Optional[ExecutorCallback]) -> None:
    """Register (or clear, with None) the workflow executor callback."""
    global _executor
    with _lock:
        _executor = callback


def get_executor() -> Optional[ExecutorCallback]:
    with _lock:
        return _executor


def dispatch(execution_id: str) -> bool:
    """Hand a pending execution to the registered executor, if any.

    Returns True when an executor picked the run up. The executor is
    invoked on its own daemon thread; exceptions are contained and
    logged (a workflow failure never propagates, Requirement 13.7).
    """
    executor = get_executor()
    if executor is None:
        logger.info(
            "No workflow executor registered; execution %s stays pending",
            execution_id,
        )
        return False

    def _run() -> None:
        try:
            executor(execution_id)
        except Exception:  # noqa: BLE001 - workflow failures must be contained
            logger.exception("Workflow execution %s raised", execution_id)

    thread = threading.Thread(
        target=_run, name=f"workflow-execution-{execution_id}", daemon=True
    )
    thread.start()
    return True
