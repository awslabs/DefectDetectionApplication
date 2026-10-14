# Requirements Document

## Introduction

Workflows that judge parts with a multimodal model — the `bedrock_inference` node (Amazon Bedrock, Converse API) and the `llm_inference` node (the device-local vLLM Text_Generation_API) — run in **anomaly mode**: the executor appends the canonical JSON instruction to the operator's prompt, sends the captured input frame (or Detection_Crop) and the optional reference frame, and parses `{is_anomalous, confidence}` out of the answer. That verdict drives every downstream gate (conditionals, Modbus coils, MQTT results, the HMI).

Today the prompt is written blind and can only be judged by watching live runs. The blue-plate deployment on adlink-dlap-701 showed how expensive that is: the deployed prompt returned `is_anomalous: true` for every plate for a day, and the causes were only found by pulling the exact images the executor had sent to Bedrock off the device, re-pairing them by hand, labelling them, and replaying candidate prompts through a byte-identical copy of the executor's Converse call. That offline loop scored the deployed prompt at 0/40 on correctly matching plates and a rewritten prompt at 131/134, and along the way exposed two defects no prompt could fix (a detection-order mismatch pairing crops with the wrong reference, and a slot outside the camera's field of view).

This feature turns that loop into a Portal capability. It introduces a new top-level Portal section, **Workflow Tuning**, whose first sub-item is **VLM/LLM Anomaly Tuning** (the section is designed to hold further workflow-analysis tools later). From there — or from the loaded workflow in the designer — a Workflow_Author can, for any anomaly-mode Bedrock or VLM node:

1. **Collect** the real inputs the node sent on production runs. Devices export, at invocation time, the exact input image, the exact reference image and the recorded verdict for every anomaly-mode invocation to the Use_Case's S3 bucket, so the Portal works from what the model really saw.
2. **Label** each pair as OK (must pass) or NOK (must fail), optionally generating wrong-reference negatives by cross-pairing sibling nodes of the same run.
3. **Score** candidate prompts against the labelled set by replaying the **same invocation construction the executor uses** — for Bedrock nodes directly from the Portal, for VLM nodes as a job the device executes against its local model — with the current prompt always present as the baseline.
4. **Compare** candidates on accuracy, false passes, false fails, parse failures, verdict stability and cost, down to the per-sample disagreement.
5. **Apply** the winning prompt as a new version of the Workflow_Definition, changing nothing else, from where the normal validate → package → deploy flow takes over.

Faithfulness is obtained structurally: the request construction and verdict parsing move into the shared `workflow_core` package (authored in the Portal layer, vendored to the device), so the executor at run time, the Portal's Bedrock scorer and the device's VLM job runner all build invocations with the same code. Existing run behaviour, artifacts and metadata are preserved unchanged; devices without the feature enabled export nothing.

## Glossary

