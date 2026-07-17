# Requirements Document

## Introduction

The DDA edge application's primary camera input sources are Aravis (GenICam / GigE Vision / USB3 Vision) cameras: the LocalServer enumerates the Aravis bus through `edge_ml1_p_camera_management/aravis_functions.py` (each camera carrying an id, model, address, physical id, protocol, serial, and vendor), acquires frames through the camera manager (`utils/camera_manager.py`), and stores camera-backed Image_Sources of type `Camera` whose `cameraId` references an Aravis device. The Edge CV Portal's flow designer, however, has no node that speaks this language. Its only camera input node, `camera_source`, is shaped around a V4L2 device path (`device=/dev/video0`), and the camera-registry-sync feature that synchronizes edge camera inventory to the Portal discovers only V4L2 hardware — the Aravis bus inventory that the edge app and camera manager actually work with never reaches the Portal's Camera_Registry, and a designer user cannot express "this workflow's input is that GenICam camera".

This feature adds an Aravis camera input sub-type to the flow designer that stays in sync with the edge. It introduces an `aravis_camera_source` node type in the shared workflow_core catalog whose parameters mirror the edge's Aravis camera identity (camera id, plus gain/exposure acquisition settings), extends the edge's camera discovery and sync agent to report Aravis bus cameras (merged with configured `Camera`-type Image_Sources by camera id) into the existing Camera_Registry, and wires the node through the whole existing binding pipeline: the Workflow_Builder camera picker offers the synced Aravis inventory for the node's camera id parameter, the Component_Packager emits Aravis-typed binding points, deploy-time validation enforces Aravis type compatibility, and the LocalServer resolves bindings to a local Aravis camera and executes the node by feeding frames grabbed through the existing camera manager into the compiled pipeline's appsrc. The workflow_core catalog exists in two mirrored copies (the portal layer and the edge vendor mirror), and both must stay identical.

## Glossary

- **Portal**: The edge-cv-portal cloud web application (React frontend, Lambda backend, DynamoDB storage) used to manage DDA use cases, models, workflows, deployments, and devices.
- **LocalServer**: The Greengrass component running on an Edge_Device. It owns the device-local Image_Source configuration, the Aravis camera stack, and executes GStreamer pipelines and deployed Workflow_Components.
- **Edge_Device**: A Greengrass core device registered in the Portal's devices table that runs LocalServer.
- **Aravis_Camera**: A GenICam-protocol camera (GigE Vision, USB3 Vision, or the Aravis Fake interface) visible to LocalServer through the Aravis library, identified by the identity fields the Aravis bus enumeration reports: id, model, address, physical id, protocol, serial number, and vendor.
- **Aravis_Enumeration**: The existing LocalServer capability (`aravis_functions.getCameras()` / `rescan_cameras()`) that lists the Aravis_Cameras currently visible on the device's GenICam bus.
- **Camera_Manager**: The existing LocalServer subsystem (`utils/camera_manager.py`) that connects, configures (gain, exposure, GenICam features), and grabs frames from Aravis_Cameras by camera id.
- **Image_Source**: The device-local input-source record managed by LocalServer's image_source DAO and accessors. An Image_Source of type `Camera` is Aravis-backed: its `cameraId` field references an Aravis_Camera and Camera_Manager acquires its frames.
- **Camera_Registry**: The existing Portal-side store and API (`dda-portal-camera-registry` table, `camera_registry.py` Lambda) holding, per Edge_Device, the Camera_Sources known for that device, introduced by the camera-registry-sync feature.
- **Camera_Source**: A per-device entry in the Camera_Registry (and in the edge-reported inventory) carrying a stable identifier, name, type, parameters, capability metadata, origin, version, and sync metadata.
- **Camera_Discovery**: The existing LocalServer subsystem (`src/backend/camera_discovery/`) that enumerates physical capture hardware for the inventory. Today it enumerates V4L2 devices only.
- **Edge_Sync_Agent**: The existing LocalServer component (`src/backend/camera_sync/`) that merges configured Image_Sources with Camera_Discovery results (`build_inventory`) and reports the inventory to the Portal over the `dda-camera-registry` named IoT shadow.
- **Aravis_Camera_Source**: A Camera_Source entry representing an Aravis_Camera: either a discovered-only bus camera (type `AravisDiscovered`, origin edge-discovered) or a configured `Camera`-type Image_Source merged with its discovered bus identity.
- **Node_Catalog**: The shared workflow_core node type catalog (`workflow_core/catalog/nodes.py`), maintained as two mirrored copies: the Portal layer (`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/`) and the edge vendor mirror (`src/backend/workflow_engine/vendor/workflow_core/`).
- **Aravis_Camera_Source_Node**: The new input node type (`aravis_camera_source`) added to the Node_Catalog by this feature, whose parameters carry an Aravis camera id and acquisition settings and whose output port emits VideoFrames.
- **Workflow_Builder**: The graphical canvas UI within the Portal where users compose workflow definitions from Node_Catalog nodes.
- **Camera_Picker**: The existing Workflow_Builder camera reference control (`cameraReference.ts` + NodeConfigPanel) that offers a device's Camera_Registry entries for a Camera_Input_Node parameter and records a `cameraBindingHint` on the node.
- **Component_Packager**: The existing Portal packaging Lambda (`workflow_packaging.py`) that compiles workflow definitions into per-architecture `compiled_pipeline.json` documents and emits `bindingPoints` entries for Camera_Input_Nodes.
- **Camera_Input_Node**: A workflow node whose frames come from a device camera; today the `camera_source` node type and camera-backed custom node types, extended by this feature to include the Aravis_Camera_Source_Node.
- **Camera_Binding**: The existing per-deployment, per-device mapping from a Camera_Input_Node to a Camera_Source identifier or manual override, validated by `validate_camera_bindings` in `deployments.py` and delivered over the `dda-camera-bindings` shadow.
- **Workflow_Engine**: The LocalServer subsystem (`src/backend/workflow_engine/`) that discovers deployed Workflow_Components, resolves Camera_Bindings (`camera_binding.resolve_bindings`), and executes triggered workflow runs (`pipeline_executor.WorkflowExecutor`).
- **Workflow_Executor**: The Workflow_Engine component that runs a registered workflow's compiled pipeline through a GstPipelineManager instance.
- **Frame_Feed**: The existing single-frame appsrc feed mechanism of `GstPipelineManager.run_pipeline` (its `frame_data` argument), used today by classic Camera-type pipelines to push a Camera_Manager frame into an appsrc-headed pipeline.

