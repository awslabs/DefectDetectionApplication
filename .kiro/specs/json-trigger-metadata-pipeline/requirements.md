# Requirements Document

## Introduction

This spec verifies end-to-end that metadata from an MQTT JSON trigger payload reaches a Custom Python source node, that the node extracts an image from the payload (by URI reference or embedded base64), passes the resulting frame to the model inference node, and that the inference results plus echoed trigger-payload metadata (such as a correlation ID) are published back out through an MQTT output node.

The workspace already contains the supporting runtime pieces: the MQTT Subscribe trigger builds a Trigger_Context (topic, payload, qos, timestamp) that is persisted and pre-parsed into `payload_json`; the Custom Python source node receives the Trigger_Context in its produce request and can load images via the `dda_frames` helper; the Metadata node resolves trigger-payload field paths for output-node payloads. This spec is a verification effort, delivering two artifacts:

1. An automated integration test that builds the workflow, fires a simulated MQTT JSON trigger, and asserts metadata propagation through the full pipeline.
2. An on-device verification runbook (workflow definition, handler script, step-by-step procedure) for real hardware using the Greengrass IPC / AWS IoT Core transport, satisfying the workspace rule that on-device edge features are verified on real hardware before commit.

In addition to the verification scope above, this spec delivers configurable prompt roles for the two LLM inference nodes: a new optional `system_prompt` node parameter for the Bedrock inference node (`bedrock_inference`), passed as the Converse API `system` parameter alongside the existing `prompt` user message, and a new optional `system_prompt` node parameter for the VLM inference node (`llm_inference`), threaded through the executor invoker and the device Text_Generation_API to the vLLM runtime as a system-role chat message. In both cases an absent or empty system prompt keeps the outgoing requests byte-identical to today's behavior so that existing packaged workflows are unaffected.

## Glossary

- **Pipeline_Workflow**: A workflow definition consisting of an MQTT Subscribe trigger node (`mqtt_subscribe`), a Custom Python source node (`custom_python_source`), a model inference node (`model_inference`), a Metadata node (`metadata`), and an MQTT Publish output node (`mqtt_publish`), connected end to end.
- **Trigger_Context**: The context object built by the MQTT Subscribe trigger runtime containing `topic`, `payload`, `qos`, and `timestamp`, persisted as `workflow_executions.trigger_context_json`, with `payload_json` pre-parsed by `pipeline_executor.load_trigger_context` when the payload is valid JSON.
- **Handler_Script**: The user-authored Python script executed by the Custom Python source node that reads the Trigger_Context from the produce request, extracts an image, and returns a frame plus producer metadata.
- **Python_Bridge**: The `CustomPythonBridge` component (`python_bridge.py`) that delivers the produce request, including the Trigger_Context, to the Handler_Script.
- **DDA_Frames_Helper**: The `dda_frames` helper module available to the Handler_Script providing `load_image` (local disk, S3, or HTTP with allowed URI prefixes), `to_array`, and `to_bytes`.
- **Run_Metadata**: The per-execution metadata store into which the trigger context is seeded under the `trigger` key and producer metadata is merged under `python_source.<nodeId>`.
- **Metadata_Node**: The workflow node (`metadata`, implemented in `output_bindings.py`) that resolves trigger-payload field paths against `trigger["payload_json"]` and attaches the resolved values to output-node payloads.
- **Integration_Test**: The automated test delivered by this spec that constructs the Pipeline_Workflow, fires a simulated MQTT JSON trigger, and asserts metadata and image propagation without requiring real hardware.
- **Verification_Runbook**: The on-device verification deliverable consisting of a Pipeline_Workflow definition, a Handler_Script, and a step-by-step procedure for executing and validating the pipeline on real edge hardware.
- **Trigger_Payload**: The JSON document carried in the triggering MQTT message, containing image acquisition information (an image URI or base64-embedded image data) and correlation metadata (for example, a correlation ID).
- **Correlation_Metadata**: Fields from the Trigger_Payload (for example, a correlation ID) that a downstream consumer uses to match a published result message to the triggering request.
- **Output_Message**: The MQTT message published by the MQTT Publish output node containing inference results and Correlation_Metadata.
- **Greengrass_Transport**: The Greengrass IPC / AWS IoT Core MQTT transport path used in production deployments.
- **Bedrock_Inference_Node**: The workflow node (`bedrock_inference`) that invokes an Amazon Bedrock model via the Converse API, implemented by the executor binding whose default invoker is `_default_bedrock_invoker` in `src/backend/workflow_engine/output_bindings.py`.
- **LLM_Inference_Node**: The workflow node (`llm_inference`, displayed as "VLM/LLM Inference") whose executor binding POSTs an inference request to the device Text_Generation_API.
- **System_Prompt**: The optional text configured via a node's `system_prompt` parameter that sets the model's system-role instructions, carried separately from the User_Prompt.
- **User_Prompt**: The text configured via a node's existing `prompt` parameter that forms the user-role message of the model invocation.
- **Converse_API**: The Amazon Bedrock `converse` client operation, which accepts `modelId`, a `messages` list, an optional top-level `system` parameter (a list of `{"text": ...}` blocks), and `inferenceConfig`.
- **Text_Generation_API**: The device HTTP endpoint `/text-generation/{model_name}/generate` (`src/backend/endpoints/text_generation.py`) that validates the inference request body and forwards it to the VLM_Runtime.
- **VLM_Runtime**: The vLLM runtime manager (`src/backend/vllm_runtime/manager.py`) that builds the chat messages for a generation request, applying the model tokenizer's Chat_Template when available and otherwise falling back to a literal Qwen VL prompt form in `_build_multimodal_prompt`.
- **Chat_Template**: The model tokenizer's chat template applied via `apply_chat_template` to render role-tagged messages into the final model prompt.
- **Node_Catalog**: The workflow_core catalog of NodeTypeDescriptors (`src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`) from which the workflow designer UI renders each node's configurable parameters.
- **Anomaly_Mode_Instruction**: The `BEDROCK_JSON_INSTRUCTION` text that the workflow executor appends to the User_Prompt when a node's anomaly mode is enabled, constraining the model to a parseable JSON answer shape.
- **Backend_Test_Suite**: The existing backend unit test suite under `test/backend-test/`, executed via its standard pytest invocation without real hardware.

