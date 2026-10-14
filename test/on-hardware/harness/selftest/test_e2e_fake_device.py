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
  diagnostic (Req 8.4);
* the stream camera stage: a green run that deletes every Image_Source it
  created and resumes its pause, per-check skips naming missing inputs,
  aggregated failures with restoration still running, a stream password
  that never reaches output or the bundle, and ``CapabilityMismatchError``
  on a device without the feature.

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

import pytest
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

#: Every stage module, keyed the way results.json groups them.
STAGE_NAMES = {
    "test_00_health",
    "test_10_vision_models",
    "test_20_vllm_textgen",
    "test_25_vlm_image_generate",
    "test_30_workflows",
    "test_35_stream_cameras",
    "test_40_coexistence",
}

#: Tests per capability-gated module that skips as a whole when its
#: capability is not granted: vlm image generate (vllm) and stream cameras.
VLM_STAGE_TESTS = 2
STREAM_STAGE_TESTS = 7

# The stream camera stage's fake sources (never resolved: the fake answers
# every URL from its scripted FakeStreamServer entries).
STREAM_USER = "camuser"
#: Distinctive, so its absence from every harness artifact is meaningful.
STREAM_PASSWORD = "S3cret-Stream-Pass!"
RTSP_H264 = "rtsp://cams.test:8554/h264"
RTMP_H265 = "rtmp://cams.test:1935/live/h265"
RTSP_SECURE = "rtsp://cams.test:8554/secure"
RTSP_MISSING = "rtsp://cams.test:8554/nosuchpath"
RTSP_VP9 = "rtsp://cams.test:8554/vp9"
RTSPS_SELF_SIGNED = "rtsps://cams.test:8322/h264"
STREAM_WORKFLOW = "wf-stream-trigger"
CONTINUOUS_WORKFLOW = "wf-stream-continuous"
STREAM_FRAME = {
    "seq": 42,
    "acquiredAtMs": 1790000000123,
    "width": 1280,
    "height": 720,
    "cameraSourceId": "cfg-src",
}


def _configure_harness_env(
    monkeypatch,
    pytester,
    base_url: str,
    capabilities: str,
    config_text: str = "devices: {}\n",
    **extra_env: str,
) -> None:
    """Point the subprocess harness at the fake device via environment only.

    Clears every inherited ``DDA_HARNESS_*`` variable first and pins
    ``DDA_HARNESS_CONFIG`` to a devices.yaml in the pytester tmpdir (empty
    unless ``config_text`` carries a file-only setting), so a developer's
    local configuration can never leak into the selftest. Poll-facing
    timeouts are lowered so scripted transitions resolve in seconds while
    staying far from flaky bounds.
    """
    for name in list(os.environ):
        if name.startswith("DDA_HARNESS_"):
            monkeypatch.delenv(name, raising=False)
    config = pytester.path / "devices.yaml"
    config.write_text(config_text, encoding="utf-8")
    monkeypatch.setenv("DDA_HARNESS_CONFIG", str(config))
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
    on-hardware; the Results_Bundle lands in ``bundle_dir``. The cache
    plugin is off, so the run writes no ``.pytest_cache`` into the harness
    directory (its rootdir).
    """
    return pytester.runpytest_subprocess(
        str(STAGES_DIR), f"--harness-output-dir={bundle_dir}", "-p", "no:cacheprovider"
    )


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
    # The vlm image stage skips (no multimodal model deployed) and the
    # stream camera stage skips (capability not granted).
    result.assert_outcomes(passed=14, skipped=VLM_STAGE_TESTS + STREAM_STAGE_TESTS)

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
    # workflows: 3 skipped; coexistence: 1 skipped; vlm image and stream
    # cameras: all skipped.
    result.assert_outcomes(passed=5, skipped=9 + VLM_STAGE_TESTS + STREAM_STAGE_TESTS)

    results = _load_results(bundle)
    vllm_reasons = results["stages"]["test_20_vllm_textgen"]["skip_reasons"]
    assert f"capability 'vllm' not granted by device profile {DEVICE_NAME}" in vllm_reasons
    workflow_reasons = results["stages"]["test_30_workflows"]["skip_reasons"]
    assert f"capability 'workflows' not granted by device profile {DEVICE_NAME}" in workflow_reasons
    stream_reasons = results["stages"]["test_35_stream_cameras"]["skip_reasons"]
    assert stream_reasons == [
        f"capability 'stream_cameras' not granted by device profile {DEVICE_NAME}"
    ]


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
    # workflows and stream cameras: skipped (capability); vllm 4 + vlm
    # image 2 + coexistence 1 error out of the session-scoped vllm_surface
    # probe.
    result.assert_outcomes(passed=3, skipped=6 + STREAM_STAGE_TESTS, errors=5 + VLM_STAGE_TESTS)
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
    # fails; vllm 4 + workflows 3 + coexistence 1 + vlm image + stream
    # cameras skipped (capability).
    result.assert_outcomes(
        passed=4, failed=1, skipped=9 + VLM_STAGE_TESTS + STREAM_STAGE_TESTS
    )
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
    result.assert_outcomes(errors=5, skipped=9 + VLM_STAGE_TESTS + STREAM_STAGE_TESTS)
    assert "run budget exceeded" in result.stdout.str()


# ---------------------------------------------------------------------------
# Stream camera stage (rtsp-rtmp-stream-cameras task 25.5)
# ---------------------------------------------------------------------------

#: ``expected.stream_failures`` is file-only: the stream selftests pass it
#: through the pinned devices.yaml, everything else through the environment.
STREAM_FAILURES_CONFIG = f"""\
devices:
  {DEVICE_NAME}:
    expected:
      stream_failures:
        not_found: {RTSP_MISSING}
        unsupported_codec: {RTSP_VP9}
        tls_verification_failed: [{RTSPS_SELF_SIGNED}]
