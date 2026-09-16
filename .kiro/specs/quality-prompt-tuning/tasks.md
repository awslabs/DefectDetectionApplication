# Implementation Plan: Workflow Tuning — VLM/LLM Anomaly Tuning

## Overview

Implementation proceeds bottom-up so that "a candidate's score predicts the deployed node" holds by construction. First the preservation baseline is captured and the executor's request construction is extracted into `workflow_core.anomaly_invocation` (Portal layer, re-vendored to the device) with both processors delegating to it — a behaviour-neutral refactor proven by Property 7 before any tuning code exists. Then the device side: Sample_Export at invocation time, the one-shot backfill, and the Device_Score_Job runner. Then the Portal backend: deployment configuration and grants, the DynamoDB table, the `workflow_tuning.py` Lambda with session/label/candidate routes, the chunked Bedrock_Scorer, the Device_Score_Job dispatcher and apply. Finally the Portal frontend: navigation, overview, session workspace, and the toolbar/node-panel entry points.

Tests: `workflow_core` and Portal backend in `edge-cv-portal/backend/tests/` (pytest + moto + Hypothesis); device in `test/backend-test/workflow_engine/` (pytest + Hypothesis, fake invokers/S3/shadow); Portal frontend with vitest + `@testing-library/react` + fast-check. Every correctness property gets exactly one property-based test at ≥ 100 iterations, in the file the design names, tagged `Feature: quality-prompt-tuning, Property {n}: {text}`.

Per the repo's builds steering: changes under `src/backend` must be verified on a real device (JP7 at minimum, since `llm_inference` only exists on vLLM architectures) before they are committed; `output_bindings.py` is preservation-tracked and its hash must be rebaselined in the same commit as the refactor; do not run a portal deploy while a component build is running.

## Tasks

- [ ] 1. Capture the preservation baseline and extract the shared Invocation_Builder
  - [ ] 1.1 Write the preservation property test against the pre-refactor executor
    - `test/backend-test/workflow_engine/test_property_anomaly_invocation_preservation.py`: generate `bedrock_inference` and `llm_inference` bindings across whole-frame, crop path, payload reference, missing/unfed/unreadable reference, anomaly and freeform modes, with and without system prompt; drive both processors with a recording fake invoker and a temp artifact directory; snapshot invoker arguments, raised/recorded errors, artifact names and merged metadata as the baseline fixture
    - **Property 7: The extraction into `workflow_core` is behaviour-neutral**
    - **Validates: Requirements 11.1, 11.2**

  - [ ] 1.2 Create `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/anomaly_invocation.py`
    - `BEDROCK_JSON_INSTRUCTION`, `BEDROCK_DEFAULT_MODEL`, `DEFAULT_MAX_TOKENS`; `is_tunable_node` / `is_anomaly_mode` with the executor's `_coerce` semantics; `BedrockInvocation` (+ `converse_kwargs()`), `build_bedrock_invocation`; `LlmInvocation` (+ `request_body()`), `build_llm_invocation` with an injectable downscaler and the output-token-budget rule; `parse_verdict` (moved `parse_bedrock_answer`); `prompt_fingerprint`; `categorize_outcome`; `summarize_outcomes`
    - Pure module: no boto3, no I/O, package-relative imports only
    - _Requirements: 1.5, 6.2, 6.5, 6.7_

  - [ ]* 1.3 Write property tests for the shared module
    - `edge-cv-portal/backend/tests/test_property_anomaly_invocation.py`
    - **Property 6: Executor, Bedrock_Scorer and Device_Score_Job build identical invocations** (pure-module half; the three call paths are asserted in tasks 3.4 and 6.3) — **Validates: Requirements 6.1, 6.2, 6.3, 6.4**
    - **Property 8: Outcome categorization is total and exact** — **Validates: Requirements 6.5**
    - **Property 9: The Score_Summary is a function of the persisted outcomes** — **Validates: Requirements 6.7, 6.11, 6.14, 10.4**

  - [ ]* 1.4 Write property test for eligibility (shared-module half)
    - `edge-cv-portal/backend/tests/test_property_anomaly_invocation_eligibility.py`; publish its generated case table as the fixture `edge-cv-portal/backend/tests/fixtures/anomaly_tuning_eligibility_cases.json`, which the Portal frontend half (task 8.5) consumes so both halves assert the same cases
    - **Property 1: Tunable classification is one function used everywhere**
    - **Validates: Requirements 1.5**

  - [ ] 1.5 Re-vendor and refactor the device executor to delegate
    - Run `src/backend/workflow_engine/vendor/re_vendor.sh`; confirm the drift guard passes
    - `BedrockInferenceProcessor._run_one`: keep `_detection_crop` / `_payload_reference` / capturePaths resolution, metadata assembly and `_persist_annotated_frame`; build via `build_bedrock_invocation`, call the invoker with the pre-feature positional arity from the invocation's fields, parse via `parse_verdict`
    - `LlmInferenceProcessor._run_one`: keep `render_prompt`, capturePaths reads, fail-closed rules and metrics handling; build via `build_llm_invocation`, preserve the 3/4/5-arg invoker forms and keyword gating
    - `_default_bedrock_invoker` / `_default_llm_invoker` consume `converse_kwargs()` / `request_body()`; keep `parse_bedrock_answer` as an alias of `parse_verdict` for existing importers
    - Run task 1.1's test plus `test_property_bedrock_inspection.py`, `test_property_bedrock_concurrency.py`, `test_property_triple_annotated_frame.py`; rebaseline the preservation-gate hash for `output_bindings.py` in the same change
    - _Requirements: 6.2, 11.1, 11.2_

  - [ ]* 1.6 Write unit tests for the shared module and the delegation
    - Instruction appended iff anomaly mode per node type; system verbatim/None; image label order; defaults; `converse_kwargs` shape; LLM downscale/base64/budget parity; fingerprint stability; processors preserve invoker arities with 3/4/5-arg fakes
    - _Requirements: 6.2, 11.1_

