# Requirements Document

## Introduction

The Workflow Manager adds a graphical video-pipeline builder to the edge-cv-portal cloud portal. Users compose video analytics pipelines on a drag-and-drop canvas by placing nodes (inputs, preprocessing, model inference, post-processing, and outputs) and drawing connections between them. Completed workflows are stored per account in the portal, validated, compiled into GStreamer/NVIDIA DeepStream pipeline definitions, packaged as Greengrass components (including any GStreamer plugin dependencies), and deployed to one or more edge devices where the LocalServer component executes them in its GStreamer path. All existing model types served via the embedded NVIDIA Triton Inference Server remain usable as inference nodes, and existing edge functions such as digital input and digital output become first-class nodes. A secondary capability allows users to generate workflows from a natural-language prompt using configurable Amazon Bedrock models through a chat interface.

## Glossary

- **Portal**: The edge-cv-portal cloud web application (React frontend, Lambda backend, DynamoDB storage) used to manage DDA use cases, models, components, deployments, and devices.
- **LocalServer**: The Greengrass component (aws.edgeml.dda.LocalServer.&lt;arch&gt;) running on an edge device. It embeds the NVIDIA Triton Inference Server and executes GStreamer pipelines, calling into Triton via the emltriton plugin.
- **Pipeline_Configuration**: The pre-existing pipeline definition format that LocalServer executes through its current GStreamer path (src/backend/gstreamer/), predating and distinct from a Workflow_Definition.
- **Workflow_Builder**: The graphical canvas UI within the Portal where users place Nodes and draw Connections to compose a Workflow_Definition.
- **Node**: A single processing stage in a workflow (for example a camera source, dewarp filter, model inference stage, or MQTT output) represented as a box on the canvas.
- **Node_Palette**: The categorized list of available Node types shown in the Workflow_Builder, organized into input, preprocessing, model inference, post-processing, and output sections.
- **Connection**: A directed edge drawn between an output port of one Node and an input port of another Node, defining data flow.
- **Port**: A typed attachment point on a Node where a Connection begins or ends. Each Port declares a media or data type (for example video frames, inference metadata, or event signals).
- **Workflow_Definition**: The serializable graph document (Nodes, Node configurations, and Connections) that fully describes a workflow, stored as JSON.
- **Workflow_Serializer**: The Portal component that serializes a Workflow_Definition graph to its JSON document form and parses JSON documents back into Workflow_Definition graphs.
- **Workflow_Validator**: The component that checks a Workflow_Definition for structural and semantic correctness (connectivity, port type compatibility, cycles, required configuration).
- **Workflow_Compiler**: The component that translates a valid Workflow_Definition into a GStreamer pipeline configuration executable by LocalServer, including element ordering, caps, and plugin arguments.
- **Component_Packager**: The Portal backend component that packages a compiled workflow and its GStreamer plugin dependencies into a versioned Greengrass component.
- **Workflow_Component**: The Greengrass component produced by the Component_Packager for a specific workflow version.
- **Deployment_Service**: The existing Portal capability, extended by this feature, that creates Greengrass deployments targeting edge devices or thing groups.
- **Workflow_Store**: The Portal backend persistence layer (API plus DynamoDB/S3 storage) that stores Workflow_Definitions per account and use case.
- **Workflow_Generator**: The Portal backend component that invokes a configured Amazon Bedrock model to produce a Workflow_Definition from a natural-language prompt.
- **Bedrock_Configuration**: Portal settings identifying which Amazon Bedrock model, region, and inference parameters the Workflow_Generator uses.
- **Custom_Python_Node**: A Node type whose behavior is defined by user-supplied Python code executed within the pipeline.
- **Use_Case**: The existing Portal tenancy unit (per-account production line) to which workflows, models, and devices are scoped.
- **Workflow_Test_Runner**: The Portal backend component that executes a validated and compiled Workflow_Definition against a Test_Dataset in a simulated, cloud-side environment, substituting stubs for hardware-dependent Nodes, without deploying artifacts to any edge device.
- **Test_Dataset**: A named collection of canned sample inputs (for example sample images or video frames) scoped to a Use_Case, selected or uploaded by a user, that the Workflow_Test_Runner feeds into a workflow test run as source data.

