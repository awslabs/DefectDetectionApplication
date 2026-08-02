# Implementation Plan

## Overview

Build the Edge_Test_Harness: a pytest suite under `test/on-hardware/harness/` that validates a live edge device through its Backend_API — health, vision-model lifecycle, vLLM text generation, workflows, and coexistence — with capability gating, state restoration, a run budget, and a machine-readable results bundle. Host-side selftests (unit + fake-device end-to-end) keep the harness itself verifiable without hardware.

## Task Dependency Graph

```json
{
  "waves": [
    {"wave": 1, "tasks": ["1"], "description": "Scaffold the harness package and configuration layer"},
    {"wave": 2, "tasks": ["2"], "description": "Implement the EdgeApiClient and SSE parser"},
    {"wave": 3, "tasks": ["3"], "description": "Implement StateRegistry and ResultsPlugin"},
    {"wave": 4, "tasks": ["4"], "description": "Implement conftest wiring: fail-fast, capability gating, budget"},
    {"wave": 5, "tasks": ["5"], "description": "Implement the five stage modules"},
    {"wave": 6, "tasks": ["6"], "description": "Build the fake device and end-to-end selftests"},
    {"wave": 7, "tasks": ["7"], "description": "Checkpoint — harness selftests green in repo CI"},
    {"wave": 8, "tasks": ["8"], "description": "Documentation and on-hardware smoke recipe"}
  ]
}
```

## Tasks

- [x] 1. Scaffold the harness package and configuration layer
  - Create `test/on-hardware/harness/` layout: `harnesslib/`, `stages/`, `selftest/`, `pytest.ini` (marker declarations, junitxml addopts), `devices.yaml.example`
  - Implement `harnesslib/config.py`: `DeviceProfile` / `DeviceTarget` dataclasses, `load_config()` merging `devices.yaml` + `DDA_HARNESS_DEVICE` selection + `DDA_HARNESS_*` env overrides; validate architecture against the known set and reject unknown capability names (fail closed); parse `env:`/`file:` credential references without reading values into config reprs
  - Unit tests in `selftest/test_config.py`: file+env merge precedence, unknown capability rejected, credential reference parsing, timeout defaults
  - _Requirements: 1.1, 1.2, 2.1_

- [x] 2. Implement the EdgeApiClient and SSE parser
  - `harnesslib/client.py`: `requests.Session` wrapper with per-call timeouts; endpoint methods (`system_health`, `component_status`, `auth_status`/`login`, `feature_configurations`, `start_model`/`stop_model`, `wait_for_model_state` poll loop with backoff surfacing device-reported reasons verbatim, `textgen_models`, `generate`, `generate_stream`, `workflows`, `run_workflow`, `workflow_images`, `capture_task`)
  - `DeviceApiError` carrying method/path/status/8KB-bounded body excerpt/elapsed; diagnostics formatter redacts the `Authorization` header
  - `harnesslib/sse.py`: minimal `data:`-framed event parser over `iter_lines` with clean-termination detection
  - Unit tests in `selftest/test_client.py` + `selftest/test_sse.py` (mocked transport): bearer token attach after login, token never in error reprs, body excerpt bounding, poll-loop terminal states, SSE chunking/termination/malformed-stream handling
  - _Requirements: 3.3, 4.2, 5.1, 5.3, 8.2_

- [x] 3. Implement StateRegistry and ResultsPlugin
  - `harnesslib/restoration.py`: session registry recording `(kind, name, pre_state)`; reverse-order teardown stopping only harness-started entries; restoration failures collected as warnings, never raising into test outcomes
  - `harnesslib/results.py`: pytest plugin hooking `pytest_runtest_logreport`/`pytest_sessionfinish`; write `results.json` (schema_version 1: device identity, profile, LocalServer version, per-stage pass/fail/skip with reasons, metrics, restoration_warnings) into `--harness-output-dir` (default `harness-results/<device>-<timestamp>/`) alongside junit.xml and `failures/` captures; `record_metric` fixture channel
  - Unit tests in `selftest/test_restoration.py` + `selftest/test_results.py`: teardown ordering, found-running entries untouched, warning capture, results schema contents, skip reasons propagated
  - _Requirements: 8.1, 8.2, 8.3, 4.3, 5.4_

