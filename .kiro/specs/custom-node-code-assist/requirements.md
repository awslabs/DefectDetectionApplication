# Requirements Document

## Introduction

The Portal's workflow designer and custom node designer let users write Python code for custom node modules: the `custom_python` post-processing node and the `custom_python_preprocess` node (spec: custom-python-frames). Today the user writes that code unaided in a plain code editor and hand-maintains the node's pip `requirements` list.

This feature adds Bedrock-backed code assistance to every surface where custom Python node module code is edited. The user describes the desired custom code or filter in natural language; the Code_Assistant generates or modifies Python code that conforms to the correct runtime contract for the node type being edited (`process_frame(frame, metadata)` for frame processing, `handle(frame_bytes, metadata)` for raw bytes), using the runtime's pre-bound OpenCV/NumPy bindings and the `dda_frames` helper module. Alongside generation, an Import_Analyzer derives the node's pip requirements from the code's import statements (whether user-written or generated), mapping import names to the correct pip distribution names (for example `import cv2` → the OpenCV package), so the user no longer hand-maintains the pip list but can still review and edit it.

The feature reuses the existing per-account Bedrock_Configuration (spec: workflow-manager) — model id, region, max tokens, optional sampling parameters, and a timeout of at most 60 seconds — rather than introducing new configuration, and surfaces Bedrock failures (throttling, authorization, model errors, timeouts) as descriptive errors that leave the editing surface and the user's code untouched.

## Glossary

- **Portal**: The edge-cv-portal cloud web application (React frontend, Lambda backend) used to design, package, and deploy workflows.
- **Workflow_Builder**: The graphical canvas UI (Node_Palette, canvas, NodeConfigPanel) where users compose workflows and configure node parameters, including the code editor for custom Python node modules.
- **Node_Designer**: The Portal capability (spec: custom-node-designer) that lets authorized users create Custom_Node_Types, including wizard surfaces where node module code is written or generated.
- **Custom_Python_Node**: The post-processing node type (`custom_python`) whose user code runs on the edge through the Python_Bridge under the `handle(frame_bytes, metadata)` or `process_frame(frame, metadata)` contract.
- **Custom_Python_Preprocess_Node**: The preprocessing node type (`custom_python_preprocess`, spec: custom-python-frames) with VideoFrames input and output ports and the `process_frame(frame, metadata)` NumPy-array contract.
- **Code_Editing_Surface**: Any Portal UI where a user edits the Python code of a custom node module: the `code` parameter editor of a Custom_Python_Node or Custom_Python_Preprocess_Node in the Workflow_Builder, and any Node_Designer wizard step presenting Python node module code.
- **Code_Assistant**: The capability introduced by this feature: a prompt interface attached to each Code_Editing_Surface through which the user describes desired code in natural language and receives generated or modified Python code.
- **Code_Assist_Generator**: The Portal backend component that invokes the Amazon Bedrock model specified in the Bedrock_Configuration with the user's prompt, the target Node_Contract, and the current editor code, and returns the resulting Python code.
- **Node_Contract**: The runtime entry-point contract of the node type being edited: `process_frame(frame, metadata)` receiving a NumPy uint8 array for frame processing, or `handle(frame_bytes, metadata)` receiving raw bytes, as executed by the Python_Bridge runner.
- **Python_Bridge**: The LocalServer component (`src/backend/workflow_engine/python_bridge.py`) that executes custom Python node handlers on the edge, pre-binding `cv2`, `np`/`numpy`, and providing the Frame_Helpers module.
- **Frame_Helpers**: The runtime helper module importable from handler code as `dda_frames` (spec: custom-python-frames), providing `to_array`, `to_bytes`, `frame_info`, and `load_image` (local path or `s3://` URI).
- **Import_Analyzer**: The component introduced by this feature that derives a node module's Pip_Requirements from the import statements in the module's code.
- **Pip_Requirements**: The value of a custom Python node's `requirements` parameter: pip packages in requirements.txt form, installed on the edge device for the node module (packaged by the Component_Packager as `python/{nodeId}/requirements.txt`).
- **Import_Mapping**: The mapping from a Python import name to the pip distribution name that provides it (for example `cv2` → the OpenCV package, `PIL` → `Pillow`, `sklearn` → `scikit-learn`).
- **Bedrock_Configuration**: The existing per-account Portal settings (spec: workflow-manager, stored under setting key `bedrock_configuration`) identifying the Amazon Bedrock model id, region, max tokens, optional temperature and top_p sampling parameters, and invocation timeout used by generation features.
- **Use_Case**: The existing Portal tenancy unit to which workflows and Custom_Node_Types are scoped.

