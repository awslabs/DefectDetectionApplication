# Implementation Plan: vLLM Triton Inference

## Overview

Implementation follows the dependency order the design lays out. Shared foundations land first: the `workflow_core` catalog gains the `min_exclusive` constraint key, the `JP5_VLLM_ENABLED` flag, and the `LLM_INFERENCE` descriptor, then the catalog is re-vendored into `src/backend/workflow_engine/vendor`. The portal backend follows in pipeline order — registration (`model_import.py`), packaging bypass (`packaging.py`), publish branch (`greengrass_publish.py`), deployment gate (`deployments.py` + the `component_name-index` GSI), and the workflow packaging gate (`workflow_packaging.py`). The device side then builds bottom-up: the `vllm_runtime` companion runtime, `vllm_model_prep.py` staging, the `endpoints/text_generation.py` API, the workflow engine `LlmInferenceProcessor`, the feature-config status merge and `app.py` startup probe, and finally the Dockerfile layers (JP6 wheel, JP5 disabled hook, and the base-image COPY lines for every new `src/backend` module — missing COPY lines have caused startup regressions twice). The portal frontend (Register LLM form, NodeConfigPanel filtering, CreateDeployment warnings) closes it out. Property tests for the design's 24 Correctness Properties sit directly beside the code they validate.

Test baselines that must stay green throughout: portal backend pytest scoped to `tests/` run from `edge-cv-portal/backend` (moto-backed conftest stack), the `workflow_core` layer tests run from `edge-cv-portal/backend/layers/workflow_core`, the device backend suite under `test/backend-test/`, the frontend suite (`npx vitest run`) plus `npm run build` from `edge-cv-portal/frontend`, and the full-repo security audit and preservation gates (see the checkpoints). Python property tests use `hypothesis` as `test_property_*.py` (project default provides ≥100 iterations); TypeScript property tests use `fast-check` with `numRuns: 100`. Each property test is tagged `**Feature: vllm-triton-inference, Property {number}: {property title}**`.

## Tasks

- [x] 1. Shared catalog foundations (`workflow_core` + vendored copy)
  - [x] 1.1 Add the `min_exclusive` constraint key to the shared parameter-constraint vocabulary
    - In `edge-cv-portal/backend/layers/workflow_core/python/workflow_core`: extend the parameter-constraint validator/predicate so `min_exclusive` rejects values ≤ the bound (existing `min`/`max` stay inclusive); extend the catalog wellformedness checks to accept the new key
    - _Requirements: 6.10_

  - [x] 1.2 Add the `JP5_VLLM_ENABLED` flag and the `LLM_INFERENCE` descriptor to the catalog
    - In `workflow_core/catalog` (models + `nodes.py`): module-level `JP5_VLLM_ENABLED = False`; `VLLM_ARCHITECTURES` derived from it; `LLM_INFERENCE` descriptor per the design — inference category, `InferenceMeta` in/out ports, parameters `modelName` (`model_ref`, required), `prompt_template` (string, required, `min_length: 1`), `max_tokens` (int, optional, default 256, `min: 1`), `temperature` (float, optional, default 0.7, `[0.0, 2.0]`), `top_p` (float, optional, default 1.0, `min_exclusive: 0.0`, `max: 1.0`); `executor_binding` mappings `llm_inference` for `VLLM_ARCHITECTURES` only plus the `sim_llm_inference` sim stub; `hardware_dependent=True`; no mapping for `x86_64`, `x86_64_nvidia`, `arm64_jp4` (existing compiler unmapped-arch error implements 6.8); appended to the catalog list (additive)
    - _Requirements: 6.1, 6.3, 6.4, 6.8, 6.9, 6.10, 2.5_

  - [x] 1.3 Extend validator model-reference resolution with the model-type/node-family rule
    - The validator pass that resolves `model_inference.modelName` also resolves `llm_inference.modelName`, requiring a `vllm`-typed record for `llm_inference` and a non-`vllm` record for `model_inference`; unresolvable references produce a finding identifying the node and the model reference; structural/parameter checks (min_length, bounds, `min_exclusive`) apply to `llm_inference` through the existing generic validator path, and the existing `validation_guard` blocks compile/package while findings exist
    - _Requirements: 6.5, 6.6, 6.7, 6.12_

  - [x] 1.4 Sync the vendored catalog copy
    - Mirror the `workflow_core` changes (constraint vocabulary, flag, descriptor, validator extension) into `src/backend/workflow_engine/vendor/workflow_core`, following the established vendoring procedure so portal and device catalogs stay identical
    - _Requirements: 6.1, 8.1_

  - [x] 1.10 Wire a Use_Case model-registry snapshot into the portal validation endpoint
    - `edge-cv-portal/backend/functions/workflow_validation.py` (or wherever run_validator is invoked for validation runs): load the Use_Case's model records (MODELS_TABLE / training-jobs table) into the `model_registry` snapshot mapping and pass it to `validate()` so MODEL_REF_UNRESOLVED findings (6.12) are produced in production, not only in tests
    - _Requirements: 6.12_

  - [ ]* 1.5 Write property test for additive catalog identity
    - **Feature: vllm-triton-inference, Property 23: Additive catalog identity**
    - **Validates: Requirements 8.1, 8.4**
    - hypothesis in `edge-cv-portal/backend/layers/workflow_core/tests` over workflow definitions built exclusively from pre-existing node types (reusing `tests/generators.py`): validation findings and compiled per-architecture documents are identical with and without the `LLM_INFERENCE` descriptor in the catalog

  - [ ]* 1.6 Write property test for port compatibility acceptance
    - **Feature: vllm-triton-inference, Property 18: Port compatibility acceptance**
    - **Validates: Requirements 6.4**
    - hypothesis over every catalog node type: a connection from that type's output port to `llm_inference`'s input validates iff the output port type is accepted by `InferenceMeta` under the existing compatibility rules and declared coercions

  - [ ]* 1.7 Write property test for validator finding exactness
    - **Feature: vllm-triton-inference, Property 17: Validator finding exactness**
    - **Validates: Requirements 6.5, 6.6, 6.12**
    - hypothesis over `llm_inference` nodes with generated parameter values (empty/valid prompt templates, present/absent/unresolvable model selections, in/out-of-bounds max_tokens/temperature/top_p including the top_p 0.0 exclusive boundary) × registry snapshots: a finding exists iff the configuration is invalid, each finding identifies the node, parameter, and reason, and fully valid configurations yield no findings

  - [ ]* 1.8 Write property test for per-architecture compilation
    - **Feature: vllm-triton-inference, Property 19: Per-architecture compilation**
    - **Validates: Requirements 6.8, 6.9, 7.1**
    - hypothesis over validated workflows containing an `llm_inference` node: compiling for a non-vLLM architecture errors naming the node and arch with no document; compiling for `arm64_jp6` produces a document whose `llm_inference` executor binding carries exactly the bound `modelName`, `prompt_template`, and generation parameters with defaults applied for omitted ones; compiling for sim produces the `sim_llm_inference` stub with no model-invoking binding

  - [ ]* 1.9 Write unit tests for descriptor content and validation-guard blocking
    - Catalog wellformedness/content assertions riding the existing suite: descriptor category, ports, required/optional parameters, defaults, and bounds per 6.1/6.3/6.10; `validation_guard` blocks compilation and packaging of a workflow version with an unresolved `llm_inference` finding (6.7)
    - _Requirements: 6.1, 6.3, 6.7, 6.10_

