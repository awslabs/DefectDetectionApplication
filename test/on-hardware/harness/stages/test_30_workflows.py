"""Stage 30 — workflow execution (Reqs 6.1–6.4).

Capability-gated on ``workflows`` (Req 2.1): the module skips with a recorded
reason when the Device_Profile does not grant it, and the session
``workflows_surface`` probe fails the stage with ``CapabilityMismatchError``
when the capability is declared but the enumeration does not answer (Req 2.4).

The stage enumerates Deployed_Workflows and asserts every workflow expected
by the Harness_Configuration is present (Req 6.1); it then runs ONE expected
workflow and asserts observable output — the run's inference result, or
captured artifacts for model-less capture workflows — within
``timeouts.workflow_output_s`` (Req 6.2). When the workflow carries an
``llm_inference`` node and ``vllm`` is granted, the output metadata must
carry the node's generated content (Req 6.3).

State_Restoration (Req 6.4): ``POST /workflows/{id}/run`` is a one-shot
trigger against a workflow the device is already serving — the harness never
starts a persistent workflow process. The run is still recorded in the
session ``state_registry`` with the found-running pre-state, documenting that
restoration must leave the workflow untouched.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import pytest
from harnesslib.client import DeviceApiError

pytestmark = [pytest.mark.stage("workflows"), pytest.mark.capability("workflows")]

#: Poll interval while waiting for a capture workflow's observable output.
CAPTURE_POLL_INTERVAL_S = 2.0


def _workflow_names(workflow: Dict[str, Any]) -> List[str]:
    """The identifiers an expectation may name a workflow by."""
    return [value for value in (workflow.get("workflowId"), workflow.get("name")) if value]


@pytest.fixture(scope="module")
def target_workflow(harness_target, workflows_surface, device_identity):
    """The one expected Deployed_Workflow this stage runs (Req 6.2): the
    first entry of ``expected.workflows``, resolved against the device
    enumeration by workflowId or name. Skips (enumerate-only) when the
    Harness_Configuration expects no workflows."""
    expected = harness_target.expected.workflows
    if not expected:
        pytest.skip(
            f"no workflows expected for device {harness_target.name}; "
            "enumeration-only (empty expected.workflows)"
        )
    wanted = expected[0]
    for workflow in workflows_surface:
        if wanted in _workflow_names(workflow):
            return workflow
    pytest.fail(
        f"expected workflow {wanted!r} is not reported by the device; "
        f"device reports: {[_workflow_names(w) for w in workflows_surface]}"
    )


def _wait_for_capture_output(
    edge_client, workflow_id: str, deadline: float
) -> List[Dict[str, Any]]:
    """Captured artifacts of a model-less capture workflow, polled until the
    ``workflow_output_s`` deadline; empty when none appeared in time."""
    while True:
        try:
            images = edge_client.workflow_images(workflow_id).get("images") or []
            if images:
                return images
        except DeviceApiError:
            # Results not written yet (or the workflow is still starting);
            # keep polling until the stage deadline.
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        time.sleep(min(CAPTURE_POLL_INTERVAL_S, remaining))


@pytest.fixture(scope="module")
def workflow_run_output(harness_target, edge_client, state_registry, target_workflow):
    """One run of the target workflow with its observable output (Req 6.2).

    Model-backed workflows run synchronously and answer with the inference
    result; model-less workflows start a bounded one-image capture task whose
    artifacts are polled through ``/workflows/{id}/images``. The workflow is
    recorded found-running so State_Restoration leaves it untouched (Req 6.4).
    """
    workflow_id = target_workflow.get("workflowId")
    # The run endpoint only succeeds against a workflow the device is already
    # serving; the harness starts nothing persistent, so the entry documents
    # a found-running workflow restoration must not stop (Req 6.4).
    state_registry.record("workflow", workflow_id, "RUNNING", lambda: None)
    timeout_s = harness_target.timeouts.workflow_output_s
    deadline = time.monotonic() + timeout_s
    if target_workflow.get("featureConfigurations"):
        run_response = edge_client.run_workflow(
            workflow_id,
            request={"returnImageString": False},
            timeout_s=timeout_s,
        )
        return {
            "mode": "inference",
            "workflow_id": workflow_id,
            "run_response": run_response,
            "captured": None,
        }
    run_response = edge_client.run_workflow(
        workflow_id,
        request={"captureImageCount": 1, "captureTimeInterval": 1},
        timeout_s=timeout_s,
    )
    return {
        "mode": "capture",
        "workflow_id": workflow_id,
        "run_response": run_response,
        "captured": _wait_for_capture_output(edge_client, workflow_id, deadline),
    }


def test_expected_workflows_present(harness_target, workflows_surface):
    """Every Deployed_Workflow expected by the Harness_Configuration is
    present in the device enumeration (Req 6.1). An empty expectation list
    means enumerate-only."""
    expected = harness_target.expected.workflows
    if not expected:
        pytest.skip(
            f"no workflows expected for device {harness_target.name}; "
            "enumeration-only (empty expected.workflows)"
        )
    reported = [_workflow_names(workflow) for workflow in workflows_surface]
    missing = [wanted for wanted in expected if not any(wanted in names for names in reported)]
    assert not missing, (
        f"expected workflows missing from the device enumeration: {missing}; "
        f"device reports: {reported}"
    )


def test_workflow_produces_observable_output(harness_target, workflow_run_output):
    """The run produces observable output within ``timeouts.workflow_output_s``
    (Req 6.2): the inference result for model-backed workflows, captured
    artifacts for model-less capture workflows."""
    run_response = workflow_run_output["run_response"]
    workflow_id = workflow_run_output["workflow_id"]
    if workflow_run_output["mode"] == "inference":
        assert run_response.get("inferenceResult"), (
            f"workflow {workflow_id!r} run answered without an inference "
            f"result; response: {run_response!r}"
        )
        assert run_response.get("captureId"), (
            f"workflow {workflow_id!r} run answered without a captureId; "
            f"response: {run_response!r}"
        )
    else:
        assert workflow_run_output["captured"], (
            f"workflow {workflow_id!r} produced no captured artifacts within "
            f"{harness_target.timeouts.workflow_output_s:.0f}s "
            f"(timeouts.workflow_output_s); run response: {run_response!r}"
        )


def _find_llm_metadata(document: Any) -> Optional[Dict[str, Any]]:
    """The first ``llm`` node-outcome mapping found in ``document`` (the
    workflow engine merges outcomes under ``metadata['llm'][nodeId]``)."""
    if isinstance(document, dict):
        candidate = document.get("llm")
        if isinstance(candidate, dict) and candidate:
            return candidate
        for value in document.values():
            found = _find_llm_metadata(value)
            if found is not None:
                return found
    elif isinstance(document, list):
        for value in document:
            found = _find_llm_metadata(value)
            if found is not None:
                return found
    return None


def test_llm_inference_output_metadata(harness_target, target_workflow, workflow_run_output):
    """When the workflow carries an ``llm_inference`` node and ``vllm`` is
    granted, the run's output metadata carries the node's generated content
    (Req 6.3): every ``llm`` node outcome holds non-empty ``generated_text``
    and no recorded error."""
    workflow_id = workflow_run_output["workflow_id"]
    if "llm_inference" not in json.dumps(target_workflow, default=str):
        pytest.skip(
            f"workflow {workflow_id!r} declares no llm_inference node; "
            "metadata assertion not applicable (Req 6.3)"
        )
    if not harness_target.profile.grants("vllm"):
        pytest.skip(
            f"capability 'vllm' not granted by device profile "
            f"{harness_target.name}; llm_inference metadata assertion "
            "requires it (Req 6.3)"
        )
    run_response = workflow_run_output["run_response"]
    llm_outcomes = _find_llm_metadata(run_response)
    assert llm_outcomes, (
        f"workflow {workflow_id!r} output metadata carries no 'llm' node "
        f"outcomes; run response: {run_response!r}"
    )
    for node_id, outcome in llm_outcomes.items():
        assert isinstance(outcome, dict) and not outcome.get("error"), (
            f"llm_inference node {node_id!r} recorded an error instead of "
            f"generated content: {outcome!r}"
        )
        text = outcome.get("generated_text")
        assert isinstance(text, str) and text.strip(), (
            f"llm_inference node {node_id!r} produced no generated content: " f"{outcome!r}"
        )