## Requirements

### Requirement 1: Code Assistance on All Custom Node Code Editing Surfaces

**User Story:** As a computer vision engineer, I want AI code assistance available wherever I edit custom Python node module code, so that I can get help writing custom code and filters without leaving the editor I am in.

#### Acceptance Criteria

1. WHEN the code editor for the `code` parameter of a Custom_Python_Node is displayed in the Workflow_Builder, THE Portal SHALL present the Code_Assistant in the same view as the code editor, without requiring the user to navigate away.
2. WHEN the code editor for the `code` parameter of a Custom_Python_Preprocess_Node is displayed in the Workflow_Builder, THE Portal SHALL present the Code_Assistant in the same view as the code editor, without requiring the user to navigate away.
3. WHEN a Node_Designer wizard step presenting Python node module code is displayed, THE Portal SHALL present the Code_Assistant in the same view as that editor, without requiring the user to navigate away.
4. THE Code_Assistant SHALL accept a natural-language description of the desired custom code or filter of 1 to 4,000 characters as its prompt input.
5. WHILE a Code_Assist_Generator invocation is in progress, THE Code_Editing_Surface SHALL continue to accept manual edits to the editor code and SHALL make no change to the editor code other than the user's own edits.
6. WHILE a Code_Assist_Generator invocation is in progress, THE Code_Assistant SHALL display an in-progress indication and SHALL NOT accept a concurrent prompt submission.

### Requirement 2: Contract-Conforming Code Generation

**User Story:** As a process engineer, I want to describe the filter or processing step I need in plain language and receive working Python code for the node I am editing, so that I can build custom processing without knowing the node's runtime contract by heart.

#### Acceptance Criteria

1. WHEN a user submits a non-empty prompt from a Code_Editing_Surface, THE Code_Assist_Generator SHALL invoke the Amazon Bedrock model specified in the Bedrock_Configuration with the prompt, the target node type's Node_Contract, and the runtime environment description (pre-bound `cv2`, `np`/`numpy`, and the Frame_Helpers `dda_frames` module).
2. WHEN generating code for a Custom_Python_Preprocess_Node, THE Code_Assist_Generator SHALL return syntactically valid Python code defining a `process_frame(frame, metadata)` entry point that operates on the NumPy array frame.
3. WHEN generating code for a Custom_Python_Node, THE Code_Assist_Generator SHALL return syntactically valid Python code defining exactly one entry point valid under the Custom_Python_Node's runtime contract, either `process_frame(frame, metadata)` or `handle(frame_bytes, metadata)`.
4. WHEN the Code_Assist_Generator returns code, THE Code_Assistant SHALL display the returned code to the user for review before any change to the editor content.
5. WHEN a user accepts reviewed code, THE Code_Assistant SHALL place the accepted code into the code editor as the node module's code value.
6. WHEN a user submits a follow-up prompt while the editor contains code with at least one non-whitespace character, THE Code_Assist_Generator SHALL include the current editor code in the Bedrock invocation and SHALL return the complete modified node module code rather than a fragment, diff, or code unrelated to the current editor code.
7. THE Code_Assistant SHALL leave saving of the workflow or node declaration to the user's existing explicit save action and SHALL NOT itself persist the workflow or node declaration.
8. IF a user submits an empty or whitespace-only prompt, THEN THE Code_Assistant SHALL reject the submission without invoking the Code_Assist_Generator and SHALL indicate that a prompt description is required.
9. WHEN a user rejects or dismisses reviewed code, THE Code_Assistant SHALL leave the editor content unchanged and SHALL preserve the user's prompt.
10. WHEN a user submits a prompt while the editor is empty or contains only whitespace, THE Code_Assist_Generator SHALL generate new code from the prompt and the target Node_Contract without treating the empty editor content as code to modify.

