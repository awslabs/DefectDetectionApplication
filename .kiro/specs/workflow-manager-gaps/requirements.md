# Requirements Document

## Introduction

This feature closes three gaps in the Workflow Manager of the Edge CV Portal:

1. **Asynchronous workflow generation (504 fix)**: `POST /workflows/generate` invokes Bedrock Converse synchronously inside the API Gateway request. The Portal API is an edge-optimized REST API whose integration timeout is hard-capped at 29 seconds, while the Workflow_Generator Lambda has a 270-second timeout and a Bedrock client timeout configurable up to 240 seconds. Complex prompts routinely exceed 29 seconds, so API Gateway returns 504 to the client while the Lambda continues and succeeds (observed: a 38.8-second successful generation whose result the client never received). Generation must move to an asynchronous submit/poll pattern while preserving all existing generation semantics (Generation_Gate, session persistence rules, error envelopes, RBAC, chat follow-up flow, and the designer chat panel UX).

2. **Workflow display-name rename**: Users cannot rename a workflow's display name after creation without also saving a new definition version (`PUT /workflows/{id}` requires `definition` and always allocates a new version). A metadata-only rename must be added to the workflows API and surfaced in the Portal UI. The workflow ID stays stable; only the display name changes, so deployments, versions, and packaged components are unaffected.

3. **Custom ID / JSON metadata passthrough**: In trigger-driven workflows (for example an MQTT trigger on topic `swagfactory/invoke` carrying a job ID and file path), users need correlation data from the trigger payload carried through the pipeline and attached to results at output nodes (for example the MQTT output to `swagfactory/quality` includes the original job ID). A metadata mechanism (a post-process/metadata node or equivalent) must let users map fields from the Trigger_Context payload and attach arbitrary optional JSON, so outputs carry results plus the original correlation ID plus optional user JSON. This must work across the workflow_core catalog/validator/compiler, the workflow designer canvas, and the edge runtime including the trigger runtime that already parses trigger payloads.

Implementation lands on branch `spec/workflow-manager-gaps` (off `integration/all-specs`).

## Glossary

