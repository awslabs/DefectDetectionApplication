# Implementation Plan: vLLM Workflow Latency Optimization

## Overview

Implementation proceeds measurement-first: build the pure Generation_Phase_Breakdown
module, instrument the manager's generate path additively, surface metrics through the
endpoint and the LLM_Binding into the run log, fix the Pipeline_EOS timing artifact,
add the output-token-budget and image-downscaling levers, and update the catalog and
docs. All code is host-testable via the injectable fake-engine seams
(`PYTHONPATH=src/backend:test/backend-test`, `/tmp/kiro-test-venv` interpreter). New
property tests live in `test/backend-test/vllm_latency/` (one file per property, so
they can be authored in parallel), following the fake-engine patterns in
`test/backend-test/vllm_runtime/`. Device measurement tasks come last: they require
user-coordinated builds and deploys to jetson-thor1 (JP7) per
`.kiro/steering/builds.md` and produce the Latency_Report and Model_Variant_Guidance
evidence.

## Tasks

- [x] 1. Generation_Phase_Breakdown module
  - [x] 1.1 Create `src/backend/vllm_runtime/generation_metrics.py`
    - `GenerationPhaseBreakdown` frozen dataclass: `queueing_ms`/`prefill_ms`/`decode_ms`
      as `Optional[int]` (None = unavailable), `prompt_tokens`/`output_tokens`/`image_tokens`
      counts, `image_tokens_applicable`, `truncated`, `prefill_includes_queueing`
    - `build_breakdown()` with layered phase sources: V0 `output.metrics`
      (`arrival_time`/`first_scheduled_time`/`first_token_time`/`finished_time`) when
      usable; otherwise manager monotonic fallback with `queueing_ms=None` and
      `prefill_includes_queueing=True`; each duration `max(0, round(s*1000))`,
      unreadable/negative sources marked unavailable
    - All-zero guard: all-present-and-zero phases become all-unavailable
    - Token counts best-effort from `prompt_token_ids` / `outputs[0].token_ids`;
      image token count = placeholder-id occurrences when image supplied and id
      readable; no image ⇒ `image_tokens_applicable=False` (rendered `n/a`)
    - Truncation from `finish_reason == "length"`; unreadable ⇒ None
    - `to_payload()` and `to_log_line(node_id, model_name)` render None as
      `"unavailable"` and non-applicable image count as `"n/a"`, never dropping fields
    - _Requirements: 1.1, 1.3, 1.4, 1.6, 3.5, 5.1, 5.5_

  - [x]* 1.2 Write property test for breakdown construction
    - **Property 1: Breakdown well-formedness and measurement honesty**
    - **Validates: Requirements 1.1, 1.3, 1.4, 1.6, 5.1, 5.5**
    - New file in `test/backend-test/vllm_latency/`; hypothesis, min 100 examples,
      tagged `# Feature: vllm-workflow-latency-optimization, Property 1: Breakdown well-formedness and measurement honesty`

- [x] 2. VllmRuntimeManager instrumentation (additive)
  - [x] 2.1 Instrument `src/backend/vllm_runtime/manager.py` generate path
    - `_request` captures `t_submit`/`t_first`/`t_last` monotonic timestamps and the
      final `RequestOutput`, all inside try/except shells that never disturb the
      yield stream (contained per R1.5)
    - New additive `generate_with_breakdown(...) -> Tuple[str, Optional[GenerationPhaseBreakdown]]`;
      `generate` delegates to it and discards the breakdown (single shared code path,
      untouched signature and semantics per R9.1)
    - Image placeholder token id: best-effort reader over the loaded engine's model
      config (`image_token_id`, `_safe_attr` discipline), cached per `_ManagedModel`
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 3.2, 9.1_

  - [x] 2.2 Add prefix-caching load-time log to `manager.py`
    - In the load-success path, when `engine_args.get("enable_prefix_caching")` is
      truthy, write one INFO line to the backend application log naming the model and
      stating Prefix_Caching is active; falsy/absent ⇒ no line, no behavior change
      (passthrough already exists)
    - _Requirements: 4.1, 4.6_

  - [x]* 2.3 Write property test for generation semantics under instrumentation
    - **Property 2: Generation semantics preserved under instrumentation**
    - **Validates: Requirements 1.5, 9.1**
    - Deterministic fake engine + injected capture/emission failures; identical text
      and error types (ModelUnavailableError, GenerationError)

  - [x]* 2.4 Write property test for prefix-caching passthrough and logging
    - **Property 8: Prefix_Caching passthrough and load-time logging**
    - **Validates: Requirements 4.1, 4.6**
    - Random engine-arguments mappings; unchanged key-for-key passthrough; log entry
      iff `enable_prefix_caching` truthy

  - [x]* 2.5 Write property test for failure isolation
    - **Property 12: Failure isolation preserved under optimization**
    - **Validates: Requirements 9.6**
    - Multiple loaded fake models, injected generation failure in one; existing error
      types, failing model named, other models READY and serving

  - [x]* 2.6 Write example test for prefix-caching load-failure fallback log
    - A load failure with `enable_prefix_caching` set flows through the existing
      FAILED path and produces the existing ERROR log line (model + classified
      reason) as the R4.5 fallback notification
    - _Requirements: 4.4, 4.5_