## Requirements

### Requirement 1: Aravis Camera Source Node Type in the Catalog

**User Story:** As a computer vision engineer building a workflow, I want a dedicated Aravis camera input node in the flow designer, so that I can declare a GenICam camera input with the same identity the edge application uses instead of faking it with a V4L2 device path.

#### Acceptance Criteria

1. THE Node_Catalog SHALL include an Aravis_Camera_Source_Node with type id `aravis_camera_source`, category input, display name "Aravis Camera Source", no input ports, and exactly one output port of type VideoFrames.
2. THE Aravis_Camera_Source_Node SHALL declare a required string parameter `camera_id` with a minimum length of 1, carrying the Aravis camera identifier the Camera_Manager connects by, with a description and at least one working example.
3. THE Aravis_Camera_Source_Node SHALL declare optional parameters `gain` (int, constraints min 0 and max 100, default 4) and `exposure` (int, constraint min 0, default 5000000) matching the acquisition settings the Camera_Manager applies, each with a description and at least one working example.
4. THE Aravis_Camera_Source_Node SHALL be marked hardware dependent, and its architecture mappings SHALL render an appsrc-headed element chain (appsrc followed by videoconvert, with the app and videoconvertscale plugin dependencies) on every physical device architecture, and the shared dataset-fed simulation stub on the sim architecture.
5. WHEN a workflow definition containing an Aravis_Camera_Source_Node is validated, compiled, or serialized through workflow_core, THE Node_Catalog SHALL process the node through the same generic descriptor-driven paths as existing catalog nodes, with parsing then serializing a definition containing the node producing an equivalent definition.
6. THE Node_Catalog SHALL carry the Aravis_Camera_Source_Node identically in both copies (the Portal layer and the edge vendor mirror), with the two catalog source trees remaining byte-identical.

### Requirement 2: Edge Aravis Camera Discovery in the Inventory

**User Story:** As an operator, I want the edge device's Aravis bus cameras to appear in the Portal's camera registry, so that the Portal inventory reflects the GenICam cameras the edge application actually manages.