## Requirements

### Requirement 1: Trigger metadata reaches the Custom Python source node

**User Story:** As a workflow developer, I want an automated test proving that JSON trigger payload metadata is delivered to the Custom Python source node, so that I can trust the trigger-to-source metadata path without manual inspection.

#### Acceptance Criteria

1. THE Integration_Test SHALL construct a Pipeline_Workflow containing an MQTT Subscribe trigger node, a Custom Python source node, a model inference node, a Metadata_Node, and an MQTT Publish output node, connected end to end in that order.
2. WHEN the Integration_Test fires a simulated MQTT message carrying a Trigger_Payload, THE Integration_Test SHALL assert that the Trigger_Context delivered to the Handler_Script produce request contains `topic`, `payload`, and `qos` fields whose values equal the simulated MQTT message's topic, payload, and qos, and a non-empty `timestamp` field.
3. WHEN the Trigger_Payload is valid JSON, THE Integration_Test SHALL assert that the Trigger_Context delivered to the Handler_Script contains a `payload_json` field whose content equals the parsed Trigger_Payload.
4. IF the Trigger_Payload is not valid JSON, THEN THE Integration_Test SHALL assert that the Trigger_Context delivered to the Handler_Script contains a `payload_json` field whose value is None.
5. WHEN the Pipeline_Workflow executes, THE Integration_Test SHALL assert that the value stored in Run_Metadata under the `trigger` key equals the Trigger_Context delivered to the Handler_Script, including the `payload_json` field.
6. WHEN the Handler_Script returns producer metadata, THE Integration_Test SHALL assert that the value stored in Run_Metadata under the `python_source.<nodeId>` key for the Custom Python source node equals the producer metadata returned by the Handler_Script.

### Requirement 2: Image extraction from a URI reference in the trigger payload

**User Story:** As a workflow developer, I want the Handler_Script to fetch an image referenced by URI in the trigger payload, so that triggers can carry lightweight image references instead of embedded image data.

#### Acceptance Criteria