- [ ] 2. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 3. Implement device-side Sample_Export, backfill and the job runner
  - [ ] 3.1 Create `src/backend/workflow_engine/tuning/sample_export.py` and wire the processors
    - `ExportConfig.from_component_configuration(...)` (absent/disabled/empty bucket/prefix without `/` ⇒ None); `SampleExporter` with a 200-entry bounded queue, background thread, 3 attempts with backoff, oldest-drop WARNING, > 8 MiB skip; object layout `{prefix}{workflowId}/{nodeId}/{thingName}/{executionId}.json` + `.input.jpg` + `.reference.jpg` with the sidecar schema (including `metadataSnippet` for `llm_inference`)
    - Processors enqueue the exact bytes sent, the answer, verdict/parse error, fingerprint, `detection_id`/slot, inside the existing containment; no exporter when unconfigured
    - _Requirements: 2.2, 2.3, 2.4, 2.5, 2.6, 2.10, 11.2, 11.3_

  - [ ] 3.2 Create `tuning/backfill.py`
    - One-shot on the enabled transition guarded by `/aws_dda/workflow-tuning/backfilled.json`; per registration and tunable node, newest 500 executions (`started_at DESC, id DESC`), pair `original`-when-`detection_id`-else-`in` with `reference` when present, skip error outcomes and missing inputs, enqueue with `source: backfill`
    - _Requirements: 2.7_

  - [ ] 3.3 Create `tuning/job_runner.py`
    - Subscribe to the `dda-workflow-tuning` named shadow delta following `camera_binding_store.py`; per new `desired.jobs[jobId]` read the manifest, resolve the registration's Node_Parameters, replay each `(sample, repeat)` with concurrency 1 on a dedicated worker (`GET` bytes → `render_prompt` on the manifest's metadata snippet → `build_llm_invocation` → the executor's transport → `parse_verdict` → `categorize_outcome`), append outcome batches ≤ 20 to `workflow-tuning/sessions/{session}/runs/{run}/outcomes-{n}.json`, update `reported.jobs[jobId]`, honour `cancel`, prune `reported` entries absent from `desired`
    - _Requirements: 6.4, 6.9, 6.11, 6.15, 9.6_

  - [ ]* 3.4 Write device property tests
    - `test/backend-test/workflow_engine/test_property_tuning_sample_export.py`:
      **Property 2: Every completed anomaly-mode invocation exports exactly what was sent** — **Validates: Requirements 2.2, 2.3**;
      **Property 3: Export is contained, bounded and inert when unconfigured** — **Validates: Requirements 2.4, 2.5, 2.6, 11.2, 11.3**;
      **Property 4: Backfill pairs by the executor's artifact rules, once, within bounds** — **Validates: Requirements 2.7**;
      device config half of **Property 17** — **Validates: Requirements 2.6, 11.3**
    - `test/backend-test/workflow_engine/test_property_tuning_job_runner.py`: device half of **Property 11: Device_Score_Jobs are delivered, executed, reported and bounded exactly** — **Validates: Requirements 6.9, 6.11, 6.15**
    - `test/backend-test/workflow_engine/test_property_anomaly_invocation_vendored.py`: device half of **Property 6** driving the vendored copy through the processors and the job runner

  - [ ]* 3.5 Write device unit and integration tests
    - Queue/drop/retry/skip-oversize; startup config parsing; backfill marker; job runner delta handling, cancel, batch sizes, reported updates, separate worker
    - End-to-end: compiled document with two anomaly-mode nodes through the processors with a recording invoker and fake S3 → three objects per invocation with byte-identical images; a manifest through the runner against a fake Text_Generation_API → batches and reports
    - _Requirements: 2.2, 2.4, 2.5, 2.7, 2.10, 6.9, 6.15_

