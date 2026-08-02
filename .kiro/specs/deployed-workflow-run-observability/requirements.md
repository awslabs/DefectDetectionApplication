# Requirements Document

## Introduction

This feature adds run observability to the on-device Deployed Workflow experience in the LocalServer frontend. Today a user can trigger a deployed Workflow_Component and see only a terminal status (pending/running/completed/failed) plus a single failing-node id and error string. This feature adds three capabilities to the per-execution view:

1. **View results** — for a run whose terminal node is a file-output (capture) node, a screen that shows the run's output image artifacts with any applicable overlay (anomaly mask / detection boxes), reusing the same show/hide overlay toggle the existing "run inference" results screen provides.
2. **View run log** — a per-execution log view so a user can see what happened during a run, whether it succeeded or failed.
3. **Run status graph** — a screen that mirrors the workflow graph and highlights each node by its live execution state: in-progress, success (green), failure (red), and warning (yellow).

The work is scoped to the edge device: the LocalServer backend (`src/backend`), the on-device frontend (`src/frontend`), and the deployed-workflow execution engine (`workflow_engine`). It does not change the cloud Portal's authoring/packaging flow. It builds on the existing WorkflowExecutor, the WorkflowRegistration/WorkflowExecution model, and the existing inference-result image/overlay rendering components.

Two prerequisite gaps must be closed for the features to have data to show: the deployed-workflow executor must persist per-run output artifacts (it does not today), and it must capture per-run logs and per-node status (it records only a single terminal status today).

## Glossary

- **LocalServer**: The Greengrass component (`aws.edgeml.dda.LocalServer.<arch>`) running on an edge device; embeds Triton and runs GStreamer pipelines. Hosts the on-device backend (`src/backend`) and frontend (`src/frontend`).
- **Deployed Workflow / Workflow_Component**: A workflow authored and packaged in the Portal and deployed to the device as a Greengrass component, discovered under `/aws_dda/workflows/{workflowId}/{version}/` as an artifact set (`manifest.json`, `workflow.json`, `compiled_pipeline.json`, optional `plugins/`, `python/`).
- **WorkflowRegistration**: The device-side record of a discovered Workflow_Component artifact set (id `{workflowId}:{version}`), `registered` (runnable) or `invalid`.
- **WorkflowExecution**: The device-side record of one triggered run of a WorkflowRegistration, with a lifecycle status (pending → running → completed/failed).
- **WorkflowExecutor**: The `workflow_engine.pipeline_executor.WorkflowExecutor` that renders `compiled_pipeline.json` to a GStreamer launch string and runs it through a fresh `GstPipelineManager`, then records the execution result.
- **Compiled_Pipeline_Document**: `compiled_pipeline.json` — the ordered segments/elements (each carrying a `nodeId` and `factory`) the executor renders and runs.
- **Workflow_Definition**: `workflow.json` — the Portal-authored graph (nodes with positions/parameters and connections) that the run-status graph mirrors.
- **Node**: A logical workflow step in the Workflow_Definition, identified by a `nodeId` that also tags the corresponding Compiled_Pipeline_Document elements.
- **Run_Artifact**: An output file produced by a run (e.g. captured JPEG, anomaly mask PNG, overlay JPEG, result JSONL) written to a per-run output location.
- **Capture_Node / File_Output_Node**: The terminal output node that writes image/result files (compiles to a `jpegenc ! emlcapture` chain). "View results" applies when a run's terminal output node is of this type.
- **Overlay**: A mask or detection annotation drawn over the base output image; toggled on/off in the results view.
- **Node_Run_Status**: The per-node execution state for a run: `pending`, `running`, `success`, `warning`, or `failure`.
- **Run_Log**: The set of log lines produced by the backend for a single WorkflowExecution.
- **Inference_Results_UI**: The existing on-device components that render a result image plus an overlay show/hide toggle (`ResultDetailsCardDisplay`, `InteractableImage`, `RefreshDisplayActions`, `live-result/helpers`).

## Requirements

### Requirement 1: Persist per-run output artifacts

**User Story:** As an operator running a deployed workflow on the device, I want each run's output images and result data to be saved to a known per-run location, so that I can review what the workflow produced.

#### Acceptance Criteria

