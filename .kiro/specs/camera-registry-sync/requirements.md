# Requirements Document

## Introduction

Today, camera and input-source knowledge exists only on the edge device. The DDA LocalServer stores Image_Sources (cameras, folders, ICam, NVIDIA CSI) in its local SQLite database, created and edited through the device's local UI and REST API, and physical capture devices (V4L2 nodes, CSI sensors) are visible only to the device itself. The Edge CV Portal has no visibility into any of this: portal-built workflows reference camera device paths blindly (for example `device=/dev/video0` inlined into the compiled pipeline at packaging time), with no validation that the referenced camera exists on the target device. On JetPack 4/5, the camera_source node renders as an appsrc fed by LocalServer's camera adapter, which requires a matching device-side Image_Source to exist — a dependency that is invisible at deploy time.

This feature introduces a per-device Camera_Registry in the Portal, populated by camera registration and discovery on the edge device and synchronized between edge and Portal over the existing AWS IoT channel. It adds Portal-side camera selection: users building workflows and creating deployments pick cameras from the synced per-device inventory instead of typing device paths blind, and can bind a workflow's camera input nodes to different cameras per target device at deploy time (or supply explicit overrides). Deploy-time validation warns or errors when a workflow's input source does not match anything registered on the target device. Existing workflows with hardcoded device paths continue to work unchanged.

## Glossary

- **Portal**: The edge-cv-portal cloud web application (React frontend, Lambda backend, DynamoDB storage) used to manage DDA use cases, models, workflows, deployments, and devices.
- **LocalServer**: The Greengrass component (aws.edgeml.dda.LocalServer.&lt;arch&gt;) running on an Edge_Device. It owns the device-local Image_Source configuration tables (SQLite via image_source and input_configuration DAOs) and executes GStreamer pipelines.
- **Edge_Device**: A Greengrass core device registered in the Portal's devices table, scoped to a Use_Case, that runs LocalServer and deployed Workflow_Components.
- **Camera_Source**: A named input-source entry describing a physical or logical frame source available on an Edge_Device, including its type (V4L2 camera, NVIDIA CSI, RTSP, Folder, ICam, or adapter-fed), device path or URL, and available metadata (for example supported resolutions, formats, and acquisition settings). A Camera_Source originates either from a configured device-local Image_Source record or from device discovery of physical capture hardware.
- **Image_Source**: The pre-existing device-local record managed by LocalServer's image_source DAO and accessors, which stores input-source configuration on the Edge_Device today.
- **Camera_Discovery**: The device-side capability that enumerates physical capture devices present on an Edge_Device (for example V4L2 device nodes and CSI sensors) together with their metadata.
- **Camera_Registry**: The Portal-side store and API that holds, per Edge_Device, the set of Camera_Sources known for that device, together with per-source sync metadata (origin, version, last-reported timestamp, sync status).
- **Edge_Sync_Agent**: The LocalServer-side component that reports the Edge_Device's Camera_Source inventory (configured Image_Sources and Camera_Discovery results) to the Portal, and applies Portal-originated Camera_Source changes to the device-local Image_Source tables.
- **Portal_Sync_Service**: The Portal-side component that receives Camera_Source state from Edge_Devices, updates the Camera_Registry, and delivers Portal-originated Camera_Source changes to Edge_Devices.
- **Sync_Channel**: The AWS IoT communication path (device shadow or equivalent, following the existing iot_shadow_accessor pattern) used to exchange Camera_Source state between an Edge_Device and the Portal.
- **Conflict**: The condition in which the same Camera_Source was modified on both the Edge_Device and in the Portal after the last completed synchronization of that Camera_Source.
- **Workflow_Builder**: The graphical canvas UI within the Portal where users compose workflow definitions from nodes, including camera input nodes.
- **Workflow_Component**: The versioned Greengrass component produced by the Portal's Component_Packager for a workflow, containing the compiled pipeline (compiled_pipeline.json) deployed to Edge_Devices under /aws_dda/workflows/{workflowId}/{version}/.
- **Camera_Input_Node**: An input node in a workflow definition whose frames come from a device camera (the camera_source node type, or a custom input node type declared as camera-backed such as an RTSP source), as opposed to folder or digital-input nodes.
- **Camera_Binding**: A per-deployment, per-device mapping from a Camera_Input_Node in a workflow to either a Camera_Source registered for the target Edge_Device or an explicit set of override parameter values supplied at deployment time.
- **Deployment_Service**: The existing Portal capability (deployments.py Lambda and CreateDeployment page), extended by this feature, that creates Greengrass deployments targeting Edge_Devices.
- **Workflow_Engine**: The LocalServer subsystem (watcher, discovery, api) that discovers deployed Workflow_Components on the device and registers them as runnable workflow registrations.
- **Staleness_Threshold**: The configurable duration (default 24 hours) after which a Camera_Registry entry whose last-reported timestamp has not been refreshed is presented as stale.
- **Use_Case**: The existing Portal tenancy unit (usecase_id) to which devices, workflows, and deployments are scoped, with RBAC roles Viewer, Operator, DataScientist, UseCaseAdmin, and PortalAdmin.

