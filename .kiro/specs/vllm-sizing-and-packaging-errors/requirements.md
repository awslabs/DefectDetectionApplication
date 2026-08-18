# Requirements Document

## Introduction

Two related enhancements motivated by a live JP6 failure: `model-vllm-qwen-2-5-7b`
(Qwen2.5-7B, bf16, ~14.25 GiB of weights) was deployed with
`gpu_memory_utilization = 0.3` on a 30 GiB Orin. vLLM computed a negative KV-cache
budget (0.3 × 30 GiB ≈ 9 GiB < 14.25 GiB of weights), the Triton model-control load
returned 409 FAILED repeatedly, the Greengrass component went BROKEN, and the
deployment rolled back.

Investigation established that the 0.3 was supplied at registration time (the
portal import default has always been 0.5; packaging copies the stored record
verbatim into the on-device `model.json`; `vllm_model_prep.py` injects no
defaults). The operator-facing documentation prominently shows
`gpu_memory_utilization = 0.3` for the tiny opt-125m smoke model, adjacent to the
Qwen guidance, making this misconfiguration easy to reproduce. Nothing in the
pipeline checks whether the model can possibly fit before the device fails.

**Enhancement 1** adds per-model vLLM GPU memory sizing: the per-model
Engine_Configuration remains authoritative end-to-end (import → package → publish →
device `model.json`), becomes editable after import (API + GUI), and a preflight
Fit_Check estimates weight size and warns or fails with a clear message when
weights plus a minimum KV cache exceed `gpu_memory_utilization` × the
target-architecture memory budget. The device-side load failure surfaces the vLLM
409 reason prominently and actionably in the component log.

**Enhancement 2** makes workflow packaging errors actionable in the workflow
builder: the package dialog currently drops the backend's remediation-bearing
`error.message` (showing only `Failing artifact: models/{name}`); it must show the
message plus the failing artifact, hint at the Models page publish action for the
unpublished-model case, and preserve the existing "already exists" rewrite.

Portal changes ship via the portal deploy (`deploy-infrastructure.sh` + frontend
deploy); device-side changes ride the next LocalServer build. Execution of this
spec is deferred until after a JP5 build publishes.

## Glossary

- **Portal**: The edge-cv-portal (backend Lambda functions under `edge-cv-portal/backend/functions/` plus the React frontend under `edge-cv-portal/frontend/`).
- **vLLM_Model_Record**: A model record in the training-jobs DynamoDB table with `model_type = 'vllm'` / `source = 'vllm'`, created by `model_import.register_vllm_model`.
- **Engine_Configuration**: The resolved vLLM engine settings stored on a vLLM_Model_Record: `dtype`, `gpu_memory_utilization`, `max_model_len`, `tensor_parallel_size`, `enforce_eager`.
- **Model_Packager**: The `packaging.py` Lambda path that generates the Triton_vLLM_Repository (`{model_name}/1/model.json` + `config.pbtxt`) from a vLLM_Model_Record.
- **Model_Publisher**: The `greengrass_publish.py` Lambda path that registers the `model-vllm-{safe_model_name}` Greengrass component from the packaged artifact.
- **Model_Prep_Script**: The device-side `src/backend/dda_triton/vllm_model_prep.py`, which validates and stages the Triton_vLLM_Repository and requests the model load through the Triton model-control endpoint.
- **Fit_Check**: A preflight computation comparing Weight_Estimate plus Minimum_KV_Cache against `gpu_memory_utilization` × the Device_Memory_Profile budget for a Target_Architecture.
- **Weight_Estimate**: The estimated on-GPU size in bytes of a model's weights, derived from Hugging Face metadata (safetensors sizes), parameter count × dtype byte width, quantization configuration, or the S3 artifact size.
- **Minimum_KV_Cache**: A configured lower bound of GPU memory (beyond weights and activation overhead) that vLLM needs for KV-cache blocks to serve at all.
- **Device_Memory_Profile**: A per-Target_Architecture table of usable device GPU memory (unified memory on Jetson), e.g. `arm64_jp6` → 30 GiB usable.
- **Target_Architecture**: A device architecture identifier (`arm64_jp4`, `arm64_jp5`, `arm64_jp6`, `x86_64`); vLLM components target `arm64_jp6` (and `arm64_jp5` behind the JP5_VLLM_ENABLED flag).
- **Workflow_Packager**: The `workflow_packaging.py` Lambda that packages a workflow version into a `dda.workflow.{id}` component, raising `PackagingError` (surfaced as a 502 `PACKAGING_FAILED` envelope with `error.message` and `error.details.failing_artifact`).
- **Package_Dialog**: The packaging modal in the workflow builder toolbar (`WorkflowToolbar.tsx`), which displays packaging errors via `setPackageError`.
- **Models_Page**: The Portal frontend page listing models, from which the publish action for a vLLM model is reachable (model detail with the vLLM package/publish section).

