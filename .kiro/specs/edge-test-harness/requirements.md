# Requirements Document

## Introduction

On-hardware validation of the DDA edge stack is currently a manual runbook (`test/on-hardware/jp6_vllm_validation.md`): an operator clicks through the portal and the device web UX to verify that deployed models load, text generation answers, and workflows produce output. A full pass takes an afternoon, is easy to skip, and its coverage silently drifts as features land. Recent regressions (vLLM tokenizer/dependency breakage, model-reference errors) were each discovered one deploy-fail-diagnose cycle at a time because nothing exercised the deployed device end-to-end.

The edge-test-harness feature turns that runbook into a repeatable, one-command pytest suite that runs from a workstation or build host against a live edge device's HTTP APIs. The harness validates what is already deployed on the device — it does not register, package, publish, or deploy anything, and it must leave the device in the state it found it. It is device-profile aware: stages that a target cannot support (vLLM on JP4/JP5, DLR models on JP6) are skipped with recorded reasons rather than failed, so one suite serves the whole fleet. Results land as standard pytest output plus a machine-readable results bundle suitable for regression tracking across component versions.

Reaching the device is the operator's concern (LAN access or an SSH tunnel with forwarded ports); the harness only needs a reachable base URL. Cloud-side orchestration (deploying the stack under test) stays with the existing portal flows and is out of scope.

## Glossary

- **Edge_Test_Harness**: The pytest-based test suite and its supporting library delivered by this feature, run from a host with HTTP access to a Target_Device.
- **Target_Device**: A physical or virtual edge device running the DDA LocalServer component whose deployed stack the Edge_Test_Harness validates.
- **Backend_API**: The device FastAPI backend (default port 5000): health, feature-configurations model lifecycle, image sources, workflow endpoints, and the Text_Generation_API router.
- **Text_Generation_API**: The Backend_API router (`endpoints/text_generation.py`) exposing non-streaming generate and SSE streaming over loaded vLLM models.
- **Device_Profile**: The declared characteristics of a Target_Device the harness uses for stage selection: architecture/JetPack generation (`x86_64`, `arm64_jp4`, `arm64_jp5`, `arm64_jp6`) and Capability_Flags.
- **Capability_Flag**: A per-device boolean in the Device_Profile enabling or disabling a capability-gated Test_Stage (e.g. `vllm`, `dlr_models`, `onnx_models`, `workflows`, `auth_enabled`).
- **Test_Stage**: A named group of harness tests validating one functional area: backend health, vision model lifecycle, vLLM model lifecycle, text generation, workflow execution, coexistence.
- **Vision_Model**: A Triton-served model component (DLR/Neo or ONNX) exposed through the Backend_API feature-configurations endpoints.
- **VLLM_Model**: A model of runtime type `vllm` exposed through the Backend_API feature-configurations endpoints and served by the device's companion vLLM runtime.
- **Deployed_Workflow**: A workflow component (`dda.workflow.*`) present on the Target_Device and manageable through the Backend_API workflow endpoints.
- **Harness_Configuration**: The file- or environment-supplied inputs naming the Target_Device (base URL, Device_Profile, credentials reference, timeouts) — no harness code changes per device.
- **Results_Bundle**: The persisted output of one harness run: pytest outcome, JUnit XML, per-stage pass/fail/skip with reasons, target identity (device name, LocalServer version), and captured diagnostics for failures.
- **State_Restoration**: The harness guarantee that models and workflows it started or stopped are returned to their pre-run state before the run ends, including on test failure.

## Requirements

### Requirement 1: One-Command Invocation Against a Configured Target

**User Story:** As a developer validating a device after a deployment, I want to run the full on-hardware suite with a single command against a named device, so that a regression pass costs minutes of attention instead of an afternoon of portal clicking.

#### Acceptance Criteria

1. THE Edge_Test_Harness SHALL run as a standard pytest invocation taking the Target_Device selection from Harness_Configuration (config file entry or environment variables) without requiring code changes per device.
2. THE Harness_Configuration SHALL declare, per Target_Device: a base URL for the Backend_API, a Device_Profile, an optional credentials reference, and optional stage timeout overrides.
3. WHEN the configured Target_Device is unreachable at its base URL, THE Edge_Test_Harness SHALL fail fast during collection/setup with a diagnostic naming the URL and the connection error, rather than failing each test individually.
4. THE Edge_Test_Harness SHALL support selecting a subset of Test_Stages through standard pytest selection (markers/keywords) without editing test code.
5. THE Edge_Test_Harness SHALL run non-interactively end to end (no prompts), so it can execute unattended from a build host or CI job.

### Requirement 2: Device-Profile-Aware Stage Selection

**User Story:** As an operator with a mixed JP4/JP5/JP6 fleet, I want one suite that adapts to each device's capabilities, so that unsupported stages are recorded as skipped-with-reason instead of failing the run.

#### Acceptance Criteria

1. WHEN a Test_Stage requires a Capability_Flag the Target_Device's Device_Profile does not grant, THE Edge_Test_Harness SHALL skip that stage's tests and record the skip reason naming the missing capability.
2. THE Edge_Test_Harness SHALL gate vLLM model lifecycle, text generation, and coexistence stages on the `vllm` Capability_Flag (granted for `arm64_jp6` and, when enabled, JP5 targets).
3. THE Edge_Test_Harness SHALL gate DLR/Neo Vision_Model assertions on the `dlr_models` Capability_Flag, so JP6 targets (TensorRT 10, no TRT8 `libnvinfer.so.8`) skip them with a recorded reason instead of failing.
4. WHEN the Device_Profile's declared capabilities contradict the device's observed state (e.g. `vllm` granted but the Backend_API reports no vLLM runtime active), THE Edge_Test_Harness SHALL fail the affected stage with a diagnostic distinguishing "capability declared but absent on device" from an ordinary test failure.