- **Portal**: The edge-cv-portal cloud web application (React frontend, Lambda backend, DynamoDB/S3 storage) used to manage DDA use cases, models, workflows, deployments, and devices.
- **Portal_API**: The edge-optimized API Gateway REST API fronting the Portal backend Lambdas (edge-cv-portal/infrastructure/lib/api-gateway-stack.ts). Its integration timeout is hard-capped at 29 seconds.
- **Workflow_Generator**: The Portal backend Lambda (edge-cv-portal/backend/functions/workflow_generator.py) that invokes a configured Amazon Bedrock model via the Converse API to produce a Workflow_Definition from a natural-language prompt. Lambda timeout 270 seconds; Bedrock client timeout configurable up to 240 seconds.
- **Generation_Job**: A single asynchronous workflow-generation execution introduced by this feature, identified by a Job_ID, with a lifecycle status and, on completion, a stored Generation_Result or error.
- **Job_ID**: The unique identifier of a Generation_Job, returned by the submit endpoint and used by the client to poll status.
- **Generation_Result**: The payload produced by a completed Generation_Job: the generated Workflow_Definition, the complete Workflow_Validator findings list, and the chat session identifier, in the same shape the existing synchronous endpoint returns today.
- **Generation_Gate**: The existing classification/decision logic every generated Workflow_Definition passes before it is returned or persisted: accept, repair (one Repair_Pass at most), or reject with 422 GENERATION_REJECTED / GENERATION_VALIDATION_INCOMPLETE. Session persistence happens only on accept paths.
- **Chat_Session**: The existing generation chat session record in the WorkflowChatSessions DynamoDB table (TTL 24 hours), holding message history and the current canvas Workflow_Definition snapshot; follow-up prompts modify rather than regenerate.
- **Chat_Panel**: The chat UI in the Portal workflow designer (edge-cv-portal/frontend/src/pages/workflows/) through which users submit generation and follow-up prompts and see generated results rendered on the canvas.
- **Error_Envelope**: The existing Portal error response shape `{"error": {"code", "message", "details"}}` with codes such as GENERATION_TIMEOUT, GENERATION_REJECTED, GENERATION_VALIDATION_INCOMPLETE, and INVALID_TEMPERATURE.
- **Workflows_API**: The Portal backend Lambda serving workflow CRUD (edge-cv-portal/backend/functions/workflows.py): list/create on /workflows, get/update/delete on /workflows/{id}, duplicate, and versions.
- **Display_Name**: The human-readable `name` attribute of a workflow record. Distinct from the immutable `workflow_id` that deployments, versions, and packaged components reference.
- **Workflow_Definition**: The serializable graph document (nodes, node configurations, connections) that fully describes a workflow, stored as JSON.
- **Node_Type_Catalog**: The shared node catalog in workflow_core (edge-cv-portal/backend/layers/workflow_core) describing every node type's ports, parameters, and defaults, consumed by the designer palette, Workflow_Validator, and Workflow_Compiler.
- **Workflow_Validator**: The workflow_core component that checks a Workflow_Definition for structural and semantic correctness.
- **Workflow_Compiler**: The workflow_core component that translates a valid Workflow_Definition into the compiled pipeline artifacts the Edge_Runtime executes.
- **Edge_Runtime**: The device-side workflow engine (src/backend/workflow_engine) that executes deployed workflows, including its node implementations and executor.
- **Trigger_Runtime**: The device-side trigger activation machinery (src/backend/workflow_engine/trigger_runtime.py) that subscribes to trigger bindings (MQTT/OPC UA), builds a Trigger_Context per firing, and starts runs with the Trigger_Context persisted as `trigger_context_json`.
- **Trigger_Context**: The per-firing context dict the Trigger_Runtime builds from a trigger delivery (for MQTT: `{topic, payload, qos, timestamp}`; for OPC UA: `{endpoint, node_id, value, source_timestamp}`).
- **Metadata_Node**: The node type introduced by this feature that extracts mapped fields from the Trigger_Context payload and attaches them, together with optional user-supplied static JSON, to the data flowing to output nodes.
- **Metadata_Mapping**: A user-configured association on a Metadata_Node from a field path in the trigger payload (for example a JSON pointer or dotted path such as `job_id`) to an output metadata key.
- **Output_Node**: A node type that emits workflow results off the device (for example `mqtt_publish`, `opcua_write`, `modbus_write`, `digital_output`).
- **RBAC**: The Portal's role-based access control; workflow generation requires WORKFLOW_CREATE or WORKFLOW_EDIT, and workflow updates require WORKFLOW_SAVE-level permissions, enforced per Use_Case.
- **Use_Case**: The existing Portal tenancy unit (per-account production line) to which workflows, models, and devices are scoped.

## Requirements

### Requirement 1: Asynchronous generation submission

**User Story:** As a workflow author, I want my generation prompt accepted immediately and processed in the background, so that complex prompts no longer fail with a 504 even though generation actually succeeded.

#### Acceptance Criteria

1. WHEN a client submits a generation request with a valid body, THE Portal_API SHALL respond within the 29-second integration timeout with HTTP 202 containing a Job_ID and the Chat_Session identifier.
2. WHEN a generation request is accepted, THE Workflow_Generator SHALL continue the Bedrock invocation, Generation_Gate evaluation, and result preparation in a background execution decoupled from the submitting HTTP request, such that a generation whose processing exceeds the 29-second integration timeout still reaches a terminal state retrievable via the status endpoint.
3. WHEN a generation request body fails synchronous validation (missing required fields, INVALID_TEMPERATURE, unknown or inaccessible usecase_id), THE Workflow_Generator SHALL reject the submission synchronously with the existing Error_Envelope and status code, and SHALL create no Generation_Job.
4. WHEN a user without WORKFLOW_CREATE or WORKFLOW_EDIT permission submits a generation request, THE Workflow_Generator SHALL reject the submission synchronously with the existing 403 RBAC Error_Envelope, SHALL create no Generation_Job, and SHALL start no background processing.
5. WHEN a generation request includes a session_id for an existing Chat_Session, THE Workflow_Generator SHALL process the prompt as a follow-up modification of the session's current canvas snapshot, preserving the existing follow-up/modification semantics.
6. WHEN a generation request includes a session_id that does not reference an existing Chat_Session (for example one that has expired), THE Workflow_Generator SHALL process the prompt with the same follow-up semantics as if the session existed, using the client-provided current_definition as the canvas snapshot, or an empty canvas snapshot IF the request contains no current_definition.
7. WHEN a generation request with a valid body contains no session_id, THE Workflow_Generator SHALL create a new Chat_Session and return that session's identifier in the HTTP 202 response.
8. WHEN the Portal_API returns HTTP 202 for a generation request, THE Workflow_Generator SHALL have created the referenced Generation_Job in the pending or running state, such that a status request for the returned Job_ID issued immediately after the 202 response returns that Generation_Job's state rather than a 404 Error_Envelope.

