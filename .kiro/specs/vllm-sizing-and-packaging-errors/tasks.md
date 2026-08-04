# Implementation Plan: vLLM Sizing and Packaging Errors

## Overview

Implements per-model vLLM GPU memory sizing (visibility, post-import editing,
preflight fit check, device-side 409 surfacing) and actionable packaging error
messages in the workflow builder. Portal changes (Lambda + frontend) ship via the
portal deploy (`deploy-infrastructure.sh` + frontend deploy); the
`vllm_model_prep.py` change is device-side and rides the next LocalServer build.

> **Execution is deferred.** Do not start these tasks until after a JP5 build
> publishes. This plan is a planning artifact only.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "6", "7"],
      "description": "Independent starting points: the pure fit-check module, the workflow-builder dialog fix, and the device-side log improvement"
    },
    {
      "wave": 2,
      "tasks": ["2", "3"],
      "description": "Backend wiring that consumes the fit-check module: detail exposure, update endpoint, registration findings, and the publish gate"
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "Checkpoint: backend tests pass"
    },
    {
      "wave": 4,
      "tasks": ["5"],
      "description": "Models GUI view/edit, depending on the backend detail and update endpoints"
    },
    {
      "wave": 5,
      "tasks": ["8"],
      "description": "Final checkpoint across portal and device-side changes"
    }
  ]
}
```

Tasks 6 (workflow builder frontend) and 7 (device-side script) are independent of
the sizing work and of each other; they can run in parallel with tasks 1–5.

## Tasks

- [x] 1. Create the fit-check module (`edge-cv-portal/backend/functions/vllm_fit_check.py`)
  - [x] 1.1 Implement Device_Memory_Profile, Minimum_KV_Cache, and `evaluate_fit`
    - `DEVICE_MEMORY_PROFILE_BYTES` (`arm64_jp6` → 30 GiB usable, `arm64_jp5` entry behind the same table), `MINIMUM_KV_CACHE_BYTES`, `DTYPE_BYTES`
    - `FitFinding` dataclass; `evaluate_fit(engine_configuration, estimate, architectures)` computing `fits = gpu_memory_utilization × profile[arch] ≥ estimate + min_kv_cache`, with failing messages naming the profile entry, budget, estimate, and "raise gpu_memory_utilization / reduce max_model_len / smaller model" remediation (never advising to lower it)
    - _Requirements: 3.1, 3.8, 3.9_

  - [x] 1.2 Implement `estimate_weights` with injected fetchers
    - HF source: sum `*.safetensors` sizes from `https://huggingface.co/api/models/{id}?blobs=true`; fallback to parameter count × dtype bytes; size quantized variants from `quantization_config`; short (~5 s) timeout
    - S3 source: artifact `ContentLength` via injected `s3_head`
    - Return `None` on any fetch/parse failure (callers report "unverified", never block)
    - _Requirements: 3.2, 3.3, 3.4_

  - [x]* 1.3 Write property test for fit decision correctness
    - **Property 4: Fit_Check decision correctness**
    - **Validates: Requirements 3.1, 3.8, 3.9**

  - [x]* 1.4 Write unit tests for weight estimation
    - Fixtures: safetensors index, param-count fallback, quantization config, S3 ContentLength, fetch failure → `None`
    - _Requirements: 3.2, 3.3, 3.4_

- [x] 2. Wire the fit check and engine-configuration editing into the portal backend
  - [x] 2.1 Expose `engine_configuration` in the model detail response
    - `models.py get_model`: include the stored Engine_Configuration (Decimal-safe) for vLLM records
    - _Requirements: 1.2_

  - [x] 2.2 Add `PUT /api/v1/models/vllm/{training_id}/engine-configuration` (`model_import.py`)
    - Validate supplied settings with the existing `_validate_engine_setting` / unknown-key fail-closed rules; reject non-vLLM records (400); overlay onto the stored config and write back; audit with before/after values; return the complete updated configuration, a re-package/publish notice, and a fit-check result
    - Route wiring in the model-import router and API Gateway resources (`compute-stack.ts`)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.6_

  - [x] 2.3 Add non-blocking fit-check findings to registration and update responses
    - `register_vllm_model` and the update handler evaluate the fit over `vllm_supported_architectures()` and include `fit_check: {status, estimate, findings}` (registration/update never blocked by the check)
    - _Requirements: 3.4, 3.5_

  - [x]* 2.4 Write property test for the update round trip
    - **Property 2: Engine-configuration update round trip**
    - **Validates: Requirements 2.1, 2.4**

  - [x]* 2.5 Write property test for invalid-update rejection
    - **Property 3: Invalid updates change nothing**
    - **Validates: Requirements 2.2**

  - [x]* 2.6 Write property test for fail-open on unverifiable estimates
    - **Property 5: Unverifiable estimates never block**
    - **Validates: Requirements 3.4**

  - [x]* 2.7 Write unit tests for detail exposure, non-vLLM rejection, and audit event
    - `get_model` includes the config; PUT against a vision record → 400; audit event carries previous and updated values
    - _Requirements: 1.2, 2.3, 2.6_

  - [x]* 2.8 Write property test for packaging preservation (regression guard on the existing path)
    - **Property 1: Packaging preserves the stored engine configuration**
    - **Validates: Requirements 1.1**