### Requirement 3: Backend Health and Readiness Stage

**User Story:** As a developer, I want the suite to establish the device baseline first, so that downstream stage failures are attributable to features rather than a dead backend.

#### Acceptance Criteria

1. THE Edge_Test_Harness SHALL verify the Backend_API answers its health/readiness surface before any other Test_Stage runs.
2. THE Edge_Test_Harness SHALL record the Target_Device identity in the Results_Bundle: device name, LocalServer component version as reported by the device, and the Device_Profile used.
3. WHEN the Backend_API requires authentication (`auth_enabled` Capability_Flag), THE Edge_Test_Harness SHALL authenticate using the configured credentials reference, SHALL fail fast with a clear diagnostic when authentication fails, and SHALL NOT write credential values into logs or the Results_Bundle.

### Requirement 4: Vision Model Lifecycle Stage

**User Story:** As a developer, I want deployed vision models exercised through start/status/stop, so that Triton-served model regressions surface as named test failures.

#### Acceptance Criteria

1. THE Edge_Test_Harness SHALL enumerate the Vision_Models the Backend_API reports and assert that every model expected by the Harness_Configuration for this Target_Device is present.
2. WHEN the harness starts a Vision_Model that is not running, THE Edge_Test_Harness SHALL assert the model reaches READY state within the stage timeout, and SHALL treat a FAILED state or timeout as a stage failure carrying the device-reported reason.
3. THE Edge_Test_Harness SHALL stop only the Vision_Models it started, restoring each to its pre-run state (State_Restoration) even when assertions fail mid-stage.

### Requirement 5: vLLM Model Lifecycle and Text Generation Stage

**User Story:** As a developer, I want the deployed vLLM model loaded and answering generate calls under test, so that the engine/dependency regressions we have been finding by hand fail a named test instead.

#### Acceptance Criteria

1. WHEN the `vllm` Capability_Flag is granted, THE Edge_Test_Harness SHALL assert each expected VLLM_Model is reported by the Backend_API and reaches READY within the stage timeout, treating FAILED-with-reason as a stage failure surfacing the device-reported reason verbatim.
2. THE Edge_Test_Harness SHALL issue a non-streaming generate request through the Text_Generation_API against a READY VLLM_Model and assert a well-formed response containing non-empty generated text within the stage timeout.
3. THE Edge_Test_Harness SHALL issue a streaming (SSE) generate request and assert incremental chunks arrive and terminate correctly.
4. THE Edge_Test_Harness SHALL record generate latency and token counts in the Results_Bundle as informational metrics without asserting performance thresholds by default.

### Requirement 6: Workflow Execution Stage

**User Story:** As a developer, I want a deployed workflow started and observed producing output, so that workflow-engine and node regressions (including `llm_inference`) are caught on hardware.

#### Acceptance Criteria

1. WHEN the `workflows` Capability_Flag is granted, THE Edge_Test_Harness SHALL enumerate Deployed_Workflows and assert every workflow expected by the Harness_Configuration is present.
2. WHEN the harness starts a Deployed_Workflow, THE Edge_Test_Harness SHALL assert it reaches its running state and produces observable output (captured artifacts or output metadata via the Backend_API) within the stage timeout.
3. WHEN a Deployed_Workflow contains an `llm_inference` node and the `vllm` Capability_Flag is granted, THE Edge_Test_Harness SHALL assert the workflow's output metadata carries the node's generated content.
4. THE Edge_Test_Harness SHALL stop workflows it started and leave workflows it found running untouched (State_Restoration).

### Requirement 7: Coexistence Stage

**User Story:** As a developer, I want vision and LLM serving validated simultaneously on one device, so that GPU/memory contention regressions surface before a customer hits them.

#### Acceptance Criteria

1. WHEN the `vllm` Capability_Flag is granted and at least one Vision_Model is available, THE Edge_Test_Harness SHALL bring a Vision_Model and a VLLM_Model to READY simultaneously and assert both remain READY while a generate request completes successfully.
2. IF either model leaves READY during the coexistence window, THEN THE Edge_Test_Harness SHALL fail the stage with both models' device-reported states in the diagnostic.

### Requirement 8: Results, Diagnostics, and Non-Destructiveness

**User Story:** As a team tracking regressions across component versions, I want every run to leave a comparable results artifact and an untouched device, so that runs are safe to repeat and their outcomes are auditable.

#### Acceptance Criteria

1. THE Edge_Test_Harness SHALL emit a Results_Bundle per run: overall outcome, JUnit XML, per-stage pass/fail/skip with reasons, target identity, and run timestamp, written to a configurable output directory.
2. WHEN a test fails against the Backend_API, THE Edge_Test_Harness SHALL capture the failing request, response status, and response body (bounded in size) into the failure diagnostic.
3. THE Edge_Test_Harness SHALL uphold State_Restoration across all stages via teardown that runs on failure paths, and SHALL never call Greengrass/cloud APIs or mutate deployments, model registries, or device configuration beyond starting/stopping the models and workflows under test.
4. THE Edge_Test_Harness SHALL complete a full run within a configurable overall time budget, with each stage bounded by its own timeout so a hung device cannot stall the run indefinitely.
