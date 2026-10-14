# Implementation Plan: Workflow Manager Gaps

## Overview

Implementation proceeds along six largely independent tracks that converge at checkpoints: (1) workflow_core catalog → validator → compiler for the Metadata_Node, ending with a vendored-copy refresh; (2) the edge runtime metadata passthrough in `output_bindings.py` (pure resolution functions first, then processor integration after the re-vendor); (3) the async generation submit/worker/status split in `workflow_generator.py`; (4) the metadata-only rename in `workflows.py`; (5) CDK wiring (compute stack self-invoke, resource-id props, new nested API stack); (6) frontend (API client, chat-panel poll reducer, rename modal, Metadata node config editor). Property-based tests (Hypothesis in `edge-cv-portal/backend/tests/` and `test/backend-test/`, fast-check in `frontend/src/pages/workflows/`) sit directly beside the code they validate, one file per property so waves can run them in parallel. Implementation lands on branch `spec/workflow-manager-gaps`.

## Tasks

- [x] 1. workflow_core catalog: Metadata_Node descriptor and shared config parser
  - [x] 1.1 Create `metadata_config.py` and register the `metadata` descriptor
    - Create `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/metadata_config.py` with pure helpers `parse_mappings(raw) -> (list[{path, key}], errors)` and `parse_static_json(raw) -> (dict | None, errors)` implementing the shared validity rules (JSON array of `{path, key}` string pairs, non-empty trimmed paths/keys, no duplicate keys, ≤ 50 mappings; static JSON parseable, object-typed, ≤ 10,240 characters)
    - Add the `METADATA` `NodeTypeDescriptor` to `catalog/nodes.py`: `type_id="metadata"`, `CATEGORY_POST_PROCESSING`, `InferenceMeta` in/out ports, string parameters `mappings` (default `"[]"`) and `static_json` (default `""`, max_length 10240), `executor_binding="metadata"` on all archs, `hardware_dependent=False`; append to `NODE_CATALOG`
    - _Requirements: 6.1_

  - [ ]* 1.2 Write catalog descriptor smoke unit test
    - Assert the `metadata` descriptor is present in `NODE_CATALOG` with the declared ports, both parameters, defaults, and the `metadata` executor binding on every architecture
    - _Requirements: 6.1_

  - [ ]* 1.3 Write property test for Metadata_Node serializer round-trip
    - New file `edge-cv-portal/backend/tests/test_property_wmg_serializer_roundtrip.py`: Hypothesis-generated definitions containing Metadata_Nodes with arbitrary valid `mappings`/`static_json` strings; parse → serialize → parse yields the same node ids, types, parameters, and connections
    - **Property 19: Serializer round-trip for Metadata_Node definitions**
    - **Validates: Requirements 8.4**

- [x] 2. workflow_core validator: Metadata_Node checks
  - [x] 2.1 Implement `_check_v10_metadata` and `_check_w2_metadata_no_trigger` in `validator/checks.py`
    - `_check_v10_metadata`: per `metadata`-typed node, one SEVERITY_ERROR finding per violation via `metadata_config` parsing — `CODE_V10_METADATA_MAPPINGS_INVALID`, `CODE_V10_METADATA_EMPTY_FIELD_PATH`, `CODE_V10_METADATA_EMPTY_KEY`, `CODE_V10_METADATA_DUPLICATE_KEY`, `CODE_V10_METADATA_TOO_MANY_MAPPINGS`, `CODE_V10_METADATA_STATIC_JSON_INVALID`
    - `_check_w2_metadata_no_trigger`: when the graph has no `CATEGORY_TRIGGER` node, exactly one SEVERITY_WARNING `CODE_W2_METADATA_NO_TRIGGER` per Metadata_Node with ≥ 1 mapping; none for static-JSON-only nodes
    - Both checks fire only on `metadata`-typed nodes so metadata-free graphs produce identical findings; do NOT add the new codes to `generation_gate.STRUCTURAL_ERROR_CODES`
    - _Requirements: 6.4, 6.5, 8.1_

  - [ ]* 2.2 Write property test for the validator partition (Python half)
    - New file `edge-cv-portal/backend/tests/test_property_wmg_metadata_validator.py`: Hypothesis over generated Metadata_Node configurations (valid and each violation class); exactly one SEVERITY_ERROR per present violation, zero findings for valid configs
    - **Property 16: Metadata_Node validator partition**
    - **Validates: Requirements 6.3, 6.4, 6.7**

  - [ ]* 2.3 Write property test for trigger-less mapping warning counts
    - New file `edge-cv-portal/backend/tests/test_property_wmg_trigger_warning.py`: Hypothesis over graphs with/without trigger nodes and Metadata_Nodes with/without mappings; warning count equals the number of mapping-bearing Metadata_Nodes in trigger-less graphs, zero otherwise
    - **Property 17: Trigger-less mapping warning counts**
    - **Validates: Requirements 6.5**