#### Acceptance Criteria

1. WHEN the Edge_Sync_Agent builds the device inventory, THE Camera_Discovery SHALL include the Aravis_Cameras reported by Aravis_Enumeration, each as a Camera_Source with type `AravisDiscovered` and origin edge-discovered.
2. THE Camera_Discovery SHALL derive each discovered Aravis_Camera's stable Camera_Source identifier deterministically from bus-stable identity fields (vendor, model, and serial number), remaining stable across device reboots and bus re-enumerations.
3. WHEN Camera_Discovery records a discovered Aravis_Camera, THE Camera_Discovery SHALL capture the Aravis identity fields (id, model, address, physical id, protocol, serial number, vendor) as the Camera_Source's parameters and capability metadata.
4. WHEN a configured Image_Source of type `Camera` carries a `cameraId` equal to a discovered Aravis_Camera's id, THE Edge_Sync_Agent SHALL report the configured Image_Source and the discovered Aravis_Camera as one Camera_Source under the configured identifier, combining the configured parameters with the discovered identity metadata.
5. WHEN a re-enumeration detects that a previously discovered Aravis_Camera is no longer present on the bus, THE Camera_Discovery SHALL mark the corresponding Camera_Source as absent rather than deleting the record.
6. IF Aravis_Enumeration fails or the Aravis runtime is unavailable, THEN THE Camera_Discovery SHALL record the failure, report the remaining (V4L2 and configured) inventory unchanged, and continue operation.
7. THE Camera_Discovery SHALL enumerate the Aravis bus through an injectable enumeration layer, leaving the existing V4L2 enumeration, absence tracking, and reporting cadence unchanged for V4L2 Camera_Sources.

### Requirement 3: Aravis Camera Picker in the Workflow Builder

**User Story:** As a computer vision engineer, I want to pick an Aravis camera from a device's synced registry when configuring the Aravis camera input node, so that the node's camera identity matches a camera that actually exists on the edge.

#### Acceptance Criteria

1. WHEN a user configures an Aravis_Camera_Source_Node's `camera_id` parameter in the Workflow_Builder, THE Camera_Picker SHALL render the camera reference control for that parameter.
2. WHEN the Camera_Picker presents Camera_Sources for an Aravis_Camera_Source_Node, THE Camera_Picker SHALL offer only Aravis-compatible Camera_Sources (type `AravisDiscovered`, or type `Camera` entries carrying a camera id parameter) from the selected reference device's Camera_Registry entries.
3. WHEN a user selects an Aravis_Camera_Source for an Aravis_Camera_Source_Node, THE Camera_Picker SHALL populate the node's `camera_id` parameter from the Camera_Source's camera id parameter, populate `gain` and `exposure` when the Camera_Source's parameters carry them, and record the selection as a `cameraBindingHint` on the node's advisory data.
4. WHERE a user prefers manual entry, THE Camera_Picker SHALL continue to accept a directly typed camera id value for the Aravis_Camera_Source_Node.
5. WHEN the Camera_Picker presents Aravis_Camera_Sources, THE Camera_Picker SHALL display each entry's name, type, camera id, sync status, and staleness indication.

### Requirement 4: Packaging Aravis Binding Points

**User Story:** As an operator, I want workflows containing the Aravis camera node to package with binding points, so that deploy-time camera binding works for Aravis inputs the same way it works for existing camera inputs.

#### Acceptance Criteria

1. WHEN the Component_Packager packages a workflow containing an Aravis_Camera_Source_Node, THE Component_Packager SHALL treat the node as a Camera_Input_Node: it SHALL emit a `bindingPoints` entry for the node in every architecture's compiled document and record the node in the version item's `camera_input_nodes` with `has_binding_points: true`.
2. THE Component_Packager SHALL mark each Aravis_Camera_Source_Node binding point as adapter-fed on every physical device architecture (carrying `aravisBinding: true` with empty slots), with the binding point's parameters carrying the node's rendered `camera_id`, `gain`, and `exposure` values.
3. WHEN the Component_Packager packages a workflow containing no Aravis_Camera_Source_Node, THE Component_Packager SHALL produce output byte-identical to its pre-feature output for that workflow.

### Requirement 5: Deploy-Time Binding for Aravis Nodes

