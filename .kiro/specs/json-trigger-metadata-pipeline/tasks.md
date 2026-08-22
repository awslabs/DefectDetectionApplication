# Implementation Plan: json-trigger-metadata-pipeline

## Overview

Three largely independent deliverable groups, all in Python following existing backend patterns:

- **A — Integration test** (Requirements 1–5): a `PipelineHarness` in `test/backend-test/workflow_engine/` that builds the five-node workflow, fires simulated MQTT triggers through the real engine seams (real `CustomPythonBridge` subprocess, fake transport/pipeline-manager/publisher), plus the full example and property test cases.
- **B — Verification runbook** (Requirement 6): `runbook/handler.py`, `runbook/workflow.json`, and `runbook.md` under the spec directory. Manual on-device execution of the runbook happens after all coding tasks, per the workspace `builds.md` steering rule, and is not part of this task list.
- **C — `system_prompt` parameter** (Requirements 7–8): catalog descriptors, `output_bindings.py` processors/invokers, `text_generation.py` endpoint validation, and `vllm_runtime/manager.py` threading, with the backward-compatibility invariant that absent/empty/whitespace system prompts keep every outgoing artifact byte-identical.

Property-based tests implement the design's 12 correctness properties with Hypothesis, minimum 100 examples each, tagged `Feature: json-trigger-metadata-pipeline, Property N`. Each property test lives in its own test module so they can be authored independently.

## Tasks

- [x] 1. Integration test harness and shared handler
  - [x] 1.1 Create the dual-path Handler_Script module
    - Create `test/backend-test/workflow_engine/trigger_pipeline_handler.py` containing the `produce_frame(context)` handler source from the design: base64 field wins over URI, `dda_frames.load_image` for the URI path, base64-decode + image-decode for the embedded path, `ValueError` messages naming the failing source (`image_b64`, the URI, or "no image source"), and echo of the produce-request context plus `image_source` into the returned producer metadata
    - Expose the handler source as a string/constant so both the harness (temp-dir script per test) and the runbook asset reuse identical logic
    - _Requirements: 2.1, 3.1, 3.4, 3.5, 6.2_

  - [x] 1.2 Implement PipelineHarness and RunResult
    - Create `test/backend-test/workflow_engine/test_workflow_trigger_metadata_pipeline.py` with the `PipelineHarness` class: build the compiled five-node document (`mqtt_subscribe` → `custom_python_source` → `model_inference` → `metadata` → `mqtt_publish`), wire `mqtt_transport_factory` (fake worker with `deliver(context)`), `run_starter_factory` (synchronous execution), `session_factory` (in-memory SQLite), `pipeline_manager_factory` (fake returning canned inference tag values), `bridged_pipeline_runner` (captures frame bytes/caps, delegates to in-process no-op pump), and `mqtt_publisher` (captures `(topic, payload)`)
    - Implement `fire(topic, payload, qos) -> RunResult` exposing `handler_context`, `produced_frame`, `run_metadata`, `published`, `status`, `error`
    - Use the real `CustomPythonBridge` subprocess with the Handler_Script from task 1.1 written to a temp dir
    - Prefix every assertion helper message with its stage label: `trigger context delivery:`, `image extraction:`, `inference input:`, `output publication:`
    - _Requirements: 1.1, 5.1, 5.2, 5.5_