- [x] 3. workflow_core compiler: metadata binding emission and vendored-copy refresh
  - [x] 3.1 Emit the `metadata` executor binding entry in `compiler/compiler.py`
    - `metadata` node: empty `element_chain`, flows through the existing executor-level collapse (like `inference_filter`)
    - In the executor-bindings emission loop add for `BINDING_METADATA` entries: `metadataMappings` (`[{fieldPath, key}]` from `parse_mappings`), `staticJson` (parsed object or `{}`), and `attachTo` from a new `_reachable_output_nodes(node_id, successors, typed_nodes)` — BFS over the full node-level successors adjacency returning `CATEGORY_OUTPUT` node ids in stable order
    - _Requirements: 6.6_

  - [ ]* 3.2 Write property test for compiler emission completeness
    - New file `edge-cv-portal/backend/tests/test_property_wmg_compiler_emission.py`: Hypothesis over valid graphs with Metadata_Nodes; every mapping and the full static JSON reach the compiled entry unaltered, and `attachTo` equals exactly the reachable output-category node ids
    - **Property 18: Compiler emission completeness**
    - **Validates: Requirements 6.6**

  - [ ]* 3.3 Write property test for non-interference (portal half)
    - New file `edge-cv-portal/backend/tests/test_property_wmg_noninterference_portal.py`: Hypothesis over metadata-free definitions; new validator checks add zero findings and compiled documents carry no `metadata` bindings and no `attachTo`/`metadataMappings`/`staticJson` keys
    - **Property 20: Non-interference with metadata-free workflows (portal half)**
    - **Validates: Requirements 8.1**

  - [x] 3.4 Refresh the vendored workflow_core copy in the edge runtime
    - Run `src/backend/workflow_engine/vendor/re_vendor.sh` and commit the refreshed `src/backend/workflow_engine/vendor/workflow_core` so the edge runtime sees the new catalog descriptor, validator checks, and compiler emission
    - _Requirements: 6.6, 8.1_

