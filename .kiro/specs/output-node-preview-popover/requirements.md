# Requirements Document

## Introduction

Enhancement to the deployed-workflow run status graph (`src/frontend/src/components/deployed-workflow/graph/RunStatusGraph.tsx`): when a user clicks an output node, the graph shows a preview of that node's output and a link to the full run results page. Builds on the workflow-output-bindings-fixes spec, which made executor-binding nodes (llm_inference, mqtt_publish, opcua_write, digital_output, bedrock_inference) reach terminal statuses in `node_status_json` and made the run directory reliably contain `{capture_id}.jpg` and `{capture_id}.json` (run metadata including each llm node's `generated_text`).

Clarifying decisions were made autonomously per the user's standing preference (auto-advance without pausing); each is documented here:

- **Decision D1 (UI pattern)**: The preview renders in the existing selected-node detail area below the graph canvas (an enriched preview card), not a floating Cloudscape `Popover`. Rationale: the canvas is an `overflow: auto` scroll container with absolutely-positioned buttons — a floating popover would be clipped at the canvas edges; the graph already has click-to-select semantics with a detail panel, so enriching it preserves the existing failure/warning rendering and its tests; Cloudscape `Popover` wraps its trigger and manages its own open state, which conflicts with the existing controlled `selectedNodeId` toggle.
- **Decision D2 (output-node scope)**: Output nodes are the node types `capture`, `llm_inference`, `bedrock_inference`, `mqtt_publish`, `opcua_write`, and `digital_output`. Preview content per type: `capture` → output-image thumbnail (existing `.../output-image` endpoint); `llm_inference` → generated-text snippet from run metadata; `bedrock_inference` → the `is_anomalous`/`confidence` fields from run metadata; `mqtt_publish`/`opcua_write`/`digital_output` → the node's run status plus its recorded status detail (publish bindings do not persist payloads, so status detail is the available output evidence).
- **Decision D3 (backend addition)**: One small additive endpoint is genuinely needed — `GET /workflows/executions/{execution_id}/metadata` serving the parsed `{output_dir}/{capture_id}.json` — because no existing endpoint exposes the run metadata (the LLM `generated_text` source). All other preview data uses existing endpoints (output-image, node-status, execution).
- **Decision D4 (results link visibility)**: The "View full results" link is shown on every output-node preview card regardless of run outcome; the results page already renders graceful states for runs without viewable images.
- **Decision D5 (snippet length)**: Text snippets in the preview card are truncated to 280 characters with an ellipsis; the full text lives on the results page / run metadata.

## Glossary

- **Run_Status_Graph**: The deployed-workflow run status screen (`RunStatusGraph.tsx`) that renders the authored workflow graph with per-node run-status coloring and a selected-node detail area.
- **Output_Node**: A workflow graph node whose `type` is one of `capture`, `llm_inference`, `bedrock_inference`, `mqtt_publish`, `opcua_write`, `digital_output`.
- **Preview_Card**: The enriched selected-node detail area shown below the graph canvas when an Output_Node is selected, containing a preview of the node's output and a link to the Run_Results_Page.
- **Run_Results_Page**: The existing full results screen at `/deployed-workflows/{registrationId}/executions/{executionId}/results`.
- **Run_Metadata**: The parsed content of `{output_dir}/{capture_id}.json` written by the pipeline executor — the run's final tag values, including `llm[nodeId].generated_text` (or `error`) and Bedrock's merged `is_anomalous`/`confidence` fields.
- **Metadata_Endpoint**: The new backend route `GET /workflows/executions/{execution_id}/metadata` serving Run_Metadata.
- **Node_Run_Status**: The per-node status (`pending`, `running`, `success`, `warning`, `failure`) with optional `detail`, served by `GET /workflows/executions/{execution_id}/node-status`.
- **Terminal_Status**: A Node_Run_Status of `success`, `warning`, or `failure`.

## Requirements

### Requirement 1: Output-node preview card with results link

**User Story:** As an operator viewing a run's status graph, I want clicking an output node to show a preview of that node's output and a link to the full results page, so that I can inspect run outputs without leaving the graph.