- [ ] 2. Portal — vLLM model registration (`model_import.py`)
  - [x] 2.1 Implement the pure registration validation and defaults functions
    - In `edge-cv-portal/backend/functions/model_import.py`: `HF_MODEL_ID_RE`, `ENGINE_DEFAULTS` and accepted ranges per the design table (`dtype`, `gpu_memory_utilization`, `max_model_len`, `tensor_parallel_size`, `enforce_eager`); `validate_vllm_registration(body)` returning the complete finding list `{field, value, reason}` — source XOR (missing/both), malformed HF ID, non-`s3://…tar.gz` artifact URI, unknown engine keys rejected fail-closed, out-of-range engine values; `resolve_engine_configuration(supplied)` overlaying supplied values on `ENGINE_DEFAULTS` so the result contains every defined setting
    - _Requirements: 1.1, 1.2, 1.6, 1.9, 1.10, 1.11_

  - [x] 2.2 Implement the registration and engine-spec routes
    - `POST /api/v1/models/vllm`: `check_user_access(user_id, usecase_id, 'DataScientist')`; S3-artifact readability via `head_object` through `get_usecase_client('s3', usecase)` before any write (unreadable → 400 naming the S3 location); any validation failure → 400 with the finding list and no `put_item`; success writes the vLLM_Model_Record per the design data model (`model_type: 'vllm'`, `source: 'vllm'`, `model_source` XOR map, complete resolved `engine_configuration`, `publish_eligible: true`), logs the `register_vllm_model` audit event, and returns `201 {training_id, publish_eligible: true, labeling_steps: 0, training_steps: 0}`; `GET /api/v1/models/vllm/engine-spec` returns the documented settings, defaults, and ranges; `list_models` unchanged (vLLM records surface via existing `model_type`/`source` fields)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.7, 1.8_

  - [ ]* 2.3 Write property test for registration validation exactness and atomicity
    - **Feature: vllm-triton-inference, Property 1: Registration validation exactness and atomicity**
    - **Validates: Requirements 1.1, 1.5, 1.6, 1.9, 1.10, 1.11**
    - hypothesis in `edge-cv-portal/backend/tests` over registration payloads (neither/one/both sources, well-formed and malformed HF IDs, known/unknown engine keys, in/out-of-range values): findings are empty iff the payload is valid per the rule set; every finding names the offending field and value; when findings exist the handler performs no record write and marks nothing publish-eligible (mocked boto3)

  - [x]* 2.4 Write property test for engine configuration defaults overlay
    - **Feature: vllm-triton-inference, Property 2: Engine configuration defaults overlay**
    - **Validates: Requirements 1.2, 1.3**
    - hypothesis over valid partial engine configurations: the resolved configuration contains every defined setting, supplied settings keep their values, omitted settings equal their documented defaults; the record built from a valid request stores model type `vllm`, the given source reference, and the complete configuration

  - [x]* 2.5 Write property test for model listing discrimination
    - **Feature: vllm-triton-inference, Property 3: Model listing discrimination**
    - **Validates: Requirements 1.8**
    - hypothesis over mixed sets of vision and vLLM records in a Use_Case: the listing includes every vLLM record, and a record carries the `vllm` model type indicator iff it is a vLLM_Model_Record

  - [ ]* 2.6 Write unit tests for the registration handler flows
    - Success response shape (`201`, `training_id`, `publish_eligible: true`, zero labeling/training steps) (1.4); S3 `head_object` access-denied mapping to 400 naming the unreadable location with no record written (1.7); engine-spec route content
    - _Requirements: 1.4, 1.7_

