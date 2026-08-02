"""Stage 40 — vision + vLLM coexistence (Reqs 7.1, 7.2).

Capability-gated on ``vllm`` (Req 2.2). With at least one Vision_Model
available, the stage brings one Vision_Model and one VLLM_Model to READY
simultaneously — recording both for State_Restoration with their
device-reported pre-run states — then completes a generate request and
re-polls both models, asserting neither left READY under the combined load
(Req 7.1). On any departure the failure diagnostic carries BOTH models'
device-reported states (Req 7.2), so GPU/memory contention regressions
surface with the full dual-state picture.

Runs last (module naming ``test_40_*``): the earlier stages have typically
already brought the models READY, in which case this stage reuses that
registry-tracked state without issuing further starts.
"""

from __future__ import annotations

import time

import pytest
from harnesslib.restoration import RUNNING_PRE_STATES

pytestmark = [pytest.mark.stage("coexistence"), pytest.mark.capability("vllm")]

#: Feature-configurations entry ``type`` of vLLM models (mirrors
#: ``conftest.VLLM_FEATURE_TYPE``); everything else is a Vision_Model entry.
VLLM_FEATURE_TYPE = "VllmModel"

#: The generate request completing inside the coexistence window (Req 7.1).
GENERATE_PROMPT = "Complete this sentence in a few words: running two models"
GENERATE_PARAMS = {"max_tokens": 64, "temperature": 0.0}


def _pick_vision_model(harness_target, edge_client) -> str:
    """The Vision_Model of the coexistence pair: the first expected vision
    model, else the first Vision_Model entry the device reports; the stage
    skips when neither exists (Req 7.1 precondition)."""
    if harness_target.expected.vision_models:
        return harness_target.expected.vision_models[0]
    for entry in edge_client.feature_configurations():
        if entry.get("type") != VLLM_FEATURE_TYPE and entry.get("modelName"):
            return entry["modelName"]
    pytest.skip(
        f"no Vision_Model available on device {harness_target.name} (none "
        "expected, none reported); coexistence requires at least one (Req 7.1)"
    )


def _bring_to_ready(edge_client, state_registry, name: str, timeout_s: float) -> None:
    """Bring one model to READY, recording it for State_Restoration with its
    device-reported pre-run state before any start is issued (Req 8.3)."""
    entry = edge_client.model_entry(name)
    pre_state = entry.get("status") if entry is not None else None
    state_registry.record("model", name, pre_state, lambda n=name: edge_client.stop_model(n))
    if pre_state not in RUNNING_PRE_STATES:
        edge_client.start_model(name)
    edge_client.wait_for_model_state(name, target="READY", timeout_s=timeout_s)


@pytest.fixture(scope="module")
def coexistence_pair(harness_target, edge_client, state_registry, vllm_surface, device_identity):
    """One Vision_Model and one VLLM_Model, both READY simultaneously
    (Req 7.1). The ``vllm_surface`` probe guarantees at least one VllmModel
    entry exists (Req 2.4)."""
    vision_name = _pick_vision_model(harness_target, edge_client)
    if harness_target.expected.vllm_models:
        vllm_name = harness_target.expected.vllm_models[0]
    else:
        vllm_name = vllm_surface["feature_entries"][0].get("modelName")
    _bring_to_ready(
        edge_client,
        state_registry,
        vision_name,
        harness_target.timeouts.model_ready_s,
    )
    _bring_to_ready(
        edge_client,
        state_registry,
        vllm_name,
        harness_target.timeouts.vllm_ready_s,
    )
    return vision_name, vllm_name


def test_vision_and_vllm_remain_ready_through_generate(
    harness_target, edge_client, coexistence_pair, record_metric
):
    """Both models stay READY while a generate request completes successfully
    (Req 7.1); any departure fails with both models' device-reported states
    in the diagnostic (Req 7.2)."""
    vision_name, vllm_name = coexistence_pair

    started = time.monotonic()
    response = edge_client.generate(
        vllm_name,
        GENERATE_PROMPT,
        params=GENERATE_PARAMS,
        timeout_s=harness_target.timeouts.generate_s,
    )
    latency_s = time.monotonic() - started
    text = response.get("generated_text")
    assert isinstance(text, str) and text.strip(), (
        f"coexistence generate against model {vllm_name!r} returned no "
        f"generated text; response: {response!r}"
    )
    record_metric("coexistence_generate_latency_s", round(latency_s, 3))

    vision_entry = edge_client.model_entry(vision_name) or {}
    vllm_entry = edge_client.model_entry(vllm_name) or {}
    vision_state = vision_entry.get("status")
    vllm_state = vllm_entry.get("status")
    if vision_state != "READY" or vllm_state != "READY":
        pytest.fail(
            f"a model left READY during the coexistence window — "
            f"device-reported states: vision model {vision_name!r}: "
            f"{vision_state!r}, vLLM model {vllm_name!r}: {vllm_state!r} "
            f"(Req 7.2)"
        )