1. WHEN the Trigger_Payload contains an image URI field referencing a local disk, S3, or HTTP(S) source, THE Handler_Script SHALL load the referenced image using the DDA_Frames_Helper `load_image` function and return the loaded image as the produced frame.
2. WHEN the Handler_Script loads an image from a URI, THE Integration_Test SHALL assert that the frame delivered to the model inference node is pixel-for-pixel identical (equal dimensions and equal pixel values) to the decoded source image referenced by the URI.
3. IF the image URI does not match an allowed URI prefix, THEN THE Handler_Script SHALL report an image acquisition error that identifies the rejected URI and indicates the prefix restriction, and THE Pipeline_Workflow SHALL record the execution as failed.
4. IF the image referenced by the URI cannot be loaded (the source is missing or unreachable, the fetch fails, or the fetched content cannot be decoded as an image), THEN THE Handler_Script SHALL report an image acquisition error identifying the source URI, and THE Pipeline_Workflow SHALL record the execution as failed without delivering a frame to the model inference node.
5. IF the image URI references an HTTP source that accepts the connection but does not respond, THEN THE Handler_Script SHALL report an image acquisition error identifying the URI within the DDA_Frames_Helper HTTP timeout bound rather than blocking the execution indefinitely.

### Requirement 3: Image extraction from base64-embedded data in the trigger payload

**User Story:** As a workflow developer, I want the Handler_Script to decode a base64-embedded image from the trigger payload, so that triggers can be self-contained and require no external image storage.

#### Acceptance Criteria

1. WHEN the Trigger_Payload contains a base64-encoded image field, THE Handler_Script SHALL base64-decode the field into image bytes, decode those bytes into a frame, and return that frame as the produce result for the model inference node.
2. WHEN the Handler_Script decodes a base64-embedded image, THE Integration_Test SHALL assert that the frame delivered to the model inference node has pixel dimensions and pixel values equal to those obtained by decoding the original image bytes that were base64-encoded into the Trigger_Payload.
3. IF the base64 image field is not decodable as base64, decodes to zero bytes, or the decoded bytes cannot be decoded into an image, THEN THE Handler_Script SHALL report an image acquisition error identifying the base64 image field as the failure source, SHALL return no frame to the model inference node, and THE Pipeline_Workflow SHALL record the execution as failed.
4. WHEN the Trigger_Payload contains both an image URI field and a base64-encoded image field, THE Handler_Script SHALL produce the frame from the base64-encoded image field and SHALL NOT load the image URI.
5. IF the Trigger_Payload contains neither an image URI field nor a base64-encoded image field, THEN THE Handler_Script SHALL report an image acquisition error indicating that no image source is present in the Trigger_Payload, and THE Pipeline_Workflow SHALL record the execution as failed.

### Requirement 4: Published output contains inference results and echoed trigger metadata

**User Story:** As a downstream consumer, I want the published MQTT result message to contain inference results plus echoed trigger metadata such as a correlation ID, so that I can match each result to the request that produced it.

#### Acceptance Criteria

1. WHEN the Pipeline_Workflow completes an execution, THE Integration_Test SHALL assert that the Output_Message payload contains the inference results produced by the model inference node for the extracted frame.
2. WHEN the Trigger_Payload contains Correlation_Metadata, THE Metadata_Node SHALL resolve each configured Correlation_Metadata field path against `trigger["payload_json"]` and attach each resolved value as a top-level entry in the Output_Message JSON payload.
3. WHEN the Pipeline_Workflow completes an execution, THE Integration_Test SHALL assert that, for each Correlation_Metadata field path configured on the Metadata_Node, the corresponding value in the Output_Message equals the value at that field path in the Trigger_Payload.
4. IF a Correlation_Metadata field path is absent from the Trigger_Payload, THEN THE Metadata_Node SHALL omit the corresponding key from the Output_Message, log a message identifying the unresolved field path, and continue the execution without failing it, and THE Integration_Test SHALL assert that the omitted key is absent from the Output_Message while the inference results and the remaining resolved Correlation_Metadata entries are still present.
5. WHEN a Correlation_Metadata field path resolves to a JSON null value in the Trigger_Payload, THE Metadata_Node SHALL attach the key with a null value to the Output_Message, distinguishable from an omitted key.
6. IF an attached Correlation_Metadata key name collides with a key already present in the Output_Message workflow-result payload, THEN THE Output_Message SHALL retain the workflow-result value for that key.

### Requirement 5: Automated integration test execution

**User Story:** As a maintainer, I want the integration test to run in the standard test environment without real hardware or a live MQTT broker, so that the metadata pipeline is verified continuously.

#### Acceptance Criteria