- **Portal**: The edge-cv-portal web application (React frontend, Lambda backend, CDK infrastructure) where Workflow_Definitions are authored, versioned, validated, packaged and deployed.
- **Workflow_Tuning_Section**: The new top-level Portal navigation entry "Workflow Tuning", a group of workflow-analysis tools of which Anomaly_Tuning is the first.
- **Anomaly_Tuning**: The "VLM/LLM Anomaly Tuning" tool under the Workflow_Tuning_Section — this feature.
- **LocalServer**: The on-device DDA backend (`src/backend`) that runs deployed workflows.
- **Workflow_Definition**: The versioned graph document (`nodes`, `connections`) stored by the Portal; every save creates a new immutable version.
- **Use_Case**: The Portal tenant construct; workflows, devices, buckets and tuning state are scoped to a Use_Case.
- **Workflow_Author**: A Portal user holding `workflow:read` (view), `workflow:edit` (label, author, score) and/or `workflow:save` (apply) on the workflow's Use_Case.
- **Inspection_Node**: A `bedrock_inference` node or an `llm_inference` node of a Workflow_Definition.
- **Anomaly_Mode**: The Inspection_Node setting under which the executor appends the Verdict_Instruction and parses a verdict: `anomaly_mode` absent or true for `bedrock_inference`; `anomaly_mode` true for `llm_inference`.
- **Tunable_Node**: An Inspection_Node in Anomaly_Mode.
- **Verdict_Instruction**: The canonical text appended to every Anomaly_Mode prompt: `Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.`
- **Verdict_Parser**: The tolerant parser that extracts `{is_anomalous, confidence}` from a model answer, or fails when no such JSON object is present.
- **Invocation_Builder**: The shared `workflow_core` code that turns node parameters, a Prompt_Set and image bytes into the exact model request (Bedrock Converse content and inference configuration; Text_Generation_API body), and the Verdict_Parser beside it.
- **Prompt_Set**: The tunable parameters of an Inspection_Node: `prompt` (`prompt_template` for `llm_inference`), `system_prompt`, and `max_tokens`.
- **Node_Parameters**: The Inspection_Node's non-tunable invocation parameters: `model`, `region`, `crop_margin_percent` for `bedrock_inference`; `modelName`, `temperature`, `top_p`, `max_image_dimension` for `llm_inference`.
- **Input_Image**: The bytes the executor sent as the node's primary image on an invocation (the Detection_Crop when `crop_detection_index` was used, otherwise the captured `in` frame; downscaled when `max_image_dimension` applied).
- **Reference_Image**: The bytes the executor sent as the node's reference image on an invocation (captured `reference` frame, or the payload-resolved reference), if any.
- **Sample_Export**: The device-side capability that, for every Anomaly_Mode invocation, uploads the Input_Image, Reference_Image, recorded verdict and invocation context to the Sample_Store.
- **Sample_Store**: The S3 prefix `workflow-tuning/samples/` in the Use_Case's inference results bucket where Sample_Exports land.
- **Tuning_Sample**: One exported invocation: `(workflowId, version, executionId, nodeId, device, Input_Image, Reference_Image?, recorded verdict, recorded confidence, recorded raw answer, prompt fingerprint, detection_id?, detection slot, exportedAt, source ∈ {live, backfill})`.
- **Synthetic_Negative**: A Tuning_Sample constructed by pairing one node's Input_Image with the Reference_Image of a different Inspection_Node of the same execution on the same device, pre-labelled NOK.
- **Label**: The Workflow_Author's expected verdict for a Tuning_Sample: `OK`, `NOK` or `EXCLUDE`.
- **Tuning_Session**: The Portal's persisted working set for one `(workflowId, nodeId)`: indexed Tuning_Samples, Labels, Candidates, Score_Runs and the selection.
- **Candidate**: One Prompt_Set under evaluation. The Baseline_Candidate is the Prompt_Set of the Tunable_Node in the latest Workflow_Definition version and is always present and read-only.
- **Score_Run**: One evaluation of one Candidate against the session's labelled, non-excluded Tuning_Samples with a configured number of repeats per sample.
- **Bedrock_Scorer**: The Portal component that executes Score_Runs for `bedrock_inference` nodes by invoking Bedrock from the Portal.
- **Device_Score_Job**: The unit of work through which a Score_Run for an `llm_inference` node is executed on a device against its local model, delivered through the device's named shadow and reported back through the Sample_Store.
- **Sample_Outcome**: The result of replaying one Candidate on one Tuning_Sample once: raw answer, parsed verdict and confidence, output token count, latency, and a category from `{correct, false_pass, false_fail, parse_failure, invocation_error}`.
- **Score_Summary**: The aggregate of a Score_Run: sample count, invocations, accuracy, false passes, false fails, parse failures, invocation errors, verdict instability count, mean and max output tokens, mean latency.

## Requirements

### Requirement 1: Navigation and Entry Points

**User Story:** As a Workflow_Author, I want anomaly tuning to be a first-class Portal tool that I can also reach from the workflow I am editing, so that tuning sits beside the other workflow lifecycle steps rather than in a hidden corner.

#### Acceptance Criteria