## Requirements

### Requirement 1: Graphical Workflow Canvas

**User Story:** As a computer vision engineer, I want to build video pipelines by dragging nodes onto a canvas and drawing connections between them, so that I can compose edge pipelines visually without writing GStreamer syntax.

#### Acceptance Criteria

1. WHEN a user opens the Workflow_Builder, THE Workflow_Builder SHALL display an empty canvas and the Node_Palette organized into input, preprocessing, model inference, post-processing, and output sections.
2. WHEN a user drags a Node type from the Node_Palette onto the canvas, THE Workflow_Builder SHALL add a new Node instance at the drop position with that Node type's default configuration.
3. WHEN a user drags from an output Port of one Node to an input Port of another Node, THE Workflow_Builder SHALL create a Connection between the two Ports.
4. WHEN a user attempts to connect two Ports with incompatible declared types, THE Workflow_Builder SHALL reject the Connection and display the reason for the rejection.
5. WHEN a user selects a Node or Connection and issues a delete action, THE Workflow_Builder SHALL remove the selected element and all Connections attached to a removed Node.
6. THE Workflow_Builder SHALL support canvas panning, zooming, and repositioning of Nodes by dragging.
7. WHEN a user selects a Node, THE Workflow_Builder SHALL display a configuration panel showing that Node's configurable parameters with their current values.
8. WHEN a user edits a Node parameter in the configuration panel, THE Workflow_Builder SHALL validate the entered value against the parameter's declared type and constraints and display a validation error for invalid values.
9. WHILE a user edits a workflow on the canvas, THE Workflow_Builder SHALL display inline validation markers on Nodes that have required parameters without values and on Nodes that are not reachable from any input Node.
10. WHEN a canvas edit resolves a condition indicated by an inline validation marker, THE Workflow_Builder SHALL remove that marker from the canvas.

### Requirement 2: Node Type Catalog

**User Story:** As a computer vision engineer, I want a catalog of input, preprocessing, inference, post-processing, and output node types, so that I can build complete pipelines covering existing DDA functions and new integrations.

#### Acceptance Criteria

1. THE Node_Palette SHALL provide input Node types including camera source, folder/file source, and digital input.
2. THE Node_Palette SHALL provide preprocessing Node types including dewarp, rotate, crop, and video format conversion.
3. THE Node_Palette SHALL provide a model inference Node type that runs any model registered in the Portal model registry for the selected Use_Case via the LocalServer Triton path.
4. THE Node_Palette SHALL provide post-processing Node types including a Custom_Python_Node and inference-result filtering based on configurable conditions over inference metadata.
5. THE Node_Palette SHALL provide output Node types including digital output, MQTT publish, OPC UA write, and inference-result capture to the device file system.
6. WHEN a user places a model inference Node, THE Workflow_Builder SHALL present the models available to the selected Use_Case for selection as a Node parameter.
7. WHEN a user places a Custom_Python_Node, THE Workflow_Builder SHALL accept user-supplied Python code and declared input and output Port types as Node parameters.
8. THE Node_Palette SHALL declare, for every Node type, its input Ports, output Ports, Port types, and configurable parameters with types, defaults, and constraints.

### Requirement 3: Workflow Definition Serialization

**User Story:** As a Portal developer, I want workflow graphs serialized to and from a stable JSON format, so that workflows can be stored, versioned, transferred to devices, and reloaded into the canvas without loss.

#### Acceptance Criteria

