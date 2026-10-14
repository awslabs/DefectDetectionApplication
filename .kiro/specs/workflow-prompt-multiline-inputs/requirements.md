# Requirements Document

## Introduction

Prompt-style parameters in the Workflow Builder's node configuration panel currently render as single-line text inputs, which makes authoring and reviewing multi-sentence prompts and multi-line payload templates awkward. This feature changes the Node_Config_Panel so that designated long-text parameters render as adjustable (user-resizable) multi-line text areas:

- `prompt` and `system_prompt` on the `bedrock_inference` node
- `prompt_template` and `system_prompt` on the `llm_inference` (VLM/LLM) node
- `payload_template` on the `mqtt_publish` output node

The `system_prompt` parameter is fully supported on the device side: the Device_Vendored_Catalog declares it on both inference nodes, and the device executors consume it end-to-end (Bedrock Converse API `system` field; vLLM Text_Generation_API `system_prompt` field). That device-side work is committed and complete. However, the portal-side Node_Catalog — the catalog the Workflow Builder actually fetches via the Catalog_Endpoint — does not yet include the `system_prompt` Parameter_Descriptors in any committed or deployed form (they exist only as uncommitted working-tree edits). Because the Node_Config_Panel renders exactly the parameters the served catalog declares, the `system_prompt` fields do not appear in the builder today. This feature therefore includes finalizing the portal Node_Catalog's `system_prompt` descriptors and serving them to the builder, in addition to the rendering change. No compiler or device-side changes are required.

The catalog descriptors also do not currently declare which string parameters are long-text, so the feature introduces a way for the Node_Config_Panel to know which parameters render as multi-line controls (a catalog-declared rendering hint), keeping the frontend free of per-node hardcoded parameter lists where practical.

A parity constraint applies throughout: the portal Node_Catalog's `system_prompt` descriptors must stay consistent with the Device_Vendored_Catalog's committed declarations (same name, type, optionality, default, and description semantics), and existing Workflow_Definitions that omit `system_prompt` must remain valid, since the parameter is optional with default `""`.

## Glossary

- **Node_Config_Panel**: The workflow builder's node configuration side panel (`edge-cv-portal/frontend/src/pages/workflows/NodeConfigPanel.tsx`) that renders one form control per catalog-declared parameter of the selected node.
- **Node_Catalog**: The portal backend's source of truth for node type descriptors (`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`), served to the frontend by the Catalog_Endpoint; each parameter is described by a Parameter_Descriptor.
- **Device_Vendored_Catalog**: The device-side vendored copy of the node catalog (`src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`) used by the on-device workflow engine; it already declares `system_prompt` on both inference nodes in committed form.
- **Catalog_Endpoint**: The deployed portal backend API `GET /workflows/node-catalog` that serves the Node_Catalog's descriptors to the workflow builder frontend.
- **Parameter_Descriptor**: The per-parameter declaration in the Node_Catalog (name, type, default, constraints, description, examples, visibility gating).
- **Multiline_Control**: A multi-line text area form control (Cloudscape `Textarea`) that displays several rows of text, preserves embedded newlines, and can be resized vertically by the user.
- **Multiline_Hint**: The catalog-declared rendering hint on a Parameter_Descriptor that tells the Node_Config_Panel to render the parameter's control as a Multiline_Control.
- **Target_Parameters**: The five parameters this feature converts to Multiline_Controls: `bedrock_inference.prompt`, `bedrock_inference.system_prompt`, `llm_inference.prompt_template`, `llm_inference.system_prompt`, and `mqtt_publish.payload_template`.
- **Workflow_Definition**: The serialized workflow graph document (nodes, parameter values, connections) saved and loaded by the portal backend.

## Requirements

### Requirement 1: Portal catalog declares and serves the system_prompt parameters

**User Story:** As a workflow author, I want the system prompt parameters that the device already supports to appear in the workflow builder, so that I can configure them without editing workflow JSON by hand.

#### Acceptance Criteria

1. THE Node_Catalog SHALL declare a `system_prompt` Parameter_Descriptor on the `bedrock_inference` node type with the same name, string type, optional (not required) status, default value `""`, and description semantics as the Device_Vendored_Catalog's `bedrock_inference.system_prompt` declaration.
2. THE Node_Catalog SHALL declare a `system_prompt` Parameter_Descriptor on the `llm_inference` node type with the same name, string type, optional (not required) status, default value `""`, and description semantics as the Device_Vendored_Catalog's `llm_inference.system_prompt` declaration.
3. WHEN the workflow builder frontend requests the node catalog, THE Catalog_Endpoint SHALL include both `system_prompt` Parameter_Descriptors in the served response.
4. WHEN the Catalog_Endpoint serves a `system_prompt` Parameter_Descriptor for either inference node, THE Node_Config_Panel SHALL render a form control for the parameter (a Multiline_Control, per Requirement 3).
5. WHEN a Workflow_Definition that omits `system_prompt` on an inference node is validated against the Node_Catalog, THE Node_Catalog SHALL accept the Workflow_Definition as valid.
6. THE Node_Catalog SHALL keep every `system_prompt` Parameter_Descriptor field consistent with the corresponding Device_Vendored_Catalog declaration.

### Requirement 2: Catalog-declared multiline rendering hint

