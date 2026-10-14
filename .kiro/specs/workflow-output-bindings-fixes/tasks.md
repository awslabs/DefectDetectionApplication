# Implementation Plan

## Overview

This plan fixes the three workflow-output-bindings defects using the exploratory bugfix workflow:
surface each defect on UNFIXED code first (Properties 1–3: Bug Condition), capture existing behavior
that must not change (Properties 4–6: Preservation), apply the fixes, then validate and confirm no
regressions. All exploration and preservation tests are written and run against the UNFIXED code
before any fix is applied. Defect A adds a publish-only `PublishToIoTCore` policy entry to all four
recipe variants and makes the engine's Greengrass publisher raise an actionable error on denial.
Defect B retries 409-loading within a bounded budget, seeds the `NodeStatusCollector` with
executor-binding node ids (marking llm outcomes truthfully), and persists llm output into the
per-run artifact directory. Defect C repairs empty-basename capture files to `{capture_id}.jpg` and
writes a run metadata JSON. A final on-hardware JP6 gate (task 5) verifies end-to-end on the real
device — it consumes a ~1h gdk build and touches the live device, so it runs only with the user's
explicit go-ahead.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Write tests against UNFIXED code: task 1 (Bug Conditions for Defects A/B/C) FAILS; task 2 (Preservation) PASSES. Independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["3"],
      "description": "Apply the three fixes (3.1 recipe policy + denial diagnostics, 3.2 llm retry/status/persistence, 3.3 capture artifact repair + metadata JSON), then re-run task 1 (3.4) and task 2 (3.5). Depends on wave 1."
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "Checkpoint - run the relevant test suites and ensure all tests pass. Depends on wave 2."
    },
    {
      "wave": 4,
      "tasks": ["5"],
      "description": "On-hardware JP6 verification gate (gdk build/publish + live-device workflow run). Requires user coordination — runs only with explicit go-ahead. Depends on wave 3."
    }
  ]
}
```

- Tasks 1 and 2 are independent and must be completed BEFORE any fix (tests written against unfixed code).
- Task 3 depends on wave 1; sub-tasks 3.4 and 3.5 depend on 3.1–3.3.
- Task 4 depends on task 3. Task 5 depends on task 4 and on the user's go-ahead (live JP6 device + ~1h gdk build).

## Tasks

- [x] 1. Write bug condition exploration tests (BEFORE implementing the fix)
  - **Property 1: Bug Condition** - Greengrass workflow-topic publish authorized and diagnosable (Defect A); **Property 2: Bug Condition** - llm_inference transient retry, truthful status, persisted output (Defect B); **Property 3: Bug Condition** - Tritonless capture artifacts named and described (Defect C)
  - **CRITICAL**: These tests MUST FAIL on unfixed code — the failures confirm each defect exists
  - **DO NOT attempt to fix the tests or the code when they fail**
  - **NOTE**: These tests encode the expected behavior — they will validate the fix when they pass after implementation
  - **GOAL**: Surface counterexamples that demonstrate all three defects exist (confirming the evidence-backed causal chains from execution 85bf7a61)
  - **Scoped PBT Approach**: These are deterministic configuration/behavior defects — scope each property to the concrete failing artifact/case for reproducibility; recipe defects use config tests (parse the YAML, assert the policy properties) as the testable seam
  - Exploration case 1 — recipe policy exposure (`isBugCondition_A`, design Bug Details): parse all four recipe variants (`recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`, `recipe-amd64.yaml`); assert each `aws.greengrass.ipc.mqttproxy` accessControl authorizes `aws.greengrass#PublishToIoTCore` on resources covering non-shadow workflow topics (e.g. the catalog example `factory/line1/inspection`) — FAILS on unfixed recipes (only `$aws/things/*/shadow/name/*`)
  - Exploration case 2 — bare denial diagnostics (`isBugCondition_A`): drive `OutputBindingProcessor._run_mqtt_publish` with `greengrass=true` and an injected greengrass publisher raising `awsiot`-shaped `UnauthorizedError` (and `_default_greengrass_publisher` with a fake `awsiot.greengrasscoreipc` module whose operation result raises it); assert the surfaced error names the denied topic and the LocalServer `aws.greengrass.ipc.mqttproxy` accessControl as the cause — FAILS on unfixed code (bare `UnauthorizedError` text only)
  - Exploration case 3 — 409-loading treated terminal (`isBugCondition_B`): call `_default_llm_invoker` with mocked `requests` returning `409 {'model_name': 'opt125m-smoke', 'state': 'loading'}` twice then `200 {'generated_text': 'ok'}`, `time.sleep` stubbed so the poll interval never really elapses; assert `'ok'` is returned — FAILS on unfixed code (RuntimeError on the first 409, the exact live-device message)
  - Exploration case 4 — executor-binding node invisible in status (`isBugCondition_B`): run `WorkflowExecutor.execute` (session/pipeline-manager/invoker boundaries mocked, as in `test_workflow_pipeline_executor.py`) over a compiled document with an `llm_inference` executor binding, first with the invoker failing, then succeeding; assert `node_status_json` holds a terminal entry for the llm node — `failure` with the error detail when it failed, `success` when it succeeded — FAILS on unfixed code (node absent from the map; run view resolves absent to "pending" per `graphGeometry.ts` nodeVisual)
  - Exploration case 5 — llm output not persisted (`isBugCondition_B`): same harness, successful invoker, temp `output_dir`; assert `{output_dir}/{capture_id}.json` exists and carries the `llm` section with the node's `generated_text` — FAILS on unfixed code (no such file; text lives only in in-memory tag values)
  - Exploration case 6 — empty-basename capture and no metadata JSON (`isBugCondition_C`): simulate the broker's tritonless product by placing a bare `.jpg` file in a temp `output_dir` and running the executor's post-run path (or the repair helper directly once it exists — on unfixed code there is nothing to call, which is itself the failure); assert the directory ends with `{capture_id}.jpg`, no empty-basename file, and a `{capture_id}.json` metadata file — FAILS on unfixed code (`.jpg` stays; the run dir evidence: only `.jpg` + `run.log`, log line `meta=<none>`)
  - Run all tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests FAIL (shadow-only mqttproxy resources in all four recipes; bare UnauthorizedError; RuntimeError on first 409-loading; llm node absent from node_status_json; no `{capture_id}.json`; `.jpg` unrepaired)
  - Document counterexamples found (e.g. "recipe-arm64-jp6.yaml mqttproxy resources = ['$aws/things/*/shadow/name/*'] — workflow topic matches no policy"; "invoker raised on 409 {'state': 'loading'} without retry"; "node_status_json = {} for llm-only document, run COMPLETED")
  - Mark task complete when tests are written, run, and failures are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 4: Preservation** - MQTT publish paths and recipe structure unchanged; **Property 5: Preservation** - llm invocation contract and binding independence unchanged; **Property 6: Preservation** - Triton capture routing unchanged
  - **IMPORTANT**: Follow observation-first methodology — observe behavior on UNFIXED code, record it (golden behavior), then encode it as tests that must keep passing after the fix
  - Observe on UNFIXED code: `_run_mqtt_publish` publisher call tuples for plain-broker and `aws_iot` parameter sets (a recording publisher, the `test_mqtt_publish_call_preservation.py` style — extend, don't duplicate, that suite's coverage)
  - Observe on UNFIXED code: the full parsed structure of each recipe variant (accessControl policies, lifecycle, configuration, artifacts)
  - Observe on UNFIXED code: `_default_llm_invoker`'s single POST (URL, body with prompt + max_tokens/temperature/top_p, timeout=130) on a 200 first attempt; `LlmInferenceProcessor` merge shape (`metadata['llm'][nodeId]`), unresolved-placeholder skip, and per-binding independence
  - Observe on UNFIXED code: `NodeStatusCollector` maps for element-only documents across bus-signal/failure sequences; `_route_capture_outputs`/`_inject_inference_metadata` output for documents WITH `emltriton` (targets, meta string, correlation-id injection)
  - Write property-based tests (Hypothesis, already used in this repo) capturing these patterns from the design Preservation Requirements:
    - Non-greengrass publish equivalence: for generated broker/aws_iot parameter sets and metadata, the fixed `_run_mqtt_publish` dispatches identical publisher call tuples (Property 4; Requirements 3.1, 3.2)
    - Recipe deep-equality modulo the added entry: each fixed variant parses deep-equal to the original after deleting only the new `mqttproxy:2` policy entry (trivially passes pre-fix against the recorded goldens) (Property 4; Requirement 3.7)
    - llm 200-path equivalence: for generated prompts/parameters answered 200 first try, exactly one POST with the original URL/body/timeout and the same merged result; placeholder failures never call the API; a failing binding leaves sibling bindings' outcomes unchanged (Property 5; Requirements 3.3, 3.4)
    - Collector equivalence: for generated element-name maps and signal/failure sequences, fixed and original collectors produce identical `to_map()` results (Property 5; Requirements 3.5, 3.8)
    - Triton routing equivalence: for generated documents with `emltriton`, routing/injection output unchanged; the artifact repair is the identity on any directory containing only correctly-named `{capture_id}.*` files (Property 6; Requirement 3.6)
  - **Testing Approach**: Property-based testing is recommended — the preservation guarantees are universal ("for all non-bug inputs"); Hypothesis generates many cases automatically and catches edge cases manual tests miss
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_