1. WHEN a Workflow_Definition graph is serialized, THE Workflow_Serializer SHALL produce a JSON document containing all Nodes, Node configurations, Node positions, Connections, and a schema version identifier.
2. WHEN a valid Workflow_Definition JSON document is parsed, THE Workflow_Serializer SHALL produce a Workflow_Definition graph equivalent to the document contents.
3. WHEN a malformed or schema-violating JSON document is parsed, THE Workflow_Serializer SHALL return a descriptive error identifying the first violation encountered.
4. FOR ALL valid Workflow_Definition graphs, serializing then parsing then serializing SHALL produce an equivalent Workflow_Definition graph and identical JSON structure (round-trip property).
5. WHEN a JSON document with an older supported schema version is parsed, THE Workflow_Serializer SHALL migrate the document to the current schema version and report the migration in the parse result.

### Requirement 4: Workflow Validation

**User Story:** As a computer vision engineer, I want my workflow checked for errors before deployment, so that I do not deploy pipelines that cannot run on the edge device.

#### Acceptance Criteria

1. WHEN validation is requested for a Workflow_Definition, THE Workflow_Validator SHALL verify that the graph contains at least one input Node and at least one output Node.
2. WHEN validation is requested for a Workflow_Definition, THE Workflow_Validator SHALL verify that every Connection joins an output Port to an input Port with compatible types.
3. IF a Workflow_Definition contains a cycle, THEN THE Workflow_Validator SHALL report a validation error identifying the Nodes participating in the cycle.
4. IF a Workflow_Definition contains a Node with a required parameter that has no value, THEN THE Workflow_Validator SHALL report a validation error identifying the Node and parameter.
5. IF a Workflow_Definition contains a Node that is not reachable from any input Node, THEN THE Workflow_Validator SHALL report a validation error identifying the unreachable Node.
6. WHEN validation completes, THE Workflow_Validator SHALL return the complete list of validation errors and warnings found, each with the associated Node or Connection identifier.
7. WHEN a user requests packaging, publishing, or deployment of a Workflow_Definition that has validation errors, THE Portal SHALL reject the request and display the validation errors.
8. THE Workflow_Builder SHALL provide a user-invocable validate action for the current Workflow_Definition.
9. WHEN a user invokes the validate action, THE Workflow_Builder SHALL run all Workflow_Validator checks on the current Workflow_Definition and display the complete list of resulting validation errors and warnings.
10. WHEN a user requests packaging, publishing, or deployment of a workflow version, THE Portal SHALL verify that the workflow version passed a Workflow_Validator run with zero validation errors before starting the requested operation.

### Requirement 5: Workflow Persistence Per Account

**User Story:** As a computer vision engineer, I want my workflows saved in the cloud portal under my account, so that I can manage a library of pipelines and deploy them to devices over time.

#### Acceptance Criteria

1. WHEN a user saves a workflow, THE Workflow_Store SHALL persist the Workflow_Definition scoped to the user's account and selected Use_Case, together with a name, description, creation timestamp, and last-modified timestamp.
2. WHEN a user saves changes to an existing workflow, THE Workflow_Store SHALL create a new workflow version and retain prior versions.
3. WHEN a user lists workflows, THE Workflow_Store SHALL return the workflows belonging to Use_Cases that the user is authorized to access.
4. WHEN a user opens a saved workflow, THE Workflow_Builder SHALL load the Workflow_Definition and render the Nodes, positions, configurations, and Connections as they were saved.
5. WHEN a user deletes a workflow that has no active deployments, THE Workflow_Store SHALL remove the workflow and its versions.
6. IF a user requests deletion of a workflow that has active deployments, THEN THE Workflow_Store SHALL reject the deletion and identify the deployments that reference the workflow.
7. WHEN a user duplicates a workflow, THE Workflow_Store SHALL create a new workflow with a copy of the source Workflow_Definition under a new name.
8. IF a user attempts to access a workflow belonging to a Use_Case the user is not authorized for, THEN THE Portal SHALL deny the request and return an authorization error.

### Requirement 6: Workflow Compilation to GStreamer Pipeline

**User Story:** As a Portal developer, I want validated workflow graphs compiled into GStreamer pipeline configurations, so that LocalServer can execute them in its existing GStreamer path.

#### Acceptance Criteria