## Requirements

### Requirement 1: Engine configuration flows end-to-end and is visible

**User Story:** As a data scientist, I want the per-model engine configuration I set at registration to be the exact configuration the device loads with, and to see it in the portal, so that I can trust and verify what will run on the device.

#### Acceptance Criteria

1. WHEN a vLLM_Model_Record is packaged, THE Model_Packager SHALL write the record's stored Engine_Configuration values verbatim (after Decimal-to-number conversion) into the generated `model.json`.
2. WHEN a vLLM model detail is requested via `GET /api/v1/models/{model_id}`, THE Portal SHALL include the record's stored Engine_Configuration in the response.
3. WHEN the Models_Page renders a vLLM model detail, THE Portal SHALL display every Engine_Configuration setting with its stored value.
4. WHEN a vLLM_Model_Record is registered without a supplied `gpu_memory_utilization`, THE Portal SHALL store the documented default value 0.5.

### Requirement 2: Engine configuration is editable after import

**User Story:** As a data scientist, I want to correct a model's engine configuration after registration, so that a mis-sized model can be fixed and republished without deleting and re-registering it.

#### Acceptance Criteria

1. WHEN a `PUT /api/v1/models/vllm/{training_id}/engine-configuration` request supplies one or more Engine_Configuration settings for an existing vLLM_Model_Record, THE Portal SHALL validate each supplied setting against the same rules used at registration and store the updated resolved Engine_Configuration on the record.
2. IF an engine-configuration update request contains an unknown setting key or an out-of-range value, THEN THE Portal SHALL reject the request with HTTP 400 and a finding list naming each offending field, value, and reason, and SHALL leave the stored Engine_Configuration unchanged.
3. IF an engine-configuration update targets a record that is not a vLLM_Model_Record, THEN THE Portal SHALL reject the request with HTTP 400 identifying the record as non-vLLM.
4. WHEN an engine-configuration update succeeds, THE Portal SHALL return the complete updated Engine_Configuration and a notice that the change takes effect only after the model is packaged and published again.
5. WHEN the Models_Page renders a vLLM model detail, THE Portal SHALL provide an edit control for the Engine_Configuration settings that submits to the update endpoint and displays validation findings per field.
6. WHEN an engine-configuration update succeeds, THE Portal SHALL record an audit event carrying the previous and updated Engine_Configuration values.

### Requirement 3: Preflight fit check at registration, update, and publish

**User Story:** As a data scientist, I want the portal to tell me before deployment when a model cannot fit in the configured GPU memory fraction on the target device, so that I do not ship a configuration that can only fail on-device.

#### Acceptance Criteria