- [ ] 4. Device verification on hardware
  - Build and deploy the LocalServer to a JP7 device with export configured for its Use_Case; run the workflow once and confirm three objects per anomaly-mode node in the Sample_Store with byte-identical images and unchanged run artifacts; confirm the backfill of existing runs; dispatch one Device_Score_Job by hand-editing the named shadow and confirm outcome batches and reports while a production run completes normally
  - _Requirements: 2.2, 2.7, 6.9, 6.15, 11.1, 11.2_

- [ ] 5. Implement Portal configuration, storage and infrastructure
  - [ ] 5.1 Deliver the export configuration and grants
    - Use_Case settings `tuning_sample_export` and `tuning_sample_retention_days` (7–365, default 30) in the use-case handler and settings UI; `deployments.py` merges `workflowTuning: {enabled, bucket, prefix: "workflow-tuning/samples/"}` into the LocalServer component configuration iff enabled, and adds `s3:PutObject`/`s3:GetObject` on `arn:aws:s3:::{bucket}/workflow-tuning/*` to the device role policy
    - _Requirements: 2.1, 2.8, 2.9, 11.3_

  - [ ] 5.2 Add storage and infrastructure
    - `dda-portal-workflow-tuning` table (`pk`/`sk`, TTL attribute) in `storage-stack.ts`; bucket lifecycle rules for `workflow-tuning/samples/` (Use_Case retention), `workflow-tuning/jobs/` and `workflow-tuning/sessions/` (30 days)
    - `workflow_tuning.py` Lambda in `compute-stack.ts`: 900 s, Bedrock grant (existing shape), standalone self-invoke `iam.Policy`, iot-data shadow access through the Use_Case role path, cross-account S3 read/write on `workflow-tuning/*`; API routes under `/workflow-tuning/anomaly/**` with the Cognito authorizer and CORS
    - _Requirements: 6.3, 6.9, 9.4, 10.1_

  - [ ]* 5.3 Write configuration property test and CDK assertions
    - `edge-cv-portal/backend/tests/test_property_tuning_deployment_config.py`: deployment half of **Property 17: Configuration and grants are delivered iff export is enabled, and parsed safely** — **Validates: Requirements 2.1, 2.8**
    - CDK assertions: table + TTL; Lambda grants (Bedrock, self-invoke policy, shadow, S3 prefix); device role statement iff export enabled; lifecycle rules; routes with the authorizer
    - _Requirements: 2.8, 2.9, 10.1_