1. WHEN the existing backend test suite is executed via its standard pytest invocation, THE Integration_Test SHALL be discovered and executed without requiring additional configuration, command-line options, or environment variables beyond those required by the suite itself.
2. THE Integration_Test SHALL deliver the simulated MQTT trigger in-process, without requiring a physical edge device, a live MQTT broker connection, GStreamer pipeline execution, or any outbound network connection.
3. THE Integration_Test SHALL include at least one test case that executes the Pipeline_Workflow end to end using URI reference loading and at least one test case that executes the Pipeline_Workflow end to end using base64-embedded decoding, with each test case evaluating the propagation assertions defined in Requirements 1 through 4 that apply to its image acquisition path.
4. THE Integration_Test SHALL include test cases exercising the error-handling behaviors defined in Requirements 2, 3, and 4: a rejected URI prefix, an unloadable URI-referenced image, an undecodable base64 image field, and an absent Correlation_Metadata field path.
5. IF a propagation assertion fails, THEN THE Integration_Test SHALL produce a test failure whose failure message identifies which pipeline stage (trigger context delivery, image extraction, inference input, or output publication) failed the assertion.

### Requirement 6: On-device verification runbook

**User Story:** As a release owner, I want a step-by-step on-device verification runbook with a ready-to-use workflow definition and handler script, so that the pipeline can be verified on real hardware over the production MQTT transport before commit.

#### Acceptance Criteria

1. THE Verification_Runbook SHALL include a Pipeline_Workflow definition that imports on a real edge device through the backend workflow import mechanism without modification.
2. THE Verification_Runbook SHALL include a Handler_Script that extracts an image from the Trigger_Payload supporting both the URI reference path and the base64-embedded path.
3. THE Verification_Runbook SHALL specify the Greengrass_Transport as the MQTT transport for both the trigger subscription and the result publication, and SHALL state the exact MQTT topic strings used for the trigger subscription and for the Output_Message publication.
4. THE Verification_Runbook SHALL provide step-by-step instructions covering: deploying the Pipeline_Workflow, publishing one test Trigger_Payload for the URI reference path and one for the base64-embedded path, observing the Output_Message within 60 seconds of each publication, and confirming that the Output_Message contains inference results and Correlation_Metadata matching the Trigger_Payload.
5. THE Verification_Runbook SHALL include verification steps confirming that the backend remains healthy during and after the pipeline execution, defined as: the backend container is running, its restart count is unchanged from its pre-test value, and no crash or crash-loop occurs over a sustained observation window of at least 10 minutes after the last execution.
6. THE Verification_Runbook SHALL specify the expected Trigger_Payload schema, including the image URI field, the base64 image field, and the Correlation_Metadata fields.
7. THE Verification_Runbook SHALL list the prerequisites for the procedure, including the `aws.greengrass.ipc.mqttproxy` accessControl authorization for `aws.greengrass#SubscribeToIoTCore` and `aws.greengrass#PublishToIoTCore` covering the trigger and result topics in the deployed component configuration.
8. THE Verification_Runbook SHALL state an observable pass/fail outcome for each procedure step.
9. IF no Output_Message is observed within 60 seconds of publishing a test Trigger_Payload, THEN THE Verification_Runbook SHALL provide diagnostic steps identifying which pipeline stage (trigger subscription, execution start, image extraction, inference, or output publication) failed.

### Requirement 7: Configurable system prompt and user prompt for the Bedrock inference node

**User Story:** As a workflow developer, I want to set both a system prompt and a user prompt on the Bedrock_Inference_Node, so that I can steer the model's role and behavior independently of the per-request user message.

#### Acceptance Criteria

