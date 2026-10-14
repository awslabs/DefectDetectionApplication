# Requirements Document

## Introduction

The Edge CV Portal's flow designer offers a single generic camera input node, `camera_source`, shaped around a V4L2 device path (`device=/dev/video0`) with `gain`/`exposure` acquisition settings. On JetPack 6 that generic node already compiles to the NVIDIA CSI host-service file-capture chain, so a designer user cannot tell "generic camera", "CSI camera", and "smart (ICAM) camera" apart even though the edge LocalServer treats them as distinct `Image_Source` types (`Camera`, `NvidiaCSI`, `ICam`) with genuinely different capture pipelines. The edge already knows how to run a V4L2 smart camera (`v4l2src device=…`, `pipeline_builder._add_icam_image_source`) and an NVIDIA CSI camera (host-service capture to `/aws_dda/nvidia-csi-capture/latest.jpg`, `pipeline_builder._add_nvidia_csi_image_source`), but the designer has no node type for either.

This feature aligns the designer's input node types with the edge's real capture families. It adds two input node types to the shared workflow_core catalog — `csi_camera_source` ("CSI Camera Input") for NVIDIA CSI cameras and `icam_source` ("ICAM") for V4L2 smart cameras — with per-architecture GStreamer mappings that match the edge's existing CSI and ICAM capture paths, threads them through the packaging, deploy-time binding, and device-side execution seams the camera input pipeline already provides, and removes the now-redundant generic `camera_source` node type entirely (no existing workflow uses it). The workflow_core catalog exists in two mirrored copies (the portal layer and the edge vendor mirror) that must stay byte-identical.

## Glossary

- **Portal**: The edge-cv-portal cloud web application (React frontend, Lambda backend, DynamoDB storage) used to manage DDA use cases, models, workflows, deployments, and devices.
- **LocalServer**: The Greengrass component running on an Edge_Device. It owns the device-local Image_Source configuration and executes GStreamer pipelines and deployed Workflow_Components.
- **Edge_Device**: A Greengrass core device registered in the Portal's devices table that runs LocalServer.
- **Node_Catalog**: The shared workflow_core node type catalog (`workflow_core/catalog/nodes.py`), maintained as two mirrored copies that must stay byte-identical: the Portal layer (`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/`) and the edge vendor mirror (`src/backend/workflow_engine/vendor/workflow_core/`).
- **Node_Type_Descriptor**: A `NodeTypeDescriptor` entry in the Node_Catalog declaring a node type's id, category, display name, ports, parameters, per-architecture GStreamer mappings, and hardware-dependence flag.
- **Camera_Source_Node** (generic): The existing generic camera input node type `camera_source` (parameter `device`, plus `gain`/`exposure`), removed by this feature.
- **CSI_Camera_Source_Node**: The new input node type `csi_camera_source` added by this feature, representing an NVIDIA CSI camera whose frames are staged by the on-device NVIDIA CSI host capture service.
- **ICAM_Source_Node**: The new input node type `icam_source` added by this feature, representing a V4L2 smart camera captured directly through `v4l2src`.
- **Camera_Input_Node**: A workflow node whose frames come from a device camera; after this feature the CSI_Camera_Source_Node, the ICAM_Source_Node, the existing `aravis_camera_source`, and camera-backed custom node types.
- **CSI_Capture_Service**: The existing on-device host service (installed via `host_scripts/install_nvidia_csi_service.sh`, unit `nvidia-csi-capture.service`) that continuously captures NVIDIA CSI frames to `/aws_dda/nvidia-csi-capture/latest.jpg` and reads acquisition settings from `/aws_dda/nvidia-csi-capture/config.json`.
- **Architecture**: One of the compilation target architectures the Node_Catalog declares mappings for: `x86_64`, `x86_64_nvidia`, `arm64_jp4`, `arm64_jp5`, `arm64_jp6` (the physical device architectures), and `sim` (the cloud test sandbox).
- **Component_Packager**: The Portal packaging Lambda (`workflow_packaging.py`) that compiles workflow definitions into per-architecture `compiled_pipeline.json` documents and emits `bindingPoints` entries for Camera_Input_Nodes.
- **Binding_Point**: A per-node entry in a compiled document mapping a Camera_Input_Node's logical parameters to rendered element arguments (slot-based) or to an adapter/sensor selection marker (`adapterBinding`, `csiSensorBinding`, `aravisBinding`) with empty slots.
- **Camera_Binding**: The per-deployment, per-device mapping from a Camera_Input_Node to a Camera_Source identifier or manual override, validated by `validate_camera_bindings` in `deployments.py`.
- **Camera_Picker**: The Workflow_Builder camera reference control (`cameraReference.ts` + `NodeConfigPanel.tsx`) that offers a device's Camera_Registry entries for a Camera_Input_Node parameter.
- **Workflow_Engine**: The LocalServer subsystem (`src/backend/workflow_engine/`) that discovers deployed Workflow_Components, resolves Camera_Bindings, and executes triggered workflow runs (`pipeline_executor.WorkflowExecutor`).
- **Workflow_Executor**: The Workflow_Engine component (`pipeline_executor.py`) that runs a registered workflow's compiled pipeline, including the existing frame-source staging (`_stage_frame_sources`) that decodes device frames to a JP6-safe PNG before the pipeline reads them.
- **BUILTIN_TYPE_IDS**: The set of catalog-defined type ids derived from `NODE_CATALOG` in `custom_node_types.py`, used to distinguish built-in node types from Custom_Node_Types.