- [ ] 6. Implement the `workflow_tuning.py` Lambda
  - [ ] 6.1 Session, sample index, labels, synthetic negatives, candidates, preview
    - Authorization first via `authorize_workflow_access` (`WORKFLOW_READ` GETs, `WORKFLOW_EDIT` mutations); overview route with per-node sample counts and `sampleExportEnabled`; create-or-get session with the `WF#/NODE#` uniqueness item and baseline snapshot/refresh from the latest version; index refresh (newest 2000 by `exportedAt`, sidecar fields verbatim, duplicate marking by hash, different-prompt flag, skip counts by reason, label-preserving); paged samples with 30-minute presigned URLs and filters; label multi-set; synthetic-negative toggle (OK × sibling references per execution and device); candidate CRUD with baseline 409; preview `{userMessage, systemText, warnings[]}` with the `max_tokens < 64` and missing-`is_anomalous` warnings; session delete removing run objects but not exported samples
    - _Requirements: 1.2, 1.6, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 4.2, 4.3, 4.5, 4.6, 4.7, 4.8, 5.1, 5.2, 5.3, 5.4, 5.5, 5.7, 9.1, 9.2, 9.4, 9.5, 10.2, 10.5_

  - [ ] 6.2 Bedrock_Scorer and Device_Score_Job dispatcher
    - `POST .../score-runs`: admission (409 in-progress via the `RUNLOCK` conditional write, 400 over 600 or repeats outside 1..3, 400 for a VLM run without an eligible device), plan the `(sample, repeat)` units, 202 with `plannedInvocations`
    - `execute_score_run` action: ≤ 100 invocations per step with 4 threads, cross-account image reads, `build_bedrock_invocation` + `converse(**converse_kwargs())` with `read_timeout=30` and no retries in the node's region, `parse_verdict`/`categorize_outcome`, batched outcome writes, cursor re-invoke, cancel flag, resume without re-issuing persisted units, 60-minute finalize-as-failed, `summarize_outcomes` on every finalize
    - VLM dispatch: manifest to `workflow-tuning/jobs/{jobId}/manifest.json`, `desired.jobs[jobId]` on the device's `dda-workflow-tuning` shadow through the Use_Case's iot-data client; poll step every 30 s ingesting `outcomes-*.json` exactly once, finalizing on `done == total`, reported failure, cancellation, or 15 minutes of silence; remove the job from `desired` on finalize
    - Run/outcome/diff/cancel/selection routes; prune to 20 runs per candidate
    - _Requirements: 6.1, 6.3, 6.5, 6.6, 6.7, 6.8, 6.9, 6.10, 6.11, 6.12, 6.13, 6.14, 7.1, 7.2, 7.3, 7.5, 9.3, 9.6, 10.3, 10.4_

  - [ ] 6.3 Apply
    - `POST .../sessions/{id}/apply` under `WORKFLOW_SAVE`: require a completed run on the selection; load the latest definition; refuse a non-tunable target (409); set exactly `prompt`/`prompt_template`, `system_prompt`, `max_tokens`; `canonicalize_definition`; reuse the workflows module's version allocation, `put_definition`, `put_version_item` and audit shape; record `latestTuningResult` and the `apply_prompt_tuning` audit event; never validate/package/deploy
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 11.4_

  - [ ]* 6.4 Write Portal backend property tests
    - `edge-cv-portal/backend/tests/test_property_tuning_session_index.py`:
      **Property 5: Indexing is faithful, additive, bounded and label-preserving** — **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**;
      **Property 12: Synthetic negatives are exactly the OK × sibling-reference product and toggle cleanly** — **Validates: Requirements 4.5, 4.6, 4.7**;
      **Property 13: Labels are total, persisted and govern scoring membership** — **Validates: Requirements 4.2, 4.3**
    - `edge-cv-portal/backend/tests/test_property_tuning_score_runs.py`:
      **Property 10: Score_Run admission, chunking, concurrency, cancellation and resume bounds hold** — **Validates: Requirements 6.8, 6.10, 6.11, 6.13, 10.4**;
      Portal half of **Property 11** — **Validates: Requirements 6.9, 6.11, 6.12**
    - `edge-cv-portal/backend/tests/test_property_tuning_apply_and_guards.py`:
      **Property 14: Applying changes exactly the three Prompt_Set parameters through the designer save path** — **Validates: Requirements 8.1, 8.2, 8.4, 8.6, 11.4**;
      **Property 15: Authorization precedes everything and mirrors the workflow handlers** — **Validates: Requirements 9.1, 9.2**;
      **Property 16: Requests and jobs carry only images and prompt content; images never enter DynamoDB** — **Validates: Requirements 9.3, 9.5, 9.6**
    - `edge-cv-portal/backend/tests/test_property_tuning_preview.py`:
      **Property 18: Candidate preview shows the exact request text and warns on parser-hostile settings** — **Validates: Requirements 5.3, 5.4, 5.5**

  - [ ]* 6.5 Write Portal backend unit and integration tests
    - Every status in the error table; overview counts; baseline refresh on a new version; refresh summary; chunk stepping and cursor; resume and 60-minute finalize; job dispatch manifest and shadow document; ingestion exactly once; 15-minute silence; apply diff, audit fields and refusal; prune; session delete leaves samples
    - moto end-to-end: seed exported samples for two devices and two versions → session → refresh → label → synthetic negatives → Bedrock run driven inline → compare → select → apply → assert definition diff, audit and Tuning_Result; VLM dispatch with stubbed iot-data through completed, silence, failed and cancelled paths
    - _Requirements: 3.1, 5.1, 6.8, 6.9, 6.12, 8.1, 8.3, 10.3, 10.5_