1. WHEN a Fit_Check is evaluated for a vLLM_Model_Record and a Target_Architecture, THE Portal SHALL compute the GPU memory budget as `gpu_memory_utilization` × the Device_Memory_Profile entry for that Target_Architecture and compare it against Weight_Estimate + Activation_Allowance + Minimum_KV_Cache, and SHALL additionally require `gpu_memory_utilization` to stay at or below the Fraction_Cap for that Target_Architecture — the verdict is the conjunction of both conditions. **REVISED by `jp6-vllm-kv-cache-oom-regression` (design Decision 2).** The original criterion compared the budget against Weight_Estimate + Minimum_KV_Cache alone; that model reported 4.50 GiB of slack (`0.4 × 30 GiB = 12.00 GiB` against `6.5 + 1 = 7.5 GiB`) for the 2026-08-17 `ryanorinagxdevkithomelabjp622` load whose device-measured KV remainder was **−7.83 GiB**, because it omitted vLLM's activation/profiling peak (measured 4.92 GiB, ~41% of the budget) and modelled no co-tenancy on a device whose co-resident ONNX GPU models already hold ~6 GiB of the same unified memory. See that spec for the Activation_Allowance formula, the Fraction_Cap definition, and the provenance of every constant.
2. THE Portal SHALL derive Weight_Estimate for a Hugging Face–sourced record from Hugging Face model metadata (per-file safetensors sizes when available, otherwise parameter count × dtype byte width, with quantized variants sized from the quantization configuration).
3. THE Portal SHALL derive Weight_Estimate for an S3-sourced record from the size of the model artifact object.
4. IF Weight_Estimate cannot be determined (metadata unavailable or unparseable), THEN THE Portal SHALL skip the Fit_Check for that record and report that the fit could not be verified, without blocking the operation.
5. WHEN a vLLM model registration or engine-configuration update completes validation, THE Portal SHALL evaluate the Fit_Check against every Target_Architecture that vLLM components can target and include any failing findings as non-blocking warnings in the response.
6. IF the Fit_Check fails for any supported Target_Architecture at model package/publish time, THEN THE Model_Publisher SHALL fail the publish before any component registration with HTTP 422 and a message stating the Weight_Estimate, the Activation_Allowance (labelled an estimate), the Minimum_KV_Cache, the configured `gpu_memory_utilization`, the per-architecture memory budget, the Co_Tenancy_Reservation and the Fraction_Cap, plus the remediation menu in the order defined by `jp6-vllm-kv-cache-oom-regression` Decision 3 (co-tenancy hazard first; then the demand-reducing options — bound `limit_mm_per_prompt.image`, reduce `max_model_len`, choose a smaller or more quantized model, free device memory; and only last, quantified and bounded by the Fraction_Cap, raising `gpu_memory_utilization`). **REVISED by `jp6-vllm-kv-cache-oom-regression` (design Decision 2 for the any-architecture gate and the term list, Decision 3 for the remediation order).** The original criterion blocked only when the Fit_Check failed for *every* supported Target_Architecture, which is how an `arm64_jp6`-infeasible configuration reached the fleet on the strength of `arm64_jp7` fitting; the `skip_fit_check` override of criterion 7 remains the audited escape hatch.
7. WHERE a publish request carries an explicit `skip_fit_check` override flag, THE Model_Publisher SHALL proceed despite a failing Fit_Check and record the override in the audit event.
8. THE Portal SHALL maintain the Device_Memory_Profile as a per-Target_Architecture table in code with at least an `arm64_jp6` entry of 30 GiB **total device memory as the engine sees it**, and every Fit_Check message SHALL name the profile entry used. **Semantics corrected by `jp6-vllm-kv-cache-oom-regression` (design Decision 2); the 30 GiB VALUE is unchanged.** The original criterion called the entry "30 GiB usable memory"; the figure is in fact a TOTAL — reconciled against the incident device's `free -g` total of 29 GB and vLLM's own four profiling terms summing to ≈29.95 GiB — and ~6 GiB of it is resident before vLLM starts. `gpu_memory_utilization` is a fraction the device applies to its real total, so the profile must stay a total for the portal's budget to be the number vLLM targets; memory held by other consumers is modelled separately as the Co_Tenancy_Reservation and the Fraction_Cap. The name-the-profile-entry rule is unchanged.
9. WHEN a Fit_Check produces a failing finding **whose Weight_Estimate alone exceeds the configured budget**, THE Portal SHALL phrase the finding with the correct remediation direction (the weights do not fit inside the configured fraction, so `gpu_memory_utilization` must be raised — within the Fraction_Cap — or the model shrunk; never advise lowering it for this failure mode). **NARROWED by `jp6-vllm-kv-cache-oom-regression` (design Decision 3).** This criterion now governs only the weights-exceed-budget arithmetic (its original incident: Qwen2.5-7B bf16, 14.25 GiB of weights at `gpu_memory_utilization = 0.3`), where the direction remains correct. For the failure mode where the weights fit but the activation/profiling peak plus co-tenancy do not, it is **superseded** by that spec's Decision 3 ordered menu: raising the fraction is a hazard on shared unified memory (it grows this model's claim on memory the co-resident ONNX GPU models are using), so it is offered last, only below the Fraction_Cap, and always quantified. The never-advise-lowering invariant is **kept in full** for every failure mode.