## Requirements

### Requirement 1: CSI Camera Input Node Type in the Catalog

**User Story:** As a computer vision engineer building a workflow, I want a dedicated CSI camera input node, so that I can declare an NVIDIA CSI camera input explicitly instead of relying on the ambiguous generic camera node.

#### Acceptance Criteria

1. THE Node_Catalog SHALL include a CSI_Camera_Source_Node with type id `csi_camera_source`, category input, display name "CSI Camera Input", no input ports, and exactly one output port of type VideoFrames.
2. THE CSI_Camera_Source_Node SHALL declare optional parameters `gain` (int, constraints min 0 and max 100, default 4) and `exposure` (int, constraint min 0, default 5000000) matching the acquisition settings the CSI_Capture_Service applies, each with a description and at least one working example.
3. THE CSI_Camera_Source_Node SHALL be marked hardware dependent, and its architecture mappings SHALL render a file-capture chain reading the CSI_Capture_Service staged frame: on `arm64_jp6` the PNG-staged decode chain reading `/aws_dda/nvidia-csi-capture/latest.jpg.dda_decoded.png`, and on the other physical architectures the standard JPEG file chain reading `/aws_dda/nvidia-csi-capture/latest.jpg`, with the shared dataset-fed simulation stub on the `sim` architecture.
4. THE CSI_Camera_Source_Node's parameters SHALL NOT appear in any element argument template, so that the node compiles with no binding slots.
5. WHEN a workflow definition containing a CSI_Camera_Source_Node is validated, compiled, or serialized through workflow_core, THE Node_Catalog SHALL process the node through the same generic descriptor-driven paths as existing catalog nodes, with parsing then serializing a definition containing the node producing an equivalent definition.

### Requirement 2: ICAM Source Node Type in the Catalog

**User Story:** As a computer vision engineer, I want a dedicated ICAM (V4L2 smart camera) input node, so that I can declare a smart camera input that the edge captures directly through v4l2src.

#### Acceptance Criteria