- [x] 4. Edge runtime: metadata passthrough in `output_bindings.py`
  - [x] 4.1 Implement the pure metadata resolution functions
    - In `src/backend/workflow_engine/output_bindings.py` add: `resolve_field_path(document, dotted_path) -> (found, value)` (segment-by-segment through JSON objects, numeric segments as list indices, resolved `null` distinguishable from not-found); `resolve_metadata_binding(binding, trigger)` (static entries first, mappings resolved against `trigger["payload_json"]` only when it is a dict, resolved mappings override colliding static entries with a logged collision, unresolved paths omitted and logged, non-JSON payload or absent context degrades to static-only with one log line, never raises); `attached_metadata_by_output(bindings, trigger)` (evaluate each `metadata` binding once, fan out via `attachTo`, merge in emission order with later-binding-wins logged)
    - _Requirements: 7.2, 7.3, 7.4, 7.5, 7.6, 7.9_

  - [ ]* 4.2 Write property test for metadata resolution and merge semantics
    - New file `test/backend-test/output_bindings_metadata/test_property_wmg_metadata_resolution.py`: Hypothesis over trigger contexts (JSON-object, non-JSON, non-object JSON, absent), mapping sets, and static JSON objects; resolution never raises and produces exactly the specified attached map
    - **Property 14: Metadata resolution and merge semantics**
    - **Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.9**

  - [x] 4.3 Integrate attachment into `OutputBindingProcessor.process` and `_run_mqtt_publish`
    - Binding loop `continue`s on `BINDING_METADATA`; compute `attached_by_output = attached_metadata_by_output(bindings, metadata.get("trigger") or {})` before the loop
    - Output bindings with an attached map receive an effective metadata dict (existing tag keys win, logged) plus a `metadata_json` placeholder for templates/conditions
    - `_run_mqtt_publish` with an attached map: JSON-object payloads merge attached entries top-level with workflow-result keys winning (logged); non-object payloads wrap as `{"payload": <rendered>, "metadata": {...}}`; re-serialization failure falls back to the wrapped form
    - Outputs with no attached map take the exact current code path (byte-identical payloads); `opcua_write`/`modbus_write`/`digital_output` gain no automatic embedding; no changes to `trigger_runtime.py` or `pipeline_executor.py`
    - _Requirements: 7.1, 7.7, 7.8_

  - [ ]* 4.4 Write property test for attachment scoping
    - New file `test/backend-test/output_bindings_metadata/test_property_wmg_output_attachment.py`: Hypothesis over compiled documents with metadata bindings and run metadata, `OutputBindingProcessor` with injected stub publishers capturing payloads; `attachTo` outputs carry every attached entry alongside unaltered result values, non-`attachTo` outputs emit byte-identical payloads
    - **Property 15: Attachment reaches exactly the downstream outputs**
    - **Validates: Requirements 7.7, 7.8**

  - [ ]* 4.5 Write property test for non-interference (runtime half)
    - New file `test/backend-test/output_bindings_metadata/test_property_wmg_noninterference_runtime.py`: Hypothesis over compiled documents without metadata bindings; output-binding processing produces results identical to pre-feature behavior on the same inputs
    - **Property 20: Non-interference with metadata-free workflows (runtime half)**
    - **Validates: Requirements 8.1**

  - [ ]* 4.6 Add the on-hardware harness metadata passthrough scenario
    - Extend `test/on-hardware/harness/stages/test_30_workflows.py` with one trigger-driven scenario: MQTT trigger on `swagfactory/invoke` carrying `job_id`, workflow publishes to `swagfactory/quality`, assertion that the published payload echoes `job_id` beside the result fields
    - _Requirements: 7.7_