1. THE Portal SHALL add a top-level navigation entry "Workflow Tuning" between the Workflows group's "Workflows" entry and "Node Designer", visible to the roles that may edit workflows (DataScientist, UseCaseAdmin, PortalAdmin), containing the sub-entry "VLM/LLM Anomaly Tuning" routed at `/workflow-tuning/anomaly`.
2. WHEN a Workflow_Author opens Anomaly_Tuning, THE Portal SHALL list, for the selected Use_Case, every workflow that has at least one Tunable_Node in its latest version, with each Tunable_Node's id, type, model, and the number of Tuning_Samples available for it across devices.
3. WHILE a workflow with at least one Tunable_Node is loaded in the designer, THE Portal SHALL show a "Tune anomaly prompts" action in the workflow toolbar that opens Anomaly_Tuning with that workflow preselected.
4. WHEN a Tunable_Node is selected in the designer, THE Portal SHALL show on its configuration panel a "Prompt tuning" link that opens the node's Tuning_Session, together with the node's latest applied Tuning_Result summary when one exists.
5. THE Portal SHALL classify an Inspection_Node as a Tunable_Node when it is a `bedrock_inference` node whose `anomaly_mode` parameter is absent or true, or an `llm_inference` node whose `anomaly_mode` parameter is true, and SHALL classify every other node as not tunable.
6. IF the Use_Case has Sample_Export disabled, THEN THE Portal SHALL display in Anomaly_Tuning a message explaining that devices export no samples until the Use_Case enables tuning sample export and is redeployed, with a link to the Use_Case settings.

### Requirement 2: Sample Export from Devices

**User Story:** As a Workflow_Author, I want every anomaly-mode invocation on my devices to leave behind exactly what the model saw and answered, so that tuning is grounded in production images without anyone logging into a device.

#### Acceptance Criteria

1. WHERE a Use_Case has Sample_Export enabled, THE Portal SHALL deliver to LocalServer, as component configuration in every deployment of that Use_Case's devices, the Sample_Store location (bucket and prefix) and the enablement flag.
2. WHILE Sample_Export is enabled on a device, WHEN an Anomaly_Mode invocation of an Inspection_Node completes with an answer, THE LocalServer SHALL enqueue for upload the exact Input_Image bytes and exact Reference_Image bytes that were sent, the raw answer, the parsed verdict and confidence (or the parse failure), the `executionId`, workflow id, registration version, node id, node type, the SHA-256 fingerprint of the Prompt_Set used, the `detection_id` and detection slot when a Detection_Crop was used, and the device thing name.
3. WHEN an enqueued Tuning_Sample is uploaded, THE LocalServer SHALL write it under the Sample_Store as `{prefix}{workflowId}/{nodeId}/{thingName}/{executionId}.json` with sibling objects `.input.jpg` and, when present, `.reference.jpg`, and SHALL write no image bytes into the JSON object.
4. THE LocalServer SHALL perform Sample_Export uploads on a background worker with a bounded queue of 200 entries, SHALL never block or delay an Execution or an invocation on export, and SHALL drop the oldest queued entry with a WARNING log line when the queue is full.
5. IF an upload fails, THEN THE LocalServer SHALL retry it up to 3 times with backoff, SHALL log the final failure with the object key, and SHALL leave the Execution's status, artifacts and Run_Metadata unchanged.
6. WHILE Sample_Export is disabled or unconfigured on a device, THE LocalServer SHALL issue no S3 request and allocate no export queue for tuning.
7. WHEN Sample_Export becomes enabled on a device, THE LocalServer SHALL backfill, once, the Tuning_Samples derivable from existing Run_Artifacts for every Tunable_Node of every registration on the device — the newest 500 Executions per node — using the persisted `original`/`in` and `reference` node frames and the Run_Metadata verdicts, marking each as `source: backfill`, and SHALL skip Executions whose node outcome is an error or whose input frame is missing.
8. THE Portal SHALL grant the device's token-exchange role permission to put and get objects under the Sample_Store prefix of the Use_Case's bucket and no other new permission.
9. THE Portal SHALL apply an S3 lifecycle rule expiring Sample_Store objects after the Use_Case's configured retention (default 30 days, 7–365).
10. THE LocalServer SHALL skip exporting a Tuning_Sample whose Input_Image or Reference_Image exceeds 8 MiB and SHALL log the skip with the execution id.