1. THE Node_Catalog SHALL include an ICAM_Source_Node with type id `icam_source`, category input, display name "ICAM", no input ports, and exactly one output port of type VideoFrames.
2. THE ICAM_Source_Node SHALL declare a required string parameter `device` with a minimum length of 1 and a default of `/dev/video0`, carrying the V4L2 device path the smart camera is captured from, with a description and at least one working example.
3. THE ICAM_Source_Node SHALL be marked hardware dependent, and its architecture mappings SHALL render a `v4l2src device={device}` capture chain (v4l2src followed by videoconvert) on every physical device architecture, and the shared dataset-fed simulation stub on the `sim` architecture.
4. THE ICAM_Source_Node's `device` parameter SHALL appear as exactly one single-placeholder element argument (the v4l2src `device` argument), so that the node compiles with exactly one binding slot for `device` on every physical device architecture.
5. WHEN a workflow definition containing an ICAM_Source_Node is validated, compiled, or serialized through workflow_core, THE Node_Catalog SHALL process the node through the same generic descriptor-driven paths as existing catalog nodes, with parsing then serializing a definition containing the node producing an equivalent definition.

### Requirement 3: Removal of the Generic Camera Source Node

**User Story:** As a product owner, I want the ambiguous generic camera source node removed, so that designer users choose an input node whose behavior matches a real edge capture family.

#### Acceptance Criteria

1. THE Node_Catalog SHALL NOT include a node type with id `camera_source` in either mirrored copy, and `get_node_type("camera_source")` SHALL return no descriptor.
2. THE Component_Packager, deploy-time binding validation, frontend Camera_Picker, and BUILTIN_TYPE_IDS derivation SHALL contain no reference to the `camera_source` type id after this feature.
3. WHEN the `/workflows/node-catalog` endpoint serves the palette after this feature, THE served catalog SHALL present `csi_camera_source` and `icam_source` in the input category and SHALL NOT present `camera_source`.
4. THE removal SHALL leave every other existing node type's descriptor (parameters, mappings, ports, category, hardware-dependence) byte-identical to before this feature.

### Requirement 4: Packaging Binding Points for the New Input Nodes

**User Story:** As an operator, I want workflows containing the CSI and ICAM nodes to package with binding points, so that deploy-time camera binding works for them the same way it works for other camera inputs.

#### Acceptance Criteria

1. WHEN the Component_Packager packages a workflow containing a CSI_Camera_Source_Node, THE Component_Packager SHALL treat the node as a Camera_Input_Node, emit a `bindingPoints` entry for it in every architecture's compiled document marked `csiSensorBinding: true` with empty slots and the node's rendered `gain`/`exposure` parameters, and record the node in the version item's `camera_input_nodes` with `has_binding_points: true`.
2. WHEN the Component_Packager packages a workflow containing an ICAM_Source_Node, THE Component_Packager SHALL treat the node as a Camera_Input_Node, emit a `bindingPoints` entry for it in every architecture's compiled document carrying one slot binding the `device` parameter to the rendered v4l2src `device` argument, with the node's rendered `device` parameter, and record the node in the version item's `camera_input_nodes` with `has_binding_points: true`.
3. WHEN the Component_Packager packages a workflow containing neither a CSI_Camera_Source_Node nor an ICAM_Source_Node, THE Component_Packager SHALL produce output byte-identical to its post-`camera_source`-removal output for that workflow.

### Requirement 5: Workflow Builder Support for the New Input Nodes

**User Story:** As a computer vision engineer, I want to configure the CSI and ICAM nodes in the flow designer, so that I can set their parameters and, for ICAM, pick a registered device path.

#### Acceptance Criteria

1. WHEN the Workflow_Builder renders the node palette, THE palette SHALL list "CSI Camera Input" and "ICAM" under the Input category from the served Node_Catalog descriptors.
2. WHEN a user configures an ICAM_Source_Node's `device` parameter, THE Camera_Picker SHALL render the camera reference control for that parameter, offering the selected reference device's V4L2-compatible Camera_Registry entries and populating `device` from the selected Camera_Source's device path, and SHALL continue to accept a directly typed device path under manual entry.
3. WHEN a user configures a CSI_Camera_Source_Node, THE Workflow_Builder SHALL render its `gain` and `exposure` parameters as standard numeric parameter inputs with no camera reference control.
4. THE Workflow_Builder SHALL render, validate, and serialize both new node types through its existing generic descriptor-driven node configuration paths, unchanged for all other node types.

### Requirement 6: Deploy-Time Binding Compatibility for the New Input Nodes

