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
import os
import time
from typing import List

from fastapi import Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from dao.sqlite_db.sqlite_db_operations import SessionLocal
from endpoints.route.access_log_router import get_api_router
from workflow_engine import executor, run_artifacts, runtime
from workflow_engine.discovery import (
    ACTIVE_STATUSES,
    STATUS_REGISTERED,
    WORKFLOW_FILE,
)
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


def _registration_name(registration: WorkflowRegistration) -> str:
    """Human-friendly workflow name for a registration, or None.

    Read from the deployed ``manifest.json`` (``workflowName``, written by the
    Portal packager) at serve time so no schema/migration is needed. Returns
    None for packages built before the field existed (or on any read error);
    the UI falls back to the workflowId in that case.
    """
    try:
        import json

        manifest_path = os.path.join(registration.artifact_path, "manifest.json")
        with open(manifest_path, encoding="utf-8") as fh:
            name = json.load(fh).get("workflowName")
        # Guard against empty strings / non-strings so the UI fallback triggers.
        return name if isinstance(name, str) and name.strip() else None
    except Exception:
        return None


def registration_to_dict(registration: WorkflowRegistration) -> dict:
    payload = {
        "registrationId": registration.id,
        "workflowId": registration.workflow_id,
        "name": _registration_name(registration),
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
        # Run observability (additive; existing keys above unchanged).
        "hasImageResults": bool(execution.has_image_results),
        "captureId": execution.capture_id,
        "outputDir": execution.output_dir,
    }


@router.get("/workflows/registrations")
def list_workflow_registrations(
    includeInactive: bool = False, db: Session = Depends(get_db)
) -> List[dict]:
    """The Workflow_Component registrations discovered on this device.

    By default only active registrations (statuses ``registered`` and
    ``invalid``) are returned — retired rows (``removed``/``superseded``,
    stale-workflow-registrations bugfix) are filtered out so the
    deployed-workflows view reflects what is actually deployed. Pass
    ``includeInactive=true`` to also return retired registrations, whose
    execution history stays reachable here and via the detail route.
    """
    query = db.query(WorkflowRegistration)
    if not includeInactive:
        query = query.filter(WorkflowRegistration.status.in_(ACTIVE_STATUSES))
    registrations = query.order_by(
        WorkflowRegistration.workflow_id, WorkflowRegistration.version
    ).all()
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
    # Trigger_Health surfacing (Requirements 9.1, 9.2): additive field,
    # omitted entirely for trigger-less registrations (or when the trigger
    # subsystem is not running) so their responses stay byte-identical.
    health = runtime.trigger_health(registration_id)
    if health:
        payload["triggerHealth"] = health
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


def _get_execution_or_404(execution_id: str, db: Session) -> WorkflowExecution:
    execution = db.get(WorkflowExecution, execution_id)
    if execution is None:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow execution '{execution_id}' was not found",
        )
    return execution


def _base_output_artifact_exists(
    execution: WorkflowExecution, node_images: list
) -> bool:
    """True when the run's base output artifact — the file the
    ``.../output-image`` route serves — actually exists on disk.

    ``run_artifacts.base_output_image_path`` falls back to any non-overlay
    ``.jpg`` in the run's ``output_dir``, which includes the
    ``{capture_id}.node.{nodeId}.{port}.jpg`` inference-node frames. Those
    are surfaced as their own ``node`` entries, so a resolution that landed
    on one of them means the run has node frames and no base output image
    (vlm-bedrock-parity Requirement 4.3)."""
    resolved = run_artifacts.base_output_image_path(
        execution.output_dir, execution.capture_id
    )
    if not resolved:
        return False
    node_names = {
        "{0}.node.{1}.{2}.jpg".format(
            execution.capture_id, entry["nodeId"], entry["port"]
        )
        for entry in node_images
    }
    return os.path.basename(resolved) not in node_names


@router.get("/workflows/executions/{execution_id}/results")
def get_workflow_execution_results(
    execution_id: str, db: Session = Depends(get_db)
) -> dict:
    """Viewable-image metadata for a run (Requirements 4.1, 5.1, 5.2;
    vlm-bedrock-parity Requirement 4.3).

    ``hasImageResults`` mirrors whether the run produced viewable images —
    a routed terminal capture (File_Output_Node) or persisted
    inference-node frames — so the "View results" link appears exactly when
    viewable images exist (Property 7). When there are no image results the
    payload is empty-but-200; the base image is served separately (see
    ``download_file`` ``.../output-image``), each node frame via
    ``.../node-image?nodeId=&port=``, and the overlay mask via
    ``.../overlay``. 404 for an unknown execution (R4.6).

    ``images`` lists what actually exists on disk: the
    ``{"kind": "output"}`` entry only when the run's base output artifact
    is present, followed by one additive
    ``{"kind": "node", "nodeId", "port"}`` entry per persisted
    inference-node frame (``run_artifacts.list_node_images``, port-generic
    so ``bedrock_inference`` and ``llm_inference`` surface identically).
    A node-image-only run therefore no longer reports an ``output`` entry
    with no file behind it. Existing consumers ignore unknown kinds."""
    execution = _get_execution_or_404(execution_id, db)
    if not execution.has_image_results:
        return {"hasImageResults": False, "captureId": None, "images": []}
    node_images = run_artifacts.list_node_images(
        execution.output_dir, execution.capture_id
    )
    images = []
    if _base_output_artifact_exists(execution, node_images):
        images.append(
            {
                "kind": "output",
                "hasOverlay": run_artifacts.overlay_artifact_exists(
                    execution.output_dir, execution.capture_id
                ),
            }
        )
    for entry in node_images:
        images.append(
            {
                "kind": "node",
                "nodeId": entry["nodeId"],
                "port": entry["port"],
                "hasOverlay": False,
            }
        )
    return {
        "hasImageResults": True,
        "captureId": execution.capture_id,
        "images": images,
    }