1. THE Node_Catalog SHALL declare a `system_prompt` parameter of type string, with `required` set to false, on the Bedrock_Inference_Node descriptor, in addition to the existing required `prompt` parameter, such that workflow documents omitting `system_prompt` remain valid against the descriptor.
2. WHEN a Bedrock_Inference_Node executes with a `system_prompt` parameter containing non-empty text (at least one non-whitespace character), THE Bedrock_Inference_Node SHALL pass the System_Prompt as the Converse_API top-level `system` parameter (a list containing exactly one `{"text": <System_Prompt>}` block), SHALL pass the User_Prompt as the user-role message text content, and SHALL keep every other Converse_API argument (modelId, the user-role message content including the labeled image blocks, and inferenceConfig) identical to the invocation produced when no System_Prompt is configured.
3. WHEN a Bedrock_Inference_Node executes with the `system_prompt` parameter absent or containing empty text (a string that is empty or consists only of whitespace characters), THE Bedrock_Inference_Node SHALL issue a Converse_API invocation whose arguments are byte-identical to the invocation produced before this feature, with no `system` parameter present in the invocation arguments.
4. WHILE anomaly mode is enabled on a Bedrock_Inference_Node, WHEN the Bedrock_Inference_Node executes, THE Bedrock_Inference_Node SHALL append the Anomaly_Mode_Instruction to the end of the User_Prompt user-role message text and SHALL NOT modify, append to, or replace the System_Prompt, regardless of whether a System_Prompt is configured.
5. THE Backend_Test_Suite SHALL include unit tests asserting the complete Converse_API invocation arguments for each of the following cases: non-empty System_Prompt with User_Prompt, absent `system_prompt` parameter, `system_prompt` parameter containing empty text (including a whitespace-only value), and anomaly mode enabled combined with a non-empty System_Prompt.

### Requirement 8: Configurable system prompt for the VLM inference node

**User Story:** As a workflow developer, I want to set a system prompt on the LLM_Inference_Node, so that the on-device VLM receives system-role instructions ahead of the user message.

#### Acceptance Criteria

1. THE Node_Catalog SHALL declare an optional `system_prompt` parameter on the LLM_Inference_Node descriptor, in addition to the existing `prompt` parameter.
2. WHEN an LLM_Inference_Node executes with a `system_prompt` parameter containing non-empty text, THE LLM_Inference_Node SHALL include the System_Prompt text verbatim (without `{placeholder}` rendering) as the `system_prompt` field in the Text_Generation_API request body alongside the existing fields (`prompt`, `max_tokens`, `temperature`, `top_p`, `image`, `reference_image`).
3. WHEN the Text_Generation_API receives a request containing a non-empty System_Prompt and an image, THE VLM_Runtime SHALL build the chat messages with a `{"role": "system"}` entry carrying the System_Prompt text placed ahead of the `{"role": "user"}` entry before applying the Chat_Template, for both the single-image and the two-image (reference) request forms.
4. IF the model tokenizer provides no Chat_Template, or applying the Chat_Template fails, THEN THE VLM_Runtime SHALL incorporate the System_Prompt text into the fallback prompt form (both the single-image and the two-image reference forms) ahead of the user content, leaving the image placeholder tokens and the remainder of the fallback form unchanged.
5. WHEN an LLM_Inference_Node executes with the `system_prompt` parameter absent or containing empty text, THE LLM_Inference_Node SHALL issue a Text_Generation_API request byte-identical to the request produced before this feature, and THE VLM_Runtime SHALL build the same messages and fallback prompt as before this feature.
6. IF a Text_Generation_API request contains a `system_prompt` field whose value is not a string, THEN THE Text_Generation_API SHALL reject the request with a validation error response whose findings identify the `system_prompt` field and the reason, and SHALL invoke no generation on the VLM_Runtime.
7. WHEN the Text_Generation_API receives a request containing a non-empty System_Prompt and no `image` field, THE VLM_Runtime SHALL produce a text-only engine prompt containing both the System_Prompt text and the User_Prompt text, with the System_Prompt text placed ahead of the User_Prompt text.
8. WHEN the Text_Generation_API receives a request whose `system_prompt` field contains an empty string, THE Text_Generation_API SHALL process the request as if the `system_prompt` field were absent, and THE VLM_Runtime SHALL build the same messages and fallback prompt as before this feature.
9. WHILE anomaly mode is enabled on an LLM_Inference_Node, THE LLM_Inference_Node SHALL append the Anomaly_Mode_Instruction to the User_Prompt and SHALL leave the System_Prompt unmodified, regardless of whether a System_Prompt is configured.
10. THE Backend_Test_Suite SHALL include unit tests asserting, without real hardware: the Text_Generation_API request body emitted by the LLM_Inference_Node executor invoker with and without a System_Prompt and with anomaly mode combined with a System_Prompt, the VLM_Runtime message construction with and without a System_Prompt, the fallback prompt form incorporating the System_Prompt when no Chat_Template exists, the text-only engine prompt for a System_Prompt request carrying no image, the Text_Generation_API treatment of an empty-string `system_prompt` field as absent, and the Text_Generation_API validation rejection of a non-string `system_prompt`.