- [x] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Backend: asynchronous generation submit and worker (`workflow_generator.py`)
  - [x] 6.1 Split `generate_workflow` into `run_generation_core` and implement `submit_generation`
    - Extract today's generation body (Bedrock config resolution onward: `converse_messages`, `invoke_generation`, gate/repair/reject flow, accept-only persistence) into `run_generation_core(...) -> (status_code, payload_dict)` with zero semantic changes
    - `submit_generation(event, user)`: today's synchronous prefix verbatim (parse_body, MISSING_FIELDS/INVALID_PROMPT/INVALID_TEMPERATURE, RBAC WORKFLOW_CREATE|WORKFLOW_EDIT, USECASE_NOT_FOUND, INVALID_CURRENT_DEFINITION) — any failure returns the existing envelope and creates no job; session resolution mints a fresh session id for missing/unresolvable `session_id` (no more 404 SESSION_NOT_FOUND) keeping follow-up semantics over the client `current_definition`; write the Generation_Job item (`session_id="genjob#{job_id}"`, `record_type="generation_job"`, `status=pending`, `dispatched_at`, `deadline_at = dispatched_at + 270000 + 60000`, `ttl`) to WorkflowChatSessions; self-invoke via `WORKFLOW_GENERATOR_FUNCTION_NAME` with `InvocationType='Event'` and a `workflow_gen_worker: true` payload; dispatch failure conditionally marks the job failed with 502 `GENERATION_NOT_STARTED` and returns the same envelope; success returns `202 {job_id, session_id, usecase_id, status: "pending"}`
    - Route `('/workflows/generate', 'POST')` to `submit_generation` in `handler()`
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8_

  - [x] 6.2 Implement `run_generation_worker`
    - Dispatch from `handler()` before HTTP routing on `workflow_gen_worker: true` (the `node_generator.py` pattern); conditional `pending → running` transition; execute `run_generation_core` unchanged
    - 200 outcome: write the full sync-endpoint payload to S3 (`.../chat-sessions/{session_id}/jobs/{job_id}/result.json`), conditional transition to `succeeded` with `result_s3_key`; error outcome: conditional transition to `failed` storing `{http_status, error}`; terminal writes set `terminal_at` and refresh `ttl = terminal + SESSION_TTL_SECONDS` (zero TTL ⇒ immediately removable); all terminal writes conditional on `status IN (pending, running)`, losers log and stop
    - Combined failure: a Repair_Pass GENERATION_TIMEOUT after a first-pass rejection records one `failed` state whose GENERATION_REJECTED envelope `details` carry both `structural_errors` and a `timeout` object
    - Outermost try/except records a 500 INTERNAL_ERROR terminal failure so no exception path leaves the job non-terminal
    - _Requirements: 1.2, 2.6, 2.7, 2.9, 3.1, 3.2, 3.3, 3.4, 3.5, 3.7, 3.8_

  - [ ]* 6.3 Write property test for accepted submissions creating pollable jobs
    - New file `edge-cv-portal/backend/tests/test_property_wmg_async_submit.py`: Hypothesis over valid request bodies (with/without `session_id`, `current_definition`, `temperature`) against submit + status with the `test_workflow_generation.py` module-level fakes; 202 with `job_id` + `session_id`, job exists pending/running before the response, immediate status poll returns the state (never 404), fresh session id when none/unresolvable provided
    - **Property 1: Accepted submission creates a pollable job**
    - **Validates: Requirements 1.1, 1.2, 1.6, 1.7, 1.8**

  - [ ]* 6.4 Write property test for the submit path never invoking Bedrock
    - New file `edge-cv-portal/backend/tests/test_property_wmg_submit_no_bedrock.py`: Hypothesis over valid and invalid request bodies with an instrumented Bedrock fake; submit completes with zero Bedrock invocations, returning 202 or a synchronous Error_Envelope
    - **Property 2: Submit path never invokes Bedrock**
    - **Validates: Requirements 8.5**

  - [ ]* 6.5 Write property test for synchronous rejection creating nothing
    - New file `edge-cv-portal/backend/tests/test_property_wmg_submit_rejection.py`: Hypothesis over each synchronous failure class (missing fields, invalid prompt, bad temperature, unparseable definition, unknown usecase, missing RBAC permission); same status code and envelope code as today, no job item written, no async dispatch
    - **Property 3: Synchronous rejection creates nothing**
    - **Validates: Requirements 1.3, 1.4**

  - [ ]* 6.6 Write property test for follow-up semantics across session states
    - New file `edge-cv-portal/backend/tests/test_property_wmg_followup_semantics.py`: Hypothesis over prior session states (live with history/snapshot, expired/unknown id, none) and prompts; the worker's Converse message list equals today's synchronous path output (history capped at MAX_HISTORY_MESSAGES, canvas block and modification instruction exactly when a snapshot exists)
    - **Property 4: Follow-up semantics preserved across session states**
    - **Validates: Requirements 1.5, 1.6**

  - [ ]* 6.7 Write property test for accept-only session persistence
    - New file `edge-cv-portal/backend/tests/test_property_wmg_session_persistence.py`: Hypothesis over injected generation outcomes (accept, repair-then-accept, reject, repair-failing, timeout, invocation failure, unparseable output, validator exception on either pass); snapshot + history persisted iff accept-class outcome, session/S3 unchanged on every failure with the matching existing envelope recorded on the job
    - **Property 8: Session persistence happens exactly on gate acceptance**
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.8**

  - [ ]* 6.8 Write property test for exactly one Repair_Pass
    - New file `edge-cv-portal/backend/tests/test_property_wmg_repair_pass.py`: Hypothesis over first-gate repair decisions; at most two Bedrock invocations total, repair result re-gated before any result stored, success only on re-gated accept
    - **Property 9: Exactly one Repair_Pass, always re-gated**
    - **Validates: Requirements 3.7**

  - [ ]* 6.9 Write property test for combined rejection+timeout failure recording
    - New file `edge-cv-portal/backend/tests/test_property_wmg_combined_failure.py`: Hypothesis over first-pass structural errors with an injected Repair_Pass timeout; exactly one terminal failed state whose envelope details carry both the structural errors and the timeout indication
    - **Property 10: Combined gate-rejection and timeout failures record one terminal state with both**
    - **Validates: Requirements 2.9, 3.5**