1. WHEN the WorkflowExecutor runs a Compiled_Pipeline_Document whose terminal output node is a Capture_Node, THE WorkflowExecutor SHALL configure that node so the run's output artifacts (captured image, and any produced overlay and mask images and result JSONL) are written to a per-run output location derived from the execution.
2. THE per-run output location SHALL be unique per WorkflowExecution so that artifacts from different runs of the same workflow do not overwrite each other.
3. WHEN the deployed model produces overlay and/or mask outputs, THE WorkflowExecutor SHALL route those outputs to overlay and mask artifact files using the same output-routing contract the Pipeline_Configuration path uses (the `triton_inference_output_*` capture routing), so the produced artifacts match those of an equivalent on-device inference run.
4. WHEN a run completes, THE system SHALL record on the WorkflowExecution enough information to locate its Run_Artifacts (for example a capture id and/or output directory).
5. IF a workflow's terminal output node is not a Capture_Node (for example a digital-output-only or MQTT-only workflow), THEN THE WorkflowExecutor SHALL run exactly as before and SHALL NOT be required to produce image Run_Artifacts.
6. THE artifact-writing behavior SHALL be additive: a run that previously produced its is_anomalous/confidence tag values SHALL continue to produce them unchanged.

### Requirement 2: Capture per-run logs

**User Story:** As an operator, I want the log for a specific workflow run captured and retrievable, so that I can see what happened during that run without shell access to the device.

#### Acceptance Criteria

1. WHEN a WorkflowExecution runs, THE system SHALL capture the log lines emitted for that execution into a per-execution Run_Log retrievable after the run finishes.
2. THE Run_Log SHALL include the rendered pipeline launch string, per-node resolution/injection messages, and the terminal outcome (completion with tag values, or failure with the failing node and error).
3. WHEN a run fails, THE Run_Log SHALL include the underlying element/backend error that caused the failure (not only the generic "failed to change state to PLAYING" message).
4. THE Run_Log capture SHALL be bounded in size per execution and SHALL NOT grow unbounded on the device.
5. THE Run_Log capture SHALL NOT alter existing LocalServer logging (the existing Greengrass component log SHALL continue to receive the same messages).
6. IF log capture fails for any reason, THEN THE run itself SHALL NOT fail as a result (log capture is best-effort and isolated).

### Requirement 3: Capture per-node run status

**User Story:** As an operator, I want each workflow node's execution state recorded for a run, so that I can see which steps ran, which succeeded, which warned, and which failed.

#### Acceptance Criteria

1. WHEN a WorkflowExecution runs, THE system SHALL record a Node_Run_Status for each node in the workflow that maps to one or more Compiled_Pipeline_Document elements.
2. WHEN a run fails at an identifiable element, THE system SHALL mark the corresponding node's Node_Run_Status as `failure` and record the associated error.
3. WHEN a run completes successfully, THE system SHALL mark the nodes that participated in the run as `success`.
4. WHEN a node emits a non-fatal warning during a run (for example a recoverable element warning on the pipeline bus), THE system SHALL mark that node's Node_Run_Status as `warning` and retain the warning detail.
5. WHILE a run is in progress, THE system SHOULD reflect nodes that have not yet reached a terminal state as `pending` or `running` so the status graph can animate progress.
6. IF a node cannot be individually resolved to a live pipeline state, THEN THE system SHALL still derive that node's terminal status from the run outcome (all participating nodes `success` on completion; the failing node `failure` and others best-effort on failure) so the status graph is always populated for a finished run.

### Requirement 4: Expose run results, logs, node status, and graph via the device API

**User Story:** As the on-device frontend, I want API endpoints that return a run's artifacts, log, node status, and the workflow graph, so that I can render the new views.

#### Acceptance Criteria

1. THE device backend SHALL provide an endpoint that returns, for a WorkflowExecution, whether it has viewable image results and the identifiers needed to fetch them.
2. THE device backend SHALL provide endpoints that serve a run's base output image and its overlay/mask variants for display, following the existing capture-image serving conventions.
3. THE device backend SHALL provide an endpoint that returns the Run_Log for a WorkflowExecution as text.
4. THE device backend SHALL provide an endpoint that returns the Workflow_Definition graph (nodes and connections) for a WorkflowRegistration, sufficient to render the run-status graph.
5. THE device backend SHALL provide, for a WorkflowExecution, the per-node Node_Run_Status map (Requirement 3), addressable by `nodeId`.
6. WHEN a requested execution, registration, or artifact does not exist, THE endpoint SHALL respond with a not-found error rather than a server error.
7. THE new endpoints SHALL follow the existing authentication behavior of the on-device API (matching how current capture-image and workflow endpoints handle auth/token query parameters).

