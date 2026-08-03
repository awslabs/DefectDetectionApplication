# Workflow Output Bindings Fixes — Bugfix Design

## Overview

Three related defects observed on a live JP6 device (LocalServer.arm64JP6 v1.0.45, workflow `dda.workflow.1f0b4c0c-f5f0-430d-befe-a00aacc22c47` v2, execution `85bf7a61-a126-484d-9074-08fbb73f209e`) break the output side of llm_inference workflows:

- **Defect A**: `mqtt_publish` via Greengrass IPC fails with `UnauthorizedError` because the LocalServer recipes' `aws.greengrass.ipc.mqttproxy` accessControl covers only shadow topics, never the user-configured workflow topics the node contract allows.
- **Defect B**: a `409 {'state': 'loading'}` from the Text_Generation_API (transient model warm-up) terminally fails the llm_inference node; the failure is invisible in the run view (node stuck "pending" because executor-binding nodes are never tracked in `node_status_json`), and even a successful node's generated text is persisted nowhere.
- **Defect C**: a tritonless capture pipeline writes its frame as literally `.jpg` (the message broker names files `{c_id}.{ext}` and no element attaches a buffer correlation id without `emltriton`), and no metadata JSON is written (`meta=""`).

The fix strategy: (A) add a publish-only wildcard `PublishToIoTCore` policy entry to all four recipe variants and make the engine's Greengrass publisher raise an actionable error on `UnauthorizedError`; (B) retry 409-loading within a bounded budget in `_default_llm_invoker`, seed the `NodeStatusCollector` with executor-binding node ids and mark llm nodes success/failure from the processor's outcomes, and persist llm outputs into the per-run artifact directory; (C) repair the empty-basename capture artifact to `{capture_id}.jpg` post-run and write a run metadata JSON (which is also where the llm output lands, unifying B and C). All changes ship in the LocalServer component (recipes + `src/backend`); on-hardware JP6 verification requires a gdk build and is user-gated.

## Glossary

- **Bug_Condition (C)**: per defect, the input/state condition that triggers the failure — a greengrass-enabled mqtt_publish to a non-shadow topic (A); a 409-loading API response or any executor-binding node's terminal invisibility (B); a terminal `emlcapture` run with no correlation-id-attaching element (C)
- **Property (P)**: the desired behavior for buggy inputs — authorized publish with actionable denial errors (A); bounded retry, truthful node status, persisted output (B); meaningful filename and metadata JSON (C)
- **Preservation**: shadow pub/sub, paho broker/aws_iot publishing, the 200-path llm invocation contract, pipeline-element status collection, and Triton capture routing must remain byte-for-byte unchanged
- **`_default_greengrass_publisher`**: `src/backend/workflow_engine/output_bindings.py` (~line 379) — publishes one message via Greengrass IPC `PublishToIoTCore`; called by `OutputBindingProcessor._run_mqtt_publish` (~line 1095) when `parameters['greengrass']` is set
- **`_default_llm_invoker`**: `src/backend/workflow_engine/output_bindings.py` (~line 759) — POSTs the rendered prompt to `http://localhost:5000/text-generation/{model_name}/generate` and raises `RuntimeError` on any non-200
- **`LlmInferenceProcessor`**: `src/backend/workflow_engine/output_bindings.py` (~line 790) — runs llm_inference bindings post-pipeline; records failures as `{'error': ...}` under `metadata['llm'][nodeId]` without raising
- **`NodeStatusCollector`**: `src/backend/workflow_engine/node_status.py` — per-node run status map persisted to `WorkflowExecution.node_status_json`; built from `rendering.element_name_map(document)` (pipeline elements only)
- **`_route_capture_outputs` / `_inject_inference_metadata`**: `src/backend/workflow_engine/pipeline_executor.py` (~lines 753 / 689) — point terminal `emlcapture` targets at the per-run `output_dir` and inject `metadata`/`correlation-id` args into `emltriton` elements only
- **Message broker `file-target_` pipe**: `src/backend/dda_triton/message_broker_client.py` — resolves `file-target_${workflow-path}-${ext}` to directory `${workflow-path}/`, filename `${c_id}.${ext}`, where `c_id` is the GStreamer buffer correlation id
- **`capture_id`**: `{workflow_id}-{execution_id}`, the per-run id `pipeline_executor.execute` computes; `run_artifacts.base_output_image_path` resolves artifacts as `{output_dir}/{capture_id}.{ext}`
- **Run directory (`output_dir`)**: `/aws_dda/captures/{workflow_id}/{execution_id}/` — holds the captured image, `run.log`, and (after this fix) the run metadata JSON