### Requirement 3: Sample Collection in the Portal

**User Story:** As a Workflow_Author, I want the Portal to gather a node's samples across all my devices and workflow versions into one working set, so that tuning reflects the whole fleet's behaviour.

#### Acceptance Criteria

1. WHEN a Workflow_Author opens or refreshes a Tuning_Session for `(workflowId, nodeId)`, THE Portal SHALL index every Tuning_Sample in the Sample_Store under that workflow id and node id across all devices and versions, bounded to the newest 2000 by `exportedAt`, and SHALL report the count beyond the bound.
2. WHEN indexing, THE Portal SHALL read each sample's JSON object and record its fields with references to its image objects, and SHALL store no image bytes in the index.
3. WHEN two Tuning_Samples carry byte-identical Input_Images (equal content hash recorded by the device), THE Portal SHALL keep both and mark the later one as a duplicate of the earlier so the user can exclude duplicates in one action.
4. WHEN a Tuning_Session is refreshed, THE Portal SHALL add newly exported Tuning_Samples and SHALL retain every existing indexed sample together with its Label.
5. IF a sample's JSON is unreadable or its Input_Image object is missing, THEN THE Portal SHALL omit it from the index and count it in the refresh summary by reason.
6. WHEN a Tuning_Sample's prompt fingerprint differs from the fingerprint of the Baseline_Candidate, THE Portal SHALL flag the sample as recorded under a different prompt, so recorded verdicts are not mistaken for the current prompt's behaviour.
7. IF a Tuning_Sample's image objects have expired from the Sample_Store since indexing, THEN THE Portal SHALL display the sample as unavailable, SHALL keep its Label, and SHALL skip it in Score_Runs with an `invocation_error` outcome naming the missing object.

### Requirement 4: Sample Review and Labelling

**User Story:** As a Workflow_Author, I want to see each input next to its reference with what the node answered, and mark what the right answer is, so that the score reflects my acceptance criteria rather than the model's current behaviour.

#### Acceptance Criteria

1. WHEN a Tuning_Session is open, THE Portal SHALL display each Tuning_Sample as the Input_Image beside its Reference_Image (or a single-image indication), with the recorded verdict and confidence, the recorded raw answer on demand, the device, `executionId`, version, detection slot when present, source (live or backfill), and the current Label.
2. THE Portal SHALL let the user set each Tuning_Sample's Label to `OK`, `NOK` or `EXCLUDE`, individually and for a multi-selection, and SHALL persist each Label change immediately.
3. WHEN a Tuning_Sample has no Label, THE Portal SHALL display it as unlabelled and SHALL treat it as excluded from scoring.
4. THE Portal SHALL offer filters over the samples by Label, recorded verdict, device, version, source, duplicate status, different-prompt flag, and disagreement between the recorded verdict and the Label.
5. WHEN the user enables Synthetic_Negatives for a Tuning_Session, THE Portal SHALL create, for each `OK`-labelled Tuning_Sample whose execution on the same device has at least one other Inspection_Node with an exported Reference_Image, one Synthetic_Negative per such sibling reference, labelled `NOK`, marked synthetic and linked to its source sample; and SHALL create none when the option is disabled.
6. WHEN the user disables Synthetic_Negatives, THE Portal SHALL remove every Synthetic_Negative of the session and SHALL leave every indexed sample and Label unchanged.
7. THE Portal SHALL display the counts of `OK`, `NOK`, `EXCLUDE`, unlabelled and synthetic samples and SHALL warn when either the `OK` or the `NOK` count is zero before a Score_Run is started.
8. THE Portal SHALL serve sample images to the browser through presigned URLs valid for at most 30 minutes, scoped to the Use_Case's bucket.

### Requirement 5: Candidate Authoring

**User Story:** As a Workflow_Author, I want to write several prompt variants against the current one and see exactly what the model will receive, so that I can iterate deliberately instead of guessing what the executor adds.