## Requirements

### Requirement 1: Per-Device Camera Registry in the Portal

**User Story:** As an operator, I want to see which cameras and input sources exist on each edge device from the Portal, so that I can understand device capabilities without physical or local-UI access to the device.

#### Acceptance Criteria

1. THE Camera_Registry SHALL store, for each Edge_Device, a set of Camera_Sources each carrying a stable identifier, name, type (V4L2 camera, NVIDIA CSI, RTSP, Folder, ICam, or adapter-fed), type-specific parameters (device path or URL, and acquisition settings), available capability metadata (for example supported resolutions and formats), origin (edge-configured, edge-discovered, or portal-created), a monotonically increasing version, and a last-reported timestamp.
2. THE Camera_Registry SHALL support multiple Camera_Sources per Edge_Device, each independently identified and versioned.
3. WHEN a user with the Viewer permission for a Use_Case opens an Edge_Device's detail view in the Portal, THE Portal SHALL display that Edge_Device's Camera_Sources from the Camera_Registry with their name, type, parameters, capability metadata, origin, sync status, and last-reported timestamp.
4. THE Camera_Registry SHALL scope every Camera_Source to the Use_Case of its Edge_Device.
5. WHEN a user requests Camera_Registry data for an Edge_Device outside the Use_Cases the user is authorized for, THE Portal SHALL deny the request with an authorization error.
6. WHEN an Edge_Device has never completed a synchronization, THE Portal SHALL display that Edge_Device's Camera_Registry state as "never synced" rather than as an empty camera list.

### Requirement 2: Device-Side Camera Discovery

**User Story:** As an operator, I want the edge device to discover its physical capture devices automatically, so that the Portal inventory reflects what hardware is actually attached, not just what someone configured by hand.

#### Acceptance Criteria

1. WHEN LocalServer starts, THE Camera_Discovery SHALL enumerate the physical capture devices present on the Edge_Device (V4L2 device nodes and, on Jetson platforms, CSI sensors) and record each as a Camera_Source with origin edge-discovered.
2. WHEN Camera_Discovery enumerates a capture device, THE Camera_Discovery SHALL capture the device path, device name, and available capability metadata (supported resolutions and pixel formats) reported by the device.
3. THE Camera_Discovery SHALL re-enumerate physical capture devices at a configurable interval with a default of 5 minutes.
4. WHEN a re-enumeration detects that a previously discovered capture device is no longer present, THE Camera_Discovery SHALL mark the corresponding Camera_Source as absent rather than deleting the record.
5. WHEN a device-local Image_Source record references the same device path as a discovered capture device, THE Edge_Sync_Agent SHALL report the configured Image_Source and the discovered capture device as one Camera_Source combining the configured parameters with the discovered capability metadata.
6. IF enumeration of an individual capture device fails, THEN THE Camera_Discovery SHALL record the failure for that device, include the remaining devices in the enumeration result, and continue operation.

### Requirement 3: Edge-to-Portal Synchronization

**User Story:** As an operator, I want camera sources configured, discovered, or removed on the edge device to appear in the Portal automatically, so that the Portal's view of device cameras stays accurate.

#### Acceptance Criteria

1. WHEN a Camera_Source is created, updated, or deleted on an Edge_Device through LocalServer, or a Camera_Discovery enumeration changes the discovered inventory, THE Edge_Sync_Agent SHALL publish the resulting Camera_Source state to the Sync_Channel within 30 seconds.
2. WHEN the Portal_Sync_Service receives Camera_Source state from an Edge_Device over the Sync_Channel, THE Portal_Sync_Service SHALL update the Camera_Registry entries for that Edge_Device to reflect the received state and record the last-reported timestamp.
3. WHILE an Edge_Device has no connectivity to AWS IoT, THE Edge_Sync_Agent SHALL retain unpublished local Camera_Source changes and publish the current complete Camera_Source state when connectivity is restored.
4. WHEN LocalServer starts, THE Edge_Sync_Agent SHALL publish the device's complete current Camera_Source state to the Sync_Channel.
5. IF the Portal_Sync_Service receives Camera_Source state carrying a version older than the version already recorded in the Camera_Registry for that Camera_Source, THEN THE Portal_Sync_Service SHALL discard the stale state and retain the newer Camera_Registry entry.