#### Acceptance Criteria

1. WHEN a user clicks an unselected Output_Node in the Run_Status_Graph, THE Run_Status_Graph SHALL display the Preview_Card for that node.
2. WHEN a user clicks the currently selected Output_Node, THE Run_Status_Graph SHALL close the Preview_Card and deselect the node.
3. THE Preview_Card SHALL contain a link that navigates to the Run_Results_Page for the current execution.
4. WHEN a user clicks a node that is not an Output_Node, THE Run_Status_Graph SHALL display the pre-existing selected-node detail rendering without a Preview_Card.

### Requirement 2: Per-type preview content

**User Story:** As an operator, I want the preview to show output evidence appropriate to the node type, so that the preview is meaningful for images, generated text, and published signals alike.

#### Acceptance Criteria

1. WHERE the selected Output_Node has type `capture` and the execution has image results, THE Preview_Card SHALL display a thumbnail image sourced from the existing execution output-image URL.
2. WHERE the selected Output_Node has type `llm_inference` and Run_Metadata contains a `generated_text` entry for that node id, THE Preview_Card SHALL display the generated text truncated to 280 characters.
3. WHERE the selected Output_Node has type `bedrock_inference` and Run_Metadata contains `is_anomalous` or `confidence` fields, THE Preview_Card SHALL display those field values.
4. WHERE the selected Output_Node has type `mqtt_publish`, `opcua_write`, or `digital_output`, THE Preview_Card SHALL display the node's Node_Run_Status and, when present, the node's status detail.
5. WHEN the Preview_Card displays a text snippet longer than 280 characters, THE Preview_Card SHALL indicate truncation with an ellipsis.

### Requirement 3: Degraded and in-flight states

**User Story:** As an operator, I want the preview to behave sensibly while a run is in flight, when a node failed, or when artifacts are missing, so that the graph never shows a broken preview.

#### Acceptance Criteria

1. WHILE the selected Output_Node has a Node_Run_Status that is not a Terminal_Status, THE Preview_Card SHALL display an in-progress placeholder message stating that no output is available yet.
2. WHERE the selected Output_Node has a Node_Run_Status of `failure`, THE Preview_Card SHALL display the node's failure detail using the pre-existing failure alert presentation.
3. IF the preview data source for the selected Output_Node is unavailable (Run_Metadata has no entry for the node, the metadata request fails, or the thumbnail image fails to load), THEN THE Preview_Card SHALL display a fallback message stating that no preview is available.
4. WHILE a preview data source request is in flight, THE Preview_Card SHALL display a loading indicator.
5. THE Preview_Card SHALL display the Run_Results_Page link in every preview state, including in-progress, failure, and fallback states.

### Requirement 4: Run metadata endpoint

**User Story:** As the frontend, I want a device API that serves the run's metadata JSON, so that the preview card can show LLM generated text and Bedrock fields without new artifact plumbing.

#### Acceptance Criteria

1. WHEN the Metadata_Endpoint receives a request for a known execution whose Run_Metadata file exists and parses as a JSON object, THE Metadata_Endpoint SHALL return the parsed Run_Metadata with HTTP status 200.
2. IF the execution has no recorded `output_dir` or `capture_id`, or the Run_Metadata file is missing, unreadable, or not a JSON object, THEN THE Metadata_Endpoint SHALL return an empty JSON object with HTTP status 200.
3. IF the requested execution id is unknown, THEN THE Metadata_Endpoint SHALL return HTTP status 404.

### Requirement 5: Preservation of existing graph behavior

**User Story:** As an operator, I want the existing run status graph behavior to keep working, so that the enhancement does not regress status coloring, polling, or failure/warning details.

#### Acceptance Criteria

1. THE Run_Status_Graph SHALL preserve the pre-existing per-node status coloring, in-progress affordances, edge rendering, and node-status polling behavior.
2. WHEN a node with a Node_Run_Status of `failure` or `warning` is selected, THE Run_Status_Graph SHALL continue to render that node's detail message.
3. THE Metadata_Endpoint SHALL be additive: existing `/workflows` routes and their response shapes SHALL remain unchanged.