- [x] 3. Portal — packaging bypass (`packaging.py`)
  - [x] 3.1 Implement the vLLM record predicate, repository generator, and supported-architecture set
    - In `edge-cv-portal/backend/functions/packaging.py`: `is_vllm_record(training_job)` (source or model_type is `vllm`); pure `generate_vllm_repository(record)` emitting exactly `{model_name}/1/model.json` (complete resolved engine configuration; `model` = HF ID for HF source, `./weights` sentinel for S3 source) and `{model_name}/config.pbtxt` (`backend: "vllm"` plus the decoupled transaction policy stanza), raising `VllmPackagingError` on any serialization failure; `vllm_supported_architectures()` returning `['arm64_jp6']` plus `arm64_jp5` iff `JP5_VLLM_ENABLED`, never `arm64_jp4`
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.8_

  - [x] 3.2 Implement `package_vllm_component` and the dispatch
    - `package_components` dispatches to `package_vllm_component` when `is_vllm_record` holds (vision records untouched); the packager writes the generated tree to a temp dir, zips it, uploads `model_artifacts/model-{uuid}/…zip` to the Use_Case bucket, records `packaged_components` entries per supported target (`jetson-xavier-jp6`, plus `-jp5` when flagged) each carrying `supported_architectures`; strict ordering generation → upload → DynamoDB update so any failure reports the failing artifact/step and leaves the record's packaged/published state unchanged (retryable); chains into `_trigger_component_creation`
    - _Requirements: 2.4, 2.5, 2.6, 2.8, 8.2_

  - [ ]* 3.3 Write property test for the vLLM dispatch predicate
    - **Feature: vllm-triton-inference, Property 24: vLLM dispatch predicate**
    - **Validates: Requirements 8.2**
    - hypothesis over training-job records (trained, imported PyTorch, imported ONNX, vLLM, arbitrary type/source strings): `is_vllm_record` is true iff the record carries the vLLM model type or source, and non-vLLM records dispatch through their pre-existing packaging path with no vLLM-specific validation

  - [ ]* 3.4 Write property test for repository generation round trip
    - **Feature: vllm-triton-inference, Property 4: Repository generation round trip**
    - **Validates: Requirements 2.1, 2.2, 2.3**
    - hypothesis over vLLM_Model_Records (both sources, arbitrary valid engine configurations): the generator emits exactly the two files; `config.pbtxt` declares `backend: "vllm"`; parsing `model.json` yields every resolved engine setting with equal values; `model` equals the HF ID for HF-sourced records and the `./weights` sentinel for S3-sourced records

  - [ ]* 3.5 Write property test for the supported-architecture set shape
    - **Feature: vllm-triton-inference, Property 6: Supported-architecture set shape**
    - **Validates: Requirements 2.5, 3.1, 3.2**
    - hypothesis over both values of the JP5 flag: `vllm_supported_architectures()` contains `arm64_jp6`, never contains `arm64_jp4`, and contains `arm64_jp5` iff the flag is enabled

- [x] 4. Portal — component publish branch (`greengrass_publish.py`)
  - [x] 4.1 Implement the vLLM recipe generation and naming/versioning
    - In `edge-cv-portal/backend/functions/greengrass_publish.py`: publish handler branch selected by `is_vllm_record`; component name `model-vllm-{safe_model_name}` (passes the existing `model-` validation), version `N.0.0` with `N` = 1 + highest previously published `N` for the record; pure `generate_vllm_component_recipe` mirroring `generate_component_recipe` — HARD dependency via `resolve_local_server_component`, the same `/aws_dda` seed-wait Startup gate, Startup invoking `python3 /aws_dda/vllm_model_prep.py --unarchived_repo_path … --weights_path … --model_name … --component_name …` (weights only for S3 source), Shutdown invoking `vllm_model_prep.py --cleanup`, the S3_Model_Artifact declared as a second Unarchive artifact for S3-sourced records, and nothing in the lifecycle restarting LocalServer
    - _Requirements: 2.2, 2.4, 2.7_

  - [x] 4.2 Implement publish metadata write-back and state transitions
    - On success: `supported_architectures` (from `vllm_supported_architectures()`) and `runtime: 'vllm'` written onto the record's `published_component` map and into the recipe's `ComponentConfiguration.DefaultConfiguration`; a top-level `component_name` attribute materialized for the GSI lookup; the record marked `published` with component name/version and the component version made available for deployments; on any Greengrass failure no partial state is written and the record stays pre-publish (retryable)
    - _Requirements: 2.4, 2.6, 2.9_

  - [ ]* 4.3 Write property test for component naming and version monotonicity
    - **Feature: vllm-triton-inference, Property 5: Component naming and version monotonicity**
    - **Validates: Requirements 2.4**
    - hypothesis over model names and histories of prior published versions: the derived name is `model-vllm-{safe_name}` matching the existing `model-` convention, and the derived next version is a valid `N.0.0` strictly greater than every version in the history

  - [ ]* 4.4 Write property test for publish failure atomicity
    - **Feature: vllm-triton-inference, Property 7: Publish failure atomicity**
    - **Validates: Requirements 2.6, 2.8**
    - hypothesis with injected failure points across the publish sequence (repository generation, serialization, artifact upload, Greengrass registration — the failure-injection style of `test_workflow_packaging_atomicity.py`): the operation reports the failing step, registers no component version, performs no steps past the failure, and leaves the record's packaged/published state unchanged

  - [ ]* 4.5 Write unit tests for recipe content and publish success
    - Recipe assertions: seed-wait gate present, `vllm_model_prep.py` Startup/Shutdown invocations, S3 artifact declared as a second Unarchive artifact for S3-sourced records, no LocalServer restart anywhere in the lifecycle (2.2, 2.7); publish success marks the record published with the component name/version (2.9)
    - _Requirements: 2.2, 2.7, 2.9_

