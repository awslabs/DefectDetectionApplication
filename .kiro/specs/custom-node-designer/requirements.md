# Requirements Document

## Introduction

The Custom Node Designer extends the Workflow Manager (spec: workflow-manager) with a tool for adding new node types to the Workflow_Builder palette without a platform release. Users can create a custom node from generated template code that exposes a per-frame processing hook (frame in → user code → frame out), generate that starter code from a natural-language description using a configured Amazon Bedrock model, or import an existing native plugin: a GStreamer plugin from a public source repository, an NVIDIA DeepStream plugin for Jetson devices, or a well-known GStreamer module selected from the official GStreamer module listing. Created and imported plugins are built per target device architecture in an isolated build environment, exercised against sample inputs in a visual plugin simulator before entering the library, and progressed through a dev → test → prod lifecycle in which promotion to prod requires an explicit security review (they are native code executed on edge devices). Approved plugin artifacts are signed, stored in the curated Plugin_Library used by workflow packaging, and registered in the Node_Type_Catalog so they appear in the Node_Palette with ports, parameters, and help like any built-in node type. The feature also defines how custom nodes behave in cloud test runs, which portal roles may manage them, and how custom node types are versioned, deprecated, and removed.

## Glossary

- **Portal**: The edge-cv-portal cloud web application (React frontend, Lambda backend, DynamoDB storage) used to manage DDA use cases, models, components, deployments, devices, and workflows.
- **LocalServer**: The Greengrass component (aws.edgeml.dda.LocalServer.&lt;arch&gt;) running on an edge device. It embeds the NVIDIA Triton Inference Server and executes GStreamer pipelines, loading bundled GStreamer plugins and plugins delivered with a Workflow_Component.
- **Workflow_Builder**: The graphical canvas UI within the Portal where users place Nodes and draw Connections to compose a Workflow_Definition.
- **Node**: A single processing stage in a workflow represented as a box on the Workflow_Builder canvas.
- **Node_Palette**: The categorized list of available Node types shown in the Workflow_Builder, organized into input, preprocessing, model inference, post-processing, and output sections.
- **Port**: A typed attachment point on a Node where a Connection begins or ends. Each Port declares a media or data type (for example video frames, inference metadata, or event signals).
- **Node_Type_Catalog**: The catalog of Node type declarations shared by the Portal, the cloud test sandbox, and LocalServer. Each declaration specifies a Node type's category, input and output Ports, parameters with types, defaults, constraints, descriptions, and examples, per-architecture GStreamer mappings, plugin dependencies, and hardware-dependence flag.
- **Node_Designer**: The Portal capability introduced by this feature that lets authorized users create Custom_Node_Types from template code, generated code, or imported native plugins, and manage those Custom_Node_Types through their lifecycle.
- **Custom_Node_Type**: A Node type added to the Node_Type_Catalog through the Node_Designer rather than shipped with the platform. A Custom_Node_Type is backed by one or more Plugin_Artifacts.
- **Plugin_Scaffold**: The generated project produced by the Node_Designer for a new Custom_Node_Type, containing template plugin source code with a Frame_Processing_Hook and per-architecture build configuration.
- **Frame_Processing_Hook**: The designated function within a Plugin_Scaffold where the user writes processing logic. The hook receives each frame arriving at the Node's input Port and returns the frame content to emit on the Node's output Port.
- **Node_Generator**: The Portal backend component that invokes the Amazon Bedrock model specified in the Bedrock_Configuration to produce Plugin_Scaffold source code from a natural-language description of the desired node behavior.
- **Bedrock_Configuration**: The existing Portal settings (spec: workflow-manager) identifying which Amazon Bedrock model, region, and inference parameters generation features use.
- **GStreamer_Plugin**: A native GStreamer plugin (shared library exposing one or more GStreamer elements) that can be loaded by the GStreamer runtime on a device or in the cloud test sandbox.
- **DeepStream_Plugin**: A GStreamer_Plugin built against the NVIDIA DeepStream SDK, executable only on device architectures with a matching DeepStream runtime (Jetson JetPack 4, 5, or 6).
- **Plugin_Importer**: The Portal backend component that retrieves plugin source or binaries from a user-specified public repository or from the Module_Listing, records provenance, and submits the plugin for building.
- **Module_Listing**: The official GStreamer module index published at https://gstreamer.freedesktop.org/modules/, from which the Portal offers a selectable list of well-known public GStreamer modules.
- **Plugin_Set_Classification**: The upstream GStreamer quality taxonomy for a plugin's official plugin set: good (gst-plugins-good: well-maintained, well-tested, properly licensed), bad (gst-plugins-bad: lacking review, testing, or active maintenance), ugly (gst-plugins-ugly: good quality but with licensing or distribution concerns), or unclassified (not part of an official GStreamer plugin set).
- **Plugin_Build_Service**: The Portal backend component that compiles plugin source into Plugin_Artifacts for each selected Target_Architecture within an isolated build environment and signs the resulting artifacts.
- **Plugin_Artifact**: A built plugin binary (.so shared library) for one Target_Architecture, stored in the Plugin_Library with an integrity checksum and a signature produced by the Plugin_Build_Service.
- **Plugin_Library**: The curated per-account plugin storage (portal S3) from which the Component_Packager retrieves plugin artifacts (plugins/&lt;arch&gt;/*.so) when packaging a Workflow_Component.
- **Plugin_Record**: The stored metadata for a created, generated, or imported plugin: name, version, provenance (source repository URL and revision, scaffold origin, or generation prompt), importing or creating user, timestamps, per-architecture Plugin_Artifacts with checksums and signatures, Lifecycle_State, and security review decision.
- **Plugin_Simulator**: The Portal capability that executes a plugin's x86_64 Plugin_Artifact in a sandboxed cloud environment against user-selected sample inputs and displays the input frames, output frames, and emitted metadata for comparison.
- **Lifecycle_State**: The state of a Plugin_Record version within the dev → test → prod progression. dev: under development, usable only inside the Node_Designer and Plugin_Simulator. test: available in the Node_Palette for building and cloud test runs, deployable only to Test_Devices. prod: fully released, deployable to any device.
- **Test_Device**: An edge device designated by a UseCaseAdmin within a Use_Case as a non-production device for evaluating workflows and custom nodes.
- **Target_Architecture**: A device architecture a plugin can be built for: x86_64 (amd64), x86_64 with NVIDIA GPU runtime (x86_64_nvidia), arm64 JetPack 4 (arm64_jp4), arm64 JetPack 5 (arm64_jp5), or arm64 JetPack 6 (arm64_jp6). The cloud test sandbox and the Plugin_Simulator execute x86_64 builds.
- **Plugin_Component**: The versioned Greengrass component automatically produced from a Plugin_Record's built Plugin_Artifacts, carrying one platform manifest per successfully built Target_Architecture. Plugin_Components appear on the deployment screen and are the unit Workflow_Components declare Greengrass dependencies on.
- **Workflow_Compiler**: The Workflow Manager component that translates a valid Workflow_Definition into a GStreamer pipeline configuration, including the list of plugin dependencies beyond those bundled with LocalServer.
- **Component_Packager**: The Portal backend component that packages a compiled workflow and its plugin dependencies into a versioned Greengrass component (Workflow_Component).
- **Workflow_Component**: The Greengrass component produced by the Component_Packager for a specific workflow version.
- **Workflow_Test_Runner**: The Portal backend component that executes a compiled workflow against a Test_Dataset in the cloud-side simulated environment (x86_64), substituting stubs for hardware-dependent Nodes.
- **Test_Dataset**: The existing named collection of canned sample inputs (spec: workflow-manager) scoped to a Use_Case, selected or uploaded by a user.
- **Use_Case**: The existing Portal tenancy unit (per-account production line) to which workflows, models, devices, and Custom_Node_Types are scoped.
- **PortalAdmin**: The existing Portal role with administrative rights across the Portal installation.
- **UseCaseAdmin**: The existing Portal role with administrative rights within a Use_Case.

## Requirements

### Requirement 1: Create a Custom Node from Template Code

**User Story:** As a computer vision engineer, I want the portal to scaffold a new custom node as a GStreamer plugin with template code exposing a frame-processing hook, so that I can add my own per-frame processing logic to the palette by writing only the code inside the hook.

#### Acceptance Criteria

1. WHEN a user initiates custom node creation, THE Node_Designer SHALL collect a Custom_Node_Type name, description, palette category, input and output Port declarations, parameter declarations, and one or more Target_Architectures.
2. WHEN a user confirms the collected custom node details, THE Node_Designer SHALL generate a Plugin_Scaffold containing template plugin source code with a Frame_Processing_Hook and build configuration for each selected Target_Architecture.
3. THE Frame_Processing_Hook in a generated Plugin_Scaffold SHALL receive each frame arriving at the Node's input Port and SHALL return the frame content that the plugin emits on the Node's output Port.
4. THE Plugin_Scaffold SHALL expose the Custom_Node_Type's declared parameters to the Frame_Processing_Hook as named values readable by the user's hook code.
5. WHEN a Plugin_Scaffold is generated, THE Node_Designer SHALL make the Plugin_Scaffold source available to the user for download and editing.
6. WHEN a user submits Plugin_Scaffold source (original or edited), THE Node_Designer SHALL forward the source to the Plugin_Build_Service for the Target_Architectures selected for the Custom_Node_Type.
7. IF Plugin_Scaffold generation fails, THEN THE Node_Designer SHALL display an error identifying the failing input and SHALL create no Plugin_Record.

### Requirement 2: Prompt-Based Custom Node Generation

**User Story:** As a process engineer without programming experience, I want to describe the node I need in natural language and receive working starter plugin code, so that I can create a custom node without writing code myself.

#### Acceptance Criteria

1. WHERE prompt-based node generation is enabled, THE Node_Designer SHALL provide a chat interface that accepts a natural-language description of the desired node behavior.
2. WHEN a user submits a node description prompt, THE Node_Generator SHALL invoke the Amazon Bedrock model specified in the Bedrock_Configuration with the prompt and the Plugin_Scaffold template conventions, and SHALL return complete Plugin_Scaffold source code whose Frame_Processing_Hook implements the described behavior.
3. WHEN the Node_Generator returns Plugin_Scaffold source code, THE Node_Designer SHALL display the generated source to the user for review and optional editing before submission to the Plugin_Build_Service.
4. WHEN a user submits a follow-up prompt in the same chat session, THE Node_Generator SHALL apply the requested modification to the current generated Plugin_Scaffold source rather than generating new source from scratch.
5. WHEN a user accepts generated Plugin_Scaffold source, THE Node_Designer SHALL process the generated source through the same build, simulation, lifecycle, and security review path as user-written Plugin_Scaffold source, and THE Plugin_Record SHALL record the generation prompt as provenance.
6. IF the Node_Generator output does not form a buildable Plugin_Scaffold, THEN THE Node_Designer SHALL display an error describing the failure and SHALL preserve the user's prompt for retry.
7. IF the Bedrock model invocation fails or exceeds a configurable timeout of at most 60 seconds, THEN THE Node_Designer SHALL display an error message identifying the failure and SHALL preserve the user's prompt for retry.

### Requirement 3: Per-Architecture Plugin Builds

**User Story:** As a computer vision engineer, I want my created or imported plugins built for each device architecture I target, so that the same custom node runs on x86_64 and Jetson devices and in cloud tests.

#### Acceptance Criteria

1. WHEN plugin source is submitted for building, THE Plugin_Build_Service SHALL compile the source into one Plugin_Artifact per selected Target_Architecture.
2. THE Plugin_Build_Service SHALL execute each build in an isolated build environment that has no access to the credentials, data, or build environments of other Use_Cases.
3. WHEN a build completes for a Target_Architecture, THE Plugin_Build_Service SHALL sign the resulting Plugin_Artifact and store the Plugin_Artifact in the Plugin_Library under that Target_Architecture together with an integrity checksum and the signature recorded in the Plugin_Record.
4. IF the build fails for a Target_Architecture, THEN THE Plugin_Build_Service SHALL report the failure with the failing Target_Architecture identified and the compiler output included, and SHALL store no Plugin_Artifact for that Target_Architecture.
5. WHEN builds complete, THE Node_Designer SHALL display the per-architecture build status (succeeded or failed) for the Plugin_Record.
6. WHERE a plugin source distribution already provides prebuilt binaries for a Target_Architecture, THE Plugin_Build_Service SHALL accept the prebuilt binary as the Plugin_Artifact for that Target_Architecture and record its checksum and signature in the Plugin_Record.

### Requirement 4: Import a GStreamer Plugin from a Public Repository

**User Story:** As a computer vision engineer, I want to import an existing GStreamer plugin from a public source repository and add it to the node library, so that I can use community plugins in my workflows without waiting for a platform release.

#### Acceptance Criteria

1. WHEN a user submits a public repository URL and an optional revision identifier for import, THE Plugin_Importer SHALL retrieve the plugin source from the repository at the specified revision or at the repository default revision when no revision is specified.
2. WHEN plugin source is retrieved, THE Plugin_Importer SHALL create a Plugin_Record containing the repository URL, the retrieved revision identifier, the importing user, and the retrieval timestamp.
3. WHEN a Plugin_Record is created from an import, THE Plugin_Importer SHALL submit the retrieved source to the Plugin_Build_Service for the Target_Architectures selected by the user.
4. IF the repository is unreachable or the specified revision does not exist, THEN THE Plugin_Importer SHALL display an error identifying the failure and SHALL create no Plugin_Record.
5. IF the retrieved source does not contain a buildable GStreamer_Plugin, THEN THE Plugin_Importer SHALL report the finding to the user and SHALL mark the Plugin_Record as failed.
6. WHEN an imported plugin's builds succeed for at least one Target_Architecture, THE Node_Designer SHALL prompt the user to declare the Custom_Node_Type details for the plugin's element as specified in Requirement 8.


### Requirement 5: Import an NVIDIA DeepStream Plugin

**User Story:** As a computer vision engineer, I want to import NVIDIA DeepStream plugins for my Jetson devices, so that I can use DeepStream-accelerated processing stages in workflows targeting Jetson hardware.

#### Acceptance Criteria

1. WHEN a user marks an import as a DeepStream_Plugin, THE Node_Designer SHALL restrict the selectable Target_Architectures to arm64 JetPack 4, arm64 JetPack 5, and arm64 JetPack 6.
2. WHEN a DeepStream_Plugin is built for a Jetson Target_Architecture, THE Plugin_Build_Service SHALL build against the DeepStream SDK version matching that Target_Architecture's JetPack release.
3. WHEN a Custom_Node_Type backed by a DeepStream_Plugin is registered, THE Node_Type_Catalog SHALL record the Custom_Node_Type as unavailable on Target_Architectures without a matching DeepStream runtime.
4. IF a user requests packaging of a workflow containing a DeepStream-backed Custom_Node_Type for a Target_Architecture without a matching DeepStream runtime, THEN THE Workflow_Compiler SHALL report a compilation error identifying the Node and the unsupported Target_Architecture.

### Requirement 6: Import from the Official GStreamer Module Listing

**User Story:** As a computer vision engineer, I want to pick a well-known GStreamer module from a select box populated from the official GStreamer module listing, so that I can import public plugins by browsing instead of typing repository URLs.

#### Acceptance Criteria

1. WHEN a user opens the module import view, THE Portal SHALL retrieve the current module index from the Module_Listing and present the retrieved modules in a selectable list with module names.
2. WHEN a user selects a module from the list, THE Plugin_Importer SHALL use the selected module's published repository location as the import source and proceed as specified in Requirement 4.
3. IF the Module_Listing is unreachable or returns an unparseable response, THEN THE Portal SHALL display an error identifying the failure and SHALL offer manual repository URL entry as specified in Requirement 4 as the alternative import path.
4. WHEN the module index is retrieved, THE Portal SHALL cache the retrieved index and reuse the cached index for subsequent module import views for at most 24 hours before retrieving a fresh index.

### Requirement 7: Visual Plugin Simulator

**User Story:** As a computer vision engineer, I want to feed sample input into my plugin and view the resulting output frames and metadata, so that I can verify the plugin's behavior before adding the node to the library.

#### Acceptance Criteria

1. WHEN a user opens the Plugin_Simulator for a Plugin_Record that has an x86_64 Plugin_Artifact, THE Plugin_Simulator SHALL prompt the user to select an existing Test_Dataset scoped to the selected Use_Case or upload sample input frames or video.
2. WHEN a user starts a simulation run, THE Plugin_Simulator SHALL execute the plugin's x86_64 Plugin_Artifact against the selected sample input within a sandboxed environment that has no access to other Portal workloads, other Use_Cases' data, or the Plugin_Library write path.
3. WHEN a simulation run completes, THE Plugin_Simulator SHALL display the input frames and the corresponding output frames side by side together with the metadata the plugin emitted for each frame.
4. WHEN a simulation run completes, THE Plugin_Simulator SHALL allow the user to configure the plugin's declared parameters and start another simulation run with the changed parameter values.
5. IF a Plugin_Record has no x86_64 Plugin_Artifact, THEN THE Plugin_Simulator SHALL refuse to start a simulation run and SHALL describe that simulation requires a successful x86_64 build.
6. IF plugin execution fails or terminates abnormally during a simulation run, THEN THE Plugin_Simulator SHALL report the failure with the plugin's error output included and SHALL contain the failure within the sandboxed environment.
7. IF a simulation run exceeds 5 minutes, THEN THE Plugin_Simulator SHALL terminate the run, mark the run as failed with a timeout indication, and display the partial results produced before termination.

### Requirement 8: Node Type Declaration and Palette Integration

**User Story:** As a computer vision engineer, I want my created or imported plugin to appear in the workflow builder palette as a regular node with ports, parameters, and help text, so that anyone in my use case can use the custom node like a built-in node.

#### Acceptance Criteria

1. WHEN a user registers a Custom_Node_Type for a plugin, THE Node_Designer SHALL collect the display name, palette category, input and output Ports with declared Port types, parameters with types, defaults, constraints, descriptions, and examples, the hardware-dependence flag, and the mapping from the plugin's element and its properties to the declared parameters for each built Target_Architecture.
2. WHEN a Custom_Node_Type registration is completed for a Plugin_Record whose Lifecycle_State is test or prod, THE Node_Type_Catalog SHALL include the Custom_Node_Type with the same declaration structure as built-in Node types, scoped to the Use_Cases selected at registration.
3. WHEN a Custom_Node_Type is included in the Node_Type_Catalog for a Use_Case, THE Node_Palette SHALL display the Custom_Node_Type in its declared category for users of that Use_Case.
4. WHEN a user places a Custom_Node_Type Node on the canvas, THE Workflow_Builder SHALL provide the same configuration panel behavior as for built-in Node types, including parameter validation, field-level descriptions, and example values.
5. IF a Custom_Node_Type registration declares a Port type outside the Port types defined by the Node_Type_Catalog, THEN THE Node_Designer SHALL reject the registration and identify the invalid Port declaration.
6. WHEN a Custom_Node_Type is registered, THE Node_Type_Catalog SHALL record the plugin dependency of the Custom_Node_Type so that the Workflow_Compiler includes the plugin in the compiled pipeline's dependency list for each Target_Architecture.

### Requirement 9: Plugin Lifecycle States (dev, test, prod)

**User Story:** As a use case administrator, I want every custom or imported node to progress through dev, test, and prod states with usage gated per state, so that unproven native code cannot reach production devices.

#### Acceptance Criteria

1. WHEN a Plugin_Record is created (from a Plugin_Scaffold, generated source, or an import), THE Portal SHALL set the Plugin_Record's Lifecycle_State to dev.
2. WHILE a Plugin_Record's Lifecycle_State is dev, THE Node_Type_Catalog SHALL exclude Custom_Node_Types backed by that Plugin_Record from the Node_Palette.
3. WHILE a Plugin_Record's Lifecycle_State is dev, THE Node_Designer SHALL permit editing the plugin source, rebuilding, and Plugin_Simulator runs for that Plugin_Record.
4. WHEN a UseCaseAdmin promotes a Plugin_Record from dev to test, THE Portal SHALL verify that the Plugin_Record has at least one successfully built Plugin_Artifact and set the Lifecycle_State to test.
5. IF promotion from dev to test is requested for a Plugin_Record with no successfully built Plugin_Artifact, THEN THE Portal SHALL reject the promotion and identify the missing build.
6. WHILE a Plugin_Record's Lifecycle_State is test, THE Node_Palette SHALL display Custom_Node_Types backed by that Plugin_Record with a visible test-state marker.
7. WHILE a Plugin_Record's Lifecycle_State is test, THE Portal SHALL permit workflows containing Custom_Node_Types backed by that Plugin_Record to be saved, validated, executed by the Workflow_Test_Runner, and deployed to Test_Devices.
8. IF a user requests deployment of a Workflow_Component containing a Custom_Node_Type backed by a test-state Plugin_Record to a device that is not a Test_Device, THEN THE Deployment_Service SHALL reject the deployment and identify the Custom_Node_Type and its Lifecycle_State.
9. WHEN a UseCaseAdmin promotes a Plugin_Record from test to prod, THE Portal SHALL verify that the Plugin_Record has an approved security review as specified in Requirement 10 and set the Lifecycle_State to prod.
10. IF promotion from test to prod is requested for a Plugin_Record without an approved security review, THEN THE Portal SHALL reject the promotion and identify the missing security review approval.
11. WHILE a Plugin_Record's Lifecycle_State is prod, THE Deployment_Service SHALL permit deployment of Workflow_Components containing Custom_Node_Types backed by that Plugin_Record to any device in the Use_Case.
12. WHEN a UseCaseAdmin demotes a Plugin_Record from prod to test or from test to dev, THE Portal SHALL apply the demoted state's gates to subsequent packaging and deployment requests WHILE Workflow_Components already deployed to devices continue to run unchanged.
13. WHEN a new Plugin_Record version is created from changed source or a changed declaration, THE Portal SHALL set the new version's Lifecycle_State to dev independently of the Lifecycle_State of prior versions.

### Requirement 10: Security Review, Signing, and Approval of Native Plugins

**User Story:** As a portal administrator, I want every created, generated, or imported plugin security-reviewed, approved, and signed before it can reach production devices, so that untrusted native code cannot run in production without an explicit trust decision.

#### Acceptance Criteria

1. WHEN a Plugin_Record version is created, THE Portal SHALL set the Plugin_Record version's security review decision to pending.
2. WHEN a PortalAdmin reviews a pending Plugin_Record, THE Portal SHALL display the Plugin_Record provenance (source repository URL and revision, scaffold origin, or generation prompt, plus the importing or creating user and timestamps), the per-architecture Plugin_Artifact checksums and signatures, and the plugin source for inspection.
3. WHEN a PortalAdmin approves or rejects a Plugin_Record's security review, THE Portal SHALL record the decision, the acting PortalAdmin, and a timestamp in the existing audit log.
4. WHEN the Component_Packager includes a Plugin_Artifact in a Workflow_Component, THE Component_Packager SHALL verify the Plugin_Artifact against the checksum and signature recorded in the Plugin_Record and SHALL reject the packaging request when either verification fails.
5. WHEN a new version of a plugin is created from changed source or rebuilt, THE Portal SHALL set the new Plugin_Record version's security review decision to pending independently of prior approvals.
6. WHEN a LocalServer loads a plugin delivered by a Workflow_Component, THE LocalServer SHALL verify the plugin file against the checksum recorded in the Workflow_Component manifest and SHALL refuse to load a plugin whose checksum verification fails, reporting the failure through its existing status reporting path.

### Requirement 11: Packaging and Delivery of Custom Node Plugins

**User Story:** As an operator, I want workflows that use custom nodes packaged with the required plugin binaries per architecture, so that a single deployment delivers everything the edge device needs to run the custom node.

#### Acceptance Criteria

1. WHEN a user requests packaging of a workflow containing a Custom_Node_Type, THE Component_Packager SHALL include the Custom_Node_Type's Plugin_Artifact from the Plugin_Library for each Target_Architecture selected for packaging.
2. IF the Plugin_Library contains no Plugin_Artifact for a Custom_Node_Type on a Target_Architecture selected for packaging, THEN THE Component_Packager SHALL reject the packaging request and identify the Custom_Node_Type and the missing Target_Architecture.
3. IF a user requests packaging of a workflow containing a Custom_Node_Type whose backing Plugin_Record's Lifecycle_State is dev, THEN THE Component_Packager SHALL reject the packaging request and identify the Custom_Node_Type and its Lifecycle_State.
4. WHEN a Workflow_Component containing a Custom_Node_Type's Plugin_Artifact is deployed to a device, THE LocalServer SHALL load the delivered plugin and execute the Custom_Node_Type's element within the compiled pipeline.

### Requirement 12: Custom Nodes in Cloud Test Runs

**User Story:** As a computer vision engineer, I want to test workflows containing custom nodes in the cloud test sandbox, so that I can verify custom node behavior before deploying to a device.

#### Acceptance Criteria

1. WHEN a test run executes a workflow containing a Custom_Node_Type that has an x86_64 Plugin_Artifact, THE Workflow_Test_Runner SHALL load the x86_64 Plugin_Artifact and execute the Custom_Node_Type within the simulated pipeline.
2. IF a test run executes a workflow containing a Custom_Node_Type that has no x86_64 Plugin_Artifact, THEN THE Workflow_Test_Runner SHALL substitute a stub for that Node that records the data the Node would have consumed and passes input frames through unchanged, and SHALL identify the Node as stubbed in the test run report.
3. WHEN a test run report includes a stubbed Custom_Node_Type, THE Workflow_Builder SHALL describe the limitation that the Custom_Node_Type was simulated because no x86_64 build exists.

### Requirement 13: Access Control for Node Designer Operations

**User Story:** As a portal administrator, I want custom node creation, import, and library management restricted to administrative roles, so that only trusted users can introduce native code into the platform.

#### Acceptance Criteria

1. THE Portal SHALL permit users with the UseCaseAdmin role in a Use_Case or the PortalAdmin role to create Plugin_Scaffolds, use the Node_Generator, import plugins, run the Plugin_Simulator, register Custom_Node_Types, promote or demote Plugin_Records between dev and test, and manage Plugin_Records scoped to that Use_Case.
2. THE Portal SHALL permit only users with the PortalAdmin role to approve or reject Plugin_Record security reviews.
3. THE Portal SHALL permit users with the DataScientist, Operator, or Viewer role in a Use_Case to view the Custom_Node_Types and Plugin_Records of that Use_Case in read-only form.
4. IF a user without a permitted role attempts a create, generate, import, simulate, register, promote, demote, approve, update, or remove operation on a Plugin_Record or Custom_Node_Type, THEN THE Portal SHALL deny the operation and return an authorization error.
5. WHEN a Plugin_Record or Custom_Node_Type is created, generated, imported, simulated, registered, promoted, demoted, approved, rejected, updated, deprecated, or removed, THE Portal SHALL record the action, the acting user, and a timestamp in the existing audit log.

### Requirement 14: Custom Node Type Versioning, Deprecation, and Removal

**User Story:** As a use case administrator, I want to version, deprecate, and remove custom node types, so that I can evolve the node library without breaking saved workflows.

#### Acceptance Criteria

1. WHEN plugin source or a Custom_Node_Type declaration is updated, THE Node_Designer SHALL create a new Custom_Node_Type version and retain prior versions.
2. WHEN a workflow is saved with a Custom_Node_Type Node, THE Workflow_Store SHALL record the Custom_Node_Type version used, and packaging of that workflow version SHALL use the recorded Custom_Node_Type version.
3. WHEN a UseCaseAdmin deprecates a Custom_Node_Type, THE Node_Palette SHALL stop offering the Custom_Node_Type for new placement WHILE existing saved workflows referencing the Custom_Node_Type remain loadable, packagable, and deployable.
4. WHEN a UseCaseAdmin requests removal of a Custom_Node_Type that no saved workflow references, THE Node_Designer SHALL remove the Custom_Node_Type from the Node_Type_Catalog and its Plugin_Artifacts from the Plugin_Library.
5. IF a UseCaseAdmin requests removal of a Custom_Node_Type that at least one saved workflow references, THEN THE Node_Designer SHALL reject the removal and identify the referencing workflows.

### Requirement 15: Upstream Quality Classification Display for Public Plugins

**User Story:** As a computer vision engineer, I want the portal to show each public GStreamer plugin's upstream good/bad/ugly classification when I browse or import it, so that I understand the maintenance and licensing risk of what I am bringing into the library.

#### Acceptance Criteria

1. WHEN the Portal presents the module list retrieved from the Module_Listing, THE Portal SHALL display each module's Plugin_Set_Classification as a risk indicator beside the module name.
2. WHEN a user confirms an import from the Module_Listing or from a public repository URL, THE Plugin_Importer SHALL display the plugin's Plugin_Set_Classification and a plain-language explanation of what that classification means on the import confirmation view before the import proceeds.
3. THE Portal SHALL present the following plain-language explanation with each Plugin_Set_Classification: good indicates a well-maintained, well-tested, properly licensed plugin set; bad indicates a plugin set lacking upstream review, testing, or active maintenance; ugly indicates a plugin set of good quality that carries licensing or distribution concerns; unclassified indicates a plugin outside the official GStreamer plugin sets that warrants the highest caution.
4. WHEN a plugin is imported from a public repository that is not part of an official GStreamer plugin set, THE Plugin_Importer SHALL assign the Plugin_Set_Classification unclassified to the plugin.
5. WHEN the Plugin_Importer creates a Plugin_Record for an imported plugin, THE Plugin_Importer SHALL record the plugin's Plugin_Set_Classification in the Plugin_Record provenance.
6. WHEN a PortalAdmin reviews a pending Plugin_Record as specified in Requirement 10, THE Portal SHALL display the Plugin_Record's recorded Plugin_Set_Classification alongside the other provenance details.
7. WHEN a user confirms an import of a plugin whose Plugin_Set_Classification is bad, ugly, or unclassified, THE Portal SHALL require the user to acknowledge the displayed classification explanation before the import proceeds.

### Requirement 16: Automatic Plugin Component Packaging and Deployment

**User Story:** As an operator, I want built plugins automatically packaged per architecture as deployable components that appear on the deployment screen, and workflows that use them to carry the right Greengrass dependencies, so that deploying a workflow always delivers the plugins its custom nodes need.

#### Acceptance Criteria

1. WHEN a Plugin_Record's builds complete, THE Component_Packager SHALL automatically package the successfully built Plugin_Artifacts into a versioned Plugin_Component whose Greengrass recipe carries one platform manifest per successfully built Target_Architecture (x86_64, x86_64_nvidia, arm64_jp4, arm64_jp5, and arm64_jp6).
2. WHEN a Plugin_Component version is registered, THE Portal SHALL list the Plugin_Component on the deployment screen with its name, version, Lifecycle_State, and the Target_Architectures it supports.
3. WHILE a Plugin_Component's backing Plugin_Record is in the test Lifecycle_State, THE Deployment_Service SHALL permit deploying that Plugin_Component only to Test_Devices; WHILE the backing Plugin_Record is in the prod Lifecycle_State, THE Deployment_Service SHALL permit deploying it to any device in the Use_Case.
4. WHEN a workflow containing Custom_Node_Types is packaged, THE Component_Packager SHALL declare a Greengrass component dependency in the Workflow_Component recipe on each required Plugin_Component at a version compatible with the Custom_Node_Type versions recorded in the workflow.
5. WHEN a Workflow_Component with Plugin_Component dependencies is deployed, THE Deployment_Service SHALL include the depended-on Plugin_Component versions in the Greengrass deployment so the device installs the plugins together with the workflow.
6. IF a Workflow_Component is deployed to a device whose Target_Architecture has no published Plugin_Artifact in a depended-on Plugin_Component version, THEN THE Deployment_Service SHALL reject the deployment and identify the Plugin_Component and the unsupported Target_Architecture.
7. WHEN a Plugin_Record is rebuilt or its source changes, THE Component_Packager SHALL publish the resulting artifacts as a new Plugin_Component version and SHALL leave previously published Plugin_Component versions unchanged.