- [ ] 7. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Implement the Portal frontend
  - [ ] 8.1 Navigation, routes and API client
    - `Layout.tsx`: `expandable-link-group` "Workflow Tuning" → "VLM/LLM Anomaly Tuning" after "Workflows", gated by the workflow-edit roles; `App.tsx` routes `/workflow-tuning`, `/workflow-tuning/anomaly`, `/workflow-tuning/anomaly/sessions/:sessionId`; `api.ts` typed client for every `/workflow-tuning/anomaly/**` route
    - _Requirements: 1.1_

  - [ ] 8.2 `pages/workflow-tuning/WorkflowTuningLanding.tsx` and `AnomalyTuningOverview.tsx`
    - Landing lists the section's tools; overview: Use_Case → workflows with tunable nodes → per-node type, model, sample counts, "Open session"; export-disabled explanation linking to Use_Case settings; `workflowId` query preselection
    - _Requirements: 1.2, 1.6_

  - [ ] 8.3 `pages/workflow-tuning/AnomalyTuningSession.tsx` and its tabs
    - **Samples**: pair cards (presigned input/reference, single-image indication, recorded verdict/confidence, raw answer on demand, device, execution, version, slot, source, duplicate/synthetic/different-prompt badges), label buttons and multi-select, filters, counts with zero-OK/zero-NOK warning, synthetic toggle
    - **Candidates**: editor with live preview (final user message + system text) and warnings; starter template on click; baseline read-only
    - **Score runs**: start dialog with invocation count, repeats 1–3 and, for VLM, the eligible-device picker; progress and running summary; cancel
    - **Compare**: latest-run-per-candidate table, outcome drill-down (category filter, confidence sort), two-run diff, parse-failure raw answers, prominent false-pass count, selection
    - **Apply**: confirmation with both summaries and the false-pass count; success shows the new version
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.7, 5.2, 5.3, 5.4, 5.5, 5.6, 6.6, 6.7, 6.8, 6.11, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 8.2_

  - [ ] 8.4 Entry points in the designer
    - `WorkflowToolbar.tsx`: "Tune anomaly prompts" action iff the loaded definition has a Tunable_Node, navigating to `/workflow-tuning/anomaly?workflowId=`; `NodeConfigPanel.tsx`: "Prompt tuning" link iff the selected node is tunable, plus the latest applied Tuning_Result summary
    - Use_Case settings: "Tuning sample export" toggle and retention days beside the Inference_Uploader settings
    - _Requirements: 1.3, 1.4, 2.1, 2.9_

  - [ ]* 8.5 Write Portal frontend property tests
    - `edge-cv-portal/frontend/src/pages/workflow-tuning/eligibility.property.test.ts`: Portal half of **Property 1** consuming the fixture from task 1.4 — **Validates: Requirements 1.5**
    - `edge-cv-portal/frontend/src/pages/workflow-tuning/entryPoints.property.test.tsx`: **Property 19: Navigation and entry points appear exactly for the intended roles and workflows** — **Validates: Requirements 1.1, 1.3, 1.4**

  - [ ]* 8.6 Write Portal frontend unit tests
    - Nav gating; overview list and export-disabled message; pair card fields and labels; filters; preview text and warnings; start dialog count and device picker; progress/cancel; compare table, diff, parse-failure display, false-pass confirmation; toolbar and node-panel entry points
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.6, 4.1, 4.4, 5.3, 6.6, 7.2, 7.3, 7.4, 7.6_