- [x] 7. Backend: generation status endpoint (`workflow_generator.py`)
  - [x] 7.1 Implement `get_generation_job` with lazy reaping
    - Load `genjob#{job_id}`; absent → fixed 404 JOB_NOT_FOUND (never distinguishes never-existed vs TTL-removed); no Use_Case access → the same 404; Use_Case access without WORKFLOW_CREATE/WORKFLOW_EDIT → existing 403 RBAC envelope
    - Reap: non-terminal and `now_ms > deadline_at` → conditional transition to `failed` with 504 GENERATION_ABNORMAL_TERMINATION; on ConditionalCheckFailed re-read and serve the stored terminal state
    - Respond: pending/running → `200 {job_id, status}` only; succeeded → 200 embedding `result.json` field-for-field (`session_id, usecase_id, definition, findings, error_count, warning_count, validation_passed, assistant_text, model_id, gate`); failed → stored `http_status` + stored envelope verbatim; S3 read failure for a succeeded job → 500 INTERNAL_ERROR leaving the record untouched; reads never mutate terminal jobs
    - Add `('/workflows/generate/{job_id}', 'GET')` to `handler()` routing
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.8, 2.10, 3.6_

  - [ ]* 7.2 Write property test for state-machine legality and terminal immutability
    - New file `edge-cv-portal/backend/tests/test_property_wmg_job_state_machine.py`: Hypothesis-generated interleavings of worker transitions, reaper evaluations, and status reads against an in-memory conditional-write table fake; only legal transitions occur, exactly one terminal write binds, post-terminal reads are payload-identical
    - **Property 5: Job state machine legality and terminal immutability**
    - **Validates: Requirements 2.1, 2.6**

  - [ ]* 7.3 Write property test for state-appropriate status payloads
    - New file `edge-cv-portal/backend/tests/test_property_wmg_status_payloads.py`: Hypothesis over stored job states; pending/running responses carry neither result nor failure envelope, succeeded embeds the exact sync-endpoint payload, failed returns the stored status and envelope verbatim
    - **Property 6: Status responses carry exactly the state-appropriate payload**
    - **Validates: Requirements 2.2, 2.3, 2.8**

  - [ ]* 7.4 Write property test for 404 indistinguishability
    - New file `edge-cv-portal/backend/tests/test_property_wmg_status_404.py`: Hypothesis over never-existed, TTL-removed, and cross-tenant job ids → byte-identical 404 envelope; accessible failed jobs return their failure envelope (never 404); Use_Case access without generation permissions → existing 403
    - **Property 7: Status 404 indistinguishability**
    - **Validates: Requirements 2.4, 2.5, 2.10**

  - [ ]* 7.5 Write property test for the reaping rule
    - New file `edge-cv-portal/backend/tests/test_property_wmg_reaper.py`: Hypothesis over job states, deadlines, and read times; reap fires exactly on non-terminal past-deadline reads, in-deadline reads return the stored state, terminal jobs never reaped, races resolve first-write-wins, terminal `ttl` equals terminal time + session TTL (zero TTL ⇒ immediate removability)
    - **Property 11: Reaper transitions exactly the overdue non-terminal jobs**
    - **Validates: Requirements 2.6, 2.7, 3.6**