## Bug Details

### Bug Condition — Defect A (unauthorized Greengrass publish)

The workflow engine publishes to the user-configured topic via IPC `PublishToIoTCore` (`operation.get_response().result(timeout=10.0)`, output_bindings.py ~line 402). All four recipe variants (`recipe-arm64-jp6.yaml` ~lines 37–44, and the same block in `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`, `recipe-amd64.yaml`) grant `aws.greengrass#PublishToIoTCore` / `SubscribeToIoTCore` only on `$aws/things/*/shadow/name/*`. The node catalog (`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`, `MQTT_PUBLISH`) constrains `topic` only to `min_length: 1` with the documented example `factory/line1/inspection` — arbitrary topics are part of the node contract. Any workflow topic therefore matches no policy and the nucleus raises `UnauthorizedError` through `eventstreamrpc._on_continuation_message`.

**Formal Specification:**
```
FUNCTION isBugCondition_A(input)
  INPUT: input of type MqttPublishBinding
  OUTPUT: boolean

  RETURN input.parameters.greengrass = true
         AND NOT matchesTopicFilter(input.parameters.topic,
                                    "$aws/things/*/shadow/name/*")
         -- i.e. any user-configured workflow topic; the recipes' only
         -- mqttproxy policy resource is the shadow filter
END FUNCTION
```

### Bug Condition — Defect B (409-loading terminal; invisible status/output)

`_default_llm_invoker` raises `RuntimeError("Text_Generation_API returned {status} ...")` for every non-200 — including `409 {'state': 'loading'}`, which `src/backend/vllm_runtime/server.py` and `src/backend/endpoints/text_generation.py` emit precisely so callers can distinguish a warming model from a failed one. `LlmInferenceProcessor._run_one` catches the exception and records `{'error': str(e)}`; `WorkflowExecutor.execute` then proceeds to output bindings and marks the run COMPLETED. Separately, the `NodeStatusCollector` is constructed from `rendering.element_name_map(document)` (pipeline_executor.py ~line 1113), which contains only pipeline elements; `llm_inference` compiles to `executor_binding="llm_inference"` with no element (catalog nodes.py ~line 751), so llm_inference_1 is never in `node_status_json` and the frontend (`graphGeometry.ts` `nodeVisual`, lines 141–151: "Nodes absent from the map ... resolve to `pending`") shows it "pending" forever. On success, the generated text exists only in in-memory `tag_values` (and the `completed; tags:` log line) — nothing writes it to `output_dir`.

**Formal Specification:**
```
FUNCTION isBugCondition_B(input)
  INPUT: input of type LlmInferenceRun
  OUTPUT: boolean

  RETURN (input.apiResponse.status = 409
          AND input.apiResponse.body.state = "loading")   -- transient, treated terminal
      OR (input.node.mapping = EXECUTOR_BINDING
          AND terminalNodeStatus(input.node) = ABSENT)    -- run view shows "pending"
      OR (input.outcome = SUCCESS
          AND persistedArtifacts(input.run) ∌ generatedText)
END FUNCTION
```

### Bug Condition — Defect C (empty-basename capture; no metadata JSON)