### Requirement 3: Automatic Pip Requirements Population

**User Story:** As a computer vision engineer, I want the node's pip requirements derived automatically from the code's imports, including OpenCV and any other libraries I request, so that I do not have to hand-maintain the pip list.

#### Acceptance Criteria

1. WHEN the code of a custom Python node module changes on a Code_Editing_Surface (user-written or accepted generated code), THE Import_Analyzer SHALL derive the Pip_Requirements from all import statements in the code, including imports nested inside functions or conditional blocks, within 2 seconds of the change.
2. THE Import_Analyzer SHALL resolve each imported top-level module through the Import_Mapping to the pip distribution name that the Import_Mapping designates for that module, including `cv2` to the OpenCV distribution designated in the Import_Mapping and `numpy` to `numpy`.
3. THE Import_Analyzer SHALL exclude Python standard library modules, the Frame_Helpers `dda_frames` module, and modules whose source files are packaged alongside the node module's own files from the derived Pip_Requirements.
4. WHEN a user's prompt requests use of a specific Python library, THE Code_Assist_Generator SHALL return code containing an import statement for that library.
5. WHEN the Import_Analyzer derives Pip_Requirements, THE Code_Editing_Surface SHALL replace all previously derived entries in the node's `requirements` parameter with the newly derived list and SHALL retain every entry the user added or version-pinned manually unchanged.
6. WHEN the `requirements` parameter is populated, THE Code_Editing_Surface SHALL display the populated list for user review and accept user edits to the list before saving.
7. IF an imported module has no Import_Mapping entry, THEN THE Import_Analyzer SHALL include the import name itself as the pip package entry and THE Code_Editing_Surface SHALL display a visible indication on that entry that it requires user review.
8. WHEN a user accepts generated code containing an import for a prompt-requested library, THE Import_Analyzer SHALL include that library's pip distribution name in the derived Pip_Requirements.
9. IF a derived pip distribution name matches the distribution name of an entry the user added or version-pinned manually, THEN THE Code_Editing_Surface SHALL retain the user's entry and SHALL NOT add a duplicate entry for that distribution.
10. IF the node module code cannot be parsed due to syntax errors, THEN THE Import_Analyzer SHALL leave the node's existing `requirements` parameter unchanged.

### Requirement 4: Reuse of the Existing Bedrock Configuration

**User Story:** As a portal administrator, I want code assistance to use the Bedrock model settings I already configured for workflow generation, so that one configuration governs all Bedrock-backed features in the account.

#### Acceptance Criteria

1. WHEN preparing a Bedrock invocation, THE Code_Assist_Generator SHALL read the model id, region, max tokens, temperature, top_p, and timeout values in effect at the time of that invocation from the same Bedrock_Configuration settings storage used by the existing workflow generation feature.
2. IF a sampling parameter (temperature or top_p) is unset in the Bedrock_Configuration, where unset means absent from storage or explicitly stored as null, THEN THE Code_Assist_Generator SHALL omit that parameter from the Bedrock invocation.
3. IF both temperature and top_p are set in the Bedrock_Configuration, THEN THE Code_Assist_Generator SHALL send only the temperature parameter to the Bedrock invocation and SHALL omit the top_p parameter.
4. THE Code_Assist_Generator SHALL apply an invocation timeout equal to the configured timeout value clamped to the range 1 to 60 seconds inclusive.
5. IF the Bedrock_Configuration settings storage is unreadable, THEN THE Code_Assist_Generator SHALL invoke Bedrock with the same default configuration values as the existing workflow generation feature and SHALL NOT fail the code assistance request because of the read failure.
6. IF an individual configuration value other than temperature or top_p is missing from the stored Bedrock_Configuration, THEN THE Code_Assist_Generator SHALL substitute the default value used by the existing workflow generation feature for that value while applying the stored values that are present.
7. IF the configured timeout value is missing or not interpretable as a number, THEN THE Code_Assist_Generator SHALL apply a 60-second invocation timeout.