- [x] 2. Integration test cases
  - [x] 2.1 Write happy-path end-to-end test cases
    - `test_uri_path_end_to_end`: local-file URI; assert Trigger_Context fields (topic/payload/qos equal, non-empty timestamp), `payload_json` equals parsed payload, Run_Metadata `trigger` key equals delivered context, `python_source.<nodeId>` equals producer metadata, pixel-for-pixel frame equality, Output_Message contains inference results and resolved Correlation_Metadata, null-valued correlation path attaches as null
    - `test_base64_path_end_to_end`: embedded base64; same context/metadata assertions plus pixel equality against decoding the original bytes
    - `test_non_json_payload`: invalid JSON payload; assert `payload_json` is None end to end
    - `test_base64_wins_over_uri`: both fields present; assert frame comes from base64 and the URI is never fetched
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 2.2, 3.2, 3.4, 4.1, 4.3, 4.5, 5.3_

  - [x] 2.2 Write image-acquisition error test cases
    - `test_rejected_uri_prefix`: disallowed prefix; error names the URI and the prefix restriction; execution recorded failed
    - `test_unloadable_uri`: missing file and non-image bytes; error names the source URI; no frame delivered; execution failed
    - `test_http_stall_times_out`: stalling local socket accepting but never responding; bounded failure within the `dda_frames` HTTP timeout, error names the URI
    - `test_undecodable_base64`: invalid base64, zero-byte decode, and non-image decoded bytes; error names `image_b64`; no frame; execution failed
    - `test_no_image_source`: neither field present; error states no image source; execution failed
    - _Requirements: 2.3, 2.4, 2.5, 3.3, 3.5, 5.4_

  - [x] 2.3 Write metadata-resolution test cases
    - `test_absent_correlation_path`: configured field path missing from payload; assert key omitted from Output_Message, unresolved path logged, execution not failed, inference results and remaining resolved entries still present
    - `test_collision_keeps_result_value`: attached key collides with a workflow-result key; assert the workflow-result value is retained
    - _Requirements: 4.2, 4.4, 4.6, 5.4_

  - [ ]* 2.4 Write property test for trigger payload parsing
    - **Property 1: Trigger payload parsing is total and faithful**
    - **Validates: Requirements 1.3, 1.4**
    - New module `test/backend-test/workflow_engine/test_property_trigger_payload_parsing.py`; Hypothesis over arbitrary payload strings and valid JSON documents against `load_trigger_context`; topic/payload/qos round-trip through persistence

  - [ ]* 2.5 Write property test for pixel preservation
    - **Property 2: Image acquisition preserves pixels**
    - **Validates: Requirements 2.2, 3.2**
    - New module `test/backend-test/workflow_engine/test_property_pipeline_pixels.py`; Hypothesis over small image dimensions/pixel values, losslessly encoded, via both acquisition paths through the real `CustomPythonBridge` (bridge reused across examples per the existing `test_property_python_bridge_*` pattern)

  - [ ]* 2.6 Write property test for correlation metadata resolution
    - **Property 3: Correlation metadata resolution is exact**
    - **Validates: Requirements 4.2, 4.3, 4.4, 4.5**
    - New module `test/backend-test/workflow_engine/test_property_metadata_resolution.py`; Hypothesis over JSON object payloads and mapping sets against `resolve_metadata_binding`; resolved paths attach exact values (null attaches as null), unresolved paths omit

  - [ ]* 2.7 Write property test for collision precedence
    - **Property 4: Workflow-result keys win collisions**
    - **Validates: Requirements 4.6**
    - New module `test/backend-test/workflow_engine/test_property_output_merge.py`; Hypothesis over result payloads and attached metadata maps

- [x] 3. Checkpoint - Ensure all tests pass
  - Run the backend test suite via its standard pytest invocation; ensure all tests pass, ask the user if questions arise.

- [x] 4. Bedrock inference node system_prompt
  - [x] 4.1 Add the system_prompt parameter to the Bedrock catalog descriptor
    - In `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`, append an optional string `ParameterDescriptor("system_prompt", "string", required=False, default="")` with the design's description to the `BEDROCK_INFERENCE` descriptor; documents omitting the parameter remain valid
    - _Requirements: 7.1_

  - [x] 4.2 Thread system_prompt through the Bedrock processor and invoker
    - In `src/backend/workflow_engine/output_bindings.py`, extend `_default_bedrock_invoker` with trailing `system_prompt: Optional[str] = None`; include `system=[{"text": system_prompt}]` in the converse kwargs only when non-empty, keeping the kwargs byte-identical otherwise
    - In `BedrockInferenceProcessor._run_one`, normalize the parameter (absent/empty/whitespace-only ⇒ None, otherwise the raw configured text verbatim) and preserve pre-feature invoker arity when None (call with the original argument count so injected fakes keep working)
    - Keep the anomaly-mode `BEDROCK_JSON_INSTRUCTION` append on the user prompt only; never touch the system prompt
    - _Requirements: 7.2, 7.3, 7.4_

  - [x] 4.3 Write unit tests for the Bedrock converse invocation
    - Assert the complete converse kwargs via a captured-kwargs fake for: non-empty System_Prompt with User_Prompt, absent `system_prompt` parameter, empty-text value, whitespace-only value, and anomaly mode combined with a non-empty System_Prompt
    - _Requirements: 7.5_

  - [ ]* 4.4 Write property test for Bedrock system parameter shape
    - **Property 5: Bedrock system parameter shape**
    - **Validates: Requirements 7.2**
    - New module `test/backend-test/workflow_engine/test_property_bedrock_system_shape.py`; Hypothesis over non-empty system prompts and user prompts; exactly one `{"text": ...}` block, all other kwargs equal the no-system invocation

  - [ ]* 4.5 Write property test for Bedrock absent/empty invariance
    - **Property 6: Bedrock absent/empty invariance**
    - **Validates: Requirements 7.3**
    - New module `test/backend-test/workflow_engine/test_property_bedrock_system_absent.py`; Hypothesis over absent/empty/whitespace-only values; kwargs byte-identical with no `system` key, pre-feature invoker arity preserved

  - [ ]* 4.6 Write property test for Bedrock anomaly-mode isolation
    - **Property 7: Bedrock anomaly mode touches only the user prompt**
    - **Validates: Requirements 7.4**
    - New module `test/backend-test/workflow_engine/test_property_bedrock_anomaly_system.py`; Hypothesis over user and system prompts with anomaly mode enabled; instruction appended to user text only, system prompt verbatim