### Requirement 2: Generation job status and result retrieval

**User Story:** As a workflow author, I want to poll the status of my generation job, so that I receive the generated definition and validator findings when they are ready, however long generation takes.

#### Acceptance Criteria

1. THE Workflow_Generator SHALL expose a status endpoint that responds within the Portal_API 29-second integration timeout with the state of a Generation_Job identified by Job_ID, where the state is one of: pending, running, succeeded, failed.
2. WHEN a Generation_Job has succeeded, THE status endpoint SHALL return the Generation_Result containing the generated Workflow_Definition, the complete Workflow_Validator findings list, and the Chat_Session identifier, in the same payload shape the synchronous endpoint returns today.
3. WHEN a Generation_Job has failed with a single recorded failure, THE status endpoint SHALL return the originating Error_Envelope (including GENERATION_TIMEOUT, GENERATION_REJECTED, and GENERATION_VALIDATION_INCOMPLETE) with the same HTTP status code and the same code, message, and details semantics the synchronous endpoint produces today.
4. WHEN a client requests the status of a Job_ID that does not exist or belongs to a Use_Case the requesting user cannot access, THE Workflow_Generator SHALL return a 404 Error_Envelope that does not reveal whether the Job_ID exists; a failed Generation_Job that the requesting user can access SHALL return its Error_Envelope per criterion 3, never the 404.
5. WHEN a user without WORKFLOW_CREATE or WORKFLOW_EDIT permission requests the status of a Generation_Job, THE Workflow_Generator SHALL return the existing 403 RBAC Error_Envelope.
6. THE Workflow_Generator SHALL retain a completed Generation_Job's status and result for at least the Chat_Session TTL window (24 hours today) measured from the moment the Generation_Job reaches a terminal state (succeeded or failed), and SHALL return the identical Generation_Result on repeated status requests for the same succeeded Job_ID and the identical Error_Envelope on repeated status requests for the same failed Job_ID.
7. WHERE the Chat_Session TTL is configured as zero, THE Workflow_Generator SHALL apply a minimum retention duration of zero to completed Generation_Jobs, permitting their removal immediately upon reaching a terminal state.
8. WHILE a Generation_Job is pending or running, THE status endpoint SHALL return HTTP 200 containing the current state and neither a Generation_Result nor a failure Error_Envelope.
9. IF a Generation_Job has both a Generation_Gate rejection and a timeout failure recorded (per Requirement 3), THEN THE status endpoint SHALL return a failed state with an Error_Envelope whose details include both recorded failures.
10. WHEN a client requests the status of a Job_ID whose Generation_Job has been removed after its retention window elapsed, THE Workflow_Generator SHALL return the same 404 Error_Envelope specified in criterion 4.

### Requirement 3: Preservation of generation semantics

**User Story:** As a workflow author, I want asynchronous generation to behave exactly like today's generation except for the transport, so that gate decisions, session state, and error reporting remain trustworthy.

#### Acceptance Criteria