1. WHEN a valid Workflow_Definition is compiled, THE Workflow_Compiler SHALL produce a GStreamer pipeline configuration in which each Node maps to its corresponding GStreamer element chain and each Connection maps to element linkage in topological order.
2. WHEN a Workflow_Definition contains a model inference Node, THE Workflow_Compiler SHALL emit an emltriton element configured with the selected model name and the Triton model repository and server paths used by LocalServer.
3. WHEN a Workflow_Definition contains a branch where one Node output connects to multiple downstream Nodes, THE Workflow_Compiler SHALL emit tee and queue elements to realize the branch.
4. WHEN compilation completes, THE Workflow_Compiler SHALL include in the output the list of GStreamer plugin dependencies required by the compiled pipeline beyond those bundled with LocalServer.
5. IF a Workflow_Definition contains a Node type with no available GStreamer element mapping for the target device architecture, THEN THE Workflow_Compiler SHALL return a compilation error identifying the Node and the unsupported architecture.
6. FOR ALL valid Workflow_Definitions, THE Workflow_Compiler SHALL produce a pipeline configuration that references every Node in the Workflow_Definition exactly once.

### Requirement 7: Greengrass Component Packaging

**User Story:** As an operator, I want workflows packaged as Greengrass components including their GStreamer plugin dependencies, so that a single deployment delivers everything the edge device needs to run the pipeline.

#### Acceptance Criteria

1. WHEN a user requests packaging of a validated workflow version, THE Component_Packager SHALL create a Workflow_Component containing the Workflow_Definition, the compiled pipeline configuration, and the GStreamer plugin dependency artifacts identified by the Workflow_Compiler.
2. WHEN the Component_Packager creates a Workflow_Component, THE Component_Packager SHALL assign a component version derived from the workflow version and register the component in the Greengrass component registry of the Use_Case account.
3. WHEN a workflow contains a Custom_Python_Node, THE Component_Packager SHALL include the user-supplied Python code and its declared Python package dependencies in the Workflow_Component artifacts.
4. WHEN a workflow targets multiple device architectures, THE Component_Packager SHALL produce architecture-specific artifacts for each supported LocalServer architecture (x86_64, arm64 JetPack 4, arm64 JetPack 5, arm64 JetPack 6) selected by the user.
5. IF packaging fails for any artifact, THEN THE Component_Packager SHALL report the failure with the failing artifact identified and register no partial component version.

### Requirement 8: Deployment to Edge Devices

**User Story:** As an operator, I want to deploy a workflow to one or more edge devices from the portal, so that the same pipeline can run across my fleet.

#### Acceptance Criteria

1. WHEN a user initiates deployment of a Workflow_Component, THE Deployment_Service SHALL allow selection of one or more target edge devices or thing groups within the Use_Case.
2. WHEN a deployment is created, THE Deployment_Service SHALL create a Greengrass deployment that includes the Workflow_Component and records the deployment association between the workflow version and the target devices.
3. WHEN a user views a workflow, THE Portal SHALL display the deployment status (in progress, succeeded, failed, cancelled) of that workflow per target device.
4. IF a target device does not have a LocalServer component version compatible with the Workflow_Component, THEN THE Deployment_Service SHALL report the incompatibility for that device before the deployment is submitted.
5. WHEN a user deploys a newer version of a workflow to a device that runs an older version, THE Deployment_Service SHALL replace the older Workflow_Component version with the newer version on that device.

### Requirement 9: LocalServer Workflow Execution

**User Story:** As an operator, I want deployed workflows executed by LocalServer on the edge device, so that pipelines run with the existing Triton inference and GStreamer infrastructure.

#### Acceptance Criteria

