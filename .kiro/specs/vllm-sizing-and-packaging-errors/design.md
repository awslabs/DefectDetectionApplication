# Design Document

## Overview

Two enhancements sharing one theme: make vLLM model misconfiguration visible and
actionable at the earliest possible point instead of failing opaquely on-device.

**Enhancement 1 (sizing):** the Engine_Configuration already flows correctly
end-to-end — registration stores the resolved config in DynamoDB
(`model_import.py`), packaging serializes it verbatim into
`{model_name}/1/model.json` (`packaging.generate_vllm_repository`), publish zips
that repository into the `model-vllm-*` component, and the device stages it
unchanged (`vllm_model_prep.py` rewrites only the `model` weights reference for
S3-sourced records). The live failure's `0.3` was operator-supplied at
registration (import default has always been 0.5; the docs show 0.3 for the tiny
opt-125m smoke model right next to the Qwen guidance). What is missing is:
(a) visibility (the model detail API/GUI doesn't show the config), (b) a way to
fix it without re-registering, and (c) any preflight check that the model can
fit. This design adds all three plus a clearer device-side failure log.

**Enhancement 2 (packaging errors):** the backend already returns a
remediation-bearing message (`error_response(502, 'PACKAGING_FAILED', e.message,
{'failing_artifact': ...})` in `workflow_packaging.py`), but
`WorkflowToolbar.tsx` overwrites `content` (initialized from `err.message`) with
only `Failing artifact: {name}` whenever `details.failing_artifact` is present.
The fix is a small, targeted change to the error-content composition, preserving
the findings path and the "already exists" rewrite.

### Investigation summary (verified)

- `model_import.ENGINE_DEFAULTS['gpu_memory_utilization']` = 0.5 for the entire
  git history of the file; 0.3 never appeared as a portal default.
- `packaging.generate_vllm_repository` copies the stored record's
  `engine_configuration` (Decimal→native) into `model.json` — no mutation.
- `vllm_model_prep.py` validates layout, optionally rewrites the `model` key
  (S3 weights sentinel), and requests the load; on non-200 it logs
  `"Request failed with status code: {code}"` plus the raw `response.text` —
  the 409 body IS logged today, but as an unstructured dump with no model
  context or remediation (assessed inadequate for Requirement 4).
- `models.py get_model` does not return `engine_configuration`; there is no
  update endpoint for it (only `PUT .../stage`); `ModelDetail.tsx` does not
  display it. The Models GUI exists (vllm-package-publish-gui:
  `ModelDetail.tsx` + `components/vllm-publish/`).
- No per-architecture device memory table exists anywhere in the codebase —
  the Device_Memory_Profile is new.
- `ApiError` (frontend `services/api.ts`) carries the backend envelope's
  `message`, `code`, and `details` — the data needed by the Package_Dialog is
  already on the client.
- README triage advice ("If loading fails with CUDA OOM, lower
  gpu_memory_utilization") is wrong for the weights-don't-fit failure mode;
  Fit_Check messages must advise the opposite direction (Requirement 3.9).

## Architecture

```
Registration / Edit (Portal backend)
  model_import.py
    ├── validate + resolve engine_configuration        (existing)
    ├── NEW update_vllm_engine_configuration handler   (Req 2)
    └── NEW vllm_fit_check.py (pure module)            (Req 3)
          ├── Device_Memory_Profile table
          ├── estimate_weights(record) -> WeightEstimate | None
          └── evaluate_fit(engine_cfg, estimate, archs) -> [FitFinding]

Publish (Portal backend)
  greengrass_publish.py
    └── vLLM branch: evaluate_fit gate before any component
        registration (fail 422 unless skip_fit_check)   (Req 3.6, 3.7)

Model detail (Portal backend + frontend)
  models.py get_model: + engine_configuration            (Req 1.2)
  ModelDetail.tsx: view + edit Engine_Configuration      (Req 1.3, 2.5)

Device (LocalServer build)
  vllm_model_prep.py request_load / prepare:
    structured 409-reason extraction + prominent log     (Req 4)

Workflow builder (Portal frontend)
  WorkflowToolbar.tsx handleConfirmPackage error path    (Req 5)
```

## Components and Interfaces

### 1. `vllm_fit_check.py` (new, `edge-cv-portal/backend/functions/`)

Pure module (no boto3 at import time) so it is unit- and property-testable, used
by both `model_import.py` and `greengrass_publish.py`.

```python
# Per-Target_Architecture usable device GPU memory (unified memory on Jetson),
# in bytes. Conservative "usable" figures, not nameplate RAM.
DEVICE_MEMORY_PROFILE_BYTES = {
    'arm64_jp6': 30 * GIB,   # 32 GB Orin class, ~30 GiB usable
    'arm64_jp5': 30 * GIB,   # only reachable when JP5_VLLM_ENABLED
}

# Floor for vLLM KV-cache blocks beyond weights + activation overhead.
MINIMUM_KV_CACHE_BYTES = 1 * GIB

DTYPE_BYTES = {'float32': 4, 'auto': 2, 'float16': 2, 'bfloat16': 2}

@dataclass
class WeightEstimate:
    total_bytes: int
    method: str          # 'safetensors_index' | 'param_count' | 's3_artifact'
    detail: str          # human-readable derivation

@dataclass
class FitFinding:
    arch: str
    fits: bool
    budget_bytes: int            # gpu_memory_utilization * profile[arch]
    required_bytes: int          # estimate + MINIMUM_KV_CACHE_BYTES
    message: str                 # names profile entry, numbers, remediation
```

`estimate_weights(record, s3_head=None, hf_fetch=None)`:
- HF source: `hf_fetch` (injected; default `urllib` GET) retrieves
  `https://huggingface.co/api/models/{id}?blobs=true`; sum the sizes of
  `*.safetensors` siblings (this equals the stored weight bytes, which matches
  on-GPU bytes for non-quantized checkpoints). Fallback: `safetensors.total` /
  parameter count from the API × `DTYPE_BYTES[dtype]`; when the model config
  carries a `quantization_config`, size by its bits-per-weight instead.
- S3 source: `s3_head` (injected) returns `ContentLength` of the `.tar.gz`;
  used as-is (compressed size underestimates slightly — acceptable for a
  warning-grade estimate; noted in `detail`).
- Returns `None` on any fetch/parse failure — callers skip the check and
  report "fit could not be verified" (Requirement 3.4). Network calls use a
  short timeout (~5 s) so registration latency stays bounded.

`evaluate_fit(engine_configuration, estimate, architectures)`:
- For each arch present in `DEVICE_MEMORY_PROFILE_BYTES`:
  `fits = gpu_memory_utilization * profile[arch] >= estimate.total_bytes +
  MINIMUM_KV_CACHE_BYTES`.
- Failing messages state the estimate, the configured fraction, the budget for
  the named profile entry, and remediation phrased in the correct direction:
  "raise gpu_memory_utilization (weights alone exceed the configured budget),
  reduce max_model_len, or choose a smaller model" (Requirement 3.9).

### 2. Registration + update (`model_import.py`)

- `register_vllm_model`: after validation and record write, run
  `estimate_weights` + `evaluate_fit` over
  `vllm_supported_architectures()`; include `fit_check` in the 201 response:
  `{status: 'passed'|'warnings'|'unverified', findings: [...]}` (Requirement
  3.5). Non-blocking by design — registration succeeds regardless.
- New handler `update_vllm_engine_configuration` for
  `PUT /api/v1/models/vllm/{training_id}/engine-configuration`:
  - Loads the record, rejects non-vLLM records (400), validates supplied keys
    with the existing `_validate_engine_setting` / unknown-key fail-closed
    logic, overlays onto the stored config, writes back
    (Decimal-converted), audits with before/after values, and returns
    `{engine_configuration, fit_check, notice}` where `notice` states the
    change takes effect on next package + publish (Requirements 2.1–2.4, 2.6).
  - RBAC: DataScientist on the use case, mirroring registration.
- Route wiring in the model-import router and API Gateway resource
  (compute-stack.ts) alongside the existing `/models/vllm` routes.

### 3. Publish gate (`greengrass_publish.py`)

In the vLLM branch, before any `create_component_version` call: run
`estimate_weights` + `evaluate_fit` over the branch's
`vllm_supported_architectures()`. If every supported arch fails and the request
body lacks `skip_fit_check: true`, return
`create_response(422, {error, fit_check: {findings}})` with the full sizing
message (Requirement 3.6). With the override, log + audit `skip_fit_check` and
proceed (Requirement 3.7). `None` estimate → log, annotate response, proceed
(Requirement 3.4). This is the fail-closed point because publish is the moment
the configuration becomes deployable.

### 4. Model detail exposure (`models.py`, `ModelDetail.tsx`)

- `models.py get_model`: add `'engine_configuration':
  _decimal_safe(item.get('engine_configuration'))` to the detail response for
  vLLM records (Requirement 1.2).
- `ModelDetail.tsx`: for vLLM models, render an "Engine configuration" section
  (key/value list) with an Edit action opening an inline form pre-filled with
  stored values, reusing the field-rendering/validation-finding patterns from
  `RegisterLlm.tsx`; submit via a new
  `apiService.updateVllmEngineConfiguration(trainingId, values)`; on success
  show the returned notice ("takes effect after re-package + publish") and any
  fit-check warnings (Requirements 1.3, 2.5).

### 5. Device-side 409 surfacing (`vllm_model_prep.py`)

`request_load` currently logs the status code and raw body. Change (Requirement 4):

```python
def extract_load_failure_reason(body_text: str) -> str:
    # Triton returns {"error": "..."} — fall back to the raw text.
    try:
        parsed = json.loads(body_text)
        if isinstance(parsed, dict) and parsed.get("error"):
            return str(parsed["error"])
    except ValueError:
        pass
    return body_text.strip()

KV_CACHE_HINT_MARKERS = ("No available memory for the cache blocks",
                         "gpu_memory_utilization")
```

On an authoritative non-200: one prominent ERROR line —
`"VllmLoadModel: model '{name}' FAILED to load (HTTP {code}): {reason}"` —
followed, when a KV-cache marker matches, by a remediation line naming
`gpu_memory_utilization`/`max_model_len` and stating the value must be RAISED
or the model reduced (Requirements 4.1–4.3). `prepare` passes the staged
engine args (already parsed by `validate_repository`) into the load path so the
failure log includes the active `gpu_memory_utilization` and `max_model_len`
(Requirement 4.4). Raw-body logging is retained at debug level for triage.

### 6. Package_Dialog error content (`WorkflowToolbar.tsx`)

Replace the overwriting `else if` branch:

```typescript
} else if (typeof err.details?.failing_artifact === 'string') {
  const artifact = err.details.failing_artifact;
  content = `${err.message} (failing artifact: ${artifact})`;
  if (artifact.startsWith('models/') && /no published Greengrass component/i.test(err.message)) {
    content += ' — open the Models page and use Package & Publish on this model, then package the workflow again.';
  }
}
```

Ordering: findings branch first (unchanged, Requirement 5.2), then the
message+artifact branch (Requirements 5.1, 5.3), then the existing
"already exists" regex rewrite runs last over `content` (Requirement 5.4 —
unchanged, and still matches since the backend message is now included). No
structured details → `content` stays `err.message` (Requirement 5.5).

## Data Models

- **vLLM_Model_Record** (training-jobs table): unchanged shape;
  `engine_configuration` becomes mutable via the new endpoint; `updated_at`
  refreshed on edit.
- **Fit_Check result** (API responses, not persisted):
  `{status: 'passed'|'warnings'|'unverified', estimate: {total_bytes, method,
  detail} | null, findings: [{arch, fits, budget_bytes, required_bytes,
  message}]}`.
- **Device_Memory_Profile / MINIMUM_KV_CACHE_BYTES**: code constants in
  `vllm_fit_check.py` (single source; publish Lambda imports the module file
  bundled with the functions directory).

## Error Handling

- Engine-config update: 400 with `{error, findings[]}` on validation failure
  (same finding shape as registration); 404-style behavior for missing records
  mirrors existing model handlers; record untouched on any failure.
- Fit_Check: never throws out of `estimate_weights`/`evaluate_fit`; estimation
  failure degrades to `unverified` (fail open at registration/update, explicit
  annotation at publish). Publish gate failure is 422 before any Greengrass
  mutation, preserving the record's pre-publish state (consistent with the
  existing vLLM publish atomicity design).
- Device: behavior classification (`LOAD_OK`/`LOAD_HTTP_ERROR`/
  `LOAD_UNREACHABLE`) and retry semantics are unchanged; only log content
  improves.
- Package_Dialog: all existing failure paths keep their behavior; only the
  content composition changes.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid
executions of a system — essentially, a formal statement about what the system
should do. Properties serve as the bridge between human-readable specifications
and machine-verifiable correctness guarantees.*

### Property 1: Packaging preserves the stored engine configuration

For any vLLM_Model_Record with a resolved Engine_Configuration, the `model.json`
emitted by `generate_vllm_repository` contains exactly the record's engine
settings (numerically equal after Decimal conversion) plus the `model` reference
key, and nothing else.

**Validates: Requirements 1.1**

### Property 2: Engine-configuration update round trip

For any existing vLLM_Model_Record and any valid partial update, the stored
Engine_Configuration afterward equals the previous configuration overlaid with
the supplied values, and the update response returns that same complete
configuration.

**Validates: Requirements 2.1, 2.4**

### Property 3: Invalid updates change nothing

For any engine-configuration update containing at least one unknown key or
out-of-range value, the response is HTTP 400 with a finding naming every
offending field, and the stored Engine_Configuration is byte-identical to its
pre-request value.

**Validates: Requirements 2.2**

### Property 4: Fit_Check decision correctness

For any engine configuration, weight estimate, and architecture set, a
FitFinding reports `fits = true` if and only if
`gpu_memory_utilization × DEVICE_MEMORY_PROFILE_BYTES[arch] ≥ estimate +
MINIMUM_KV_CACHE_BYTES`, and every failing finding's message contains the
architecture name, the budget, the estimate, and the word "raise" applied to
`gpu_memory_utilization` (never advice to lower it).

**Validates: Requirements 3.1, 3.8, 3.9**

### Property 5: Unverifiable estimates never block

For any vLLM_Model_Record whose weight estimation returns no estimate,
registration, update, and publish all proceed (no fit-related rejection), and
the response marks the fit check as unverified.

**Validates: Requirements 3.4**

### Property 6: Load-failure reason extraction

For any HTTP error body, `extract_load_failure_reason` returns the `error`
field's text when the body is a JSON object with a non-empty `error`, and the
raw body text otherwise; the resulting prominent ERROR line always contains the
model name, HTTP status, and that reason.

**Validates: Requirements 4.1, 4.3**

### Property 7: Package_Dialog message composition

For any ApiError with a message and a string `failing_artifact` (and no findings
list), the dialog content contains both the backend message and the failing
artifact; when the artifact is `models/{name}` and the message states the model
has no published Greengrass component, the content additionally contains the
Models-page publish hint; and for any content matching "already exists", the
final displayed content is the existing immutability rewrite.

**Validates: Requirements 5.1, 5.3, 5.4**

## Testing Strategy

- **Property-based tests** (Hypothesis for Python, fast-check for TypeScript,
  matching existing repo conventions: `edge-cv-portal/backend/tests/` and
  frontend `*.property.test.ts`) for Properties 1–7, each tagged
  `Feature: vllm-sizing-and-packaging-errors, Property N`.
- **Unit tests** for: HF metadata parsing fixtures (safetensors index, param
  count, quantization config), S3-size estimation, the publish 422 gate and
  `skip_fit_check` override, the models.py detail exposure, and the
  Package_Dialog branches (findings, artifact+message, no details).
- **Integration-style backend tests** using the existing moto/table fixtures for
  the update endpoint (RBAC, audit event, record round trip).
- No new on-hardware automation: device-side log wording is covered by unit
  tests of `extract_load_failure_reason` and the prepare/load logging path with
  a mocked runtime; on-device verification rides the existing manual JP6
  validation procedure.