#### Acceptance Criteria

1. WHEN a Tuning_Session is created or the workflow gains a new latest version, THE Portal SHALL set the Baseline_Candidate to the Prompt_Set of the Tunable_Node in the latest Workflow_Definition version, read-only, and SHALL keep prior Score_Runs of superseded baselines visible as history.
2. THE Portal SHALL let the user create, name, edit, duplicate and delete Candidates, each holding a `prompt` (or `prompt_template`), an optional `system_prompt` and a `max_tokens` value within the node type's catalog bounds, and SHALL persist every change.
3. WHILE a Candidate is being edited, THE Portal SHALL display the exact user-message text the Invocation_Builder will send — the Candidate's prompt followed by the Verdict_Instruction — and the exact system text.
4. IF a Candidate's `system_prompt` or prompt demands an answer format that omits `is_anomalous`, THEN THE Portal SHALL display a warning that the Verdict_Parser requires a JSON object carrying `is_anomalous`, without blocking the Candidate.
5. IF a Candidate's `max_tokens` is below 64, THEN THE Portal SHALL display a warning that truncated answers fail the Verdict_Parser, without blocking the Candidate.
6. THE Portal SHALL offer a starter Candidate for two-image inspection whose prompt instructs the model to describe the reference, describe the input, compare them, list which differences are and are not defects, and answer in one JSON object that includes `is_anomalous` and `confidence`; the starter SHALL be editable and inserted only on the user's action.
7. WHEN a Candidate is deleted, THE Portal SHALL delete its Score_Runs and leave every other Candidate, Score_Run, Tuning_Sample and Label unchanged.

### Requirement 6: Scoring by Faithful Replay

**User Story:** As a Workflow_Author, I want candidate scores to predict what the deployed node will do, so that a prompt that wins in tuning also wins in production.

#### Acceptance Criteria

1. WHEN a Score_Run replays a Candidate on a Tuning_Sample, THE Portal (for `bedrock_inference`) or THE LocalServer (for `llm_inference`) SHALL build the invocation through the Invocation_Builder with the Candidate's Prompt_Set, the sample's Input_Image and Reference_Image bytes, and the Node_Parameters of the Tunable_Node in the latest Workflow_Definition version.
2. THE workflow executor, the Bedrock_Scorer and the Device_Score_Job runner SHALL use the Invocation_Builder from the same `workflow_core` source such that, for the same Prompt_Set, images and Node_Parameters, the request content, the appended Verdict_Instruction, the image labelling and ordering, the system text, the inference configuration and the parse rules are identical.
3. WHEN the Bedrock_Scorer replays a Candidate, THE Portal SHALL issue exactly one Bedrock Converse request per sample per repeat, in the node's configured region, with the executor's read timeout and no automatic retries.
4. WHEN a Device_Score_Job replays a Candidate, THE LocalServer SHALL issue the request to its Text_Generation_API for the node's `modelName` exactly as the executor does, including the `image`/`reference_image`/`system_prompt` fields and the loading-state wait policy.
5. WHEN a Sample_Outcome is produced, THE scorer SHALL categorize it as `correct` when the parsed verdict equals the Label, `false_pass` when the Label is `NOK` and the verdict is not anomalous, `false_fail` when the Label is `OK` and the verdict is anomalous, `parse_failure` when the Verdict_Parser rejects the answer, and `invocation_error` when the invocation fails; and SHALL record the raw answer, parsed verdict and confidence, output token count when reported, and latency.
6. WHEN a user starts a Score_Run, THE Portal SHALL show the number of invocations the run will issue (labelled non-excluded samples × repeats) and, for `llm_inference`, the device that will execute it, and SHALL require confirmation.
7. THE Portal SHALL let the user configure repeats per sample between 1 and 3, defaulting to 1, and SHALL report verdict instability as the number of samples whose repeats disagree.
8. WHILE a Bedrock Score_Run is in progress, THE Portal SHALL process samples with at most 4 concurrent invocations, SHALL persist each Sample_Outcome as it completes, and SHALL display progress and the running Score_Summary.
9. WHEN a Score_Run targets an `llm_inference` node, THE Portal SHALL create a Device_Score_Job for a device the user selects from those that exported samples for the node and that currently report the node's registration, SHALL deliver the job through the device's named shadow, and THE LocalServer SHALL execute it with at most 1 concurrent invocation, persisting Sample_Outcomes to the Sample_Store in batches of at most 20 and reporting progress through the shadow.
10. THE Portal SHALL allow at most one Score_Run in progress per Tuning_Session and SHALL reject a second start naming the in-progress run.
11. WHEN the user cancels a Score_Run, THE Portal SHALL issue no further invocations (and, for a Device_Score_Job, SHALL request cancellation through the shadow), SHALL keep the Sample_Outcomes already produced, and SHALL mark the run cancelled with its partial Score_Summary.
12. IF a Device_Score_Job reports no progress for 15 minutes or the device reports the job failed, THEN THE Portal SHALL mark the Score_Run failed with the reason and its partial Score_Summary.
13. THE Portal SHALL bound a Score_Run to 600 labelled samples × repeats and SHALL reject a larger run stating the bound.
14. WHEN a Score_Run completes, is cancelled, or fails, THE Portal SHALL persist the Score_Summary and every Sample_Outcome with the Tuning_Session.
15. WHILE a Device_Score_Job executes, THE LocalServer SHALL not delay or alter any Execution: job invocations SHALL run on a worker separate from the workflow executor, and Execution outcomes and artifacts SHALL be unchanged.