- [x] 3. Text_Generation_API additive metrics field
  - [x] 3.1 Update `src/backend/endpoints/text_generation.py` non-streaming handler
    - Call `runtime.generate_with_breakdown(...)` when the runtime exposes it
      (`getattr` check — injected fakes without it keep working, producing no
      metrics), else `runtime.generate(...)` exactly as today
    - When a breakdown is captured, add the single additive 200-response field
      `"generation_metrics": breakdown.to_payload()`; all existing fields untouched;
      no breakdown ⇒ byte-identical pre-feature body
    - No change to request validation, `GENERATION_DEFAULTS`, retry, timeout,
      streaming, or error mappings (R3.7 preserved by not touching the API defaults)
    - _Requirements: 1.1, 3.7, 9.2_

  - [x]* 3.2 Write property test for API additivity
    - **Property 11: API additivity**
    - **Validates: Requirements 9.2**
    - Fake runtime through the endpoint; every pre-feature field identical, response
      differs at most by `generation_metrics`

  - [x]* 3.3 Write example test for direct-caller default behavior (R3.7)
    - Non-LLM_Binding caller omitting `max_tokens` gets the pre-feature
      `GENERATION_DEFAULTS` 256 behavior unchanged — no injected default, no new
      fields on the request path
    - _Requirements: 3.7_

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - Run: `PYTHONPATH=src/backend:test/backend-test /tmp/kiro-test-venv/bin/python -m pytest test/backend-test/vllm_latency test/backend-test/vllm_runtime test/backend-test/text_generation -q`

- [x] 5. LLM_Binding: Output_Token_Budget and metrics emission
  - [x] 5.1 Add budget resolution to `src/backend/workflow_engine/output_bindings.py`
    - `DEFAULT_OUTPUT_TOKEN_BUDGET = 256` and pure helper
      `resolve_output_token_budget(raw) -> Tuple[int, Optional[str]]`: valid integral
      number ≥ 1 (bool excluded, integral floats accepted) ⇒ (value, None); absent ⇒
      (256, None); invalid ⇒ (256, notice naming the rejected value)
    - `_run_one` resolves the budget before invocation and always places the resolved
      integer into the request parameters (explicit `max_tokens` on every call);
      substitution notice logged at WARNING into the run log
    - _Requirements: 3.1, 3.3, 3.4_

  - [x] 5.2 Add generation-metrics return path to `output_bindings.py`
    - `_default_llm_invoker` gains keyword-only `metrics_sink: Optional[Callable[[dict], None]] = None`;
      on 200 calls `metrics_sink(payload.get("generation_metrics"))` (contained)
    - `_run_one` passes `metrics_sink` only when the (possibly injected) invoker
      accepts the keyword (`_accepts_keyword` pattern) so pre-feature fakes work
    - With a captured metrics dict: emit one INFO run-log line (all fields,
      `unavailable`/`n/a` included, truncation clause iff `truncated: true`) and merge
      the dict additively as `outcome["generation_metrics"]`; both steps contained
      (debug log on failure, node outcome and run state unchanged)
    - _Requirements: 1.2, 1.5, 3.5, 3.6_

  - [x]* 5.3 Write property test for budget resolution
    - **Property 7: Output_Token_Budget resolution is total and explicit**
    - **Validates: Requirements 3.1, 3.3, 3.4**
    - Any configured value (valid / absent / invalid); request `max_tokens` equals
      configured value when valid, exactly 256 otherwise; substitution notice iff
      present-and-invalid

  - [x]* 5.4 Write property test for run-log breakdown emission
    - **Property 3: Run-log breakdown emission with iff-truncation**
    - **Validates: Requirements 1.2, 3.5, 3.6**
    - Any generation_metrics payload delivered by the invoker; exactly one run-log
      line with every field; truncation statement iff payload reports truncation

  - [x]* 5.5 Write example test for run-log line format
    - Assert the concrete formatted line for a representative breakdown (node, model,
      phases, token counts, truncation clause) and for unavailable/`n/a` renderings
    - _Requirements: 1.2, 3.5_