**User Story:** As a workflow platform maintainer, I want long-text parameters marked in the node catalog itself, so that the frontend renders them consistently without hardcoding per-node parameter lists.

#### Acceptance Criteria

1. THE Node_Catalog SHALL support a Multiline_Hint on any string-typed Parameter_Descriptor.
2. THE Node_Catalog SHALL declare the Multiline_Hint on each of the five Target_Parameters.
3. WHEN the node catalog is served to the frontend, THE Node_Catalog SHALL include each declared Multiline_Hint in the wire form of the Parameter_Descriptor.
4. WHEN a Parameter_Descriptor omits the Multiline_Hint, THE Node_Catalog SHALL serve that descriptor in a form identical to its pre-feature wire form.
5. THE Node_Catalog SHALL evaluate the Multiline_Hint independently per Parameter_Descriptor, allowing a node type to mix hinted and unhinted string parameters.
6. THE Node_Catalog SHALL leave the parameter names, types, defaults, constraints, and visibility gating of the Target_Parameters unchanged from their Device_Vendored_Catalog declarations, apart from the added Multiline_Hint.

### Requirement 3: Multiline rendering in the node configuration panel

**User Story:** As a workflow author, I want prompt and template fields to be adjustable multi-line text boxes, so that I can write and read multi-sentence prompts comfortably.

#### Acceptance Criteria

1. WHEN the Node_Config_Panel renders a string parameter whose Parameter_Descriptor carries the Multiline_Hint, THE Node_Config_Panel SHALL render the parameter's control as a Multiline_Control.
2. THE Node_Config_Panel SHALL render every Multiline_Control with an initial height of at least 4 text rows.
3. THE Node_Config_Panel SHALL render every Multiline_Control as vertically resizable by the user.
4. WHEN the user types text containing newlines into a Multiline_Control, THE Node_Config_Panel SHALL store the entered text, including the newlines, as the parameter's string value.
5. WHEN the Node_Config_Panel renders a Multiline_Control for a parameter whose stored value contains newlines, THE Node_Config_Panel SHALL display the value across multiple lines.
6. WHEN the Node_Config_Panel renders a string parameter without the Multiline_Hint, THE Node_Config_Panel SHALL render the single-line input control used before this feature.
7. WHEN a string parameter without the Multiline_Hint carries a stored value containing newlines, THE Node_Config_Panel SHALL preserve those newlines in the stored value rather than stripping them.
8. THE Node_Config_Panel SHALL render the label, description, examples, validation messages, and visibility gating of a Multiline_Hint parameter the same way it renders them for single-line string parameters.

### Requirement 4: System prompt fields on both inference nodes

**User Story:** As a workflow author, I want a system prompt text box of the same style next to the user prompt on the Bedrock and VLM/LLM inference nodes, so that I can separate system-role instructions from the per-run user prompt.

#### Acceptance Criteria

1. WHEN a `bedrock_inference` node is selected, THE Node_Config_Panel SHALL render both the `prompt` control and the `system_prompt` control as Multiline_Controls of the same style.
2. WHEN an `llm_inference` node is selected, THE Node_Config_Panel SHALL render both the `prompt_template` control and the `system_prompt` control as Multiline_Controls of the same style.
3. WHEN a workflow is saved with a multi-line value in any Target_Parameter, THE Workflow_Definition SHALL persist the value with its newlines intact, and loading the workflow SHALL restore the identical value (round-trip property).
4. WHEN a string parameter value containing newlines is validated against the Workflow_Definition schema, THE Node_Catalog SHALL treat newline characters as ordinary string content and accept the value.
5. WHEN a node carrying a multi-line Target_Parameter value is duplicated or its definition is exported and re-imported, THE Node_Config_Panel SHALL preserve the value, including its newlines, in the resulting copy.
6. THE Node_Config_Panel SHALL leave the existing empty-value semantics of `system_prompt` unchanged: an empty value continues to mean no system prompt is sent to the model.

### Requirement 5: Payload template on the MQTT publish node

**User Story:** As a workflow author, I want the MQTT publish node's payload template to use the same adjustable multi-line text box, so that I can author structured multi-line payloads (for example, pretty JSON templates).

#### Acceptance Criteria

1. WHEN an `mqtt_publish` node is selected, THE Node_Config_Panel SHALL render the `payload_template` control as a Multiline_Control of the same style as the inference prompt controls.
2. THE Node_Config_Panel SHALL leave the `payload_template` placeholder semantics and default value (`{inference_json}`) unchanged.

### Requirement 6: No behavioral change outside the catalog additions and rendering

**User Story:** As a workflow platform maintainer, I want this change confined to the catalog's system_prompt declarations and how the fields render, so that existing workflows, validation, compilation, and device execution behave identically.

#### Acceptance Criteria

1. WHEN an existing Workflow_Definition containing any Target_Parameter is opened after this feature ships, THE Node_Config_Panel SHALL display the stored parameter values unchanged.
2. THE Node_Catalog SHALL keep the validation constraints of every Target_Parameter identical to the Device_Vendored_Catalog's committed declarations.
3. WHEN a workflow containing any Target_Parameter is compiled, THE Node_Catalog SHALL produce executor bindings identical to those the Device_Vendored_Catalog produces for the same parameter values.