1. THE Workflow_Generator SHALL pass every generated Workflow_Definition through the Generation_Gate before storing it in a Generation_Result, applying the Generation_Gate decision as follows: accept when the gate reports zero Structural_Errors, repair when the gate reports only repairable Structural_Errors, and reject when the gate reports any Unrepairable_Error.
2. IF the Generation_Gate rejects a generated definition, or the Repair_Pass result still contains Structural_Errors, THEN THE Workflow_Generator SHALL record the failure on the Generation_Job with the existing GENERATION_REJECTED Error_Envelope carrying the user-readable structural error details, and SHALL leave the Chat_Session message history and canvas snapshot unchanged.
3. WHEN the Generation_Gate accepts a generated or repaired definition, THE Workflow_Generator SHALL persist the accepted Workflow_Definition as the Chat_Session canvas snapshot and SHALL append the generation exchange to the Chat_Session message history, performing this persistence only after the accept decision and on no other path.
4. IF the Bedrock invocation exceeds the configured Bedrock client timeout, THEN THE Workflow_Generator SHALL record the failure on the Generation_Job with the existing GENERATION_TIMEOUT Error_Envelope stating the applied timeout in seconds, and SHALL leave the Chat_Session message history and canvas snapshot unchanged.
5. IF both a Generation_Gate rejection and a Bedrock timeout occur for the same Generation_Job (for example, the Repair_Pass invocation times out after the first pass produced Structural_Errors), THEN THE Workflow_Generator SHALL record exactly one terminal failed state on the Generation_Job whose Error_Envelope details include both the gate rejection's structural errors and the timeout indication.
6. IF the background execution terminates abnormally (for example the Lambda times out or crashes) without recording a terminal state on the Generation_Job, THEN THE Workflow_Generator SHALL transition the Generation_Job state to failed with an Error_Envelope indicating abnormal termination no later than 60 seconds after the Generation_Job's configured maximum execution duration elapses, so that polling clients observe the failed state rather than a Generation_Job remaining pending or running indefinitely.
7. WHEN the Generation_Gate returns a repair decision, THE Workflow_Generator SHALL execute exactly one Repair_Pass and SHALL pass the Repair_Pass result through the Generation_Gate before storing any Generation_Result.
8. IF the Workflow_Validator raises an exception during any validation pass, THEN THE Workflow_Generator SHALL record the failure on the Generation_Job with the existing GENERATION_VALIDATION_INCOMPLETE Error_Envelope, and SHALL leave the Chat_Session message history and canvas snapshot unchanged.

### Requirement 4: Chat panel asynchronous UX

**User Story:** As a workflow author using the designer chat panel, I want the panel to submit, show progress, and render the result when generation completes, so that long generations feel seamless instead of erroring out.

#### Acceptance Criteria

1. WHEN a user submits a prompt in the Chat_Panel, THE Chat_Panel SHALL submit the generation request, display an in-progress indicator within 1 second of submission, and poll the status endpoint at an interval of no more than 5 seconds until the Generation_Job reaches a terminal state (succeeded or failed).
2. WHEN a Generation_Job succeeds, THE Chat_Panel SHALL remove the in-progress indicator, render the generated Workflow_Definition on the canvas, and display the complete Workflow_Validator findings list from the Generation_Result, preserving the existing review-before-save behavior (the generated definition is never auto-saved or deployed).
3. WHEN a Generation_Job fails, THE Chat_Panel SHALL remove the in-progress indicator, display the message from the Error_Envelope returned by the status endpoint, and retain the submitted prompt text in the Chat_Panel input so the user can resubmit it without retyping.
4. WHILE a Generation_Job for the current Chat_Session is pending or running, THE Chat_Panel SHALL disable prompt submission so that no additional generation request can be submitted from that Chat_Session until the Generation_Job reaches a terminal state.
5. WHEN a user submits a follow-up prompt in an existing Chat_Session after a completed generation, THE Chat_Panel SHALL include the session identifier and the current canvas Workflow_Definition in the request so the existing modification flow is preserved.
6. IF 3 consecutive status poll requests fail (network error or a non-success response that is not a Generation_Job failure Error_Envelope), THEN THE Chat_Panel SHALL stop polling, remove the in-progress indicator, display an error message indicating the generation status could not be retrieved, and retain the submitted prompt text for retry.
7. IF a Generation_Job has not reached a terminal state within 300 seconds of submission, THEN THE Chat_Panel SHALL stop polling, remove the in-progress indicator, display an error message indicating generation did not complete in time, and retain the submitted prompt text for retry.

### Requirement 5: Workflow display-name rename