- [ ] 9. Final verification
  - Portal backend: `cd edge-cv-portal/backend && python3 -m pytest tests/ -q`; Portal frontend: `npx tsc --noEmit` and `npx vitest run`; infrastructure: `npm test` and `npm run build`; device backend: `PYTHONPATH=src/backend python3 -m pytest test/backend-test/workflow_engine -q` from the repo root; vendoring drift guard green after `re_vendor.sh`
  - Security preservation guard suite from the repo root per the builds steering (`output_bindings.py` and any other tracked file rebaselined in the same commit); move `cdk.out` aside if the drift guards fail
  - Sequence the LocalServer build (task 4) and the portal deploy; never run both concurrently

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP; core implementation tasks are never optional
- Every correctness property from the design has exactly one property-based test (19 total), at ≥ 100 iterations, tagged `Feature: quality-prompt-tuning, Property {n}: {text}`; Properties 1, 6, 11 and 17 have two halves (shared/Portal and device) asserted from the same generators
- Property 7 (preservation) must be written against the pre-refactor executor, so task 1.1 lands before task 1.5
- Task 4 is the hardware verification the builds steering requires for on-device changes and is not optional
- The design's smoke tests (export on a real device, faithfulness sanity check, apply round trip) are deployment activities folded into tasks 4 and 9

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3", "1.4", "1.5", "5.1", "5.2"] },
    { "id": 3, "tasks": ["1.6", "3.1", "3.2", "3.3", "5.3", "6.1"] },
    { "id": 4, "tasks": ["3.4", "3.5", "6.2", "6.3", "8.1"] },
    { "id": 5, "tasks": ["4", "6.4", "6.5", "8.2", "8.3", "8.4"] },
    { "id": 6, "tasks": ["8.5", "8.6"] },
    { "id": 7, "tasks": ["9"] }
  ],
  "dependencies": {
    "1.2": ["1.1"], "1.3": ["1.2"], "1.4": ["1.2"], "1.5": ["1.2"], "1.6": ["1.5"],
    "3.1": ["1.5"], "3.2": ["3.1"], "3.3": ["1.5"], "3.4": ["3.1", "3.2", "3.3"], "3.5": ["3.1", "3.2", "3.3"],
    "4": ["3.1", "3.2", "3.3"],
    "5.3": ["5.1", "5.2"],
    "6.1": ["1.2", "5.2"], "6.2": ["6.1"], "6.3": ["6.1"], "6.4": ["6.2", "6.3"], "6.5": ["6.2", "6.3"],
    "8.1": ["6.1"], "8.2": ["8.1"], "8.3": ["8.1", "6.2", "6.3"], "8.4": ["8.1", "5.1"], "8.5": ["8.4", "1.4"], "8.6": ["8.3", "8.4"],
    "9": ["4", "6.5", "8.6"]
  }
}
```