### Requirement 4: Inventory Staleness and Offline Devices

**User Story:** As an operator, I want the Portal to tell me how fresh a device's camera inventory is, so that I do not trust stale data from a device that has been offline.

#### Acceptance Criteria

1. WHEN the Portal displays a Camera_Source whose last-reported timestamp is older than the Staleness_Threshold, THE Portal SHALL present the Camera_Source as stale together with its last-reported timestamp.
2. WHEN the Portal displays Camera_Registry data for an Edge_Device that AWS IoT reports as disconnected, THE Portal SHALL indicate the Edge_Device's disconnected status alongside the camera inventory.
3. THE Portal SHALL allow a user with the PortalAdmin role to configure the Staleness_Threshold.
4. WHEN a Camera_Source is marked absent by Camera_Discovery, THE Portal SHALL display the Camera_Source as absent with the timestamp at which the absence was reported.

### Requirement 5: Portal-to-Edge Synchronization

**User Story:** As an operator, I want to define or edit camera sources for a device from the Portal, so that I can prepare and correct device input configuration remotely.

#### Acceptance Criteria

1. WHEN a user with the Operator permission creates, updates, or deletes a Camera_Source for an Edge_Device in the Portal, THE Portal_Sync_Service SHALL record the change in the Camera_Registry as pending and deliver the change to the Edge_Device over the Sync_Channel.
2. WHEN the Edge_Sync_Agent receives a Portal-originated Camera_Source change, THE Edge_Sync_Agent SHALL apply the change to the device-local Image_Source tables through the existing LocalServer input-configuration accessors, preserving the validation those accessors enforce.
3. WHEN the Edge_Sync_Agent successfully applies a Portal-originated change, THE Edge_Sync_Agent SHALL report the applied state to the Sync_Channel, and THE Portal_Sync_Service SHALL mark the corresponding Camera_Registry entry as synced.
4. IF the Edge_Sync_Agent fails to apply a Portal-originated change (for example the LocalServer schema validation rejects the configuration), THEN THE Edge_Sync_Agent SHALL report the failure with a descriptive reason to the Sync_Channel, and THE Portal_Sync_Service SHALL mark the Camera_Registry entry as failed and display the reason in the Portal.
5. WHILE a target Edge_Device is disconnected from AWS IoT, THE Portal_Sync_Service SHALL retain Portal-originated changes as pending and deliver them when the Edge_Device reconnects.
6. WHEN a user attempts to create, update, or delete a Camera_Source with origin edge-discovered, THE Portal SHALL reject the modification and identify the Camera_Source as discovery-managed.
7. WHEN a user without the Operator permission attempts to create, update, or delete a Camera_Source in the Portal, THE Portal SHALL deny the request with an authorization error.

### Requirement 6: Conflict Detection and Resolution

**User Story:** As an operator, I want the system to handle the same camera being edited on the device and in the Portal at the same time in a predictable way, so that no edit is silently lost without a trace.

#### Acceptance Criteria

1. WHEN synchronization discovers that a Camera_Source was modified on both the Edge_Device and in the Portal after that Camera_Source's last completed synchronization, THE Portal_Sync_Service SHALL classify the condition as a Conflict.
2. WHEN a Conflict is detected, THE Portal_Sync_Service SHALL retain the Edge_Device's version as the effective Camera_Source configuration in the Camera_Registry.
3. WHEN a Conflict is detected, THE Portal_Sync_Service SHALL record a conflict event containing both conflicting versions, the resolution applied, and the timestamp, and THE Portal SHALL display the conflict event on the affected Edge_Device's Camera_Registry view.
4. WHEN a user with the Operator permission reviews a recorded conflict event in the Portal, THE Portal SHALL offer to re-apply the overridden Portal version as a new Portal-originated change.
5. WHEN a Camera_Source is deleted on the Edge_Device and modified in the Portal after the last completed synchronization, THE Portal_Sync_Service SHALL treat the deletion as the effective state and record a conflict event.