- [x] 4. Implement conftest wiring: fail-fast, capability gating, budget
  - `conftest.py`: session fixtures (config load, client construction, auth handshake when `auth_enabled`); reachability probe calling `pytest.exit(rc=2)` with URL + error on connection failure; `device_identity` fixture populated by the health stage
  - `pytest_collection_modifyitems` hook: skip capability-marked items absent from the device profile with reasons naming the capability and device
  - Capability probe fixtures raising `CapabilityMismatchError` (distinct message: profile claim vs device observation) for `vllm` and `workflows` stages
  - Run-budget deadline: monotonic deadline from `run_budget_s`; `pytest_runtest_setup` fails remaining tests with a budget-exceeded message once past it
  - Covered by task 6 selftests (pytester-driven)
  - _Requirements: 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 8.4_

- [x] 5. Implement the five stage modules
  - `stages/test_00_health.py`: health/readiness, device identity capture (LocalServer version via `/dda-component-status`), auth fail-fast (Reqs 3.1–3.3)
  - `stages/test_10_vision_models.py`: enumeration vs `expected.vision_models`, start→READY within `model_ready_s` with device-reported reason on failure, registry registration; DLR expectations gated on `dlr_models` (Reqs 4.1–4.3, 2.3)
  - `stages/test_20_vllm_textgen.py` (`capability: vllm`): expected VLLM_Models READY within `vllm_ready_s`; non-streaming generate asserting non-empty text; streaming generate asserting incremental chunks + termination; latency/token metrics recorded (Reqs 5.1–5.4)
  - `stages/test_30_workflows.py` (`capability: workflows`): enumeration vs expectations; run one workflow → observable output within `workflow_output_s`; `llm_inference` metadata assertion when applicable; registry registration (Reqs 6.1–6.4)
  - `stages/test_40_coexistence.py` (`capability: vllm`): vision + vLLM READY simultaneously through a completed generate; dual-state diagnostic on departure (Reqs 7.1, 7.2)
  - _Requirements: 3.1–3.3, 4.1–4.3, 5.1–5.4, 6.1–6.4, 7.1, 7.2_

- [x] 6. Build the fake device and end-to-end selftests
  - `selftest/fake_device.py`: in-process FastAPI app imitating the touched Backend_API surface — feature-configurations with scriptable state transitions (LOADING→READY, FAILED-with-reason), start/stop, text-generation (canned non-streaming + SSE), workflows with output metadata, optional local-auth
  - Pytester-driven selftests running the real stages against the fake: honest skip on missing capability, `CapabilityMismatchError` on declared-but-absent, restoration executed on failure paths, fail-fast on unreachable target, results bundle contents (junit + results.json + failures/), budget-exceeded behavior
  - _Requirements: 1.3, 2.1, 2.4, 4.3, 6.4, 8.1, 8.3, 8.4_

- [x] 7. Checkpoint — harness selftests green in repo CI
  - Run the full selftest suite (`pytest test/on-hardware/harness/selftest`) plus repo lint/gates touched by the new files; fix any failures
  - Ensure the harness package is excluded from suites that must not require a device (stages collect but skip cleanly when no `DDA_HARNESS_DEVICE` is configured)
  - _Requirements: 1.5_

- [x] 8. Documentation and on-hardware smoke recipe
  - `test/on-hardware/harness/README.md`: invocation, `devices.yaml` format, credential references, SSH-tunnel recipe for remote devices, results bundle layout, stage/marker selection examples
  - Update `test/on-hardware/jp6_vllm_validation.md`: mark the stages the harness now automates and point to the harness invocation; keep the deploy-side (portal) steps as the remaining manual procedure
  - Document the jp6-orinagx smoke run as the reference example (profile, expected models incl. `opt125m-smoke`)
  - _Requirements: 1.1, 2.2, 2.3_

## Notes

- All 8 tasks completed. Final state: 112 host-side selftests passing; all 14 stage tests collect and skip cleanly when no `DDA_HARNESS_DEVICE` is configured.
- Tasks are sequential — each builds on the modules of the previous one.
- Run artifacts (`harness-results/`) and per-operator `devices.yaml` are gitignored.