### Requirement 5: Bedrock Failure Handling

**User Story:** As a computer vision engineer, I want clear error messages when code generation fails, so that I understand what went wrong and can retry without losing my code or my prompt.

#### Acceptance Criteria

1. IF a Bedrock invocation fails with a throttling, authorization, model-access, or model error, THEN THE Code_Assistant SHALL display an error message identifying which of these four failure categories occurred and SHALL retain the submitted prompt text unmodified in the Code_Assistant prompt input for resubmission.
2. IF a Bedrock invocation does not complete within the applied timeout (the Bedrock_Configuration timeout clamped to at most 60 seconds, per Requirement 4), THEN THE Code_Assistant SHALL display a timeout error stating the applied timeout value in seconds and SHALL retain the submitted prompt text unmodified in the Code_Assistant prompt input for resubmission.
3. IF the Code_Assist_Generator returns empty output or output from which no Python code can be extracted, THEN THE Code_Assistant SHALL display an error indicating that no code was produced and SHALL retain the submitted prompt text unmodified in the Code_Assistant prompt input for resubmission.
4. IF a Code_Assist_Generator invocation fails for any reason in criteria 1, 2, 3, or 6, THEN THE Code_Editing_Surface SHALL leave the editor code, the `requirements` parameter, and the enclosing workflow or node declaration unchanged from their values at the moment of prompt submission.
5. IF a Code_Assist_Generator invocation fails, THEN THE Code_Editing_Surface SHALL cease displaying any in-progress indication, SHALL accept manual edits to the editor code, and SHALL accept a new or resubmitted prompt from the Code_Assistant.
6. IF the Code_Assist_Generator returns Python code that does not define an entry point required by the target Node_Contract (`process_frame(frame, metadata)` or `handle(frame_bytes, metadata)`), THEN THE Code_Assistant SHALL display an error indicating that the generated code lacks the required entry point and SHALL retain the submitted prompt text unmodified in the Code_Assistant prompt input for resubmission.

### Requirement 6: Access Control

**User Story:** As a portal administrator, I want code assistance gated by the same permissions that gate editing on each surface, so that the assistant grants no capability beyond what the user could already do by hand.

#### Acceptance Criteria

1. THE Portal SHALL permit Code_Assistant use on a Workflow_Builder Code_Editing_Surface only for users holding the workflow create or workflow edit permission on the Use_Case to which the edited workflow belongs.
2. THE Portal SHALL permit Code_Assistant use on a Node_Designer Code_Editing_Surface only for users holding the UseCaseAdmin role in the Use_Case to which the edited plugin belongs or the PortalAdmin role, matching the Node_Designer access rules of spec custom-node-designer.
3. IF a user without a permitting role or permission invokes the Code_Assist_Generator, THEN THE Portal SHALL deny the request before any generation is performed, return an authorization error, and record the denied attempt with the acting user, the target Code_Editing_Surface, the Use_Case, and a timestamp in the existing audit log.
4. THE Portal SHALL evaluate Code_Assistant authorization against the user's current role and permission assignments on each Code_Assist_Generator request, independent of authorization results from earlier requests in the same session.
5. WHILE a user lacks the permitting role or permission for a Code_Editing_Surface, THE Portal SHALL omit the Code_Assistant entry point from that Code_Editing_Surface.