**User Story:** As an operator deploying a workflow with CSI or ICAM inputs, I want binding validation to accept compatible camera sources and reject mismatches, so that a deployed workflow references a capture source the device actually has.

#### Acceptance Criteria

1. WHEN a Camera_Binding for an ICAM_Source_Node references a Camera_Source, THE Portal SHALL accept it exactly when the Camera_Source's type is within the ICAM_Source_Node's declared compatible set (`ICam`, `V4L2Discovered`, or `Camera`) and reject it with a type-mismatch message otherwise.
2. WHEN a Camera_Binding for a CSI_Camera_Source_Node references a Camera_Source, THE Portal SHALL accept it exactly when the Camera_Source's type is within the CSI_Camera_Source_Node's declared compatible set (`NvidiaCSI`, or a Camera_Source whose capability metadata marks it CSI) and reject it with a type-mismatch message otherwise.
3. WHERE a user chooses manual override for a new input node, THE Portal SHALL validate the override values against that node type's declared parameter constraints and record the override as the Camera_Binding.
4. WHEN a deployment containing CSI or ICAM node bindings is submitted successfully, THE Portal SHALL deliver the bindings to target devices through the existing `dda-camera-bindings` shadow mechanism, leaving the packaged artifact unchanged.
5. WHEN validating a deployment binding set that references no removed `camera_source` node, THE Portal SHALL apply the same unbound-node, missing-source, degraded-source, and hint-preselection rules to the new node types through the existing generic validation paths.

### Requirement 7: Device-Side Execution of the New Input Nodes

**User Story:** As an operator, I want the edge device to run CSI and ICAM workflow nodes with their configured settings, so that a deployed workflow captures frames the way each camera family requires.

#### Acceptance Criteria

1. WHEN the Workflow_Executor runs a registered workflow whose document contains a CSI_Camera_Source_Node, THE Workflow_Executor SHALL write the node's effective `gain` and `exposure` values to the CSI_Capture_Service config file (`/aws_dda/nvidia-csi-capture/config.json`) before starting the pipeline.
2. WHEN the Workflow_Executor runs a CSI_Camera_Source_Node on the `arm64_jp6` architecture, THE Workflow_Executor SHALL stage the CSI_Capture_Service frame as a Pillow-decoded PNG at the compiled read path (`/aws_dda/nvidia-csi-capture/latest.jpg.dda_decoded.png`) before the pipeline reads it, consistent with the existing JP6 frame-source staging.
3. WHEN the Workflow_Executor runs a registered workflow whose document contains an ICAM_Source_Node, THE Workflow_Executor SHALL execute the compiled `v4l2src` pipeline directly without frame-source staging.
4. IF the CSI_Capture_Service staged frame is absent or unreadable when a CSI_Camera_Source_Node run starts, THEN THE Workflow_Executor SHALL mark the execution failed with the failing node id set to the CSI_Camera_Source_Node and an error identifying the missing capture frame.
5. WHEN the Workflow_Executor runs a workflow containing neither new input node, THE Workflow_Executor SHALL execute it exactly as before this feature.

### Requirement 8: Catalog Mirror Integrity and No Regression

**User Story:** As a maintainer, I want the two catalog copies to stay identical and unrelated behavior unchanged, so that adding these node types and removing the generic one introduces no drift or regression.

#### Acceptance Criteria

1. THE Node_Catalog SHALL carry the CSI_Camera_Source_Node and the ICAM_Source_Node identically in both copies (the Portal layer and the edge vendor mirror), with the two catalog source trees remaining byte-identical.
2. WHEN the full backend and portal test suites run after this feature, THE existing behavior for every node type other than the removed `camera_source` and the two added types SHALL be unchanged.
3. THE BUILTIN_TYPE_IDS set SHALL include `csi_camera_source` and `icam_source` and SHALL NOT include `camera_source`.
4. WHEN a workflow definition, compiled document, or deployment that contains none of the affected node types is processed by the Portal or the LocalServer, THE Portal and LocalServer SHALL validate, compile, package, register, bind, and execute it with behavior identical to before this feature.