- [x] 5. LLM/VLM inference node system_prompt
  - [x] 5.1 Add the system_prompt parameter to the LLM catalog descriptor
    - In `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`, append the same optional string `ParameterDescriptor` to the `LLM_INFERENCE` descriptor, noting the value is sent verbatim with no `{placeholder}` rendering
    - _Requirements: 8.1_

  - [x] 5.2 Thread system_prompt through the LLM processor and invoker
    - In `src/backend/workflow_engine/output_bindings.py`, extend `_default_llm_invoker` with trailing `system_prompt: Optional[str] = None`; set `body["system_prompt"]` only when non-empty so the request body is byte-identical otherwise; leave the 409-loading retry loop untouched
    - In `LlmInferenceProcessor._run_one`, normalize like Bedrock (absent/empty/whitespace ⇒ absent), pass verbatim without rendering; extend the existing three-form positional dispatch with `system_prompt=` keyword supplied only when configured, so pre-feature injected fakes keep working
    - Keep anomaly mode appending `BEDROCK_JSON_INSTRUCTION` to the rendered user prompt only
    - _Requirements: 8.2, 8.5, 8.9_

  - [x] 5.3 Add system_prompt validation to the Text_Generation_API endpoint
    - In `src/backend/endpoints/text_generation.py` `validate_generate_request`, reject non-string `system_prompt` with a finding naming the field and reason (422 findings response, no runtime call); treat JSON null and empty string as absent (no finding, no `effective` entry)
    - Pass `system_prompt=` into the generate kwargs only when `effective["system_prompt"]` exists, mirroring the `image=` pattern
    - _Requirements: 8.6, 8.8_

  - [x] 5.4 Thread system_prompt through the VLM runtime prompt construction
    - In `src/backend/vllm_runtime/manager.py`, add `system_prompt: Optional[str] = None` to `generate`, `generate_stream`, and `_request`
    - No-image path: engine prompt becomes `"{system}\n\n{user}"` when a system prompt is present, bare prompt otherwise
    - `_build_multimodal_prompt`: when non-empty, prepend `{"role": "system", "content": [{"type": "text", "text": system_prompt}]}` ahead of the user entry before `apply_chat_template`, for both single-image and two-image forms; prepend the `<|im_start|>system\n{system}<|im_end|>\n` block to both Qwen VL fallback forms, leaving vision placeholder tokens and the remainder unchanged
    - Absent/empty system prompt ⇒ messages, fallback strings, and engine prompts byte-identical to today
    - _Requirements: 8.3, 8.4, 8.5, 8.7_

  - [x] 5.5 Write unit tests for the LLM/VLM system_prompt path
    - Invoker request body with and without a System_Prompt, and anomaly mode combined with a System_Prompt (captured-body fake)
    - VLM_Runtime message construction with and without a System_Prompt, single- and two-image forms (fake engine/tokenizer patterns)
    - Fallback prompt form incorporating the System_Prompt when no Chat_Template exists
    - Text-only engine prompt for a System_Prompt request carrying no image
    - Endpoint treatment of empty-string `system_prompt` as absent, and validation rejection of a non-string `system_prompt` with no runtime invocation
    - _Requirements: 8.10_

  - [ ]* 5.6 Write property test for LLM request body verbatim carriage
    - **Property 8: LLM request body carries the system prompt verbatim**
    - **Validates: Requirements 8.2**
    - New module `test/backend-test/workflow_engine/test_property_llm_body_verbatim.py`; Hypothesis over non-empty system prompts including `{`/`}` characters; `system_prompt` field verbatim, all other body fields equal the no-system invocation

  - [ ]* 5.7 Write property test for LLM absent/empty invariance
    - **Property 9: LLM absent/empty invariance at every layer**
    - **Validates: Requirements 8.5, 8.8**
    - New module `test/backend-test/workflow_engine/test_property_llm_absent_invariance.py`; Hypothesis over absent/empty/whitespace-only values across invoker body, endpoint treatment, and runtime messages/fallback/engine prompts

  - [ ]* 5.8 Write property test for system-before-user ordering
    - **Property 10: System text precedes user text at every prompt-construction site**
    - **Validates: Requirements 8.3, 8.4, 8.7**
    - New module `test/backend-test/vllm_runtime/test_property_system_prompt_ordering.py`; Hypothesis over non-empty system and user prompts across the chat-template messages (single- and two-image), the fallback forms (placeholders unchanged), and the text-only engine prompt

  - [ ]* 5.9 Write property test for non-string rejection
    - **Property 11: Non-string system_prompt is rejected before generation**
    - **Validates: Requirements 8.6**
    - New module `test/backend-test/endpoints/test_property_system_prompt_validation.py`; Hypothesis over non-string JSON values (numbers, booleans, arrays, objects); finding names the field with a reason, 422 response, runtime generate never invoked

  - [ ]* 5.10 Write property test for LLM anomaly-mode isolation
    - **Property 12: LLM anomaly mode touches only the user prompt**
    - **Validates: Requirements 8.9**
    - New module `test/backend-test/workflow_engine/test_property_llm_anomaly_system.py`; Hypothesis over rendered user prompts and system prompts with anomaly mode enabled; instruction appended to the user prompt only, system prompt verbatim