"""


def _stream_env(**overrides: str) -> dict:
    """The stream stage's inputs as ``DDA_HARNESS_*`` overrides."""
    env = {
        "DDA_HARNESS_EXPECTED_STREAM_URLS": f"{RTSP_H264},{RTMP_H265}",
        "DDA_HARNESS_EXPECTED_STREAM_SECURE_URL": RTSP_SECURE,
        "DDA_HARNESS_EXPECTED_STREAM_CREDENTIALS": "env:FAKE_STREAM_SECRET",
        "DDA_HARNESS_EXPECTED_STREAM_WORKFLOW": STREAM_WORKFLOW,
        "DDA_HARNESS_EXPECTED_CONTINUOUS_WORKFLOW": CONTINUOUS_WORKFLOW,
        "DDA_HARNESS_CONTINUOUS_WINDOW_S": "2",
    }
    env.update(overrides)
    return env


def _stream_device() -> FakeDevice:
    """A stream-capable fake answering every configured source as the
    stage expects, with a triggerable stream workflow in three versions
    (``10`` must win over ``9`` numerically; ``11`` is invalid) and a
    running continuous workflow."""
    device = FakeDevice()
    device.enable_streams(probe_503=1)
    device.add_stream_server(RTSP_H264)
    device.add_stream_server(
        RTMP_H265, codec="h265", decoder="software", decoder_element="avdec_h265"
    )
    device.add_stream_server(RTSP_SECURE, credentials=(STREAM_USER, STREAM_PASSWORD))
    device.add_stream_server(RTSP_VP9, failure="unsupported_codec")
    device.add_stream_server(RTSPS_SELF_SIGNED, failure="tls_verification_failed")
    # RTSP_MISSING has no server, so its connection test fails not_found.
    metadata = {"stream": {"rtsp_cam": dict(STREAM_FRAME)}, "trigger": {"source": "manual"}}
    device.add_registration(STREAM_WORKFLOW, "9", metadata=metadata)
    device.add_registration(STREAM_WORKFLOW, "10", metadata=metadata)
    device.add_registration(STREAM_WORKFLOW, "11", status="invalid")
    device.add_registration(CONTINUOUS_WORKFLOW, "1", continuous_fps=5.0)
    return device