### Requirement 5: "View results" link and results screen

**User Story:** As an operator, I want a "View results" link on a finished run that opens a screen showing the run's output images with an overlay toggle, so that I can visually inspect what the workflow detected.

#### Acceptance Criteria

1. WHERE a completed WorkflowExecution has viewable image results (its terminal node is a File_Output_Node), THE deployed-workflow run view SHALL show a "View results" link for that execution.
2. WHERE a WorkflowExecution has no viewable image results, THE deployed-workflow run view SHALL NOT show a "View results" link for that execution.
3. WHEN a user activates "View results", THE frontend SHALL open a results screen that displays the run's output image(s).
4. THE results screen SHALL provide the same overlay show/hide toggle behavior as the Inference_Results_UI, and WHEN an overlay/mask exists for the run THE toggle SHALL show and hide the overlay over the base image.
5. WHERE no overlay/mask exists for the run, THE results screen SHALL display the base output image without an overlay toggle.
6. THE results screen SHALL reuse the existing overlay-capable image component and toggle rather than introducing a separate image renderer.
7. IF a run's result images cannot be loaded, THEN THE results screen SHALL display a clear "results unavailable" state rather than a broken image or a crash.

### Requirement 6: "View run log" link and log viewer

**User Story:** As an operator, I want a "View run log" link on a run, so that I can read what happened during that run.

#### Acceptance Criteria

1. THE deployed-workflow run view SHALL show a "View run log" link for every WorkflowExecution that has started (running or terminal).
2. WHEN a user activates "View run log", THE frontend SHALL display the Run_Log text for that execution.
3. THE log viewer SHALL be readable for both successful and failed runs and SHALL make a failure's error and failing node evident.
4. IF the Run_Log is not yet available or empty, THEN THE log viewer SHALL show an explanatory empty state rather than an error.
5. THE log viewer SHALL present the log in a scrollable, copyable form.

### Requirement 7: "Run status" graph screen

**User Story:** As an operator, I want a run-status screen that mirrors the workflow graph and colors each node by its state, so that I can see at a glance where a run is and where it failed.

#### Acceptance Criteria

1. THE deployed-workflow run view SHALL provide a way to open a run-status graph for a WorkflowExecution.
2. THE run-status graph SHALL render the workflow's nodes and connections in a layout that mirrors the authored Workflow_Definition graph.
3. THE run-status graph SHALL color each node by its Node_Run_Status: a distinct in-progress indication for `running`, green for `success`, red for `failure`, and yellow for `warning`.
4. WHERE a node is `failure` or `warning`, THE run-status graph SHALL make the associated error or warning detail available (for example on hover or selection).
5. WHILE a run is in progress, THE run-status graph SHALL update node states as the run progresses (polling is acceptable), and SHALL stop updating once the run reaches a terminal state.
6. WHEN the run is finished, THE run-status graph SHALL show a fully-resolved coloring consistent with the run outcome (Requirement 3.6).
7. THE run-status graph SHALL render on the device frontend without requiring network access to the cloud Portal.

### Requirement 8: Non-regression and isolation

**User Story:** As a maintainer, I want these additions to leave existing behavior intact, so that current workflows, pipelines, and tests are unaffected.

#### Acceptance Criteria

1. THE Pipeline_Configuration path (`src/backend/gstreamer`) and the on-device "run inference" results experience SHALL remain unchanged in behavior.
2. WHEN a deployed workflow has no file-output node, no overlay, or no per-node warnings, THE new features SHALL degrade gracefully (no results link, empty overlay handling, plain terminal statuses) without error.
3. THE existing deployed-workflow endpoints and their response shapes (`/workflows/registrations`, `/workflows/registrations/{id}`, `/workflows/registrations/{id}/trigger`, `/workflows/executions/{id}`) SHALL remain backward compatible; new fields MAY be added but existing fields SHALL NOT be removed or repurposed.
4. THE existing backend and frontend test baselines SHALL remain green, and new behavior SHALL be covered by tests at the same layers as the code it changes.
5. THE artifact, log, and node-status capture SHALL be contained per Requirement 13.7 semantics of the workflow engine: a failure in any of them SHALL NOT crash LocalServer or prevent a run from reaching a terminal state.