- [x] 8. Backend: workflow rename (`workflows.py`)
  - [x] 8.1 Implement `rename_workflow` and route `PATCH /workflows/{id}/name`
    - `get_workflow_item` absent → existing uniform `not_found_response()`; `authorize_workflow_access(..., Permission.WORKFLOW_SAVE)`; body validation → 400 INVALID_NAME when `name` missing/non-string, empty/whitespace-only after strip, or > 128 characters; store the trimmed value
    - Single Workflows-table `update_item`: `SET #name = :name, updated_at = :updated` with `ConditionExpression='attribute_exists(workflow_id)'` (conditional failure maps to the 404), `ReturnValues='ALL_NEW'`; no writes to WorkflowVersions, S3 definitions, or `latest_version`
    - `log_audit_event(action='rename_workflow', ...)` with `previous_name`, `new_name`, `usecase_id`; return `200 {workflow: workflow_summary(new_item)}`; add the `('/workflows/{id}/name', 'PATCH')` route; `update_workflow` (PUT) untouched
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 8.2_

  - [ ]* 8.2 Write property test for the rename frame condition
    - In `edge-cv-portal/backend/tests/test_workflow_rename_properties.py`: Hypothesis over stored workflow records (any version count) and valid names (unicode whitespace, length boundaries); exactly `name` and `updated_at` change, versions/definitions/`latest_version`/derived component name unchanged, audit event carries workflow_id, both names, and the acting user
    - **Property 12: Rename is a two-attribute frame**
    - **Validates: Requirements 5.1, 5.2, 5.6, 8.2**

  - [ ]* 8.3 Write property test for the rename validity and safety partition
    - Extend `edge-cv-portal/backend/tests/test_workflow_rename_properties.py`: invalid names → 400 with the record unchanged; missing permission → existing 403 unchanged; nonexistent and inaccessible ids → the same 404 envelope; valid authorized requests succeed
    - **Property 13: Rename validity and safety partition**
    - **Validates: Requirements 5.3, 5.4, 5.5**

  - [ ]* 8.4 Write regression unit test for `PUT /workflows/{id}`
    - Concrete example asserting the existing update operation's request validation, response shape, RBAC enforcement, and version allocation are byte-identical after the rename route lands
    - _Requirements: 8.3_

- [x] 9. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Infrastructure (CDK)
  - [x] 10.1 Wire generator self-invocation in `compute-stack.ts`
    - Grant the generator role `lambda:InvokeFunction` on its own function (the `ddaLabelingWorker.grantInvoke` pattern); set `WORKFLOW_GENERATOR_FUNCTION_NAME` in its environment; add an `EventInvokeConfig` with `maximumRetryAttempts: 0`; Lambda timeout stays 270 s
    - _Requirements: 1.2, 3.6_

  - [x] 10.2 Expose the two parent resource ids from `api-gateway-stack.ts`
    - Add public readonly props `workflowGenerateResourceId` (the existing `workflowsResource.addResource('generate')`) and `workflowResourceId` (`/workflows/{id}`); zero new resources in this stack
    - _Requirements: 2.1, 5.1_

  - [x] 10.3 Create `workflow-manager-gaps-api-stack.ts` and wire it into the app
    - New `cdk.NestedStack` cloning the `CameraRegistryApiStack` pattern: `RestApi.fromRestApiAttributes`, `Resource.fromResourceAttributes` for both imported parents, own Cognito authorizer, `defaultCorsPreflightOptions` on created resources, `allowTestInvoke: false`, route-salted `CfnDeployment`
    - Routes: `GET /workflows/generate/{job_id}` → WorkflowGeneratorHandler; `PATCH /workflows/{id}/name` → WorkflowsHandler (plus OPTIONS)
    - Instantiate the nested stack with the two handlers and resource-id props, wiring through `bin/app.ts`/compute-stack as the sibling stacks do
    - _Requirements: 2.1, 5.1, 8.5_

  - [ ]* 10.4 Write CDK synth assertion tests
    - Assert the nested stack attaches exactly the two new routes with Cognito authorization and that `ApiGatewayStack` gains no new resources
    - _Requirements: 1.1, 2.1, 5.1_