def _assert_secrets_absent(secrets, bundle: Path, result) -> None:
    """None of ``secrets`` appears in the run's output or any bundle file."""
    texts = {"stdout": result.stdout.str(), "stderr": result.stderr.str()}
    for path in sorted(bundle.rglob("*")):
        if path.is_file():
            texts[str(path.relative_to(bundle))] = path.read_text(encoding="utf-8")
    for secret in secrets:
        leaked = [name for name, text in texts.items() if secret in text]
        assert not leaked, f"a stream credential leaked into: {leaked}"


def test_stream_stage_green_with_restoration(pytester, monkeypatch):
    """With ``stream_cameras`` granted and every input configured, the
    stream stage passes against the fake: capabilities (after one 503
    while the probe runs), the URL, credential, failure-category,
    triggered-run and continuous checks. Restoration leaves the device as
    found: every created Image_Source deleted, the pause resumed exactly
    once, and the stream password appears in no output or bundle file."""
    device = _stream_device()
    continuous_id = f"{CONTINUOUS_WORKFLOW}-v1-arm64_jp6"
    bundle = pytester.path / "bundle"
    with serve(device) as base_url:
        _configure_harness_env(
            monkeypatch,
            pytester,
            base_url,
            "stream_cameras",
            config_text=STREAM_FAILURES_CONFIG,
            **_stream_env(),
        )
        monkeypatch.setenv("FAKE_STREAM_SECRET", f"{STREAM_USER}:{STREAM_PASSWORD}")
        result = _run_stages(pytester, bundle)

    assert result.ret == 0, result.stdout.str()
    # health: 3 passed + auth skipped; vision: 2 skipped (none expected);
    # vllm 4 + vlm image 2 + workflows 3 + coexistence 1 skipped
    # (capability); stream cameras: all passed.
    result.assert_outcomes(passed=3 + STREAM_STAGE_TESTS, skipped=13)

    results = _load_results(bundle)
    assert results["stages"]["test_35_stream_cameras"]["passed"] == STREAM_STAGE_TESTS
    assert results["restoration_warnings"] == []
    metrics = results["metrics"]
    assert metrics["stream_h264_hardware_decoder"] == "nvv4l2decoder"
    assert metrics["stream_h265_hardware_decoder"] is None
    assert metrics["stream_h265_software_decoder"] == "avdec_h265"
    assert metrics["stream_pyav_version"] == "17.1.0"
    assert metrics[f"stream_source[{RTSP_H264}]"]["codec"] == "h264"
    assert metrics[f"stream_source[{RTSP_H264}]"]["decoder"] == "hardware"
    assert metrics[f"stream_source[{RTMP_H265}]"]["decoderElement"] == "avdec_h265"
    assert metrics[f"stream_source[{RTMP_H265}]"]["sourceFps"] == 25.0
    assert metrics[f"stream_failure[{RTSP_MISSING}]"]["category"] == "not_found"
    assert metrics["stream_workflow_frame"] == {"rtsp_cam": STREAM_FRAME}
    assert metrics["continuous_workflow_window"]["counterDeltas"]["completed"] > 0
    assert metrics["continuous_pause_window"]["counterDeltas"]["completed"] == 0

    # Every source the stage created (2 URLs, the secure one, 3 failures)
    # was deleted again, with the auto decoder policy on each.
    created = device.calls_of("create_image_source")
    assert len(created) == 6
    assert device.image_sources == {}
    assert sorted(device.calls_of("delete_image_source")) == sorted(created)
    creates = [body for body in device.image_source_requests if "type" in body]
    assert all(body["streamSettings"] == {"decoder": "auto"} for body in creates)
    assert {body["type"] for body in creates} == {"RTSP", "RTMP"}
    # The pause was undone exactly once (the check's own resume; the
    # restoration entry then had nothing left to do).
    assert device.calls_of("pause") == [continuous_id]
    assert device.calls_of("resume") == [continuous_id]
    assert not device.continuous[continuous_id].paused
    # The highest registered version was triggered, never the invalid one.
    assert device.calls_of("trigger") == [f"{STREAM_WORKFLOW}-v10-arm64_jp6"]

    # The device received the real password (its connection test accepted
    # it) and a wrong one; neither reaches any harness artifact.
    wrong = [
        body["credentials"]["password"]
        for body in device.image_source_requests
        if "type" not in body and body.get("credentials")
    ]
    assert len(wrong) == 1 and wrong[0] != STREAM_PASSWORD
    _assert_secrets_absent([STREAM_PASSWORD, wrong[0]], bundle, result)