The broker pipe writes `filename: "${c_id}.${ext}"` (message_broker_client.py lines 56–64). `emlcapture.cpp` (`SendData`, ~line 262) initializes `id = ""` and only fills it from the buffer's correlation id, which is attached exclusively by `emltriton` (its `correlation-id` property; `_inject_inference_metadata` sets it, but only on `emltriton` elements that declare a METADATA input). `folder_source` compiles to plain `filesrc` (catalog nodes.py ~line 144) which cannot attach a correlation id. So a tritonless capture pipeline publishes with `c_id = ""` → file `.jpg`. In parallel, `_route_capture_outputs` builds `meta` only from `_model_declared_outputs` of `emltriton` elements; a tritonless document gets `meta=""` (run log evidence: `buffer-message-id=file-target_/aws_dda/captures/.../<exec-id>-jpg`, `meta=<none>`), so no `.json`/`.jsonl` sidecar is ever produced.

**Formal Specification:**
```
FUNCTION isBugCondition_C(input)
  INPUT: input of type CaptureRun
  OUTPUT: boolean

  RETURN terminalEmlcapture(input.document)
         AND NOT EXISTS element IN input.document.elements
                 WHERE attachesCorrelationId(element)   -- only emltriton does
         -- consequence: brokerFilename = "" + ".jpg" AND meta = ""
END FUNCTION
```

### Examples

- Execution `85bf7a61` published to its configured workflow topic with `greengrass=true`: expected delivery to IoT Core, actual `UnauthorizedError` at output_bindings.py line 1129 → 402 (Defect A)
- Same execution: `Text_Generation_API returned 409 for model 'opt125m-smoke': {'model_name': 'opt125m-smoke', 'state': 'loading'}` — expected wait-then-generate, actual immediate node failure; run view showed llm_inference_1 "pending" (Defect B)
- Run directory `/aws_dda/captures/1f0b4c0c-.../14f0b38b-.../` contains `.jpg` (679428 bytes) + `run.log` only — expected `{capture_id}.jpg` plus a metadata JSON (Defect C)
- Edge case: a Triton workflow (with `emltriton`) is NOT the bug condition — correlation id injected, `{capture_id}.jpg` and declared meta targets written correctly today

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Shadow pub/sub through ShadowManager and the existing `mqttproxy:1` shadow policy (StreamShadow/AppRunnerShadow flows)
- `mqtt_publish` plain-broker and `aws_iot` mutual-TLS paths: identical paho call arguments, port/QoS defaulting, and error text (`test_mqtt_publish_call_preservation.py` already pins much of this)
- The llm 200-path: one POST to the same URL with the same body and `LLM_GENERATION_TIMEOUT_SEC`, result merged as `{'generated_text': ...}` under `metadata['llm'][nodeId]`; unresolved-placeholder failures still skip the API call; binding independence (failures recorded, remaining bindings processed)
- `NodeStatusCollector` transitions for pipeline-element nodes (pending→running via bus signals, warning retention, mark_failure/mark_success_all/finalize semantics)
- Triton capture routing: `_inject_inference_metadata` and `_route_capture_outputs` behavior for documents WITH `emltriton` (correlation-id injection, declared-output meta targets, `{capture_id}.jpg`)
- Recipe variants: everything except the added mqttproxy publish policy entry (shadow policies, CLI policies, lifecycle scripts, configuration, artifacts)
- Frontend: no change; absent-node→"pending" defaulting remains (correct for in-flight runs)

**Scope:**
All inputs that do NOT hit a bug condition are unaffected: non-greengrass mqtt publishes, shadow-topic IPC traffic, llm calls answered 200 on the first attempt, 409 with state `failed`/`unknown` (still terminal, message enriched with state only where the design says), Triton capture workflows, and all pipeline-element node statuses.

## Hypothesized Root Cause

All three root causes are CONFIRMED by code inspection (not hypotheses requiring re-hypothesizing; the exploration tests confirm them executably):