- [x] 5. Portal — deployment gate (`deployments.py` + infrastructure GSI)
  - [x] 5.1 Implement the pure `evaluate_vllm_arch_gate` function
    - In `edge-cv-portal/backend/functions/deployments.py`, a structural twin of `evaluate_plugin_arch_gate`: pure over `component_manifests` (`{name: {version, architectures}}`) and `device_archs` (`{thing: arch or None}`); exact-name matching, no fallback, `None` fails closed; returns one entry per (component, device) miss `{component, version, device, deviceArch, supported, reason}` with `reason = 'JP4_UNSUPPORTED'` ("JetPack 4 does not support vLLM inference") when the device arch is `arm64_jp4`, else `'ARCH_UNSUPPORTED'`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [x] 5.2 Wire gate activation, the GSI lookup, and the 409 response
    - Gate evaluated in the pre-submit pass iff the component set contains a `model-vllm-*` component or a workflow component whose version item records `has_llm_inference`; `model-vllm-*` supported sets loaded from the backing record via the new `component_name-index` GSI on the training-jobs table (unresolvable → fail closed, like `load_plugin_record`); workflow components use the packaged arch set recorded on the version item; deployments containing neither contribute zero findings (pre-feature validation verbatim, jp4 included); any violation returns `409 VLLM_ARCH_UNSUPPORTED` with the complete offending list and submits nothing; add the `component_name-index` GSI to the training-jobs table definition in `edge-cv-portal/infrastructure`
    - _Requirements: 3.3, 3.4, 3.7, 8.5, 8.6_

  - [ ]* 5.3 Write property test for architecture gate exactness
    - **Feature: vllm-triton-inference, Property 8: Architecture gate exactness**
    - **Validates: Requirements 3.3, 3.4, 3.5, 3.6, 3.7, 3.9**
    - hypothesis (following `test_property_plugin_deployment_gates.py`) over manifest maps and device-arch maps including `None` and `arm64_jp4`: the gate returns empty iff every device arch is in every component's supported set by exact name; otherwise the entries are exactly the missing/None (component, device) pairs, each carrying device, arch, and supported set, with jp4 misses carrying the JetPack-4 reason

  - [ ]* 5.4 Write property test for gate activation
    - **Feature: vllm-triton-inference, Property 9: Gate activation**
    - **Validates: Requirements 8.5, 8.6**
    - hypothesis over deployment component sets and device-arch maps: the gate contributes findings only when the set contains a vLLM_Model_Component or a workflow component recorded as containing an LLM_Inference_Node; with neither, zero findings for every device-arch map including jp4 devices

- [x] 6. Portal — workflow packaging gate (`workflow_packaging.py`)
  - [x] 6.1 Implement `llm_arch_gate_findings` and the `has_llm_inference` discriminator
    - In `edge-cv-portal/backend/functions/workflow_packaging.py`, a pure gate alongside `custom_plugin_gate_findings`: one finding `{code: 'V6_LLM_ARCH_UNSUPPORTED', nodeId, arch}` per (llm_inference node, requested arch outside `VLLM_ARCHITECTURES`); empty when the workflow has no `llm_inference` node; findings reject the packaging request (`409`, complete list, no component version registered); on success the packager writes `has_llm_inference: true` and the packaged arch list onto the workflow version item (the same way the camera-binding discriminator is written) for the deployment gate
    - _Requirements: 7.1, 7.2, 8.1_

  - [ ]* 6.2 Write property test for the workflow packaging architecture gate
    - **Feature: vllm-triton-inference, Property 20: Workflow packaging architecture gate**
    - **Validates: Requirements 7.2**
    - hypothesis over workflow definitions (with and without `llm_inference` nodes) × requested architecture sets: findings are non-empty iff the workflow contains an `llm_inference` node and the set contains a non-vLLM architecture; findings identify the node and every unsupported requested arch; when findings exist no workflow component version is registered

- [x] 7. Checkpoint — portal backend and workflow_core
  - Run the portal backend suite (pytest scoped to `tests/` from `edge-cv-portal/backend`) and the `workflow_core` layer tests; the entire pre-existing suite must pass unchanged (catalog wellformedness, compiler properties, plugin gate tests, `test_workflow_generation.py`); ensure all tests pass, ask the user if questions arise.

