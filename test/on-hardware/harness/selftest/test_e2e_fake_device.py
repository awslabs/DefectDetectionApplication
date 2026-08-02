"""End-to-end selftests: the real stages against the fake device.

Each test serves a scripted :class:`fake_device.FakeDevice` over real HTTP
(uvicorn on an ephemeral localhost port) and runs the *real* harness — the
actual ``stages/`` modules, ``conftest.py``, ``EdgeApiClient`` transport, and
``ResultsPlugin`` — in a pytest subprocess (pytester) configured purely via
``DDA_HARNESS_*`` environment variables. The scenarios assert the behaviors
the design promises end to end:

* a full green run producing the Results_Bundle — results.json (schema 1,
  device identity, LocalServer version, per-stage outcomes, metrics),
  junit.xml, no failures/ — with restoration returning every harness-started
  model to its pre-run state (Reqs 8.1, 3.2, 4.3, 6.4, 8.3);
* honest skip-with-recorded-reason on a missing capability (Req 2.1);
* ``CapabilityMismatchError`` — distinct from an ordinary failure — on a
  declared-but-absent capability (Req 2.4);
* restoration executed on failure paths, stopping only what the harness
  started and sparing found-running components, with the device-reported
  failure reason surfaced verbatim and failures/ captures written
  (Reqs 4.3, 8.3, 4.2, 8.2);
* fail-fast ``pytest.exit`` (returncode 2) on an unreachable target
  (Req 1.3);
* budget-exceeded behavior failing remaining tests with an explicit
  diagnostic (Req 8.4).

``runpytest_subprocess`` (not in-process) is required: the harness conftest
does session-level work at ``pytest_configure`` (config load, budget arming,
plugin registration) that must not leak into this outer session. The
subprocess inherits the environment, and the uvicorn thread shares this
process's ``FakeDevice`` state, so tests script transitions before the run
and assert device-observed calls after it.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

from selftest.fake_device import (
    FAKE_LOCAL_SERVER_VERSION,
    FakeDevice,
    serve,
)

pytest_plugins = ["pytester"]

HARNESS_DIR = Path(__file__).resolve().parent.parent
STAGES_DIR = HARNESS_DIR / "stages"

DEVICE_NAME = "fake-device"
VISION_MODEL = "fake-vision-onnx"
VLLM_MODEL = "fake-opt125m"
WORKFLOW_ID = "wf-fake-inspection"

#: All five stage modules, keyed the way results.json groups them.
STAGE_NAMES = {
    "test_00_health",
    "test_10_vision_models",
    "test_20_vllm_textgen",
    "test_30_workflows",
    "test_40_coexistence",
}


def _configure_harness_env(
    monkeypatch, pytester, base_url: str, capabilities: str, **extra_env: str
) -> None:
    """Point the subprocess harness at the fake device via environment only.

    Clears every inherited ``DDA_HARNESS_*`` variable first and pins
    ``DDA_HARNESS_CONFIG`` to an empty devices.yaml in the pytester tmpdir,
    so a developer's local configuration can never leak into the selftest.
    Poll-facing timeouts are lowered so scripted transitions resolve in
    seconds while staying far from flaky bounds.
    """
    for name in list(os.environ):
        if name.startswith("DDA_HARNESS_"):
            monkeypatch.delenv(name, raising=False)
    empty_config = pytester.path / "devices.yaml"
    empty_config.write_text("devices: {}\n", encoding="utf-8")
    monkeypatch.setenv("DDA_HARNESS_CONFIG", str(empty_config))
    monkeypatch.setenv("DDA_HARNESS_DEVICE", DEVICE_NAME)
    monkeypatch.setenv("DDA_HARNESS_BASE_URL", base_url)
    monkeypatch.setenv("DDA_HARNESS_ARCHITECTURE", "arm64_jp6")
    monkeypatch.setenv("DDA_HARNESS_CAPABILITIES", capabilities)
    monkeypatch.setenv("DDA_HARNESS_MODEL_READY_S", "30")
    monkeypatch.setenv("DDA_HARNESS_VLLM_READY_S", "30")
    monkeypatch.setenv("DDA_HARNESS_GENERATE_S", "30")
    monkeypatch.setenv("DDA_HARNESS_WORKFLOW_OUTPUT_S", "30")
    # The fake lives on 127.0.0.1; a proxy from the environment must never
    # intercept the loopback transport.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    # pytest and its dependencies may live in the user site-packages, which
    # the pytester-isolated HOME hides from the subprocess interpreter;
    # passing the outer interpreter's import path through keeps the
    # subprocess able to import the same packages.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in sys.path if p))
    for name, value in extra_env.items():
        monkeypatch.setenv(name, value)


def _run_stages(pytester, bundle_dir: Path):
    """One real harness run over the ``stages/`` modules in a subprocess.

    The harness ``pytest.ini`` (rootdir) and ``conftest.py`` apply exactly as
    on-hardware; the Results_Bundle lands in ``bundle_dir``.
    """
    return pytester.runpytest_subprocess(str(STAGES_DIR), f"--harness-output-dir={bundle_dir}")


def _standard_device() -> FakeDevice:
    """A full-surface fake: one vision model, one vLLM model (both stopped,
    one LOADING observation before READY), and one model-backed workflow
    whose run response carries ``llm`` node output metadata."""
    device = FakeDevice()
    device.add_model(VISION_MODEL, model_type="TritonModel", status="STOPPED")
    device.add_model(VLLM_MODEL, model_type="VllmModel", status="STOPPED")
    device.add_workflow(
        {
            "workflowId": WORKFLOW_ID,
            "name": "fake-inspection",
            "featureConfigurations": [VISION_MODEL],
            "nodes": [{"nodeId": "llm-1", "type": "llm_inference"}],
        },
        run_response={
            "inferenceResult": {"detections": []},
            "captureId": "capture-0001",
            "metadata": {"llm": {"llm-1": {"generated_text": "a fake summary"}}},
        },
    )
    return device


def _load_results(bundle_dir: Path) -> dict:
    return json.loads((bundle_dir / "results.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Full green run: results bundle + restoration on the success path
# ---------------------------------------------------------------------------


def test_full_run_green_with_results_bundle(pytester, monkeypatch):
    """The full profile (vllm + workflows + auth) runs green against the fake
    and produces the complete Results_Bundle (Req 8.1): results.json with the
    device identity and LocalServer version (Req 3.2), per-stage outcomes,
    metrics, junit.xml relocated into the bundle, and no failures/. The
    harness-started models are stopped again by restoration — the device ends
    in the state it was found (Reqs 4.3, 6.4, 8.3)."""
    device = _standard_device()
    device.enable_auth("harness", "fake-password")
    bundle = pytester.path / "bundle"
    with serve(device) as base_url:
        _configure_harness_env(
            monkeypatch,
            pytester,
            base_url,
            "vllm,onnx_models,workflows,auth_enabled",
            DDA_HARNESS_CREDENTIALS="env:FAKE_DEVICE_SECRET",
            DDA_HARNESS_EXPECTED_VISION_MODELS=VISION_MODEL,
            DDA_HARNESS_EXPECTED_VLLM_MODELS=VLLM_MODEL,
            DDA_HARNESS_EXPECTED_WORKFLOWS=WORKFLOW_ID,
        )
        monkeypatch.setenv("FAKE_DEVICE_SECRET", "harness:fake-password")
        result = _run_stages(pytester, bundle)

    assert result.ret == 0, result.stdout.str()
    result.assert_outcomes(passed=14)

    # Results_Bundle contents (Req 8.1).
    results = _load_results(bundle)
    assert results["schema_version"] == 1
    assert results["device"] == DEVICE_NAME
    assert results["local_server_version"] == FAKE_LOCAL_SERVER_VERSION
    assert results["outcome"] == "passed"
    assert set(results["stages"]) == STAGE_NAMES
    assert "vllm_generate_latency_s" in results["metrics"]
    assert "vllm_stream_token_count" in results["metrics"]
    assert results["restoration_warnings"] == []
    assert (bundle / "junit.xml").exists()
    assert not (bundle / "failures").exists()

    # State_Restoration on the success path (Reqs 4.3, 6.4, 8.3): exactly the
    # two harness-started models were stopped; the device is back as found.
    assert sorted(device.calls_of("stop")) == sorted([VISION_MODEL, VLLM_MODEL])
    assert device.models[VISION_MODEL].status == "STOPPED"
    assert device.models[VLLM_MODEL].status == "STOPPED"
    # The one-shot workflow run left the deployed workflow untouched.
    assert device.calls_of("run_workflow") == [WORKFLOW_ID]


# ---------------------------------------------------------------------------
# Honest skip on missing capability (Req 2.1)
# ---------------------------------------------------------------------------


def test_missing_capability_skips_with_recorded_reason(pytester, monkeypatch):
    """A Device_Profile without ``vllm``/``workflows`` skips those stages —
    the run stays green and every skip reason naming the capability and the
    device flows into results.json (Req 2.1)."""
    device = FakeDevice()
    device.add_model(VISION_MODEL, model_type="TritonModel", status="STOPPED")
    bundle = pytester.path / "bundle"
    with serve(device) as base_url:
        _configure_harness_env(
            monkeypatch,
            pytester,
            base_url,
            "onnx_models",
            DDA_HARNESS_EXPECTED_VISION_MODELS=VISION_MODEL,
        )
        result = _run_stages(pytester, bundle)

    assert result.ret == 0, result.stdout.str()
    # health: 3 passed + auth skipped; vision: 2 passed; vllm: 4 skipped;
    # workflows: 3 skipped; coexistence: 1 skipped.
    result.assert_outcomes(passed=5, skipped=9)

    results = _load_results(bundle)
    vllm_reasons = results["stages"]["test_20_vllm_textgen"]["skip_reasons"]
    assert f"capability 'vllm' not granted by device profile {DEVICE_NAME}" in vllm_reasons
    workflow_reasons = results["stages"]["test_30_workflows"]["skip_reasons"]
    assert f"capability 'workflows' not granted by device profile {DEVICE_NAME}" in workflow_reasons


# ---------------------------------------------------------------------------
# Declared-but-absent capability (Req 2.4)
# ---------------------------------------------------------------------------


def test_declared_but_absent_capability_fails_distinctly(pytester, monkeypatch):
    """A profile granting ``vllm`` against a device with no VllmModel entries
    fails the vLLM stages with ``CapabilityMismatchError`` — a distinct
    diagnostic contrasting the profile claim with the device observation,
    never a silent skip or an ordinary assertion failure (Req 2.4)."""
    device = FakeDevice()
    device.add_model(VISION_MODEL, model_type="TritonModel", status="STOPPED")
    bundle = pytester.path / "bundle"
    with serve(device) as base_url:
        _configure_harness_env(monkeypatch, pytester, base_url, "vllm,onnx_models")
        result = _run_stages(pytester, bundle)

    assert result.ret != 0
    # health: 3 passed + auth skipped; vision: 2 skipped (enumerate-only);
    # workflows: 3 skipped (capability); vllm 4 + coexistence 1 error out of
    # the session-scoped vllm_surface probe.
    result.assert_outcomes(passed=3, skipped=6, errors=5)
    output = result.stdout.str()
    assert f"Capability mismatch on device '{DEVICE_NAME}'" in output
    assert "claims capability 'vllm' is available" in output
    assert "no VllmModel entries" in output


# ---------------------------------------------------------------------------
# Restoration on failure paths (Reqs 4.3, 8.3) + failure captures (Req 8.2)
# ---------------------------------------------------------------------------


def test_restoration_runs_on_failure_and_spares_found_running(pytester, monkeypatch):
    """A model that goes FAILED-with-reason fails its stage with the
    device-reported reason verbatim (Req 4.2) — and restoration still runs on
    that failure path, stopping only the harness-started model while leaving
    the found-running one untouched (Reqs 4.3, 8.3). The bundle carries the
    failures/ captures (Reqs 8.1, 8.2)."""
    fail_reason = "CUDA out of memory while loading engine (fake)"
    device = FakeDevice()
    device.add_model("vision-found-running", model_type="TritonModel", status="READY")
    device.add_model(
        "vision-doomed",
        model_type="TritonModel",
        status="STOPPED",
        fail_reason=fail_reason,
        loading_polls=0,
    )
    bundle = pytester.path / "bundle"
    with serve(device) as base_url:
        _configure_harness_env(
            monkeypatch,
            pytester,
            base_url,
            "onnx_models",
            DDA_HARNESS_EXPECTED_VISION_MODELS="vision-found-running,vision-doomed",
        )
        result = _run_stages(pytester, bundle)

    assert result.ret != 0
    # health: 3 passed + auth skipped; vision: presence passes, reach-ready
    # fails; vllm 4 + workflows 3 + coexistence 1 skipped (capability).
    result.assert_outcomes(passed=4, failed=1, skipped=9)
    # The device-reported failure reason surfaces verbatim (Req 4.2).
    assert fail_reason in result.stdout.str()

    # Restoration executed on the failure path: only the harness-started
    # model was stopped; the found-running one was left exactly as found.
    assert device.calls_of("stop") == ["vision-doomed"]
    assert device.models["vision-found-running"].status == "READY"

    # The bundle records the failed run with failures/ captures (Req 8.2).
    results = _load_results(bundle)
    assert results["outcome"] == "failed"
    assert results["stages"]["test_10_vision_models"]["failed"] == 1
    captures = list((bundle / "failures").glob("*.json"))
    assert captures, "failures/ should carry at least one capture"
    capture = json.loads(captures[0].read_text(encoding="utf-8"))
    assert fail_reason in capture["message"]


# ---------------------------------------------------------------------------
# Fail-fast on unreachable target (Req 1.3)
# ---------------------------------------------------------------------------


def test_unreachable_target_fails_fast_with_returncode_2(pytester, monkeypatch):
    """An unreachable base URL aborts the whole run in setup via
    ``pytest.exit`` with returncode 2 and one diagnostic naming the URL —
    instead of failing every test individually (Req 1.3)."""
    # An ephemeral port that was bound and released: connection refused.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    base_url = f"http://127.0.0.1:{port}"

    _configure_harness_env(monkeypatch, pytester, base_url, "onnx_models")
    result = _run_stages(pytester, pytester.path / "bundle")

    assert result.ret == 2, result.stdout.str()
    output = result.stdout.str() + "\n" + result.stderr.str()
    assert f"Target device unreachable at {base_url}" in output


# ---------------------------------------------------------------------------
# Run budget exceeded (Req 8.4)
# ---------------------------------------------------------------------------


def test_run_budget_exceeded_fails_remaining_tests(pytester, monkeypatch):
    """Once the monotonic run-budget deadline passes, every remaining test
    fails at setup with an explicit budget-exceeded diagnostic, so a hung
    device degrades to a bounded, explained run (Req 8.4). With the deadline
    armed at configure and a near-zero budget, no test ever contacts the
    device — the URL can point anywhere."""
    _configure_harness_env(
        monkeypatch,
        pytester,
        "http://127.0.0.1:9",
        "onnx_models",
        DDA_HARNESS_RUN_BUDGET_S="0.01",
    )
    result = _run_stages(pytester, pytester.path / "bundle")

    assert result.ret != 0
    # Capability-gated items still skip (collection-time markers evaluate
    # first); every other test dies at setup with the budget diagnostic
    # (a setup-phase pytest.fail is reported as an error outcome).
    result.assert_outcomes(errors=5, skipped=9)
    assert "run budget exceeded" in result.stdout.str()
