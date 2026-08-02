"""Stage 10 — Vision_Model lifecycle (Reqs 4.1–4.3, 2.3).

Enumerates the Vision_Models the Backend_API reports and asserts every model
expected by the Harness_Configuration is present (Req 4.1); brings each
expected model to READY — starting it when the device reports it not running
— within ``timeouts.model_ready_s``, surfacing the device-reported reason
verbatim on FAILED or timeout via ``ModelWaitError`` (Req 4.2).

State_Restoration (Req 4.3): every model this stage acts on is recorded in
the session ``state_registry`` with its device-reported pre-run state before
any start is issued, so teardown stops only what the harness started — even
when assertions fail mid-stage.

DLR/Neo gating (Req 2.3): the device serves DLR/Neo-compiled models through
the LFV edge agent (feature entries of type ``LFVModel``); Triton-served
entries (``TritonModel``) cover the ONNX path. When the Device_Profile does
not grant ``dlr_models`` (JP6: TensorRT 10, no TRT8 ``libnvinfer.so.8``),
DLR/Neo-backed expectations are skipped with a recorded reason instead of
failing.
"""

from __future__ import annotations

import pytest
from harnesslib.restoration import RUNNING_PRE_STATES

pytestmark = pytest.mark.stage("vision_models")

#: Feature-configurations entry ``type`` of vLLM models (mirrors
#: ``conftest.VLLM_FEATURE_TYPE``); everything else is a Vision_Model entry.
VLLM_FEATURE_TYPE = "VllmModel"

#: Feature entry types served through the DLR/Neo path (the LFV edge agent
#: runs Neo/DLR-compiled artifacts); assertions on these are gated on the
#: ``dlr_models`` Capability_Flag (Req 2.3).
DLR_BACKED_TYPES = frozenset({"LFVModel"})


@pytest.fixture(scope="module")
def vision_entries(edge_client, device_identity):
    """The device's Vision_Model enumeration: feature-configurations entries
    that are not ``VllmModel`` entries (Req 4.1). Depending on
    ``device_identity`` keeps the health stage first (Req 3.1)."""
    return [
        entry
        for entry in edge_client.feature_configurations()
        if entry.get("type") != VLLM_FEATURE_TYPE
    ]


def _dlr_gated(entry, harness_target) -> bool:
    """True when ``entry`` is DLR/Neo-backed and the profile does not grant
    ``dlr_models`` (Req 2.3)."""
    return entry.get("type") in DLR_BACKED_TYPES and not harness_target.profile.grants("dlr_models")


def test_expected_vision_models_present(harness_target, vision_entries):
    """Every Vision_Model expected by the Harness_Configuration is reported
    by the device (Req 4.1). An empty expectation list means enumerate-only."""
    expected = harness_target.expected.vision_models
    if not expected:
        pytest.skip(
            f"no vision models expected for device {harness_target.name}; "
            "enumeration-only (empty expected.vision_models)"
        )
    reported = [entry.get("modelName") for entry in vision_entries]
    missing = [name for name in expected if name not in reported]
    assert not missing, (
        f"expected vision models missing from the device enumeration: "
        f"{missing}; device reports: {reported}"
    )


def test_expected_vision_models_reach_ready(
    harness_target, edge_client, state_registry, vision_entries
):
    """Each expected Vision_Model reaches READY within
    ``timeouts.model_ready_s`` (Req 4.2); models the device reports not
    running are started, after being recorded for State_Restoration with
    their device-reported pre-run state (Req 4.3). DLR/Neo-backed
    expectations are gated on ``dlr_models`` (Req 2.3)."""
    expected = harness_target.expected.vision_models
    if not expected:
        pytest.skip(
            f"no vision models expected for device {harness_target.name}; "
            "nothing to bring to READY (empty expected.vision_models)"
        )
    entries = {entry.get("modelName"): entry for entry in vision_entries}
    dlr_gated = []
    exercised = []
    for name in expected:
        entry = entries.get(name)
        assert entry is not None, (
            f"expected vision model {name!r} is not reported by the device; "
            f"device reports: {sorted(entries)}"
        )
        if _dlr_gated(entry, harness_target):
            dlr_gated.append(name)
            continue
        pre_state = entry.get("status")
        # Record before acting so restoration runs even if the start or the
        # wait fails (Req 4.3).
        state_registry.record("model", name, pre_state, lambda n=name: edge_client.stop_model(n))
        if pre_state not in RUNNING_PRE_STATES:
            edge_client.start_model(name)
        # ModelWaitError propagates the device-reported failure reason
        # verbatim on FAILED or timeout (Req 4.2).
        edge_client.wait_for_model_state(
            name, target="READY", timeout_s=harness_target.timeouts.model_ready_s
        )
        exercised.append(name)
    if not exercised:
        pytest.skip(
            f"expected vision models ({', '.join(dlr_gated)}) are all "
            f"DLR/Neo-backed and capability 'dlr_models' is not granted by "
            f"device profile {harness_target.name} (Req 2.3)"
        )