### Requirement 7: Comparison and Selection

**User Story:** As a Workflow_Author, I want to compare candidates side by side and drill into where they disagree with my labels, so that I choose on evidence and understand the failure modes that remain.

#### Acceptance Criteria

1. WHEN a Tuning_Session has completed Score_Runs, THE Portal SHALL display a comparison table with one row per Candidate's most recent Score_Run showing the Score_Summary fields, with the Baseline_Candidate's row present when it has been scored.
2. WHEN the user selects a Score_Run, THE Portal SHALL display every Sample_Outcome with the sample's images, Label, category, parsed verdict, confidence and raw answer, filterable by category and sortable by confidence.
3. WHEN two Score_Runs of different Candidates are selected, THE Portal SHALL display the samples on which their categories differ.
4. WHEN a Score_Run reports `parse_failure` outcomes, THE Portal SHALL display for each the raw answer character-for-character and the Verdict_Parser's rejection reason.
5. THE Portal SHALL let the user mark exactly one Candidate of a Tuning_Session as selected and SHALL persist the selection.
6. IF the selected Candidate's most recent Score_Run has false passes, THEN THE Portal SHALL display their count prominently at selection and apply time, because a false pass ships a defective part.

### Requirement 8: Applying the Winning Prompt

**User Story:** As a Workflow_Author, I want the winning prompt to become a new workflow version without retyping it, so that what I validated is exactly what gets deployed.

#### Acceptance Criteria

1. WHEN a Workflow_Author applies the selected Candidate of a Tuning_Session, THE Portal SHALL save a new version of the Workflow_Definition in which the target node's `prompt` (or `prompt_template`), `system_prompt` and `max_tokens` equal the Candidate's Prompt_Set and every other node, parameter and connection is byte-identical to the previous latest version.
2. THE Portal SHALL require the selected Candidate to have at least one completed Score_Run before it can be applied, and SHALL show the Candidate's and the Baseline_Candidate's Score_Summaries in the apply confirmation.
3. WHEN a Candidate is applied, THE Portal SHALL record an audit event carrying the user, workflow id, new version, node id, session id, Candidate id and Score_Run id, and SHALL record the application on the Tuning_Session as its latest Tuning_Result.
4. IF the target node is no longer a Tunable_Node of the latest Workflow_Definition version, THEN THE Portal SHALL refuse the apply and state that the node no longer exists or is no longer in Anomaly_Mode.
5. THE Portal SHALL NOT validate, package or deploy the new version as part of applying.
6. WHEN a Candidate is applied, THE Portal SHALL apply the same canonicalization and versioning as a designer save, so the version is indistinguishable from a manual edit of the same three parameters.