### Requirement 4: Device-side load failure surfaces the vLLM reason

**User Story:** As an operator triaging a broken model component, I want the component log to state prominently why the vLLM load failed and what to change, so that I do not have to decode a raw Triton 409 body.

#### Acceptance Criteria

1. WHEN the Triton model-control load request returns a non-200 HTTP response, THE Model_Prep_Script SHALL extract the human-readable error reason from the response body and log a single prominent ERROR line containing the model name, the HTTP status, and the extracted reason.
2. IF the extracted reason indicates insufficient KV-cache memory (e.g. "No available memory for the cache blocks. Try increasing gpu_memory_utilization"), THEN THE Model_Prep_Script SHALL append an actionable remediation to the ERROR line naming the `gpu_memory_utilization` and `max_model_len` engine settings, in this order: (a) the co-tenancy hazard — the device shares one pool of unified memory with the co-resident ONNX GPU models and `gpu_memory_utilization` is a fraction of TOTAL device memory, so a larger fraction is taken from memory those models are already using; (b) the remediations that reduce this model's own demand — bound `limit_mm_per_prompt.image`, reduce `max_model_len`, choose a smaller or more quantized model, free device memory by stopping unused model components; (c) LAST, and only while the fraction stays below the Fraction_Cap for the device class, raising `gpu_memory_utilization` — replaced by "unsafe here" once the staged fraction already meets that cap. **Revised by `jp6-vllm-kv-cache-oom-regression` (design Decision 3, task 3.7).** The original criterion required the line to state "that the value must be raised or the model reduced"; leading with "raise the fraction" is defect 1.3 of that spec — on a shared unified-memory JP6 device, following it grows this model's claim on memory the co-resident ONNX GPU models hold, converting one broken model into a broken vision stack (its success condition 2.10 counts that as a failure). Ordering by "does this reduce our own demand or take memory from someone else" is what changed; **raising the fraction is still offered where headroom demonstrably exists, and the never-advise-*lowering* invariant of criterion 3.9 is kept in full**. The model name, HTTP status, extracted reason and the staged `gpu_memory_utilization` / `max_model_len` values (criteria 4.1, 4.3, 4.4) are unchanged, and a refusal from the runtime's own device-memory preflight carries that spec's measured, quantified menu instead — the prep does not append a second copy to it.
3. IF the response body cannot be parsed for a reason, THEN THE Model_Prep_Script SHALL log the raw response body as the reason.
4. WHEN the load fails with an authoritative HTTP error, THE Model_Prep_Script SHALL include the `gpu_memory_utilization` and `max_model_len` values from the staged `model.json` in the failure log output.

### Requirement 5: Actionable packaging errors in the workflow builder

**User Story:** As a workflow author, I want the package dialog to show me the backend's actual error message and what to do next, so that a failed packaging attempt tells me how to fix it instead of only naming an artifact.

#### Acceptance Criteria

1. WHEN a packaging request fails with an error envelope carrying a message and `details.failing_artifact`, THE Package_Dialog SHALL display both the backend message and the failing artifact identifier.
2. WHEN a packaging request fails with a findings list, THE Package_Dialog SHALL continue to display the joined findings messages as it does today.
3. IF the failing artifact identifies an unpublished model (a `models/{name}` artifact whose message states the model has no published Greengrass component), THEN THE Package_Dialog SHALL include a hint directing the user to the Models_Page publish action for that model.
4. WHEN a packaging failure message matches the existing "already exists" condition, THE Package_Dialog SHALL preserve the existing rewrite explaining that component versions are immutable and a new workflow version must be saved and packaged.
5. WHEN a packaging request fails without structured details, THE Package_Dialog SHALL display the error message text unchanged.

### Requirement 6: Delivery constraints

**User Story:** As a release manager, I want the changes routed through the correct delivery channels, so that portal and device artifacts stay consistent.

#### Acceptance Criteria

1. THE Portal changes (Lambda functions, frontend) SHALL be deliverable through the existing portal deployment path (`deploy-infrastructure.sh` plus the frontend deploy) with no LocalServer build required.
2. THE Model_Prep_Script changes SHALL be source-compatible with the JP5 and JP6 LocalServer images and ride the next LocalServer build with no portal dependency.