- [x] 11. Frontend: API client
  - [x] 11.1 Extend `services/api.ts`
    - `generateWorkflow` returns the 202 shape `{job_id, session_id, usecase_id, status}`; new `getWorkflowGenerationJob(jobId)` typed as a discriminated union on `status` (in-progress vs succeeded-with-`WorkflowGenerationResult` fields vs failed envelope); new `renameWorkflow(workflowId, name)` calling `PATCH /workflows/{id}/name`
    - _Requirements: 4.1, 4.5, 5.7_

- [x] 12. Frontend: chat panel submit-then-poll
  - [x] 12.1 Extract the pure poll reducer
    - New module (e.g. `frontend/src/pages/workflows/generationPollReducer.ts`): `pollReducer(state, event)` modeling interval ≤ 5 s (3 s), submission-disabled while non-terminal, terminal-success/terminal-failure transitions, consecutive-transport-failure counter (reset on any successful poll, stop at 3), and the 300 s overall deadline — all timing injected, no I/O
    - _Requirements: 4.1, 4.4, 4.6, 4.7_

  - [x] 12.2 Integrate submit-then-poll into `GenerateChatPanel.tsx`
    - Submit shows the existing in-progress state immediately, adopts the returned `session_id`, disables the prompt input while non-terminal, polls every 3 s via the reducer; terminal success renders exactly as the sync path today (`fromWorkflowDefinition`, `onApplyGenerated`, findings + gate Alerts, clear prompt); terminal failure keeps the existing error/gate-rejection rendering with the prompt retained; transport-failure stop shows "generation status could not be retrieved"; deadline stop shows "generation did not complete in time" (prompt retained in both); follow-ups keep sending `session_id` + current canvas definition
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_

  - [ ]* 12.3 Write property test for the poll loop
    - New file `frontend/src/pages/workflows/generationPollReducer.property.test.ts`: fast-check over poll-response sequences (in-progress, terminal success, terminal failure, transport failure) and timings with fake timers; polls ≤ 5 s apart, submission disabled exactly while in flight, stops exactly on first of terminal/3rd-consecutive-transport-failure/300 s, prompt retained on every non-success termination
    - **Property 21: Chat panel poll loop stops exactly when it should**
    - **Validates: Requirements 4.1, 4.4, 4.6, 4.7**

  - [ ]* 12.4 Write chat panel unit tests
    - Extend `GenerateChatPanel.test.tsx`: success rendering (canvas apply + findings, never auto-saved), failure envelope message with prompt retained, follow-up request carrying session id + current definition
    - _Requirements: 4.2, 4.3, 4.5_

- [x] 13. Frontend: rename affordance
  - [x] 13.1 Add the Rename action to `WorkflowToolbar.tsx`
    - Rename action enabled only when a workflow is loaded and `canEditWorkflows(role)`; modal pre-filled with the current name; client-side trim/length validation mirroring the backend rules; on success update the loaded-workflow name in component state and the open-picker cache without reload; on failure show the envelope message and keep the previous name
    - _Requirements: 5.7, 5.8, 5.9_

  - [ ]* 13.2 Write toolbar rename unit tests
    - Extend `WorkflowToolbar.test.tsx`: affordance visibility by role/loaded state, optimistic name update on success, failure display retaining the previous name
    - _Requirements: 5.7, 5.8, 5.9_