def test_stream_stage_without_inputs_skips_each_check_naming_its_key(pytester, monkeypatch):
    """With ``stream_cameras`` granted but no stream inputs configured, the
    capability check runs and every other check skips with a reason naming
    its missing ``expected.*`` key; nothing is created on the device."""
    device = FakeDevice()
    device.enable_streams()
    bundle = pytester.path / "bundle"
    with serve(device) as base_url:
        _configure_harness_env(monkeypatch, pytester, base_url, "stream_cameras")
        result = _run_stages(pytester, bundle)

    assert result.ret == 0, result.stdout.str()
    # health 3 + stream capabilities 1 passed; the other 6 stream checks
    # skip, plus the 13 skips of the other stages.
    result.assert_outcomes(passed=4, skipped=13 + STREAM_STAGE_TESTS - 1)
    reasons = _load_results(bundle)["stages"]["test_35_stream_cameras"]["skip_reasons"]
    assert sorted(reasons) == sorted(
        [
            f"expected.stream_urls is not configured for device {DEVICE_NAME}",
            f"expected.stream_secure_url and expected.stream_credentials are not "
            f"configured for device {DEVICE_NAME}",
            f"expected.stream_failures is not configured for device {DEVICE_NAME}",
            f"expected.stream_workflow is not configured for device {DEVICE_NAME}",
            f"expected.continuous_workflow is not configured for device {DEVICE_NAME}",
        ]
    )
    assert device.calls == []


def test_stream_stage_failures_reported_and_sources_still_deleted(pytester, monkeypatch):
    """Failures are aggregated per check and restoration still runs:

    * a URL with no stream fails its connection test, and a failed inline
      DELETE is reported and retried by restoration at teardown;
    * a device that echoes the credentials is caught, naming the JSON path
      and never the value;
    * a failure category mismatch names both categories;
    * a continuous workflow found paused is left paused (both continuous
      checks skip) and no pause/resume is issued.
    """
    device = FakeDevice()
    device.enable_streams()
    device.add_stream_server(RTSP_H264)
    device.add_stream_server(RTSP_SECURE, credentials=(STREAM_USER, STREAM_PASSWORD))
    device.add_stream_server(RTSP_VP9, failure="decoder_unavailable")
    device.leak_credentials = True
    device.fail_deletes = 1
    continuous_id = device.add_registration(
        CONTINUOUS_WORKFLOW, "1", continuous_fps=5.0, continuous_paused=True
    )
    failures_config = (
        f"devices:\n  {DEVICE_NAME}:\n    expected:\n"
        f"      stream_failures:\n        unsupported_codec: {RTSP_VP9}\n"
    )
    bundle = pytester.path / "bundle"
    with serve(device) as base_url:
        _configure_harness_env(
            monkeypatch,
            pytester,
            base_url,
            "stream_cameras",
            config_text=failures_config,
            **_stream_env(DDA_HARNESS_EXPECTED_STREAM_WORKFLOW=""),
        )
        monkeypatch.setenv("FAKE_STREAM_SECRET", f"{STREAM_USER}:{STREAM_PASSWORD}")
        result = _run_stages(pytester, bundle)

    assert result.ret != 0
    # health 3 + capabilities 1 passed; URLs, credentials and failure
    # categories fail; the triggered-run check (no workflow) and both
    # continuous checks (found paused) skip, plus the 13 other skips.
    result.assert_outcomes(passed=4, failed=3, skipped=16)
    output = result.stdout.str()
    assert f"{RTMP_H265}: test-connection answered ok=False, category='not_found'" in output
    assert "failed (restoration retries it at teardown)" in output
    assert "carries credential material at debugLocation: a credential value" in output
    assert "carries credential material at debugLocation: URL user information" in output
    assert (
        f"{RTSP_VP9}: expected ok=False with category 'unsupported_codec', got ok=False "
        "with category 'decoder_unavailable'"
    ) in output
    results = _load_results(bundle)
    assert f"was found paused on device {DEVICE_NAME}" in " ".join(
        results["stages"]["test_35_stream_cameras"]["skip_reasons"]
    )

    # Restoration on the failure path: the source whose inline DELETE
    # failed was deleted at teardown, and nothing else is left behind.
    assert device.image_sources == {}
    deletes = device.calls_of("delete_image_source")
    assert len(deletes) == len(device.calls_of("create_image_source")) + 1
    assert results["restoration_warnings"] == []
    # The operator's pause was left alone.
    assert device.calls_of("pause") == [] and device.calls_of("resume") == []
    assert device.continuous[continuous_id].paused
    _assert_secrets_absent([STREAM_PASSWORD], bundle, result)