- [x] 6. LLM_Binding: image downscaling
  - [x] 6.1 Add downscaling helpers and wiring to `output_bindings.py`
    - `resolve_max_image_dimension(raw) -> Tuple[Optional[int], Optional[str]]`:
      absent/None ⇒ unconfigured, silent; integral ≥ 1 ⇒ value; anything else ⇒
      unconfigured + invalid notice (run-log WARNING, R5.8)
    - `downscale_image_bytes(data, max_dim) -> bytes` (Pillow): longer edge > max_dim
      ⇒ resize so longer edge == max_dim, aspect preserved, LANCZOS, re-encode JPEG;
      else return data unchanged (never upscale); raises on decode/encode failure
    - Wire into `_run_one` for both the `in` frame and the optional `reference`
      frame, after read and before base64 encoding; a raised failure logs a run-log
      WARNING naming the node and failure and sends the original bytes; unconfigured
      ⇒ code path skipped entirely (byte-identical pre-feature request); no image ⇒
      no downscaling attempt
    - _Requirements: 5.3, 5.4, 5.5, 5.6, 5.7, 5.8_

  - [x]* 6.2 Write property test for downscaling geometry
    - **Property 9: Downscaling geometry (iff around the threshold)**
    - **Validates: Requirements 5.3, 5.7**
    - Real PIL images (hypothesis dimensions ~1–512 px); longer edge > max ⇒ decoded
      output's longer edge == max with aspect preserved (1-pixel rounding); ≤ max ⇒
      byte-identical

  - [x]* 6.3 Write property test for downscaling configuration/failure containment
    - **Property 10: Downscaling configuration and failure containment**
    - **Validates: Requirements 5.4, 5.6, 5.8**
    - Unconfigured ⇒ byte-identical, no warning; invalid value ⇒ unmodified bytes +
      exactly one warning naming the value; failing downscale ⇒ original bytes +
      warning + same node outcome shape and terminal effect

- [x] 7. Pipeline_EOS terminal marking
  - [x] 7.1 Add `mark_pipeline_success` to `src/backend/workflow_engine/node_status.py`
    - `NodeStatusCollector.__init__` records
      `self._pipeline_node_ids = {nid for nid in name_map.values() if nid is not None}`
      (binding nodes from `extra_node_ids` excluded by construction)
    - `mark_pipeline_success()`: for exactly the name_map-derived Pipeline_Nodes,
      running ⇒ success via the existing `_set_status` single-write path (freezing
      duration at EOS); warning retained with detail; failure retained; pending
      untouched; binding nodes untouched; fully contained (collector best-effort
      discipline)
    - _Requirements: 2.1, 2.2, 2.4, 2.6, 2.7_

  - [x] 7.2 Add call site in `src/backend/workflow_engine/pipeline_executor.py`
    - Immediately after `run_pipeline` returns cleanly (both frame-feed and plain
      invocations), before the existing `_persist_node_status_snapshot`:
      `collector.mark_pipeline_success()` when collector is not None
    - No other executor path changes; failure/timeout paths never reach the call
      site, so existing finalize rules apply byte-identically; terminal
      `_persist_node_status` remains the authoritative last write
    - _Requirements: 2.1, 2.3, 2.5, 2.6_

  - [x]* 7.3 Write property test for scoped, parity-preserving EOS marking
    - **Property 4: Pipeline_EOS terminal marking is scoped and parity-preserving**
    - **Validates: Requirements 2.1, 2.3, 2.4, 2.5, 2.7**
    - Random name_map / extra node ids / status histories; featured vs. reference
      pre-feature transition sequence for final-map parity

  - [x]* 7.4 Write property test for duration freezing at EOS
    - **Property 5: Node_Execution_Time frozen at Pipeline_EOS**
    - **Validates: Requirements 2.2**
    - Any subsequent terminal-marking sequence (`mark_success_all`, `finalize`,
      `mark_failure` on other nodes) never changes an EOS-frozen duration

  - [x]* 7.5 Write property test for EOS marking containment
    - **Property 6: EOS marking containment**
    - **Validates: Requirements 2.6**
    - Injected internal faults; `mark_pipeline_success` never raises; run lifecycle
      proceeds to the same outcome as fault-free