1. WHEN a Workflow_Component is deployed to a device, THE LocalServer SHALL discover the workflow's compiled pipeline configuration and register the workflow as runnable.
2. WHEN a registered workflow is triggered, THE LocalServer SHALL execute the compiled GStreamer pipeline, including loading any GStreamer plugin dependencies delivered by the Workflow_Component.
3. WHEN a workflow containing a model inference Node executes, THE LocalServer SHALL perform inference through its embedded Triton Inference Server using the model configured on the Node.
4. WHEN a workflow containing a digital output Node executes and the Node's condition evaluates true, THE LocalServer SHALL actuate the configured digital output pin with the configured signal type and pulse width.
5. WHEN a workflow containing an MQTT output Node executes, THE LocalServer SHALL publish the Node's configured payload to the configured MQTT broker and topic.
6. WHEN a workflow containing an OPC UA output Node executes, THE LocalServer SHALL write the Node's configured value to the configured OPC UA server node.
7. IF pipeline execution fails, THEN THE LocalServer SHALL record the failure with the failing element identified and report the workflow execution status as failed through its existing status reporting path.
8. WHEN a workflow containing a Custom_Python_Node executes, THE LocalServer SHALL execute the user-supplied Python code within the pipeline with access to the Node's input data and publish the code's output to the Node's output Port.

### Requirement 10: Prompt-Based Workflow Generation

**User Story:** As a computer vision engineer, I want to describe a pipeline in natural language and have it generated for me, so that I can create workflows faster than building them node by node.

#### Acceptance Criteria

1. WHERE prompt-based generation is enabled, THE Portal SHALL provide a chat interface within the Workflow_Builder that accepts natural-language workflow requests.
2. WHEN a user submits a prompt, THE Workflow_Generator SHALL invoke the Amazon Bedrock model specified in the Bedrock_Configuration with the prompt and the Node type catalog, and SHALL return a Workflow_Definition.
3. WHEN the Workflow_Generator returns a Workflow_Definition, THE Portal SHALL validate the generated Workflow_Definition with the Workflow_Validator and render the workflow on the canvas for user review before any save or deployment.
4. IF the Workflow_Generator output cannot be parsed into a valid Workflow_Definition, THEN THE Portal SHALL display an error describing the failure and SHALL leave the current canvas contents unchanged.
5. WHEN a user submits a follow-up prompt in the same chat session, THE Workflow_Generator SHALL apply the requested modification to the current canvas Workflow_Definition rather than generating a new workflow from scratch.
6. THE Portal SHALL allow a PortalAdmin to configure the Bedrock_Configuration, including model identifier, region, and inference parameters.
7. IF the Bedrock model invocation fails or exceeds a configurable timeout of at most 60 seconds, THEN THE Portal SHALL display an error message identifying the failure and preserve the user's prompt for retry.

### Requirement 11: Access Control

**User Story:** As a portal administrator, I want workflow capabilities governed by the existing portal roles, so that only authorized users can build, modify, or deploy pipelines.

#### Acceptance Criteria

1. THE Portal SHALL permit users with the DataScientist or UseCaseAdmin role in a Use_Case to create, edit, and save workflows within that Use_Case.
2. THE Portal SHALL permit users with the Operator or UseCaseAdmin role in a Use_Case to package and deploy workflows within that Use_Case.
3. THE Portal SHALL permit users with the Viewer role in a Use_Case to view workflows and deployment status within that Use_Case in read-only form.
4. IF a user without a permitted role attempts a workflow create, edit, package, deploy, or delete operation, THEN THE Portal SHALL deny the operation and return an authorization error.
5. WHEN a workflow is created, modified, deleted, packaged, or deployed, THE Portal SHALL record the action, the acting user, and a timestamp in the existing audit log.

### Requirement 12: Pre-Deployment Workflow Testing with Canned Data

**User Story:** As a computer vision engineer, I want to test my workflow with canned sample data before deploying it, so that I can confirm the pipeline behaves as expected without needing an edge device.

#### Acceptance Criteria