1. **Defect A — recipe accessControl gap**: the `greengrass` publish option (added by workflow-manager-integration-bugfixes Bug 2) was shipped without a matching recipe policy. The mqttproxy policy predates it and covers only shadow topics. The engine code path is correct; the denial is configuration. A second policy entry authorizing `PublishToIoTCore` is required in all four variants. Because the node contract allows arbitrary topics (documented example `factory/line1/inspection`), a topic-prefix scope would break valid existing workflows; the least-bad scope is a **publish-only wildcard** (`*` for `PublishToIoTCore` only — no new subscribe grants). The engine additionally needs clearer error surfacing so a future policy/config gap is diagnosable from the run error.

2. **Defect B — three compounding gaps**:
   - `_default_llm_invoker` has no transient-state handling; 409 is a documented "conflicts with serving state" signal that carries `state` exactly so clients can wait on `loading`.
   - `NodeStatusCollector` tracks only element-name-mapped nodes; executor-binding nodes (llm_inference, mqtt_publish, opcua_write, digital_output, bedrock_inference) are only added to the map if they FAIL via `mark_failure` (which inserts untracked ids); successful/silent ones stay absent → "pending" in the run view.
   - No persistence path exists for llm outputs; `tag_values` die with the executor thread.

3. **Defect C — correlation id only exists on Triton buffers**: the broker naming convention (`{c_id}.{ext}`) was designed around `emltriton`'s correlation-id property. Tritonless capture pipelines (new with llm workflows) violate the convention's assumption. Fixing inside the C++ plugin (emlcapture/filesrc) would require an edgemlsdk rebuild; the engine-side repair (rename post-run + engine-written metadata JSON) is minimal and stays in Python.

## Correctness Properties

Property 1: Bug Condition - Greengrass workflow-topic publish authorized and diagnosable

_For any_ `mqtt_publish` binding with `greengrass` enabled and any user-configured topic (isBugCondition_A), the fixed system SHALL authorize the publish — every recipe variant carries a `PublishToIoTCore` policy entry whose resources cover workflow topics — and, when a Greengrass IPC publish is denied, the fixed `_default_greengrass_publisher` SHALL raise an error naming the denied topic and the LocalServer `aws.greengrass.ipc.mqttproxy` accessControl configuration as the cause.

**Validates: Requirements 2.1, 2.2**

Property 2: Bug Condition - llm_inference transient retry, truthful status, persisted output

_For any_ llm_inference run where the Text_Generation_API answers 409 with state `loading` (isBugCondition_B), the fixed invoker SHALL retry within the bounded budget and return the generated text when a retry succeeds; when the budget is exhausted or the state is `failed`/`unknown`, the node SHALL be recorded failed with the state in the detail. _For any_ completed run, every executor-binding node SHALL have a terminal status in `node_status_json` (`failure` with detail for failed llm nodes, `success` otherwise), and _for any_ successful llm_inference node with a per-run artifact directory, the generated text SHALL be persisted into that directory.

**Validates: Requirements 2.3, 2.4, 2.5, 2.6, 2.7**

Property 3: Bug Condition - Tritonless capture artifacts named and described

_For any_ tritonless capture run (isBugCondition_C), the fixed system SHALL leave the run directory containing the frame as `{capture_id}.jpg` (no empty-basename files), and _for any_ run with a per-run artifact directory, a metadata JSON carrying the run's inference metadata (including the `llm` section when present) SHALL be written into that directory.

**Validates: Requirements 2.8, 2.9**

Property 4: Preservation - MQTT publish paths and recipe structure unchanged

_For any_ `mqtt_publish` binding that does NOT use greengrass (plain broker or aws_iot), the fixed code SHALL dispatch to the paho publisher with exactly the same call arguments as the original; and each fixed recipe variant SHALL be identical to the original except for the added mqttproxy publish policy entry (shadow/CLI policies, lifecycle, configuration, artifacts byte-equal).