### Requirement 7: Camera Selection in the Workflow Builder

**User Story:** As a computer vision engineer building a workflow, I want to pick a camera from a device's registered inventory when configuring a camera input node, so that I do not have to type device paths blind.

#### Acceptance Criteria

1. WHEN a user configures a Camera_Input_Node in the Workflow_Builder and selects a reference Edge_Device, THE Workflow_Builder SHALL present the Camera_Sources registered for that Edge_Device in the Camera_Registry as selectable values for the node's device parameters.
2. WHEN a user selects a Camera_Source for a Camera_Input_Node, THE Workflow_Builder SHALL populate the node's parameters from the selected Camera_Source and record the selection as a default binding hint on the node.
3. WHERE a user prefers manual entry, THE Workflow_Builder SHALL continue to accept directly typed device parameter values for a Camera_Input_Node.
4. WHEN the Workflow_Builder presents Camera_Sources for selection, THE Workflow_Builder SHALL display each Camera_Source's name, type, device path or URL, sync status, and staleness indication.
5. WHEN a Camera_Input_Node carries a default binding hint, THE Workflow_Builder SHALL retain the hint in the workflow definition without making the workflow specific to the referenced Edge_Device.

### Requirement 8: Deploy-Time Camera Binding

**User Story:** As an operator deploying a workflow, I want to bind the workflow's camera input nodes to actual cameras registered on each target device, so that one workflow can deploy to multiple devices with different camera assignments instead of relying on paths baked in at packaging time.

#### Acceptance Criteria

1. WHEN a user creates a deployment of a Workflow_Component that contains at least one Camera_Input_Node, THE Deployment_Service SHALL present, for each Camera_Input_Node and each target Edge_Device, the Camera_Sources registered for that Edge_Device in the Camera_Registry as selectable binding options.
2. WHEN a user selects a Camera_Source as the binding for a Camera_Input_Node on a target Edge_Device, THE Deployment_Service SHALL record a Camera_Binding associating that Camera_Input_Node, that Edge_Device, and the selected Camera_Source identifier in the deployment.
3. THE Deployment_Service SHALL accept distinct Camera_Bindings for the same Camera_Input_Node across different target Edge_Devices in a single deployment.
4. WHERE a user chooses manual override instead of selecting a registered Camera_Source, THE Deployment_Service SHALL accept explicit parameter values for the Camera_Input_Node, validate the values against the node type's declared parameter constraints, and record the override as the Camera_Binding.
5. WHEN a Camera_Input_Node carries a default binding hint that matches a Camera_Source present in a target Edge_Device's Camera_Registry, THE Deployment_Service SHALL pre-select that Camera_Source as the proposed binding for that Edge_Device, subject to user confirmation or change.
6. THE Deployment_Service SHALL deliver Camera_Bindings to target Edge_Devices as deployment configuration alongside the Workflow_Component, leaving the packaged Workflow_Component artifact unchanged.
7. IF a deployment of a Workflow_Component containing a Camera_Input_Node is submitted with a Camera_Input_Node that has neither a selected Camera_Source nor a manual override for a target Edge_Device, THEN THE Deployment_Service SHALL reject the deployment with a message identifying the unbound Camera_Input_Node and Edge_Device.
8. IF a target Edge_Device has no Camera_Sources in the Camera_Registry and has never completed a synchronization, THEN THE Deployment_Service SHALL display a warning and permit binding only through manual override.
9. WHERE a Workflow_Component contains no Camera_Input_Node, THE Deployment_Service SHALL create the deployment without requesting Camera_Bindings.

### Requirement 9: Deploy-Time Binding Validation

**User Story:** As an operator, I want camera bindings checked against the target device's registered cameras before the deployment goes out, so that I catch missing or mismatched cameras in the Portal instead of on the factory floor.

#### Acceptance Criteria