- [x] 6. Checkpoint - Ensure all tests pass
  - Run the backend test suite via its standard pytest invocation; ensure all tests pass, ask the user if questions arise.

- [x] 7. Verification runbook assets
  - [x] 7.1 Create the runbook handler script
    - Create `.kiro/specs/json-trigger-metadata-pipeline/runbook/handler.py` with the dual-path `produce_frame` handler, identical in logic to the integration test handler from task 1.1 (base64 wins, URI via `dda_frames.load_image`, error messages naming the failing field)
    - _Requirements: 6.2_

  - [x] 7.2 Create the runbook workflow definition
    - Create `.kiro/specs/json-trigger-metadata-pipeline/runbook/workflow.json`: `mqtt_subscribe` (topic `dda/verify/json-trigger/request`, qos 1, Greengrass target) → `custom_python_source` (handler inline) → `model_inference` (placeholder model id with substitution note) → `metadata` (mappings `correlation_id → correlation_id`, `station.line → line`) → `mqtt_publish` (topic `dda/verify/json-trigger/result`, Greengrass target); must import through the backend workflow import mechanism unmodified
    - _Requirements: 6.1, 6.3_

  - [x] 7.3 Write the verification runbook document
    - Create `.kiro/specs/json-trigger-metadata-pipeline/runbook.md` covering: Greengrass_Transport and exact topic strings; expected Trigger_Payload schema (`image_uri`, `image_b64`, `correlation_id`, `station.line`); prerequisites including the `aws.greengrass.ipc.mqttproxy` accessControl for `aws.greengrass#SubscribeToIoTCore` and `aws.greengrass#PublishToIoTCore` on the trigger and result topics; numbered procedure (record pre-test restart count, import and deploy, publish one URI-path and one base64-path Trigger_Payload, observe Output_Message within 60 seconds, confirm inference results and matching correlation metadata) with an observable pass/fail outcome per step; health checks (container running, restart count unchanged, no crash/crash-loop over ≥ 10 minutes after the last execution); and a stage-by-stage diagnostic table for the no-output-within-60-seconds case (trigger subscription, execution start, image extraction, inference, output publication)
    - _Requirements: 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9_

- [x] 8. Final checkpoint - Ensure all tests pass
  - Run the backend test suite via its standard pytest invocation; ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Property tests use Hypothesis, minimum 100 examples each, tagged `Feature: json-trigger-metadata-pipeline, Property N`; each lives in its own test module
- Manual execution of the runbook on real edge hardware (JP5/JP6, Greengrass transport) happens after all coding tasks and before commit, per the workspace `builds.md` on-device verification rule; it is intentionally not a task in this list since it cannot be performed by a coding agent
- The `system_prompt` backward-compatibility invariant (absent/empty ⇒ byte-identical outgoing artifacts) is the central regression guard for groups 4 and 5

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "4.1", "5.3", "7.1"] },
    { "id": 1, "tasks": ["1.2", "4.2", "5.1", "5.4", "7.2"] },
    { "id": 2, "tasks": ["2.1", "4.3", "5.2", "7.3"] },
    { "id": 3, "tasks": ["2.2", "4.4", "4.5", "4.6", "5.5"] },
    { "id": 4, "tasks": ["2.3", "5.6", "5.7", "5.8", "5.9", "5.10"] },
    { "id": 5, "tasks": ["2.4", "2.5", "2.6", "2.7"] }
  ]
}
```