**Validates: Requirements 3.1, 3.2, 3.7**

Property 5: Preservation - llm invocation contract and binding independence unchanged

_For any_ llm_inference run answered 200 on the first attempt, the fixed invoker SHALL make exactly one POST with the original URL, body, and timeout and merge the same result; unresolved placeholders SHALL still fail without an API call; a binding failure SHALL still leave other bindings and the run's terminal status decision unchanged; and pipeline-element node statuses SHALL be collected exactly as the original for all bus-signal sequences.

**Validates: Requirements 3.3, 3.4, 3.5, 3.8**

Property 6: Preservation - Triton capture routing unchanged

_For any_ document containing `emltriton` elements, the fixed routing SHALL produce the same `metadata`/`correlation-id` injection and the same `buffer-message-id`/`meta` targets as the original, and the post-run artifact repair SHALL not rename or touch any correctly-named artifact.

**Validates: Requirements 3.6**

## Fix Implementation

### Changes Required

**Defect A — recipes + engine error surfacing**

**Files**: `recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`, `recipe-amd64.yaml` (the tracked `recipe.yaml` is a build-time working copy overwritten by `gdk-component-build-and-publish.sh`, no separate edit needed); `src/backend/workflow_engine/output_bindings.py`

1. **Add a publish-only policy entry** to each variant's `aws.greengrass.ipc.mqttproxy` accessControl (keyed `'{ComponentName}:mqttproxy:2'`): operations `['aws.greengrass#PublishToIoTCore']` only, resources `['*']`, policyDescription documenting why (workflow mqtt_publish topics are user-configured free strings per the node catalog; publish-only so no new subscribe capability is granted). The existing `mqttproxy:1` shadow entry is untouched.
2. **Actionable denial error**: in `_default_greengrass_publisher`, catch `UnauthorizedError` from the IPC result and re-raise as a `RuntimeError` naming the topic and stating that the LocalServer component's `aws.greengrass.ipc.mqttproxy` accessControl does not authorize it (with the recipe location). The existing `OutputBindingError` collection then carries this message into the run error and node detail unchanged.

**Defect B — invoker retry + status seeding + output persistence**

**File**: `src/backend/workflow_engine/output_bindings.py`

3. **409-loading retry** in `_default_llm_invoker`: when a response is 409 and its JSON body's `state` is `loading`, re-POST every `LLM_LOADING_POLL_INTERVAL_SEC` (5s) until `LLM_LOADING_BUDGET_SEC` (240s, comfortably above small-model warm-up, bounded well below the executor thread's tolerance) elapses; first 200 wins. A 409 with any other state, any other non-200, or budget exhaustion raises the existing RuntimeError shape (message now includes the last state payload). The 200-first-attempt path is byte-identical (single POST, same body/timeout).

**File**: `src/backend/workflow_engine/pipeline_executor.py` (+ small additions in `node_status.py`)