1. THE Workflow_Builder SHALL provide a user-invocable test action for the current Workflow_Definition.
2. WHEN a user invokes the test action, THE Workflow_Builder SHALL prompt the user to select an existing Test_Dataset scoped to the selected Use_Case or upload new sample inputs scoped to the selected Use_Case.
3. WHEN a user uploads sample inputs in a supported format with a total size of at most 500 MB, THE Workflow_Store SHALL persist the inputs as a Test_Dataset scoped to the user's account and selected Use_Case and make the Test_Dataset selectable in subsequent test runs.
4. WHEN a test run is started, THE Workflow_Test_Runner SHALL run the Workflow_Validator checks and the Workflow_Compiler on the current Workflow_Definition before executing the pipeline.
5. WHEN validation and compilation succeed for a test run, THE Workflow_Test_Runner SHALL execute the compiled pipeline in a cloud-side simulated environment using the selected Test_Dataset as source data.
6. WHEN a test run executes a workflow containing hardware-dependent Nodes (for example camera source, digital input, digital output, MQTT publish, or OPC UA write), THE Workflow_Test_Runner SHALL substitute a stub for each hardware-dependent Node that records the data the Node would have consumed or emitted without actuating any physical or device-local endpoint.
7. WHEN a test run completes, THE Workflow_Test_Runner SHALL report per-Node results including produced outputs, recorded stub activity, and any errors, each associated with the Node identifier.
8. WHEN a test run report is displayed, THE Workflow_Builder SHALL identify which Nodes were executed with stubs and describe the limitation that stubbed Nodes were simulated rather than actuated.
9. THE Workflow_Test_Runner SHALL execute test runs without requiring a connected edge device and without creating any Greengrass deployment or delivering any artifact to an edge device.
10. IF pipeline execution fails during a test run, THEN THE Workflow_Test_Runner SHALL report the failure with the failing Node identified and an error description, mark the test run status as failed, and retain the per-Node results produced before the failure.
11. IF a user uploads sample inputs that exceed 500 MB in total size or are in an unsupported format, THEN THE Workflow_Store SHALL reject the upload, display an error message identifying the reason, and persist no Test_Dataset.
12. IF the Workflow_Validator or the Workflow_Compiler reports errors during a test run, THEN THE Workflow_Test_Runner SHALL report each error with the associated Node or Connection identifier, mark the test run status as failed, and not execute the pipeline.
13. IF a test run's pipeline execution exceeds 10 minutes, THEN THE Workflow_Test_Runner SHALL terminate the execution, mark the test run status as failed with a timeout indication, and report the partial per-Node results produced before termination.

### Requirement 13: Backward Compatibility with Existing DDA Pipelines and Portal Functionality

**User Story:** As an operator, I want the Workflow Manager introduced without changing my existing pipelines, models, components, or deployments, so that production lines running today continue to operate exactly as before.

#### Acceptance Criteria

1. WHEN a LocalServer version containing workflow support is deployed to a device that runs an existing Pipeline_Configuration, THE LocalServer SHALL continue to execute that Pipeline_Configuration through its existing GStreamer path, producing the same execution results and the same status reporting as before the deployment.
2. THE Portal SHALL provide all pre-existing capabilities (model registry, model deployment, existing Greengrass component management, deployments, and Use_Case management) with the same operation outcomes and no new mandatory configuration after the Workflow Manager feature is introduced.
3. WHEN a Workflow_Component is deployed to, started on, stopped on, or removed from a device, THE LocalServer SHALL preserve the configuration and execution state of every non-workflow pipeline and component on that device without stopping, restarting, or reconfiguring them.
4. WHILE a Workflow_Component executes on a device, THE LocalServer SHALL continue executing every Pipeline_Configuration that was running when the Workflow_Component started.
5. THE Portal SHALL introduce the Workflow Manager feature without requiring migration or modification of existing Pipeline_Configurations, registered models, or Greengrass components.
6. IF a device receives no Workflow_Component, THEN THE LocalServer on that device SHALL execute Pipeline_Configurations with the same execution results and the same status reporting as before the Workflow Manager feature was introduced.
7. IF a Workflow_Component fails during deployment, startup, or execution on a device, THEN THE LocalServer SHALL continue executing every non-workflow pipeline and component on that device and SHALL report the failure through its existing status reporting path.
8. WHILE a Workflow_Component and an existing Pipeline_Configuration concurrently use the same Triton-served model on a device, THE LocalServer SHALL serve inference requests from both without altering the inference results returned to the Pipeline_Configuration.