- [x] 3. Add the publish-time fit gate (`greengrass_publish.py`)
  - [x] 3.1 Gate the vLLM publish branch on the fit check
    - Before any component registration: `estimate_weights` + `evaluate_fit`; if every supported arch fails and `skip_fit_check` is absent → 422 with the full sizing message and findings; with `skip_fit_check: true` → proceed and record the override in the audit event; `None` estimate → proceed with an "unverified" annotation
    - _Requirements: 3.6, 3.7, 3.4_

  - [x]* 3.2 Write unit tests for the publish gate
    - All-arch failure → 422 with no `create_component_version` call; override proceeds and audits; unverified estimate proceeds
    - _Requirements: 3.6, 3.7, 3.4_

- [x] 4. Checkpoint - Ensure all backend tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - Verified via the full portal backend suite run (1687 passed; all failures triaged as known pre-existing issues) and focused packaging/publish/vllm suites (103 passed).

- [x] 5. Surface and edit the engine configuration in the Models GUI
  - [x] 5.1 Render the Engine_Configuration section on `ModelDetail.tsx`
    - Key/value display of every stored setting for vLLM models; add `apiService.updateVllmEngineConfiguration` and the detail-response type extension in `services/api.ts`
    - _Requirements: 1.3_

  - [x] 5.2 Implement the edit form
    - Inline edit reusing the `RegisterLlm.tsx` field-rendering and per-field finding patterns; on success show the re-package/publish notice and any fit-check warnings
    - _Requirements: 2.5, 3.5_

  - [x]* 5.3 Write component tests for the detail section and edit flow
    - Display completeness, submit success with notice, validation findings rendered per field
    - _Requirements: 1.3, 2.5_

- [x] 6. Fix the Package_Dialog error content (`WorkflowToolbar.tsx`)
  - [x] 6.1 Compose message + failing artifact and the unpublished-model hint
    - Replace the overwriting `failing_artifact` branch with `"${err.message} (failing artifact: ${artifact})"`; append the Models-page publish hint when the artifact is `models/{name}` and the message states the model has no published Greengrass component; keep the findings branch first and the "already exists" rewrite last; no structured details → plain `err.message`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [x]* 6.2 Write property test for dialog content composition
    - **Property 7: Package_Dialog message composition**
    - **Validates: Requirements 5.1, 5.3, 5.4**

  - [x]* 6.3 Write regression tests for findings and already-exists paths
    - Findings list still joined; already-exists rewrite unchanged with the backend message now included
    - _Requirements: 5.2, 5.4_

- [x] 7. Improve the device-side load failure log (`src/backend/dda_triton/vllm_model_prep.py`)
  - [x] 7.1 Implement `extract_load_failure_reason` and the prominent failure log
    - Parse the Triton `{"error": "..."}` body (raw text fallback); one prominent ERROR line with model name, HTTP status, and reason; append the KV-cache remediation when the "No available memory for the cache blocks" / `gpu_memory_utilization` markers match; pass the staged engine args from `prepare` so the failure log includes `gpu_memory_utilization` and `max_model_len`; keep retry/classification semantics unchanged; source stays 3.10/3.11-compatible
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 6.2_

  - [x]* 7.2 Write property test for reason extraction and log content
    - **Property 6: Load-failure reason extraction**
    - **Validates: Requirements 4.1, 4.3**

  - [x]* 7.3 Write unit tests for the remediation hint and engine-args logging
    - Real vLLM 409 body → remediation appended; non-matching body → no hint; failure log carries the staged `gpu_memory_utilization`/`max_model_len`
    - _Requirements: 4.2, 4.4_

- [x] 8. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - Verified: portal backend vllm/fit-check/publish/naming suites (52 passed); frontend and infrastructure `npx tsc --noEmit` both clean; device-side `test_vllm_load_failure_log.py` (13 passed) plus wider `dda_triton` run (46 passed; only the known pre-existing failures `test_triton_inference_runtimes_bug`, `test_triton_setup_preservation` and the awsiot/panorama collection errors).

## Notes

- **Execution is deferred until after a JP5 build publishes** — do not begin implementation on spec approval.
- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP.
- Delivery: tasks 1–6 are portal-side (Lambda + frontend), shipped via `deploy-infrastructure.sh` and the frontend deploy; task 7 is device-side and rides the next LocalServer build (no portal dependency in either direction).
- The Engine_Configuration pipeline (import → package → publish → device `model.json`) was verified correct during investigation; task 2.8 pins it with a regression property rather than changing it.