- [x] 8. Catalog and documentation
  - [x] 8.1 Add `max_image_dimension` parameter to both catalog copies
    - Edit `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`
      and `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`
      byte-identically in one commit: `ParameterDescriptor("max_image_dimension",
      "int", required=False, default=None, constraints={"min": 1}, description=...)`
      with description and examples meeting the catalog test requirements
    - Extend the `max_tokens` description with the 256-token default's latency effect
      and the verdict-style 20–30-token guidance
    - _Requirements: 3.8, 5.3_

  - [x] 8.2 Rebaseline catalog goldens
    - Rebaseline the csi_nvargus_optional vendored `nodes.py` sha256 golden and the
      portal `catalog_baseline.json` if needed, per the established procedure (update
      goldens, never weaken or delete tests)
    - _Requirements: 9.3_

  - [x]* 8.3 Write example test for catalog mirror byte-identity
    - Assert the portal and vendored `nodes.py` copies are byte-identical and the new
      `max_image_dimension` descriptor is present in both with the expected
      constraints (skip if an existing mirror test already pins this — then extend it
      through its documented path only)
    - _Requirements: 9.3_

  - [x] 8.4 Create `docs/vllm-latency.md`
    - R3.8 Output_Token_Budget documentation: default of 256 tokens, its latency
      effect, verdict-style (~20–30 tokens) guidance
    - Model_Variant_Guidance skeleton: prefix-caching enablement in Engine_Arguments,
      first/repeat prefill table (to fill), resolution-vs-prefill table (to fill),
      variant measurement sections with packaging steps, Engine_Arguments, verbatim
      outputs, memory tradeoffs (filled by the device measurement tasks)
    - _Requirements: 3.8, 4.3, 5.2, 7.1, 7.2, 7.3, 7.4, 7.5_

- [ ] 9. Checkpoint - Full backend suite and catalog gates green
  - Ensure all tests pass, ask the user if questions arise.
  - Full backend suite: `PYTHONPATH=src/backend:test/backend-test` with
    `/tmp/kiro-test-venv` — zero failures, zero errors, no weakened or deleted
    assertions (R9.3)
  - Catalog mirror and csi_nvargus_optional preservation suites green
  - If the catalog was touched: run the portal-layer workflow_core tests in a
    `python:3.11-slim` container
  - _Requirements: 9.3_