def test_stream_capability_declared_but_absent_fails_distinctly(pytester, monkeypatch):
    """A profile granting ``stream_cameras`` against a device whose
    ``/streams/capabilities`` answers 404 (no stream camera feature) fails
    every stream check with ``CapabilityMismatchError`` (Req 2.4)."""
    device = FakeDevice()
    bundle = pytester.path / "bundle"
    with serve(device) as base_url:
        _configure_harness_env(monkeypatch, pytester, base_url, "stream_cameras", **_stream_env())
        result = _run_stages(pytester, bundle)

    assert result.ret != 0
    # health 3 passed; the 13 other skips; every stream check errors out of
    # the session-scoped stream_cameras_surface probe.
    result.assert_outcomes(passed=3, skipped=13, errors=STREAM_STAGE_TESTS)
    output = result.stdout.str()
    assert f"Capability mismatch on device '{DEVICE_NAME}'" in output
    assert "claims capability 'stream_cameras' is available" in output
    assert "GET /streams/capabilities did not answer" in output
    assert device.calls == []


@pytest.mark.parametrize(
    "misbehavior, reported",
    [
        # The check dies right after pausing: restoration resumes the
        # workflow at teardown, because the resume was recorded before the
        # pause was issued.
        ("status_fails_while_paused", "scripted status failure"),
        # Runs go on while the status says paused: the check reports it and
        # resumes the workflow itself.
        ("runs_go_on_while_paused", "runs went on while paused"),
    ],
)
def test_stream_pause_failures_still_leave_the_workflow_resumed(
    pytester, monkeypatch, misbehavior, reported
):
    """A pause check that fails, however it fails, leaves the continuous
    workflow running again with exactly one resume and no restoration
    warning."""
    device = FakeDevice()
    device.enable_streams()
    continuous_id = device.add_registration(CONTINUOUS_WORKFLOW, "1", continuous_fps=5.0)
    if misbehavior == "status_fails_while_paused":
        device.fail_status_while_paused = True
    else:
        device.continuous[continuous_id].ignores_pause = True
    bundle = pytester.path / "bundle"
    with serve(device) as base_url:
        _configure_harness_env(
            monkeypatch,
            pytester,
            base_url,
            "stream_cameras",
            DDA_HARNESS_EXPECTED_CONTINUOUS_WORKFLOW=CONTINUOUS_WORKFLOW,
            DDA_HARNESS_CONTINUOUS_WINDOW_S="2",
            # Bounds the settle wait: counters that keep moving never settle.
            DDA_HARNESS_WORKFLOW_OUTPUT_S="3",
        )
        result = _run_stages(pytester, bundle)

    assert result.ret != 0
    # health 3 + capabilities 1 + continuous rate 1 passed; the pause check
    # fails; the 4 other stream checks (no inputs) and 13 other skips.
    result.assert_outcomes(passed=5, failed=1, skipped=17)
    assert reported in result.stdout.str()
    assert device.calls_of("pause") == [continuous_id]
    assert device.calls_of("resume") == [continuous_id]
    assert not device.continuous[continuous_id].paused
    assert _load_results(bundle)["restoration_warnings"] == []