### Requirement 9: Access Control and Data Boundaries

**User Story:** As a portal administrator, I want tuning bounded to the people and data already authorized for the workflow, so that the feature adds no new exposure of production images or credentials.

#### Acceptance Criteria

1. THE Portal SHALL allow viewing a Tuning_Session to users holding `workflow:read` on the Use_Case, labelling, candidate authoring and scoring to users holding `workflow:edit`, and applying to users holding `workflow:save`, returning the workflow handlers' uniform 404 to users without read access and 403 to users without the operation permission, with denials audited.
2. THE Portal SHALL check authorization before any other validation of an Anomaly_Tuning request.
3. THE Portal SHALL send to Bedrock, during a Score_Run, only the sample's image bytes and prompt content derived from the Candidate's Prompt_Set, the Verdict_Instruction and the Node_Parameters, and no credentials, object keys or identifiers.
4. THE Portal SHALL read and write Sample_Store objects only within the requesting Use_Case's bucket and the `workflow-tuning/` prefix, using the same cross-account access mechanism the captures listing uses.
5. THE Portal SHALL store no image bytes in DynamoDB; images live only in the Sample_Store and are shown through presigned URLs.
6. A Device_Score_Job SHALL carry only identifiers, the Candidate's Prompt_Set, the Node_Parameters and the Sample_Store keys of the samples to replay; the device SHALL read sample bytes from the Sample_Store and SHALL write outcomes only under the job's prefix.

### Requirement 10: Session Lifecycle and Limits

**User Story:** As a portal administrator, I want tuning state to be durable and bounded, so that long tuning efforts survive and storage stays predictable.

#### Acceptance Criteria

1. THE Portal SHALL persist Tuning_Sessions, indexed samples, Labels, Candidates, Score_Runs and Sample_Outcomes in DynamoDB such that they survive Lambda cold starts and Portal redeployments.
2. THE Portal SHALL allow at most one Tuning_Session per `(workflowId, nodeId)` and SHALL let a user with `workflow:edit` delete a session together with everything it owns.
3. THE Portal SHALL retain at most the 20 most recent Score_Runs per Candidate and SHALL delete older Score_Runs with their Sample_Outcomes.
4. IF a Bedrock Score_Run's execution is interrupted (Lambda timeout, deployment), THEN THE Portal SHALL resume it from the last persisted Sample_Outcome on the next execution step, and SHALL mark it failed with its partial Score_Summary when it cannot resume within 60 minutes of its start.
5. WHEN a Tuning_Session is deleted, THE Portal SHALL delete its Score_Run outcome objects from the Sample_Store and SHALL leave exported Tuning_Samples in place for other sessions and future ones.

### Requirement 11: Preservation of Existing Behaviour

**User Story:** As a workflow operator, I want production runs and the Portal's existing workflow operations to behave exactly as before this feature, so that tuning tooling cannot change verdicts or versions unexpectedly.

#### Acceptance Criteria

1. WHEN a workflow executes after this feature, THE LocalServer SHALL issue for each Anomaly_Mode Inspection_Node an invocation whose request content, inference configuration, system text and parse behaviour equal those issued before this feature for the same node parameters and captured frames.
2. WHEN a workflow executes after this feature, THE LocalServer SHALL persist the same Run_Artifacts and the same Run_Metadata keys and values as before this feature, whether Sample_Export is enabled or not.
3. WHILE a Use_Case has Sample_Export disabled, THE Portal SHALL deliver no tuning configuration in deployments and THE LocalServer SHALL behave exactly as before this feature.
4. THE Portal SHALL leave the existing workflow routes (`GET/PUT/DELETE /workflows/{id}`, validate, package, versions) unchanged in request and response shape.
5. WHILE no Tuning_Session exists for a Use_Case, THE Portal SHALL create no tuning table items and no objects under `workflow-tuning/sessions/`.