- [ ] 8. Device — companion vLLM runtime (`src/backend/vllm_runtime/`)
  - [x] 8.1 Implement `VllmRuntimeManager` with the per-model state machine
    - New `src/backend/vllm_runtime/` package: the manager owns every vLLM model — parses a staged Triton_vLLM_Repository (`config.pbtxt` must declare `backend: "vllm"`, `model.json` parsed into `AsyncEngineArgs`), creates one `AsyncLLMEngine` per model; per-model state machine `STAGED → LOADING → READY | FAILED(reason)` with `UNKNOWN` for never-staged names and `unload` freeing the engine/GPU memory from any state; failures isolated — one model's load/serve error (including GPU OOM) never touches another engine, logged with model name and backend error; `generate(model, prompt, sampling_params) → text` and `generate_stream(model, …) → async token iterator`; `VLLM_MODEL_DIR = /aws_dda/dda_triton/vllm_model_repo` (distinct from the existing `TRITON_MODEL_DIR` — the embedded vision Triton is untouched)
    - _Requirements: 4.1, 4.6, 4.7, 8.8, 8.9_

  - [x] 8.2 Implement the Triton generate-extension HTTP server
    - Loopback-only HTTP server on `VLLM_RUNTIME_PORT` served by the manager: `POST /v2/models/{m}/generate`, `POST /v2/models/{m}/generate_stream` (SSE), `GET /v2/models/{m}/ready`, `GET /v2/repository/index`, and the model-control endpoints `POST /v2/repository/models/{m}/load|unload` — the same repository layout and generate interface as the real Triton vLLM backend so the runtime stays swappable
    - _Requirements: 4.1, 4.8, 5.2_

  - [ ]* 8.3 Write property test for load-failure isolation
    - **Feature: vllm-triton-inference, Property 11: Load-failure isolation**
    - **Validates: Requirements 4.6, 8.9**
    - hypothesis in `test/backend-test/` with a fake `AsyncLLMEngine` over sets of managed models and failing subsets (including OOM-shaped backend errors): each failing model transitions to FAILED retaining the backend reason; the state and serving behavior of every non-failing model is unchanged

  - [ ]* 8.4 Write unit tests for the loading state and load-request sequencing
    - LOADING observed during a slow fake load (4.7); a staged repository triggers exactly one load request through the model-control endpoint (4.8); `unload` removes the model and frees the fake engine
    - _Requirements: 4.7, 4.8_

- [x] 9. Device — model preparation (`vllm_model_prep.py`)
  - [x] 9.1 Implement the preparation script
    - New `vllm_model_prep.py` (beside `model_convertor.py` in the dda_triton resources): (1) validate the unarchived repository — exactly `{model_name}/1/model.json` + `{model_name}/config.pbtxt`, `config.pbtxt` declares `backend: "vllm"`, `model.json` parses; any defect → exit non-zero naming the defect; (2) S3-sourced only: rewrite `model.json`'s `"model": "./weights"` sentinel to the absolute `--weights_path`, verifying the path exists and is readable before staging — if not, report the model FAILED (name + unresolved path), stage nothing, exit non-zero, never issue a load request; (3) stage atomically into `VLLM_MODEL_DIR/{model_name}` (temp sibling + rename), no LocalServer restart; (4) request load via the runtime's model-control endpoint using the `model_autostart_utils.wait_for_server` backoff; `--cleanup` unloads and removes the staged directory (mirroring `convert_model_cleanup.py`)
    - _Requirements: 4.4, 4.5, 4.8, 4.9, 2.7_

  - [x] 9.2 Seed the script to `/aws_dda`
    - Add `vllm_model_prep.py` to the `cp_model_conversion_files` copy list (`files_to_copy_to_aws_dda`) exactly like `model_convertor.py`, so the component recipe's Startup script finds it at `/aws_dda/vllm_model_prep.py`
    - _Requirements: 2.7, 4.4_

  - [ ]* 9.3 Write property test for staging and load-request gating
    - **Feature: vllm-triton-inference, Property 10: Staging and load-request gating**
    - **Validates: Requirements 4.4, 4.5, 4.8, 4.9**
    - hypothesis over generated Triton_vLLM_Repositories and weights layouts on tmp-path filesystems: preparation stages content identical to the source and issues exactly one load request; for S3-sourced models only the `model` field of `model.json` is rewritten (every other key unchanged); a missing weights path yields no load request, nothing staged, and a FAILED report naming the model and the unresolved path