- [x] 3. Fix the three workflow-output-bindings defects

  - [x] 3.1 Defect A — authorize Greengrass workflow-topic publishing and make denials actionable
    - In all four recipe variants (`recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`, `recipe-amd64.yaml`), add a second `aws.greengrass.ipc.mqttproxy` policy entry keyed `'{ComponentName}:mqttproxy:2'` with operations `['aws.greengrass#PublishToIoTCore']` only and resources `['*']`, policyDescription stating the rationale (workflow mqtt_publish topics are user-configured free strings per the node catalog — a topic-prefix scope would break the documented node contract; publish-only so no new subscribe capability is granted); leave the existing `mqttproxy:1` shadow entry untouched (`recipe.yaml` is the gdk build-time working copy overwritten by `gdk-component-build-and-publish.sh` — no separate edit)
    - In `src/backend/workflow_engine/output_bindings.py` `_default_greengrass_publisher`, catch the IPC `UnauthorizedError` (import lazily alongside the existing awsiot imports) and re-raise as `RuntimeError` naming the denied topic and the LocalServer component's `aws.greengrass.ipc.mqttproxy` accessControl as the cause with the recipe location; success path unchanged (same request construction, `get_response().result(timeout=10.0)`)
    - _Bug_Condition: isBugCondition_A(input) — greengrass=true AND topic matches no policy resource (recipes' only mqttproxy resource is `$aws/things/*/shadow/name/*`) (from design)_
    - _Expected_Behavior: Property 1 — every variant authorizes PublishToIoTCore for workflow topics; a denied publish surfaces the topic and the accessControl cause_
    - _Preservation: Property 4 — non-greengrass paho paths dispatch identically; recipes identical beyond the added entry; shadow pub/sub untouched_
    - _Requirements: 2.1, 2.2, 3.1, 3.2, 3.7_

  - [x] 3.2 Defect B — llm 409-loading retry, truthful executor-binding status, persisted output
    - In `src/backend/workflow_engine/output_bindings.py` `_default_llm_invoker`: when a response is 409 and its JSON body's `state` is `'loading'`, re-POST every `LLM_LOADING_POLL_INTERVAL_SEC = 5` seconds until `LLM_LOADING_BUDGET_SEC = 240` of wall clock elapses; return the first 200's `generated_text`; a 409 with any other state, any other non-200, or budget exhaustion raises the existing RuntimeError shape with the last state payload included; the 200-first-attempt path stays a single POST with the original URL/body/`LLM_GENERATION_TIMEOUT_SEC`
    - In `src/backend/workflow_engine/node_status.py` + `pipeline_executor.py`: seed the run's `NodeStatusCollector` with the compiled document's `executorBindings` node ids (constructor arg or explicit tracking loop in `_begin_node_status`) so executor-binding nodes participate in `mark_running_all`/`mark_success_all`/`finalize` and always reach a terminal status in `node_status_json`
    - In `pipeline_executor.py` after `self._llm_processor.process(...)`: for each llm binding node id whose `metadata['llm'][node_id]` carries `'error'`, call `collector.mark_failure(node_id, error)` — the run-level COMPLETED decision is unchanged (binding independence preserved); `mark_success_all` on the success path covers successful llm nodes and other executor-binding nodes
    - In `pipeline_executor.py`, persist the run metadata JSON (shared with 3.3): after post-run processing on both the completed path and the output-binding-failure path, when the execution has an `output_dir`, write `{output_dir}/{capture_id}.json` with the JSON-serializable view of the final tag values/metadata (notably `llm` with each node's `generated_text` or `error`); contained/best-effort in the existing R8.5 style (a write failure never changes run status)
    - _Bug_Condition: isBugCondition_B(input) — 409 state='loading' treated terminal; executor-binding node absent from node_status_json (frontend resolves absent to "pending"); success output persisted nowhere (from design)_
    - _Expected_Behavior: Property 2 — bounded retry proceeds on READY; exhausted budget / failed / unknown recorded as node failure with state detail; every executor-binding node terminal in node_status_json; generated text persisted in the run directory_
    - _Preservation: Property 5 — 200-first-attempt single POST identical; placeholder skip and binding independence intact; element-node collector behavior identical_
    - _Requirements: 2.3, 2.4, 2.5, 2.6, 2.7, 3.3, 3.4, 3.5, 3.8_

  - [x] 3.3 Defect C — capture artifact repair and metadata JSON for tritonless runs
    - In `src/backend/workflow_engine/pipeline_executor.py`, add a contained post-pipeline repair step: scan the run's `output_dir` for files whose basename is exactly `.{ext}` (empty stem — the broker's `"" + ".ext"` product when no element attached a buffer correlation id; see `emlcapture.cpp` SendData `id = ""` and `message_broker_client.py` `filename: "${c_id}.${ext}"`), and rename each to `{capture_id}.{ext}`; never touch correctly-named files (Triton runs are a no-op); best-effort/contained
    - Verify the repaired base image aligns with `run_artifacts.base_output_image_path` (`{output_dir}/{capture_id}.jpg`) so the run view image display resolves it
    - The metadata JSON written in 3.2 satisfies the missing-JSON half for tritonless runs; leave `_route_capture_outputs`/`_inject_inference_metadata` and the gstreamer-side `meta` routing for Triton documents byte-identical
    - _Bug_Condition: isBugCondition_C(input) — terminal emlcapture with no correlation-id-attaching element (folder_source compiles to plain filesrc; llm_inference is an executor binding) → broker writes ".jpg" and meta="" (from design)_
    - _Expected_Behavior: Property 3 — run directory ends with `{capture_id}.jpg` (no empty-basename files) and a metadata JSON carrying the run's inference metadata including the llm section_
    - _Preservation: Property 6 — Triton routing/injection unchanged; repair is the identity on correctly-named artifacts_
    - _Requirements: 2.8, 2.9, 3.6_

  - [x] 3.4 Verify the bug condition exploration tests now pass
    - **Property 1: Expected Behavior** - Greengrass workflow-topic publish authorized and diagnosable; **Property 2: Expected Behavior** - llm_inference transient retry, truthful status, persisted output; **Property 3: Expected Behavior** - Tritonless capture artifacts named and described
    - **IMPORTANT**: Re-run the SAME tests from task 1 — do NOT write new tests
    - The tests from task 1 encode the expected behavior; when they pass they confirm each defect is fixed
    - Run all six exploration cases from task 1
    - **EXPECTED OUTCOME**: Tests PASS (all four recipes authorize PublishToIoTCore for workflow topics; denials name the topic and accessControl cause; 409-loading retries to success; llm nodes terminal in node_status_json with truthful failure details; `{capture_id}.json` written; `.jpg` repaired to `{capture_id}.jpg`)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9_

  - [x] 3.5 Verify preservation tests still pass
    - **Property 4: Preservation** - MQTT publish paths and recipe structure unchanged; **Property 5: Preservation** - llm invocation contract and binding independence unchanged; **Property 6: Preservation** - Triton capture routing unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests
    - Run the preservation property tests from task 2
    - **EXPECTED OUTCOME**: Tests PASS (no regressions: identical paho dispatch; recipes deep-equal modulo the added entry; llm 200-path single POST; collector equivalence for element-only documents; Triton routing identical; repair identity on correct names)
    - Confirm all tests still pass after the fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the relevant test suites: `test/backend-test/workflow_engine` (including the existing `test_workflow_output_bindings.py`, `test_mqtt_publish_call_preservation.py`, `test_workflow_llm_inference.py`, `test_workflow_pipeline_executor.py`, `test_workflow_capture_routing.py` alongside the new exploration/preservation tests) plus the security preservation suite `test/backend-test/security/preservation` — no rebaseline is expected (recipes are not preservation-tracked; `src/backend/workflow_engine/*.py` is not in the secrets-audit `IN_SCOPE_FILES`; `src/docker-compose.yaml` and `app.py` are untouched), so the suite passing confirms that; ensure all tests pass, ask the user if questions arise

- [x] 5. On-hardware JP6 verification gate (REQUIRES USER COORDINATION — do not start without explicit go-ahead)
  - **NOTE**: This task consumes a ~1h gdk component build and touches the live JP6 device; run it only when the user says go. Per builds.md: never run two component builds at once (check `pgrep -af "gdk component build"` / `pgrep -af "build-custom.sh"` first), build sequentially with the target name swapped in `gdk-config.json`, and capture output to `.gdk_build_jp6.log`
  - Build and publish the modified `aws.edgeml.dda.LocalServer.arm64JP6` component (gdk) — the recipe accessControl and all `src/backend` engine changes ship in it — and deploy to the JP6 device
  - **MQTT publish test** (Property 1): run workflow `dda.workflow.1f0b4c0c-f5f0-430d-befe-a00aacc22c47` (or an equivalent llm+mqtt workflow) and verify the greengrass publish reaches AWS IoT Core (subscribe to the configured topic in the IoT console / `aws iot-data`); confirm no `UnauthorizedError` in the run log and that shadow-based flows (stream/app-runner shadows) still work
  - **llm warm-up test** (Property 2): trigger the workflow immediately after a model (re)load so the Text_Generation_API answers 409-loading; verify the node waits and succeeds within the budget, the run-view graph shows llm_inference_1 terminal (success — or failure with the state detail if the budget is genuinely exhausted), and mqtt_publish_1 no longer sits "pending"
  - **Artifact test** (Properties 2, 3): inspect `/aws_dda/captures/{workflow_id}/{execution_id}/` and verify the frame is `{workflow_id}-{execution_id}.jpg` (no bare `.jpg`), `{capture_id}.json` carries the llm generated text, and the run view displays the image
  - Confirm the backend stays healthy (no crash/restart loop) for a sustained period after several runs, per builds.md
  - Per builds.md, the change is not "done" until verified on device from a real built+deployed component; state in the commit/PR what was verified on which device
  - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.6, 2.7, 2.8, 2.9, 3.1_

## Notes

- **Test-first ordering is mandatory**: task 1 (bug conditions) must FAIL and task 2 (preservation) must PASS on the UNFIXED code before implementing task 3. Do not modify the recipe variants, `output_bindings.py`, `node_status.py`, or `pipeline_executor.py` until the tests are written and their expected outcomes documented.
- **Property references**: Properties 1–3 (Bug Condition/fix) validate Requirements 2.1–2.2 (A), 2.3–2.7 (B), 2.8–2.9 (C); Properties 4–6 (Preservation) validate 3.1+3.2+3.7, 3.3+3.4+3.5+3.8, and 3.6 respectively, per the design's Correctness Properties.
- **Confirmed root causes (file/line evidence)**: recipes' mqttproxy resources shadow-only (`recipe-arm64-jp6.yaml` ~37–44 and siblings) vs. free-string `topic` in the node catalog (Defect A); `_default_llm_invoker` non-200 → RuntimeError with no 409-state handling (`output_bindings.py` ~759–788), `NodeStatusCollector` built from `element_name_map` only (`pipeline_executor.py` ~1113) while `llm_inference` compiles to an executor binding with no element (catalog `nodes.py` ~751), frontend absent→"pending" (`graphGeometry.ts` 141–151) (Defect B); broker `filename: "${c_id}.${ext}"` (`message_broker_client.py` 56–64) + `emlcapture.cpp` `id = ""` default + correlation-id attached only by `emltriton` via `_inject_inference_metadata` (Defect C).
- **Scope decision (Defect A)**: publish-only wildcard (`PublishToIoTCore` on `*`) in a separate policy entry — the node contract allows arbitrary topics (documented example `factory/line1/inspection`), so a namespace prefix would break valid workflows; no new subscribe capability is granted and the shadow entry is untouched. Documented in the policyDescription.
- **Fix is recipe + engine**: the accessControl change alone fixes the authorization; the engine changes add denial diagnosability (A), transient handling/status truthfulness/output persistence (B), and artifact repair (C). No frontend change — with executor-binding nodes seeded into the collector, the existing absent→"pending" defaulting remains correct only for in-flight runs.
- **Security preservation gate (builds.md)**: no touched file is preservation-tracked (recipes are not in any baseline; `src/backend/workflow_engine/*.py` is not in the secrets-audit `IN_SCOPE_FILES`; `src/docker-compose.yaml`/`app.py`/Dockerfiles untouched). Task 4 re-runs the preservation suite to confirm no rebaseline is needed.
- **Primary fix locations**: `recipe-arm64-jp6.yaml`/`recipe-arm64-jp5.yaml`/`recipe-arm64.yaml`/`recipe-amd64.yaml` + `src/backend/workflow_engine/output_bindings.py` (Defect A); `src/backend/workflow_engine/output_bindings.py` + `node_status.py` + `pipeline_executor.py` (Defect B); `src/backend/workflow_engine/pipeline_executor.py` (Defect C).
- **On-hardware gate is user-gated**: task 5 consumes a ~1h gdk build and exercises the live JP6 device. It runs only with the user's explicit go-ahead and coordination.