**User Story:** As an operator deploying a workflow with an Aravis camera input, I want to bind the node to an Aravis camera registered on each target device with type mismatches rejected, so that the deployed workflow references a GenICam camera the device actually has.

#### Acceptance Criteria

1. WHEN a deployment of a workflow containing an Aravis_Camera_Source_Node is created, THE Portal SHALL present the target device's Aravis-compatible Camera_Sources as binding options for that node in the existing binding matrix, pre-selecting an entry matching the node's `cameraBindingHint` when present.
2. WHEN a Camera_Binding for an Aravis_Camera_Source_Node references a Camera_Source whose type is neither `AravisDiscovered` nor `Camera`, THE Portal SHALL reject the binding with a message identifying the type mismatch.
3. WHEN a Camera_Binding for a `camera_source` node references a Camera_Source of type `AravisDiscovered`, THE Portal SHALL accept the binding under the existing camera-type compatibility rule for camera-backed sources.
4. WHERE a user chooses manual override for an Aravis_Camera_Source_Node, THE Portal SHALL validate the override values against the Aravis_Camera_Source_Node's declared parameter constraints and record the override as the Camera_Binding.
5. WHEN a deployment containing Aravis_Camera_Source_Node bindings is submitted successfully, THE Portal SHALL deliver the bindings to target devices through the existing `dda-camera-bindings` shadow mechanism, leaving the packaged artifact unchanged.

### Requirement 6: Device-Side Resolution and Execution

**User Story:** As an operator, I want the edge device to resolve an Aravis node's binding to a local Aravis camera and execute the workflow by grabbing frames from that camera, so that the deployed workflow runs against the bound GenICam camera and fails visibly when the camera is missing.

#### Acceptance Criteria

1. WHEN the Workflow_Engine resolves Camera_Bindings for a document containing an Aravis binding point, THE Workflow_Engine SHALL resolve a `cameraSourceId` binding against the device-local inventory and produce an Aravis assignment (node id mapped to the resolved camera id and acquisition parameters) instead of substituting element arguments.
2. WHEN an Aravis binding point carries a manual override, THE Workflow_Engine SHALL produce the Aravis assignment from the override values after constraint-checking them against the vendored catalog descriptor.
3. IF an Aravis binding's `cameraSourceId` has no matching entry in the device-local inventory at resolution time, THEN THE Workflow_Engine SHALL mark the registration invalid with a reason identifying the missing Camera_Source, and the existing invalid-registration path SHALL reject trigger requests until re-resolution succeeds.
4. WHEN the Workflow_Executor runs a registered workflow whose document contains an Aravis binding point, THE Workflow_Executor SHALL determine the effective camera id and acquisition parameters (the resolved Aravis assignment when bindings were applied, otherwise the binding point's rendered parameters), grab a frame through the Camera_Manager for that camera id, and push the frame into the compiled pipeline's appsrc through the Frame_Feed.
5. IF the Camera_Manager frame grab fails for an Aravis_Camera_Source_Node during a workflow run, THEN THE Workflow_Executor SHALL mark the execution failed with the failing node id set to the Aravis_Camera_Source_Node and an error describing the camera failure.
6. WHEN the Workflow_Executor runs a workflow containing no Aravis binding point, THE Workflow_Executor SHALL execute it exactly as before this feature.

### Requirement 7: Backward Compatibility

**User Story:** As an operator, I want existing workflows, registry entries, and deployments to keep working exactly as they do today, so that adding the Aravis node type does not disrupt anything in production.

#### Acceptance Criteria

1. WHEN a workflow definition, compiled document, or deployment created before this feature is processed by the Portal or the LocalServer, THE Portal and LocalServer SHALL validate, compile, package, register, bind, and execute it with behavior identical to before this feature.
2. WHEN the Edge_Sync_Agent reports an inventory on a device with no Aravis_Cameras and no Aravis runtime, THE Edge_Sync_Agent SHALL produce a report identical in shape and content to its pre-feature report for the same configured and V4L2-discovered sources.
3. THE Camera_Registry SHALL accept and store `AravisDiscovered` Camera_Sources through the existing sync reducer without changes to the handling of existing Camera_Source types.
4. THE existing `camera_source` node type SHALL keep its current parameters, mappings, compatibility rules, and Camera_Picker behavior unchanged.