- [x] 10. Device — Text_Generation_API (`endpoints/text_generation.py`)
  - [x] 10.1 Implement the pure request-normalization core
    - New `src/backend/endpoints/text_generation.py`: `GENERATION_DEFAULTS = {"max_tokens": 256, "temperature": 0.7, "top_p": 1.0}`; `normalize_generation_request(model_name, body, model_max_len)` returning findings (each naming field and reason) when the prompt is empty/missing, the model name is empty/missing, max_tokens < 1 or > model_max_len, temperature outside [0.0, 2.0], or top_p outside (0.0, 1.0] — otherwise the effective request with supplied values overlaid on the defaults
    - _Requirements: 5.1, 5.8, 5.9_

  - [x] 10.2 Implement the FastAPI router with state check, retry, timeout, and streaming
    - Routes `POST /api/text-generation/{model_name}/generate`, `POST /api/text-generation/{model_name}/generate-stream` (SSE), `GET /api/text-generation/models`; validation failures → `422` with the complete finding list, runtime never invoked; non-READY models → `409 {model_name, state: loading|failed|unknown, reason?}` without invoking generation; READY requests invoke `generate` with transient-error retry up to `TEXT_GEN_RETRY_LIMIT` (default 2; exhausted/non-transient → `502 {model_name, reason}`), wall-clock `TEXT_GEN_TIMEOUT_SECONDS` (default 120; expiry → `504 {model_name, timeout_seconds}`); streaming forwards SSE `{"token": …}` events in generation order with terminal `{"done": true}`, a mid-stream error stops delivery and emits one `{"error": {reason}}` event with no retry and no retraction; per-request state function-local so concurrent requests are independent
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.9, 5.10, 5.11_

  - [ ]* 10.3 Write property test for generation request validation and normalization
    - **Feature: vllm-triton-inference, Property 12: Generation request validation and normalization**
    - **Validates: Requirements 5.1, 5.8, 5.9**
    - hypothesis over request bodies (missing/empty/valid prompts and model names, present/absent parameters in and out of range, boundary values for the top_p exclusive bound and max_model_len): findings exist iff the request is invalid, naming exactly the invalid fields with no runtime invocation; valid requests yield effective parameters equal to supplied values overlaid on defaults for exactly the omitted ones

  - [ ]* 10.4 Write property test for generation round trip with bounded retry
    - **Feature: vllm-triton-inference, Property 13: Generation round trip with bounded retry**
    - **Validates: Requirements 5.2, 5.6, 5.7**
    - hypothesis with an injected fake runtime yielding n transient failures before succeeding with text t under retry limit r: when n ≤ r the response contains exactly t and the runtime was invoked n+1 times; when n > r (or the failure is non-transient) the response is an error containing the model name and backend reason with min(n, r)+1 invocations

  - [ ]* 10.5 Write property test for streaming order preservation and error prefix
    - **Feature: vllm-triton-inference, Property 14: Streaming order preservation and error prefix**
    - **Validates: Requirements 5.3, 5.4**
    - hypothesis over fake token sequences and injected failure positions k: the stream delivers exactly the sequence in order followed by end-of-stream; on failure after k tokens it delivers exactly the first k tokens then a single in-stream error carrying the reason, with no retry invocation and no retraction

  - [ ]* 10.6 Write property test for not-ready rejection
    - **Feature: vllm-triton-inference, Property 15: Not-ready rejection**
    - **Validates: Requirements 5.5**
    - hypothesis over model names × runtime states {LOADING, FAILED(reason), UNKNOWN}: the request returns an error identifying the model name and the category corresponding to the state (loading / failed to load / unknown), and the generate interface is never invoked

  - [ ]* 10.7 Write unit tests for concurrency isolation and the timeout branch
    - FastAPI test client: concurrent requests against a fake runtime where one fails — the failing request does not alter any concurrent response (5.10); a tiny configured timeout produces `504` with the model name and timeout indication (5.11)
    - _Requirements: 5.10, 5.11_

- [x] 11. Device — workflow engine `LlmInferenceProcessor`
  - [x] 11.1 Implement `render_prompt`
    - In `src/backend/workflow_engine`: `PLACEHOLDER_RE`, strict substitution — every `{placeholder}` replaced by `str(metadata[name])` with dotted names resolving nested keys, literal text preserved, `{{`/`}}` escaping a literal brace, `UnresolvedPlaceholderError(name)` raised on the first missing key
    - _Requirements: 7.3, 7.5_

  - [x] 11.2 Implement the processor and executor wiring
    - `LlmInferenceProcessor` as a sibling of `BedrockInferenceProcessor` (same lifecycle: after a successful pipeline run, after the Bedrock processor, before output bindings evaluate): selects `executorBindings` with `binding == 'llm_inference'`; per binding renders the prompt and invokes the Text_Generation_API (local HTTP, injectable invoker) with the bound model name and parameters; merges `metadata['llm'][nodeId] = {'generated_text': …}` on success; on `UnresolvedPlaceholderError` records `{'error': 'unresolved placeholder {name}'}` with no API call; on API error/timeout records `{'error': reason}`; a binding failure is recorded, not raised — remaining bindings and independent nodes continue, and the merged metadata reaches downstream filters, conditionals, outputs, and custom Python through the existing metadata flow; the `sim_llm_inference` binding is a no-op on device
    - _Requirements: 7.3, 7.4, 7.5, 7.6, 7.7_

  - [ ]* 11.3 Write property test for prompt rendering exactness
    - **Feature: vllm-triton-inference, Property 21: Prompt rendering exactness**
    - **Validates: Requirements 7.3, 7.5**
    - hypothesis over templates composed of literal text, placeholders, and escaped braces × metadata dicts: with full coverage the rendered prompt replaces each placeholder with its value preserving all literal text, and the (fake) Text_Generation_API is invoked with the rendered prompt and bound parameters; with any uncovered placeholder the execution is recorded failed naming an unresolved placeholder and the API is not invoked

  - [ ]* 11.4 Write property test for output metadata recording and failure containment
    - **Feature: vllm-triton-inference, Property 22: Node output metadata recording and failure containment**
    - **Validates: Requirements 7.4, 7.6, 7.7**
    - hypothesis over sets of `llm_inference` bindings with per-binding outcomes (text, API error, timeout) via the injectable invoker: the returned run metadata records each node's outcome under that node's output before output bindings evaluate, and a failing binding neither alters any other binding's recorded outcome nor terminates processing of the remaining bindings

