"""Stage 20 — VLLM_Model lifecycle and text generation (Reqs 5.1–5.4).

Capability-gated on ``vllm`` (Req 2.2): the whole module skips with a
recorded reason when the Device_Profile does not grant it, and the session
``vllm_surface`` probe fails the stage with ``CapabilityMismatchError`` when
the capability is declared but absent on the device (Req 2.4).

The stage brings each expected VLLM_Model to READY within
``timeouts.vllm_ready_s`` — starting it when the device reports it not
running, recorded for State_Restoration first — surfacing FAILED-with-reason
verbatim via ``ModelWaitError`` (Req 5.1). Against a READY model it then:

* issues a non-streaming generate and asserts non-empty generated text
  (Req 5.2);
* issues a streaming (SSE) generate and asserts incremental token chunks
  arrive and the stream terminates cleanly with a ``done`` event (Req 5.3);
* records generate latency and token counts as informational metrics —
  no performance thresholds are asserted (Req 5.4).
"""

from __future__ import annotations

import time

import pytest
from harnesslib.restoration import RUNNING_PRE_STATES

pytestmark = [pytest.mark.stage("vllm_textgen"), pytest.mark.capability("vllm")]

#: Small deterministic-leaning generate request: modest token budget, zero
#: temperature. Values satisfy the Text_Generation_API validation bounds.
GENERATE_PROMPT = "Complete this sentence in a few words: edge devices run"
GENERATE_PARAMS = {"max_tokens": 64, "temperature": 0.0}


@pytest.fixture(scope="module")
def expected_vllm_models(harness_target, vllm_surface, device_identity):
    """The VLLM_Models this stage exercises: the Harness_Configuration's
    ``expected.vllm_models``, or — when the expectation list is empty
    (enumerate-only) — every ``VllmModel`` entry the device reports. The
    ``vllm_surface`` probe guarantees at least one entry exists (Req 2.4)."""
    expected = list(harness_target.expected.vllm_models)
    if expected:
        return expected
    return [entry.get("modelName") for entry in vllm_surface["feature_entries"]]


def test_expected_vllm_models_reported(vllm_surface, expected_vllm_models):
    """Every expected VLLM_Model is reported by the Backend_API (Req 5.1)."""
    reported = [entry.get("modelName") for entry in vllm_surface["feature_entries"]]
    missing = [name for name in expected_vllm_models if name not in reported]
    assert not missing, (
        f"expected vLLM models missing from the device's VllmModel entries: "
        f"{missing}; device reports: {reported}"
    )


@pytest.fixture(scope="module")
def ready_vllm_models(harness_target, edge_client, state_registry, expected_vllm_models):
    """Every expected VLLM_Model brought to READY within
    ``timeouts.vllm_ready_s`` (Req 5.1), each recorded for State_Restoration
    with its device-reported pre-run state before any start is issued.
    ``ModelWaitError`` carries the device-reported failure reason verbatim."""
    ready = []
    for name in expected_vllm_models:
        entry = edge_client.model_entry(name)
        pre_state = entry.get("status") if entry is not None else None
        state_registry.record("model", name, pre_state, lambda n=name: edge_client.stop_model(n))
        if pre_state not in RUNNING_PRE_STATES:
            edge_client.start_model(name)
        edge_client.wait_for_model_state(
            name, target="READY", timeout_s=harness_target.timeouts.vllm_ready_s
        )
        ready.append(name)
    return ready


def test_expected_vllm_models_reach_ready(ready_vllm_models, expected_vllm_models):
    """Each expected VLLM_Model reached READY within the stage timeout
    (Req 5.1); the fixture surfaced any FAILED state or timeout with the
    device-reported reason."""
    assert ready_vllm_models == expected_vllm_models


def test_generate_returns_text(harness_target, edge_client, ready_vllm_models, record_metric):
    """A non-streaming generate against a READY VLLM_Model answers with a
    well-formed response carrying non-empty generated text within the stage
    timeout (Req 5.2); latency and text size land in the Results_Bundle as
    informational metrics (Req 5.4)."""
    model = ready_vllm_models[0]
    started = time.monotonic()
    response = edge_client.generate(
        model,
        GENERATE_PROMPT,
        params=GENERATE_PARAMS,
        timeout_s=harness_target.timeouts.generate_s,
    )
    latency_s = time.monotonic() - started
    assert isinstance(response, dict), f"generate answered with a non-object payload: {response!r}"
    text = response.get("generated_text")
    assert isinstance(text, str) and text.strip(), (
        f"generate against model {model!r} returned no generated text; " f"response: {response!r}"
    )
    record_metric("vllm_generate_latency_s", round(latency_s, 3))
    record_metric("vllm_generate_text_chars", len(text))
    record_metric("vllm_generate_token_estimate", len(text.split()))


def test_generate_stream_incremental_chunks_and_termination(
    harness_target, edge_client, ready_vllm_models, record_metric
):
    """A streaming (SSE) generate delivers incremental token chunks and
    terminates cleanly with a ``done`` event (Req 5.3). A truncated stream
    raises ``SseStreamError``; a mid-stream error event is a failure carrying
    the device-reported reason. Token count and latency are recorded as
    informational metrics (Req 5.4)."""
    model = ready_vllm_models[0]
    started = time.monotonic()
    events = list(
        edge_client.generate_stream(
            model,
            GENERATE_PROMPT,
            params=GENERATE_PARAMS,
            timeout_s=harness_target.timeouts.generate_s,
        )
    )
    latency_s = time.monotonic() - started
    assert events, f"streaming generate against model {model!r} produced no events"
    errors = [event for event in events if "error" in event]
    assert not errors, (
        f"streaming generate against model {model!r} emitted an error event: " f"{errors[0]!r}"
    )
    tokens = [event["token"] for event in events if "token" in event]
    assert tokens, (
        f"streaming generate against model {model!r} delivered no incremental "
        f"token chunks; events: {events!r}"
    )
    assert events[-1].get("done") is True, (
        f"streaming generate against model {model!r} did not terminate with a "
        f"done event; last event: {events[-1]!r}"
    )
    record_metric("vllm_stream_latency_s", round(latency_s, 3))
    record_metric("vllm_stream_token_count", len(tokens))