**User Story:** As a workflow owner, I want to rename a workflow's display name after creation, so that workflow lists stay meaningful without creating a new definition version or breaking references.

#### Acceptance Criteria

1. THE Workflows_API SHALL provide a rename operation that updates a workflow's Display_Name without requiring a Workflow_Definition in the request and without allocating a new workflow version.
2. WHEN a rename request is processed, THE Workflows_API SHALL keep the workflow_id, all existing versions, stored definition documents, packaged components, and deployment references unchanged, permitting only the workflow record's last-updated timestamp (updated_at) to change.
3. IF a rename request contains a Display_Name that is empty, whitespace-only, or longer than 128 characters, THEN THE Workflows_API SHALL return a 400 Error_Envelope and leave the workflow unchanged.
4. IF a user without permission to modify the workflow submits a rename request, THEN THE Workflows_API SHALL return the existing 403 RBAC Error_Envelope and leave the workflow unchanged.
5. IF a rename request targets a workflow_id that does not exist or is not accessible to the requesting user, THEN THE Workflows_API SHALL return a 404 Error_Envelope that does not reveal cross-tenant existence.
6. WHEN a rename succeeds, THE Workflows_API SHALL record an audit event containing the workflow_id, the previous Display_Name, the new Display_Name, and the requesting user's identity.
7. WHEN a rename succeeds, THE Portal frontend SHALL display the new Display_Name in the workflow list and the workflow designer within 2 seconds without a full page reload.
8. THE Portal frontend SHALL provide a rename affordance for a workflow in the workflow designer or workflow list, visible only to users permitted to modify that workflow.
9. IF a rename request fails (400, 403, 404, or network error), THEN THE Portal frontend SHALL display the error message and continue to display the previous Display_Name.

### Requirement 6: Metadata_Node in the node catalog and designer

**User Story:** As a workflow author, I want a node where I map fields from the trigger payload and attach optional JSON, so that my outputs carry the original correlation ID and any extra context I choose.

#### Acceptance Criteria

1. THE Node_Type_Catalog SHALL include a Metadata_Node type whose descriptor declares the node's input and output ports and provides configuration for a list of 0 to 50 Metadata_Mappings (each pairing a trigger-payload field path with an output metadata key) and an optional static JSON object of at most 10,240 characters.
2. WHEN a user places a Metadata_Node on the designer canvas, THE Portal frontend SHALL provide configuration UI for adding, editing, and removing Metadata_Mappings and for entering the optional static JSON object.
3. IF a user enters static JSON that is not parseable as JSON or that parses to a value that is not a JSON object, THEN THE Portal frontend SHALL surface a validation error on the node configuration and SHALL prevent saving that configuration.
4. WHEN the Workflow_Validator checks a Workflow_Definition containing a Metadata_Node, THE Workflow_Validator SHALL verify the node's connections and configuration against the Node_Type_Catalog descriptor, producing a SEVERITY_ERROR finding for each of the following invalid configurations: duplicate output metadata keys across the node's Metadata_Mappings, an empty field path in any Metadata_Mapping, an empty output metadata key in any Metadata_Mapping, more than 50 Metadata_Mappings, and static JSON exceeding 10,240 characters or not parseable as a JSON object; and producing no findings from these checks for a valid Metadata_Node.
5. WHEN the Workflow_Validator checks a Workflow_Definition that has no trigger node and contains one or more Metadata_Nodes each having at least one Metadata_Mapping, THE Workflow_Validator SHALL produce exactly one SEVERITY_WARNING finding per such Metadata_Node indicating that trigger-payload Metadata_Mappings will have no source at runtime, and SHALL produce no such warning for a Metadata_Node configured with only static JSON and no Metadata_Mappings.
6. WHEN the Workflow_Compiler compiles a valid Workflow_Definition containing a Metadata_Node, THE Workflow_Compiler SHALL emit every Metadata_Mapping (both its trigger-payload field path and its output metadata key) and the complete static JSON object from the node's configuration into the compiled pipeline artifacts consumed by the Edge_Runtime, with no mapping or static JSON content omitted or altered.
7. WHEN a user attempts to save a Metadata_Node configuration containing duplicate output metadata keys, an empty field path, or an empty output metadata key, THE Portal frontend SHALL surface a validation error on the node configuration before save.