- [x] 12. Device — status merge and startup wiring
  - [x] 12.1 Merge vLLM model states into the feature-config status
    - Extend `get_features_triton` in `src/backend/utils/feature_configs_utils.py` to merge the manager's model list — each vLLM model reported as `type: "VllmModel"` with status mapped `LOADING→LOADING`, `READY→READY`, `FAILED→FAILED` (failure reason retained) — so the existing device model-status mechanisms (feature-config API, shadow sync) carry vLLM states; the manager pushes state transitions synchronously so READY propagates within the 30-second bound
    - _Requirements: 4.6, 4.7, 4.10_

  - [x] 12.2 Wire the capability probe, manager startup, and router into `app.py`
    - `src/backend/app.py`: probe `importlib.util.find_spec("vllm")` at startup; only when present start the `VllmRuntimeManager` (with its loopback server) and register the `text_generation` router beside the existing routers; images without vLLM (jp4, jp5-default, x86 variants) run exactly the pre-feature startup sequence
    - _Requirements: 4.1, 4.2, 4.3, 8.3_

  - [ ]* 12.3 Write unit tests for the status merge and startup probe
    - Fake manager states map to `VllmModel` LOADING/READY/FAILED entries merged beside existing Triton vision entries without altering them; FAILED retains the backend reason; startup with vLLM absent starts no manager and registers the pre-feature router set only
    - _Requirements: 4.6, 4.7, 4.10, 8.3_

- [x] 13. Device — image builds (Dockerfiles)
  - [x] 13.1 Add the vLLM layer and COPY lines to the Dockerfiles
    - `src/backend/Dockerfile.jp6`: the additive build-arg-gated layer (`ARG VLLM_ENABLE=1`, `ARG VLLM_SPEC`, `ARG VLLM_INDEX_URL`, conditional `pip install` of the pinned aarch64 wheel for r36.3.0/CUDA 12.2/Python 3.11) — no existing package pin, Triton deb, or CUDA staging step changes; `src/backend/Dockerfile.jp5`: the same block with `VLLM_ENABLE=0` default (adds nothing when off; JP5 build behavior unchanged); `src/backend/Dockerfile`: COPY lines for every new `src/backend` module — `vllm_runtime/` and any other new files (`endpoints/text_generation.py` and `workflow_engine` additions ride existing directory COPYs; verify) — missing COPY lines have caused startup regressions twice, so cross-check each new module lands in the image
    - _Requirements: 4.1, 4.2, 4.3, 8.7_

  - [x] 13.2 Recapture the docker preservation goldens
    - Editing the Dockerfiles requires recapturing the docker preservation baselines: delete `test/backend-test/security/baselines/docker_baseline_backend_Dockerfile.jp5_masked.txt` and the `.jp6` variant so they recapture, and update the sha256 for `src/backend/Dockerfile` in `test/backend-test/security/baselines/docker_baseline_out_of_scope.json` if the base Dockerfile changed; then run the preservation suite to confirm the recaptured goldens pass
    - _Requirements: 4.3, 8.7_

- [x] 14. Checkpoint — device backend and security gates
  - Run the device backend suite under `test/backend-test/`, the full-repo security audit gates (`python3 test/backend-test/security/repo_audit.py`, `secrets_audit.py`, `iam_audit.py`, `s3_squat_audit.py`, `docker_base_image_audit.py`, `dependency_audit.py` — all must pass), and the preservation suite (`python3 -m pytest test/backend-test/security/preservation -p no:cacheprovider --noconftest`); ensure all tests pass, ask the user if questions arise.

- [x] 15. Portal frontend
  - [x] 15.1 Implement the Register LLM form, model type badge, and supported-arch display
    - Models page: a "Register LLM" action opening a form — source radio (Hugging Face ID / S3 artifact) with the two inputs mutually exclusive in the UI matching the API's XOR, engine settings as an optional expandable section pre-filled with the documented defaults (from the engine-spec endpoint), validation errors surfaced per field; vLLM records render an `LLM (vLLM)` type badge in the model list; the model detail view shows `supported_architectures`
    - _Requirements: 1.1, 1.2, 1.8, 3.8_

  - [x] 15.2 Implement NodeConfigPanel model_ref filtering and the empty state
    - `edge-cv-portal/frontend/src/pages/workflows/NodeConfigPanel.tsx`: the `model_ref` select gains a per-node-type filter — `llm_inference` shows only `model_type === 'vllm'` records, `model_inference` excludes them (vision node list unchanged pre-feature); an empty vLLM list renders the select empty with "No vLLM models are registered for this use case"; the frontend inline constraint predicate learns the `min_exclusive` key; `llm_inference` appears in the inference palette group automatically (palette renders from the catalog)
    - _Requirements: 6.2, 6.11, 8.3_

  - [x] 15.3 Implement CreateDeployment incompatibility warnings (TS gate twin)
    - `edge-cv-portal/frontend/src/pages/CreateDeployment.tsx`: when the selection contains a `model-vllm-*` component or an LLM workflow, each selected device is checked client-side with a pure TS twin of `evaluate_vllm_arch_gate` (same shape: exact-name matching, `None`/absent arch fails closed, jp4-specific reason); incompatible devices render a warning before submit listing the device's recorded architecture (or its absence) and the component's supported set; the backend gate remains authoritative
    - _Requirements: 3.9_

  - [ ]* 15.4 Write property test for model option filtering
    - **Feature: vllm-triton-inference, Property 16: Model option filtering**
    - **Validates: Requirements 6.2, 6.11**
    - fast-check (`numRuns: 100`) over sets of Use_Case model records with mixed model types: the `llm_inference` options are exactly the `vllm`-typed records (empty when none exist), and the vision inference node's options contain no `vllm`-typed record

  - [ ]* 15.5 Write property test for the TS architecture-gate twin
    - **Feature: vllm-triton-inference, Property 8: Architecture gate exactness** (frontend twin of the backend gate)
    - **Validates: Requirements 3.9**
    - fast-check over manifest maps and device-arch maps (including absent archs and `arm64_jp4`): the TS predicate returns empty iff every device arch is in every supported set by exact name; otherwise exactly the missing pairs with device, arch, supported set, and the jp4-specific reason — matching the backend gate's semantics

  - [ ]* 15.6 Write component tests for the frontend flows
    - Vitest + Testing Library: register form XOR behavior (selecting one source clears/disables the other); model-type badge rendering; empty vLLM option state message (6.11); supported-arch display on the model detail view (3.8); deployment incompatibility warning rendering with recorded arch, absence, and supported set (3.9); palette/config-panel snapshots showing `llm_inference` present and existing node types unchanged
    - _Requirements: 1.8, 3.8, 3.9, 6.11, 8.3_