4. **Track executor-binding nodes**: seed the collector with the document's `executorBindings` node ids (e.g. `NodeStatusCollector(name_map, extra_node_ids=...)` or a `track(node_id)` loop after construction) so `mark_running_all`/`mark_success_all`/`finalize` cover them and they are never absent from `node_status_json`.
5. **Mark llm outcomes**: after `self._llm_processor.process(...)`, for each llm binding node id, call `collector.mark_failure(node_id, error)` when `metadata['llm'][node_id]` carries `'error'` (the run-level COMPLETED decision is unchanged — requirement 3.4 preserves binding independence; only the node map becomes truthful). Successful llm nodes are covered by `mark_success_all` on the success path.
6. **Persist llm output + run metadata JSON** (shared with Defect C): after post-run processing (both the completed path and the output-binding-failure path), when the execution has an `output_dir`, write `{output_dir}/{capture_id}.json` containing the JSON-serializable view of the final tag values/metadata (notably the `llm` section with each node's `generated_text` or `error`). Contained/best-effort (a write failure never changes the run status), mirroring the existing R8.5 containment style.

**Defect C — post-run artifact repair**

**File**: `src/backend/workflow_engine/pipeline_executor.py`

7. **Empty-basename repair**: after the pipeline run (before post-run handlers), scan `output_dir` for files whose basename is exactly `.{ext}` (empty stem — the broker's `"" + ".ext"` product) and rename them to `{capture_id}.{ext}`. This aligns tritonless runs with `run_artifacts.base_output_image_path` (`{output_dir}/{capture_id}.jpg`), so the run view's image display works too. No-op for Triton runs (files already correctly named); contained/best-effort.
8. The metadata JSON of change 6 satisfies the "no JSON metadata" half of Defect C for tritonless runs while leaving Triton runs' gstreamer-side `meta` routing untouched.

**Security preservation gate (builds.md)**: none of the touched files are preservation-tracked — the recipes are not in any baseline, and `src/backend/workflow_engine/*.py` is not in the secrets-audit `IN_SCOPE_FILES` (only `src/backend/app.py` from src/backend is; `src/docker-compose.yaml` and Dockerfiles are untouched by this fix). No rebaseline is expected; the checkpoint task re-runs the preservation suite to confirm.

## Testing Strategy

### Validation Approach

Two phases: first surface each defect executably on UNFIXED code (exploration tests that FAIL), and capture the behavior that must not change (preservation tests that PASS); then apply the fixes and re-run both suites. Recipe changes use config tests (parse the YAML, assert the policy properties) as the testable seam, mirroring edge-deploy-reliability; the live JP6 device is the final integration gate (user-gated — recipe + src/backend changes ship in the LocalServer component and need a ~1h gdk build).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples demonstrating all three defects BEFORE implementing the fix, confirming the evidence-backed causal chains. If a chain is refuted, re-hypothesize.

**Test Plan**: Drive the real engine functions with injected/mocked boundaries (fake IPC client raising UnauthorizedError, mocked `requests` returning 409-loading, synthetic compiled documents and temp run dirs) and parse the recipe YAML directly. Run on UNFIXED code and document the failures.

**Test Cases**:
1. **Recipe policy exposure**: parse all four recipe variants; assert an `aws.greengrass.ipc.mqttproxy` policy authorizes `PublishToIoTCore` on resources covering non-shadow workflow topics (will fail on unfixed recipes — only `$aws/things/*/shadow/name/*`)
2. **Unauthorized denial actionability**: call `_default_greengrass_publisher` (or `_run_mqtt_publish` with an injected greengrass publisher raising `UnauthorizedError`); assert the surfaced error names the topic and the accessControl cause (will fail on unfixed code — bare `UnauthorizedError`)
3. **409-loading retry**: `_default_llm_invoker` with mocked `requests` returning 409 `{'state': 'loading'}` twice then 200, `time.sleep` stubbed; assert the generated text is returned (will fail on unfixed code — RuntimeError on the first 409)
4. **Executor-binding node status**: run a `WorkflowExecutor` execution over a compiled document with an llm_inference binding (invoker mocked to fail, then to succeed); assert `node_status_json` holds a terminal entry for the llm node — `failure` with detail when it failed, `success` when it succeeded (will fail on unfixed code — node absent)
5. **Persisted llm output + run metadata JSON**: same harness, successful invoker; assert `{output_dir}/{capture_id}.json` exists and carries the `llm` section (will fail on unfixed code — no such file)
6. **Empty-basename repair**: place a broker-style `.jpg` file in a temp `output_dir` and drive the executor's repair step (or the full run with a stubbed pipeline that writes `.jpg`); assert the directory ends with `{capture_id}.jpg` and no empty-basename file (will fail on unfixed code — no repair exists)

**Expected Counterexamples**:
- All four recipes: mqttproxy resources == `['$aws/things/*/shadow/name/*']` only
- `UnauthorizedError` propagates verbatim with no remediation hint
- `RuntimeError: Text_Generation_API returned 409 for model 'opt125m-smoke': {'model_name': ..., 'state': 'loading'}` on the first attempt
- `node_status_json` lacking llm_inference_1; run COMPLETED
- Run dir contains only `.jpg` + `run.log`; no `{capture_id}.json`

### Fix Checking

**Goal**: Verify that for all inputs where a bug condition holds, the fixed functions produce the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition_A(input) OR isBugCondition_B(input)
                 OR isBugCondition_C(input) DO
  result := fixedPath(input)
  ASSERT expectedBehavior(result)   -- Properties 1-3
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where no bug condition holds, the fixed code produces the same result as the original.

**Pseudocode:**
```
FOR ALL input WHERE NOT (isBugCondition_A(input) OR isBugCondition_B(input)
                         OR isBugCondition_C(input)) DO
  ASSERT original(input) = fixed(input)   -- Properties 4-6
END FOR
```

**Testing Approach**: Property-based testing (Hypothesis, already used in this repo) is recommended — preservation is a universal claim over the non-buggy input domain; generated broker/aws_iot parameter sets, bus-signal sequences, and Triton documents catch edge cases manual cases miss.

**Test Plan**: Observe UNFIXED behavior first (recording publishers, the existing `test_mqtt_publish_call_preservation.py` baseline style, parsed recipe structures, collector transition sequences, Triton-document routing output), then encode it as tests that must keep passing after the fix.

**Test Cases**:
1. **Paho path preservation**: for generated non-greengrass parameter sets, the fixed `_run_mqtt_publish` dispatches identical publisher call tuples (extends the existing preservation suite's observed baseline)
2. **Recipe equality modulo added entry**: parsed fixed recipe deep-equals the original after deleting only the new `mqttproxy:2` entry, for all four variants
3. **llm 200-path and placeholder-path preservation**: single POST with identical URL/body/timeout on first-attempt 200; unresolved placeholders never call the API; per-binding independence intact
4. **Collector preservation**: for generated bus-signal/failure sequences over element-only documents, fixed and original `NodeStatusCollector` produce identical maps
5. **Triton routing preservation**: for documents with `emltriton`, `_route_capture_outputs`/`_inject_inference_metadata` output unchanged; the repair step never renames correctly-named `{capture_id}.*` files

### Unit Tests

- Recipe YAML policy assertions per variant (operations/resources/description of both mqttproxy entries)
- `_default_greengrass_publisher` UnauthorizedError wrapping; success path passes topic/payload/qos through unchanged
- `_default_llm_invoker` matrix: 200 first try; 409-loading→200; 409-loading until budget; 409-failed; 502; timeout
- `NodeStatusCollector` seeding with executor-binding ids; llm failure marking; `finalize` still guarantees terminal maps
- Artifact repair: `.jpg`/`.png` renamed, `{capture_id}.jpg` untouched, missing dir/no-op contained
- Metadata JSON writer: content includes `llm` section; write failure contained

### Property-Based Tests

- Property 4: generated non-greengrass mqtt parameter sets → identical publisher calls
- Property 5: generated templates/metadata and bus-signal sequences → identical llm merge results and collector maps
- Property 6: generated Triton documents → identical routing; generated correctly-named artifact sets → repair is the identity

### Integration Tests

- Full `WorkflowExecutor.execute` over a tritonless llm+capture+mqtt document with mocked boundaries: run COMPLETED, node statuses terminal and truthful, `{capture_id}.jpg` + `{capture_id}.json` present, greengrass publish attempted with the configured topic
- On-hardware JP6 (user-gated): deploy the rebuilt LocalServer, run workflow `1f0b4c0c-...`, verify MQTT delivery to IoT Core, llm_inference success after model warm-up, truthful run-view statuses, and correctly-named artifacts in `/aws_dda/captures/...`