### Requirement 7: Metadata passthrough at the edge runtime

**User Story:** As a plant integrator, I want the job ID my MQTT trigger message carries to come back attached to the quality results my workflow publishes, so that I can correlate each result with the originating job.

#### Acceptance Criteria

1. WHEN a trigger-driven run starts, THE Edge_Runtime SHALL make the run's Trigger_Context, as persisted by the Trigger_Runtime in `trigger_context_json`, available to every Metadata_Node execution within that run.
2. WHEN a Metadata_Node executes in a run with a Trigger_Context whose payload parses as a JSON document, THE Edge_Runtime SHALL resolve each Metadata_Mapping's field path against the parsed payload and attach each resolved value, including a resolved JSON null, under the mapped output metadata key.
3. WHEN a Metadata_Mapping's field path does not resolve in the parsed trigger payload (including when the parsed payload is not a JSON object and the field path cannot be resolved against it), THE Edge_Runtime SHALL omit that output metadata key from the attached metadata, log the unresolved field path, and continue the run to completion.
4. IF the Trigger_Context payload is not parseable as JSON, THEN THE Edge_Runtime SHALL attach only the static JSON metadata when configured (or empty metadata when none is configured), log the parse failure, and continue the run to completion.
5. WHEN a Metadata_Node with a configured static JSON object executes, THE Edge_Runtime SHALL attach every top-level entry of the static JSON object to the output metadata alongside any resolved Metadata_Mappings.
6. IF a static JSON entry's key equals the output metadata key of a Metadata_Mapping that resolved, THEN THE Edge_Runtime SHALL attach the resolved Metadata_Mapping value under that key and log the key collision.
7. WHEN an Output_Node whose input data flowed through a Metadata_Node emits a result, THE Edge_Runtime SHALL include every attached metadata entry in the emitted result payload alongside, and without altering or replacing, the workflow result values (for example the MQTT publish to `swagfactory/quality` includes the original job ID from the `swagfactory/invoke` trigger payload).
8. WHEN an Output_Node whose input data did not flow through any Metadata_Node emits a result in a workflow containing a Metadata_Node, THE Edge_Runtime SHALL emit that result payload with no metadata entries attached, identical in shape to the payload emitted before this feature.
9. WHEN a workflow containing a Metadata_Node runs without a Trigger_Context (for example a manual run), THE Edge_Runtime SHALL attach the static JSON metadata when configured, or empty metadata when none is configured, and complete the run without error.

### Requirement 8: Compatibility and regression safety

**User Story:** As a Portal operator, I want these changes to leave existing workflows, generations, and deployments working unchanged, so that adopting the new capabilities carries no migration cost.

#### Acceptance Criteria

1. WHEN an existing Workflow_Definition without a Metadata_Node is parsed, validated, compiled, packaged, or executed, THE Portal and Edge_Runtime SHALL produce the same validation findings, the same compiled pipeline behavior, and the same run outputs as before this feature, with no new findings or behavior changes attributable to this feature.
2. WHEN a workflow is renamed, THE Portal SHALL resolve all existing deployments, packaged components, and version history for that workflow_id exactly as before the rename, with only the Display_Name differing.
3. WHEN the existing update operation on /workflows/{id} is invoked with a definition and a name, THE Workflows_API SHALL preserve its current request validation, response shape, and RBAC enforcement while saving a new version and updating the name.
4. WHEN a Metadata_Node definition round-trips through the workflow_core layer's serializer (parse, serialize, parse), THE workflow_core serializer SHALL produce a semantically equivalent Workflow_Definition (same nodes, connections, and configuration meaning; exact structural identity is not required).
5. WHEN a client submits to the generation submit path after this feature, THE Portal_API SHALL return either the HTTP 202 asynchronous response or a synchronous Error_Envelope, and SHALL never return a 504 caused by generation duration.
6. THE feature SHALL require no migration of pre-existing workflows, versions, packaged components, or deployments (no re-save, re-package, or re-deploy) for them to keep functioning after deployment of this feature.