## Implementation Notes

### Frontend packaging/deployment gap (addressed post-implementation)

The backend Component_Packager (`workflow_packaging.py`, `POST /workflows/{id}/package`, Requirements 7.1-7.5) and its validation gate (Requirement 4.7/4.10, `validation_guard` → `VALIDATION_REQUIRED`/`VALIDATION_FAILED`) shipped and were deployed, but the **Workflow_Builder frontend exposed no way to invoke packaging**: the toolbar had only New/Open/Save/Validate/Duplicate/Delete and `apiService` had no packaging method. As a result a workflow could be built, saved, and validated but never packaged into the `dda.workflow.{id}` Greengrass component that Requirement 8.1 deploys — the component simply never existed, so it never appeared on the Create Deployment screen.

This was closed with a frontend-only change (no backend/API change; the route was already live):

- `apiService.packageWorkflow(workflowId, {architectures, version?})`.
- A **Package** action in `WorkflowToolbar` (gated to the workflow edit roles per Requirement 11.1, and requiring a saved workflow) with a target-architecture multiselect over the DEVICE_ARCHITECTURES set. It saves unsaved canvas changes first (5.2).
- **Validation gating surfaced in the UI (Requirement 4.7/4.10):** packaging pre-flight-validates the version; a non-passing validation is shown as an error *inside the Package modal* (with the error findings) and packaging is not attempted, rather than failing with the backend `VALIDATION_REQUIRED`/`VALIDATION_FAILED` rejection hidden behind the modal. All packaging gate rejections (unsupported/LLM architecture, plugin lifecycle/architecture, packaging failure) are likewise shown in the modal.
- A **Deploy this workflow** affordance on the package-success notice that opens Create Deployment with the new component pre-selected (`clone_components`).

Note: the portal has no dedicated workflows list page (workflows are opened from the builder's Open picker); a standalone Workflows list page with package/deploy/version actions remains a possible follow-up enhancement.

### Multi-architecture packaging depends on a device `variant` platform override (open gap)

`workflow_packaging.py` (and `plugin_components.py`, and `components.py`'s device-arch derivation) disambiguate the three arm64 JetPack builds — all of which report Greengrass platform `architecture: aarch64` — with a custom `variant` platform attribute (`arm64_jp4`/`arm64_jp5`/`arm64_jp6`). When a workflow is packaged for **more than one** arm64 variant, each aarch64 manifest is emitted as `{os: linux, architecture: aarch64, variant: <arch>}`, and Greengrass will only match a manifest if the device advertises that `variant` value in its Nucleus `platformOverride`.

**Gap:** DDA device provisioning never sets that override — `station_install/setup_station.sh` invokes the Greengrass installer with `--provision true` and no `--init-config`, and there is no `platformOverride` anywhere in `station_install`. The working `aws.edgeml.dda.LocalServer.arm64JP6` recipe matches with only `{os, architecture}`, so it deploys regardless — but a multi-arm-variant workflow (or plugin) component fails to deploy with `FAILED_NO_STATE_CHANGE ... does not claim platform compatibility`, because no manifest's `variant` matches the device.

**Workaround (single-arch packaging):** packaging a workflow for exactly one arm64 variant emits a plain `{os, architecture: aarch64}` manifest (the packager skips the `variant` disambiguation for a single arm arch), which deploys to any aarch64 device. This is the current viable path for arm64 devices.

**Durable fix (proposed, pending):** advertise the DDA `variant` on the device's Nucleus `platformOverride` — set it during provisioning (`setup_station.sh`, driven by the quick-setup `detect_arch.sh` result), and backfill existing devices with a one-time Greengrass deployment that merges the Nucleus config. This aligns the device platform with what the packaging design already assumes and also lets `components.py` derive a device's Target_Architecture from its advertised platform. Tracked as a follow-up because it modifies the critical provisioning path and requires a live-device Nucleus reconfiguration (brief Nucleus restart) per device.
