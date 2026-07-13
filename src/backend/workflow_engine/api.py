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

"""Workflow engine endpoints under /workflows (Requirement 9.1).

New, additive routes for Workflow_Component registrations — distinct
from the pre-existing Pipeline_Configuration routes that also live under
``/workflows`` (list/run/etc. in ``endpoints/workflow.py``), which are
not modified:

- ``GET  /workflows/registrations`` — list discovered registrations
- ``GET  /workflows/registrations/{registration_id}`` — one registration
  with its executions (status)
- ``POST /workflows/registrations/{registration_id}/trigger`` — trigger a
  run; invalid registrations are rejected (never runnable, 13.3)
- ``GET  /workflows/executions/{execution_id}`` — run status

The fixed ``registrations``/``executions`` path segments cannot collide
with real Pipeline_Configuration ids: this router is registered before
the legacy workflow router so its fixed paths take precedence over the
legacy ``/workflows/{workflowId}`` parameter route, whose behavior for
every actual workflow id is unchanged.
"""

import logging
import time
from typing import List

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from dao.sqlite_db.sqlite_db_operations import SessionLocal
from endpoints.route.access_log_router import get_api_router
from workflow_engine import executor, runtime
from workflow_engine.discovery import STATUS_REGISTERED
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.watcher import new_execution_id

logger = logging.getLogger(__name__)

router = get_api_router()

#: Initial status of a triggered run; the WorkflowExecutor (task 12.3)
#: picks pending executions up through the executor hook.
EXECUTION_STATUS_PENDING = "pending"


# Dependency (mirrors endpoints/workflow.py)
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def registration_to_dict(registration: WorkflowRegistration) -> dict:
    payload = {
        "registrationId": registration.id,
        "workflowId": registration.workflow_id,
        "version": registration.version,
        "arch": registration.arch,
        "artifactPath": registration.artifact_path,
        "status": registration.status,
        "registeredAt": registration.registered_at,
    }
    if registration.status != STATUS_REGISTERED:
        payload["invalidReason"] = runtime.invalid_reason(registration.id)
    return payload


def execution_to_dict(execution: WorkflowExecution) -> dict:
    return {
        "executionId": execution.id,
        "registrationId": execution.registration_id,
        "status": execution.status,
        "startedAt": execution.started_at,
        "finishedAt": execution.finished_at,
        "failingNodeId": execution.failing_node_id,
        "error": execution.error,
    }


@router.get("/workflows/registrations")
def list_workflow_registrations(db: Session = Depends(get_db)) -> List[dict]:
    """Every Workflow_Component registration discovered on this device."""
    registrations = (
        db.query(WorkflowRegistration)
        .order_by(WorkflowRegistration.workflow_id, WorkflowRegistration.version)
        .all()
    )
    return [registration_to_dict(registration) for registration in registrations]


def _get_registration_or_404(
    registration_id: str, db: Session
) -> WorkflowRegistration:
    registration = db.get(WorkflowRegistration, registration_id)
    if registration is None:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow registration '{registration_id}' was not found",
        )
    return registration


@router.get("/workflows/registrations/{registration_id}")
def get_workflow_registration(
    registration_id: str, db: Session = Depends(get_db)
) -> dict:
    """One registration plus its executions (workflow status)."""
    registration = _get_registration_or_404(registration_id, db)
    executions = (
        db.query(WorkflowExecution)
        .filter(WorkflowExecution.registration_id == registration_id)
        .all()
    )
    payload = registration_to_dict(registration)
    payload["executions"] = [execution_to_dict(execution) for execution in executions]
    return payload


@router.post("/workflows/registrations/{registration_id}/trigger")
def trigger_workflow(registration_id: str, db: Session = Depends(get_db)) -> dict:
    """Trigger a run of a registered workflow.

    Creates a WorkflowExecution with status ``pending`` and hands it to
    the executor hook (implemented by task 12.3). Invalid registrations
    can never be run (Requirements 9.1, 13.3).
    """
    registration = _get_registration_or_404(registration_id, db)
    if registration.status != STATUS_REGISTERED:
        reason = runtime.invalid_reason(registration_id)
        detail = (
            f"Workflow registration '{registration_id}' is invalid and cannot "
            f"be run"
        )
        if reason:
            detail += f": {reason}"
        raise HTTPException(status_code=409, detail=detail)

    execution = WorkflowExecution(
        id=new_execution_id(),
        registration_id=registration_id,
        started_at=int(time.time()),
        status=EXECUTION_STATUS_PENDING,
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)

    executor.dispatch(execution.id)
    return execution_to_dict(execution)


@router.get("/workflows/executions/{execution_id}")
def get_workflow_execution(
    execution_id: str, db: Session = Depends(get_db)
) -> dict:
    """Status of one workflow run."""
    execution = db.get(WorkflowExecution, execution_id)
    if execution is None:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow execution '{execution_id}' was not found",
        )
    return execution_to_dict(execution)