- [x] 16. Final checkpoint — all baselines
  - Ensure all baselines pass: portal backend pytest scoped to `tests/` from `edge-cv-portal/backend`; the `workflow_core` layer tests; the device backend suite under `test/backend-test/`; `npx vitest run` plus `npm run build` from `edge-cv-portal/frontend`; `npm run build` in `edge-cv-portal/infrastructure` (the GSI change); the security audit gates (`python3 test/backend-test/security/{repo_audit,secrets_audit,iam_audit,s3_squat_audit,docker_base_image_audit,dependency_audit}.py` all passing) and the preservation suite (`python3 -m pytest test/backend-test/security/preservation -p no:cacheprovider --noconftest`); the entire pre-existing test suite must pass unchanged. Ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP
- Each task references specific requirements for traceability; all 24 design properties are covered: Properties 17–19, 23 by tasks 1.5–1.8; Properties 1–3 by 2.3–2.5; Properties 4, 6, 24 by 3.3–3.5; Properties 5, 7 by 4.3–4.4; Properties 8, 9 by 5.3–5.4 (Property 8's TS twin by 15.5); Property 20 by 6.2; Property 11 by 8.3; Property 10 by 9.3; Properties 12–15 by 10.3–10.6; Properties 21, 22 by 11.3–11.4; Property 16 by 15.4
- Python property tests use hypothesis with no hardcoded `max_examples` (project default ≥100 iterations); TypeScript property tests use fast-check with `numRuns: 100`; each tagged `**Feature: vllm-triton-inference, Property {number}: {property title}**`
- All device-side tests run against fakes (fake `AsyncLLMEngine`, injectable invoker, FastAPI test client, tmp-path filesystems); no test requires GPU hardware or a real vLLM install
- The exact pinned vLLM wheel/index URL for `Dockerfile.jp6` is fixed at implementation time against the r36.3.0/CUDA 12.2/Python 3.11 image; if no prebuilt wheel matches, the fallback is a build-stage source compile behind the same `VLLM_ENABLE` build arg
- `JP5_VLLM_ENABLED` stays `False` and `Dockerfile.jp5`'s `VLLM_ENABLE` stays `0`: with the flag off, JP5 packaging, gating, and image behavior are byte-identical to today; enabling JP5 later is a flag flip plus a provisioning recipe, outside this spec
- Out of scope for automation (documented per the design's Testing Strategy, not tasks): the on-hardware integration tests — JP6 deploy of a small HF model with READY propagation, generate round trip, and streaming session (4.1, 4.10, 5.2, 5.3); JP6 vision + vLLM coexistence (8.8); JP6 image vision-suite regression on hardware (4.3); JP4 unchanged vision deployment (8.7); and the cloud end-to-end register → publish → deploy-rejection (jp4) → deploy-success (jp6) flow against a test account
- Requirements 4.2/3.2 (JP5 conditional support) are satisfied structurally by the flag-derived code paths and the `Dockerfile.jp5` hook; no JP5 vLLM execution is implemented or tested in this spec

## Task Dependency Graph

Waves group leaf tasks that touch disjoint files and have all prerequisites satisfied by earlier waves (max 5 concurrent). Foundations (catalog vocabulary, pure functions, runtime manager) come first; same-file successors (`model_import.py`, `packaging.py`, `greengrass_publish.py`, `deployments.py`) follow in later waves; test tasks land after the code they validate; Dockerfile work waits for every new device module; frontend integration and its tests close out.

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "8.1", "10.1", "11.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.2", "8.2", "9.2"] },
    { "id": 2, "tasks": ["1.4", "2.3", "2.4", "2.5", "3.1"] },
    { "id": 3, "tasks": ["1.5", "2.6", "3.2", "6.1", "9.1"] },
    { "id": 4, "tasks": ["1.6", "1.7", "4.1", "5.1", "10.2"] },
    { "id": 5, "tasks": ["1.8", "1.9", "4.2", "5.2", "11.2"] },
    { "id": 6, "tasks": ["3.3", "3.4", "3.5", "12.1", "12.2"] },
    { "id": 7, "tasks": ["4.3", "4.4", "4.5", "5.3", "13.1"] },
    { "id": 8, "tasks": ["5.4", "6.2", "8.3", "8.4", "13.2"] },
    { "id": 9, "tasks": ["9.3", "10.3", "10.4", "10.5", "10.6"] },
    { "id": 10, "tasks": ["10.7", "11.3", "11.4", "12.3", "15.1"] },
    { "id": 11, "tasks": ["15.2", "15.3"] },
    { "id": 12, "tasks": ["15.4", "15.5", "15.6"] }
  ]
}
```