- [x] 14. Frontend: Metadata node configuration
  - [x] 14.1 Port the shared metadata config rules to TypeScript
    - New module (e.g. `frontend/src/pages/workflows/metadataConfig.ts`) mirroring `metadata_config.py`: parse/validate `mappings` (array of `{path, key}` pairs, non-empty trimmed values, no duplicate keys, ≤ 50) and `static_json` (parseable, object-typed, ≤ 10,240 chars), returning field-level errors
    - _Requirements: 6.3, 6.7_

  - [x] 14.2 Add the `metadata` branch to `NodeConfigPanel.tsx`
    - Type-specific branch for `typeId === 'metadata'` (the `custom_python`/`unified_input` pattern): mapping rows editor (add/edit/remove up to 50 rows of field path → output key) serializing to the `mappings` JSON parameter; static JSON textarea bound to `static_json`; validation errors from `metadataConfig.ts` surface as field errors that block saving the configuration, feeding the existing `inlineChecks.ts` marker plumbing; palette entry appears automatically from the catalog
    - _Requirements: 6.2, 6.3, 6.7_

  - [ ]* 14.3 Write property test for shared-predicate parity (TypeScript half)
    - New file `frontend/src/pages/workflows/metadataConfig.property.test.ts`: fast-check over generated config strings; the TypeScript validity predicate accepts and rejects exactly the same configurations as the Python validator rules (same violation classes flagged)
    - **Property 16: Metadata_Node validator partition (TypeScript half)**
    - **Validates: Requirements 6.3, 6.4, 6.7**

  - [ ]* 14.4 Write node config editor unit tests
    - Extend `NodeConfigPanel.test.tsx`: mapping row add/edit/remove interactions, static JSON entry, save blocked on invalid configs with visible field errors
    - _Requirements: 6.2, 6.3, 6.7_

- [x] 15. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - Run the full existing suites unchanged — `test_workflow_generation.py` + gate tests, `workflows.py` tests, workflow_core validator/compiler tests, and the `output_bindings` suites — as the executable definition of the preserved semantics in Requirements 3 and 8.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for faster MVP
- Backend property tests use Hypothesis with the existing `edge-cv-portal/backend/tests/` and `test/backend-test/` conventions (module-level fakes from `test_workflow_generation.py`, `conftest.py` fixtures), one file per property so waves can parallelize; frontend property tests use fast-check matching the existing `*.property.test.ts` suites; every property test runs ≥ 100 iterations and is tagged `# Feature: workflow-manager-gaps, Property N: <title>`
- The vendored workflow_core copy under `src/backend/workflow_engine/vendor` MUST be refreshed (task 3.4, `vendor/re_vendor.sh`) after any workflow_core change and before the edge-runtime processor integration (task 4.3)
- Checkpoints (tasks 5, 9, 15) validate at phase boundaries; the final checkpoint additionally runs the full pre-existing suites unchanged (Requirements 3, 8 regression safety)
- Implementation lands on branch `spec/workflow-manager-gaps` (off `integration/all-specs`)
- Each task references the granular requirements it implements for traceability

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "4.1", "6.1", "8.1", "10.1", "10.2", "11.1", "12.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.1", "4.2", "6.2", "6.4", "6.5", "8.2", "8.4", "10.3", "12.2", "12.3", "13.1", "14.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.1", "6.6", "6.7", "6.8", "6.9", "7.1", "8.3", "10.4", "12.4", "13.2", "14.2"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4", "6.3", "7.2", "7.3", "7.4", "7.5", "14.3", "14.4"] },
    { "id": 4, "tasks": ["4.3"] },
    { "id": 5, "tasks": ["4.4", "4.5", "4.6"] }
  ]
}
```