@router.get("/workflows/executions/{execution_id}/overlay")
def get_workflow_execution_overlay(
    execution_id: str, db: Session = Depends(get_db)
) -> dict:
    """The run's mask overlay as base64 + chroma-key background
    (Requirements 4.2, 5.4, 5.7).

    Returns ``{maskImage, maskBackground}`` in the exact shape the existing
    on-device overlay pipeline consumes (``getMaskImageProp`` /
    ``setupMaskImage``), so the results view reuses the mask-capable image
    component unchanged (design §5.1). ``maskImage`` is ``null`` when the
    run produced no mask (the toggle is then hidden). Best-effort: missing
    or malformed artifacts yield a null mask, never a 500 (R5.7). 404 for
    an unknown execution (R4.6)."""
    execution = _get_execution_or_404(execution_id, db)
    return run_artifacts.read_mask_overlay(
        execution.output_dir, execution.capture_id
    )


@router.get("/workflows/executions/{execution_id}/log")
def get_workflow_execution_log(
    execution_id: str, db: Session = Depends(get_db)
) -> PlainTextResponse:
    """The run's Run_Log as ``text/plain`` (Requirements 4.3, 4.6, 6.4).

    Returns the captured log text so the on-device log viewer can render it
    scrollable/copyable (R6). When the run has no ``log_path`` yet, the file
    is missing/empty, or a read error occurs, the body is an empty
    ``200`` — the frontend shows an explanatory empty state rather than an
    error (R6.4). Reading is best-effort (see
    ``run_artifacts.read_run_log``): a read failure yields an empty body,
    never a 500. 404 only for an unknown execution (R4.6)."""
    execution = _get_execution_or_404(execution_id, db)
    return PlainTextResponse(run_artifacts.read_run_log(execution.log_path))


@router.get("/workflows/registrations/{registration_id}/graph")
def get_workflow_registration_graph(
    registration_id: str, db: Session = Depends(get_db)
) -> dict:
    """The registration's Workflow_Definition graph (Requirements 4.4, 4.6).

    Returns the parsed ``workflow.json`` (nodes with positions +
    connections) from the registration's artifact set, sufficient to render
    the run-status graph mirror (design §5.3). 404 when the registration is
    unknown, and — because a graph the frontend cannot render is a
    not-found condition, never a server error — also 404 when the artifact
    set's ``workflow.json`` is missing or malformed (R4.6, best-effort via
    ``run_artifacts.read_workflow_graph``)."""
    registration = _get_registration_or_404(registration_id, db)
    graph_path = os.path.join(registration.artifact_path, WORKFLOW_FILE)
    graph = run_artifacts.read_workflow_graph(graph_path)
    if graph is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Workflow graph for registration '{registration_id}' "
                f"was not found or is unreadable"
            ),
        )
    return graph


@router.get("/workflows/executions/{execution_id}/node-status")
def get_workflow_execution_node_status(
    execution_id: str, db: Session = Depends(get_db)
) -> dict:
    """The run's per-node status map (Requirements 4.5, 4.6).

    Returns the ``{nodeId: {status, detail?}}`` map parsed from
    ``node_status_json`` so the run-status graph can color each node
    (design §5.3). A run that recorded no per-node status (``None``/empty/
    malformed payload) yields an empty map with a 200 — best-effort via
    ``run_artifacts.parse_node_status``, never a 500. 404 only for an
    unknown execution (R4.6)."""
    execution = _get_execution_or_404(execution_id, db)
    return run_artifacts.parse_node_status(execution.node_status_json)


@router.get("/workflows/executions/{execution_id}/metadata")
def get_workflow_execution_metadata(
    execution_id: str, db: Session = Depends(get_db)
) -> dict:
    """The run's metadata JSON (Requirements 4.1, 4.2, 4.3, 5.3).

    Returns the parsed ``{output_dir}/{capture_id}.json`` written by the
    pipeline executor — the run's final tag values, including each llm
    node's ``generated_text``/``error`` and Bedrock's merged
    ``is_anomalous``/``confidence`` fields — so the run-status graph's
    output preview card can render LLM text and Bedrock fields (R4.1).
    Best-effort via ``run_artifacts.read_run_metadata``: a missing
    ``output_dir``/``capture_id``, a missing/unreadable file, malformed
    JSON, or a non-object top level yields ``{}`` with a 200, never a 500
    (R4.2). 404 only for an unknown execution (R4.3). Additive route: no
    existing route or response shape changes (R5.3)."""
    execution = _get_execution_or_404(execution_id, db)
    return run_artifacts.read_run_metadata(
        execution.output_dir, execution.capture_id
    )