- [ ] 10. Device measurement and verification (jetson-thor1 JP7 + JP6; user-coordinated)
  - All build/deploy steps in this phase are coordinated with the user per
    `.kiro/steering/builds.md`: strictly one component build at a time, move
    `cdk.out` aside, run the preservation guard suite and confirm green BEFORE
    starting any build, no portal deploys during a build. Each measurement writes its
    execution ids and results into the Latency_Report / Model_Variant_Guidance.
  - [ ] 10.1 Build and deploy the instrumented backend to jetson-thor1 (JP7)
    - Coordinate with the user: pre-build guard suite green, `cdk.out` moved aside,
      no concurrent build; build the JP7 component and deploy to jetson-thor1;
      confirm backend healthy (no crash, no restart loop)
    - _Requirements: 9.4_
  - [ ] 10.2 Measure instrumentation overhead (procedure 1)
    - Script ≥5 identical Generation_Calls with instrumentation vs. without (feature
      toggled by pre-feature build or capture kill-switch patch); compare median
      end-to-end latency; pass gate ≤10 ms delta; record in Latency_Report
    - _Requirements: 1.7_
  - [ ] 10.3 Verify EOS timing on device (procedure 2)
    - Run the baseline workflow; confirm Pipeline_Nodes reach terminal status with
      durations ~30 ms (not ~17.5 s) and the node-status endpoint serves them while
      the LLM binding is still running
    - _Requirements: 2.1, 2.2, 9.4_
  - [ ] 10.4 Measure Output_Token_Budget effect (procedure 3)
    - Same workflow with `max_tokens` 256 vs. unbounded baseline vs. 30
      (verdict-style); single-variable comparison from Generation_Phase_Breakdowns;
      record in Latency_Report with execution ids
    - _Requirements: 3.2, 8.2, 8.3, 8.6_
  - [ ] 10.5 Measure Prefix_Caching (procedure 4)
    - Enable `enable_prefix_caching` in the model component's model.json; two
      consecutive runs sharing a ≥100-token prefix; compare first/repeat prefill from
      breakdowns; record memory preflight outcome; document enablement and tradeoff
      in Model_Variant_Guidance
    - _Requirements: 4.2, 4.3, 4.4, 8.2_
  - [ ] 10.6 Measure image scaling (procedure 5)
    - ≥2 resolutions differing ≥2x in total pixel count via `max_image_dimension`;
      record image tokens and prefill per resolution in Model_Variant_Guidance
    - _Requirements: 5.2, 8.2_
  - [ ] 10.7 Measure fixed costs and non-generation overhead (procedure 6)
    - Consecutive runs on a READY model; assert no load/engine-construction log lines
      between request receipt and response; compute non-generation overhead =
      end-to-end − Generation_Call from per-node timing; gate ≤500 ms or attribute
      the excess to specific nodes/phases in the Latency_Report
    - _Requirements: 6.1, 6.2, 6.3, 6.4_
  - [ ] 10.8 Measure at least one model variant (procedure 7)
    - Package ≥1 quantized/smaller variant (e.g. AWQ or smaller Qwen-VL build) with
      identical workflow/prompt/image/budget; record decode rate, end-to-end latency,
      verbatim outputs and quality differences — or the load/preflight failure
      outcome — in Model_Variant_Guidance with packaging steps and Engine_Arguments
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_
  - [ ] 10.9 Verify collector/executor EOS change on JP6
    - Build and deploy the JP6 component (user-coordinated, sequential per
      builds.md); run a workflow end to end to a successful terminal state with
      sustained backend health; record device and outcome with the commit
    - _Requirements: 9.4_
  - [ ] 10.10 Write the final Latency_Report (procedure 8)
    - Create `.kiro/specs/vllm-workflow-latency-optimization/latency-report.md`:
      baseline (execution 9c98f4b7, ~17.5 s / 17.43 s) vs. optimized first-run and
      median-of-3 steady state; per-optimization attribution (or declared combined
      measurements); residual decode-rate floor as a function of output tokens;
      sub-second identification for the freeform and bounded-verdict configurations;
      non-generation overhead decomposition; execution-id traceability for every
      stated value
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 6.3_

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each property test is one hypothesis test with minimum 100 examples
  (`@settings(max_examples=100)` or higher), tagged
  `# Feature: vllm-workflow-latency-optimization, Property N: <title>`, in its own
  file under the new `test/backend-test/vllm_latency/` package
- Host test invocation: `PYTHONPATH=src/backend:test/backend-test` with the
  `/tmp/kiro-test-venv` interpreter (pytest/hypothesis/sarge/testfixtures/
  sqlalchemy/fastapi/numpy/opencv-headless/pillow/pyyaml/alembic/boto3 available)
- `output_bindings.py` is edited by 5.1, 5.2, and 6.1 — these are sequenced in
  separate waves; `manager.py` is edited by 2.1 and 2.2, catalog files by 8.1 then
  goldens by 8.2, likewise sequenced
- Phase 10 requires real hardware and user-coordinated builds; nothing in it starts
  until checkpoints 4 and 9 are green. Per `.kiro/steering/builds.md`: one build at a
  time, `cdk.out` aside, guard suite green before every build, no portal deploys
  mid-build
- Any preservation-tracked file touched gets its golden baseline updated through the
  documented maintenance path in the same commit (R9.3)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "7.1", "8.4"] },
    { "id": 1, "tasks": ["1.2", "2.1", "5.1", "7.2", "8.1"] },
    { "id": 2, "tasks": ["2.2", "3.1", "5.2", "7.3", "7.4", "7.5", "8.2"] },
    { "id": 3, "tasks": ["2.3", "2.4", "2.5", "2.6", "3.2", "3.3", "6.1", "8.3"] },
    { "id": 4, "tasks": ["5.3", "5.4", "5.5", "6.2", "6.3"] },
    { "id": 5, "tasks": ["10.1"] },
    { "id": 6, "tasks": ["10.2"] },
    { "id": 7, "tasks": ["10.3"] },
    { "id": 8, "tasks": ["10.4"] },
    { "id": 9, "tasks": ["10.5"] },
    { "id": 10, "tasks": ["10.6"] },
    { "id": 11, "tasks": ["10.7"] },
    { "id": 12, "tasks": ["10.8"] },
    { "id": 13, "tasks": ["10.9"] },
    { "id": 14, "tasks": ["10.10"] }
  ]
}
```