1. WHEN a deployment with Camera_Bindings is submitted, THE Deployment_Service SHALL verify that every Camera_Binding referencing a registered Camera_Source resolves to a Camera_Source currently present in the Camera_Registry for the target Edge_Device.
2. IF a Camera_Binding references a Camera_Source that is absent from the target Edge_Device's Camera_Registry at submission time, THEN THE Deployment_Service SHALL reject the deployment with a message identifying the missing Camera_Source and Edge_Device.
3. IF a Camera_Binding references a Camera_Source that is marked absent, stale, or whose sync status is failed or pending, THEN THE Deployment_Service SHALL display a warning identifying the Camera_Source's condition and require explicit user confirmation before creating the deployment.
4. WHEN a Camera_Binding's Camera_Source type is incompatible with the Camera_Input_Node's node type (for example a Folder source bound to a camera_source node), THE Deployment_Service SHALL reject the binding with a message identifying the type mismatch.
5. WHEN a deployment targets an Edge_Device and the Workflow_Component's Camera_Input_Nodes carry only compiled-in device paths without Camera_Bindings, THE Deployment_Service SHALL compare each compiled-in device path against the target Edge_Device's Camera_Registry and display a warning for each path that matches no registered Camera_Source.

### Requirement 10: Device-Side Binding Resolution

**User Story:** As an operator, I want the edge device to resolve camera bindings when a deployed workflow is registered, so that the workflow runs against the bound camera and fails visibly when the camera is missing.

#### Acceptance Criteria

1. WHEN the Workflow_Engine registers a deployed Workflow_Component that carries Camera_Bindings, THE Workflow_Engine SHALL resolve each Camera_Binding against the device-local Camera_Source inventory (Image_Source records and Camera_Discovery results) and apply the resolved camera parameters to the corresponding Camera_Input_Node before the workflow becomes runnable.
2. IF a Camera_Binding references a Camera_Source that has no matching entry in the device-local inventory at registration time, THEN THE Workflow_Engine SHALL mark the workflow registration as invalid with a reason identifying the missing Camera_Source, and THE Workflow_Engine SHALL reject trigger requests for that registration.
3. WHEN a Camera_Binding carries manual override parameter values, THE Workflow_Engine SHALL apply the override values to the Camera_Input_Node in place of a registered Camera_Source lookup.
4. WHEN a previously missing Camera_Source becomes present on the Edge_Device after a registration was marked invalid, THE Workflow_Engine SHALL re-evaluate the affected registration and mark it registered when all Camera_Bindings resolve.
5. WHEN a Workflow_Component without Camera_Bindings is registered, THE Workflow_Engine SHALL register the Workflow_Component using the parameter values compiled into the Workflow_Component.

### Requirement 11: Backward Compatibility

**User Story:** As an operator, I want existing workflows and device configurations to keep working exactly as they do today, so that introducing camera sync does not disrupt production lines.

#### Acceptance Criteria

1. WHEN a Workflow_Component packaged before this feature (carrying only compiled-in device paths and no Camera_Bindings) is deployed or already resides on an Edge_Device, THE Workflow_Engine SHALL register and execute the Workflow_Component using the compiled-in parameter values with the same behavior as before this feature.
2. WHEN the Edge_Sync_Agent or Camera_Discovery is unavailable or fails on an Edge_Device, THE LocalServer SHALL continue executing its existing pipelines and deployed Workflow_Components without interruption.
3. THE Edge_Sync_Agent SHALL read from and write to the device-local Image_Source tables only through the existing LocalServer accessors, leaving the schema and semantics of those tables unchanged for existing LocalServer functionality.
4. WHEN a LocalServer version containing this feature is deployed to an Edge_Device, THE LocalServer SHALL preserve all existing Image_Source records and classic pipeline configurations without migration or modification.
5. THE Portal SHALL introduce the Camera_Registry without requiring changes to existing saved workflow definitions or previously created deployments.

### Requirement 12: Access Control and Auditing

**User Story:** As a use case administrator, I want camera registry changes and binding decisions restricted by role and recorded, so that I can trace who changed device input configuration and when.

#### Acceptance Criteria

1. THE Portal SHALL permit viewing the Camera_Registry to users holding the Viewer permission or higher for the Edge_Device's Use_Case.
2. THE Portal SHALL restrict creating, updating, and deleting Camera_Sources and confirming binding warnings to users holding the Operator permission or higher for the Edge_Device's Use_Case.
3. WHEN a Camera_Source is created, updated, or deleted through the Portal, or a Conflict is resolved, or a deployment with Camera_Bindings is created, THE Portal SHALL record an audit event in the existing Portal audit log containing the acting user, the affected Edge_Device, the affected Camera_Source or deployment identifier, and the timestamp.
4. WHEN Camera_Source state is exchanged over the Sync_Channel, THE Portal_Sync_Service and Edge_Sync_Agent SHALL use the Edge_Device's existing AWS IoT identity and policies for authentication and authorization of the exchange.
