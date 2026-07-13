# Requirements Document

## Introduction

The Custom Node Designer extends the Workflow Manager (spec: workflow-manager) with a tool for adding new node types to the Workflow_Builder palette without a platform release. Users can create a custom node from generated template code that exposes a per-frame processing hook (frame in → user code → frame out), or import an existing native plugin: a GStreamer plugin from a public source repository, an NVIDIA DeepStream plugin for Jetson devices, or a well-known GStreamer module selected from the official GStreamer module listing. Created and imported plugins are built per target device architecture, reviewed and approved before use (they are native code executed on edge devices), stored in the curated Plugin_Library used by workflow packaging, and registered in the Node_Type_Catalog so they appear in the Node_Palette with ports, parameters, and help like any built-in node type. The feature also defines how custom nodes behave in cloud test runs, which portal roles may manage them, and how custom node types are versioned, deprecated, and removed.

## Glossary

- **Portal**: The edge-cv-portal cloud web application (React frontend, Lambda backend, DynamoDB storage) used to manage DDA use cases, models, components, deployments, devices, and workflows.
- **LocalServer**: The Greengrass component (aws.edgeml.dda.LocalServer.&lt;arch&gt;) running on an edge device. It embeds the NVIDIA Triton Inference Server and executes GStreamer pipelines, loading bundled GStreamer plugins and plugins delivered with a Workflow_Component.
- **Workflow_Builder**: The graphical canvas UI within the Portal where users place Nodes and draw Connections to compose a Workflow_Definition.
- **Node**: A single processing stage in a workflow represented as a box on the Workflow_Builder canvas.
- **Node_Palette**: The categorized list of available Node types shown in the Workflow_Builder, organized into input, preprocessing, model inference, post-processing, and output sections.
- **Port**: A typed attachment point on a Node where a Connection begins or ends. Each Port declares a media or data type (for example video frames, inference metadata, or event signals).
- **Node_Type_Catalog**: The catalog of Node type declarations shared by the Portal, the cloud test sandbox, and LocalServer. Each declaration specifies a Node type's category, input and output Ports, parameters with types, defaults, constraints, descriptions, and examples, per-architecture GStreamer mappings, plugin dependencies, and hardware-dependence flag.
- **Node_Designer**: The Portal capability introduced by this feature that lets authorized users create Custom_Node_Types from template code or by importing native plugins, and manage those Custom_Node_Types through their lifecycle.
- **Custom_Node_Type**: A Node type added to the Node_Type_Catalog through the Node_Designer rather than shipped with the platform. A Custom_Node_Type is backed by one or more Plugin_Artifacts.
- **Plugin_Scaffold**: The generated project produced by the Node_Designer for a new Custom_Node_Type, containing template plugin source code with a Frame_Processing_Hook and per-architecture build configuration.
- **Frame_Processing_Hook**: The designated function within a Plugin_Scaffold where the user writes processing logic. The hook receives each frame arriving at the Node's input Port and returns the frame content to emit on the Node's output Port.
- **GStreamer_Plugin**: A native GStreamer plugin (shared library exposing one or more GStreamer elements) that can be loaded by the GStreamer runtime on a device or in the cloud test sandbox.
- **DeepStream_Plugin**: A GStreamer_Plugin built against the NVIDIA DeepStream SDK, executable only on device architectures with a matching DeepStream runtime (Jetson JetPack 4, 5, or 6).
- **Plugin_Importer**: The Portal backend component that retrieves plugin source or binaries from a user-specified public repository or from the Module_Listing, records provenance, and submits the plugin for building and review.
- **Module_Listing**: The official GStreamer module index published at https://gstreamer.freedesktop.org/modules/, from which the Portal offers a selectable list of well-known public GStreamer modules.
- **Plugin_Build_Service**: The Portal backend component that compiles plugin source into Plugin_Artifacts for each selected Target_Architecture.
- **Plugin_Artifact**: A built plugin binary (.so shared library) for one Target_Architecture, stored in the Plugin_Library with an integrity checksum.
- **Plugin_Library**: The curated per-account plugin storage (portal S3) from which the Component_Packager retrieves plugin artifacts (plugins/&lt;arch&gt;/*.so) when packaging a Workflow_Component.
- **Plugin_Record**: The stored metadata for a created or imported plugin: name, version, provenance (source repository URL and revision, or scaffold origin), importing or creating user, timestamps, per-architecture Plugin_Artifacts with checksums, and approval status.
- **Target_Architecture**: A device architecture a plugin can be built for: x86_64, arm64 JetPack 4 (arm64_jp4), arm64 JetPack 5 (arm64_jp5), or arm64 JetPack 6 (arm64_jp6). The cloud test sandbox executes x86_64 builds.
- **Workflow_Compiler**: The Workflow Manager component that translates a valid Workflow_Definition into a GStreamer pipeline configuration, including the list of plugin dependencies beyond those bundled with LocalServer.
- **Component_Packager**: The Portal backend component that packages a compiled workflow and its plugin dependencies into a versioned Greengrass component (Workflow_Component).
- **Workflow_Component**: The Greengrass component produced by the Component_Packager for a specific workflow version.
- **Workflow_Test_Runner**: The Portal backend component that executes a compiled workflow against a Test_Dataset in the cloud-side simulated environment (x86_64), substituting stubs for hardware-dependent Nodes.
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

### Requirement 2: Per-Architecture Plugin Builds

**User Story:** As a computer vision engineer, I want my created or imported plugins built for each device architecture I target, so that the same custom node runs on x86_64 and Jetson devices and in cloud tests.

#### Acceptance Criteria

1. WHEN plugin source is submitted for building, THE Plugin_Build_Service SHALL compile the source into one Plugin_Artifact per selected Target_Architecture.
2. WHEN a build completes for a Target_Architecture, THE Plugin_Build_Service SHALL store the resulting Plugin_Artifact in the Plugin_Library under that Target_Architecture together with an integrity checksum recorded in the Plugin_Record.
3. IF the build fails for a Target_Architecture, THEN THE Plugin_Build_Service SHALL report the failure with the failing Target_Architecture identified and the compiler output included, and SHALL store no Plugin_Artifact for that Target_Architecture.
4. WHEN builds complete, THE Node_Designer SHALL display the per-architecture build status (succeeded or failed) for the Plugin_Record.
5. WHERE a plugin source distribution already provides prebuilt binaries for a Target_Architecture, THE Plugin_Build_Service SHALL accept the prebuilt binary as the Plugin_Artifact for that Target_Architecture and record its checksum in the Plugin_Record.

### Requirement 3: Import a GStreamer Plugin from a Public Repository

**User Story:** As a computer vision engineer, I want to import an existing GStreamer plugin from a public source repository and add it to the node library, so that I can use community plugins in my workflows without waiting for a platform release.

#### Acceptance Criteria

1. WHEN a user submits a public repository URL and an optional revision identifier for import, THE Plugin_Importer SHALL retrieve the plugin source from the repository at the specified revision or at the repository default revision when no revision is specified.
2. WHEN plugin source is retrieved, THE Plugin_Importer SHALL create a Plugin_Record containing the repository URL, the retrieved revision identifier, the importing user, and the retrieval timestamp.
3. WHEN a Plugin_Record is created from an import, THE Plugin_Importer SHALL submit the retrieved source to the Plugin_Build_Service for the Target_Architectures selected by the user.
4. IF the repository is unreachable or the specified revision does not exist, THEN THE Plugin_Importer SHALL display an error identifying the failure and SHALL create no Plugin_Record.
5. IF the retrieved source does not contain a buildable GStreamer_Plugin, THEN THE Plugin_Importer SHALL report the finding to the user and SHALL mark the Plugin_Record as failed.
6. WHEN an imported plugin's builds succeed for at least one Target_Architecture, THE Node_Designer SHALL prompt the user to declare the Custom_Node_Type details for the plugin's element as specified in Requirement 6.

### Requirement 4: Import an NVIDIA DeepStream Plugin

**User Story:** As a computer vision engineer, I want to import NVIDIA DeepStream plugins for my Jetson devices, so that I can use DeepStream-accelerated processing stages in workflows targeting Jetson hardware.

#### Acceptance Criteria

1. WHEN a user marks an import as a DeepStream_Plugin, THE Node_Designer SHALL restrict the selectable Target_Architectures to arm64 JetPack 4, arm64 JetPack 5, and arm64 JetPack 6.
2. WHEN a DeepStream_Plugin is built for a Jetson Target_Architecture, THE Plugin_Build_Service SHALL build against the DeepStream SDK version matching that Target_Architecture's JetPack release.
3. WHEN a Custom_Node_Type backed by a DeepStream_Plugin is registered, THE Node_Type_Catalog SHALL record the Custom_Node_Type as unavailable on Target_Architectures without a matching DeepStream runtime.
4. IF a user requests packaging of a workflow containing a DeepStream-backed Custom_Node_Type for a Target_Architecture without a matching DeepStream runtime, THEN THE Workflow_Compiler SHALL report a compilation error identifying the Node and the unsupported Target_Architecture.

### Requirement 5: Import from the Official GStreamer Module Listing

**User Story:** As a computer vision engineer, I want to pick a well-known GStreamer module from a select box populated from the official GStreamer module listing, so that I can import public plugins by browsing instead of typing repository URLs.

#### Acceptance Criteria

1. WHEN a user opens the module import view, THE Portal SHALL retrieve the current module index from the Module_Listing and present the retrieved modules in a selectable list with module names.
2. WHEN a user selects a module from the list, THE Plugin_Importer SHALL use the selected module's published repository location as the import source and proceed as specified in Requirement 3.
3. IF the Module_Listing is unreachable or returns an unparseable response, THEN THE Portal SHALL display an error identifying the failure and SHALL offer manual repository URL entry as specified in Requirement 3 as the alternative import path.
4. WHEN the module index is retrieved, THE Portal SHALL cache the retrieved index and reuse the cached index for subsequent module import views for at most 24 hours before retrieving a fresh index.

### Requirement 6: Node Type Declaration and Palette Integration

**User Story:** As a computer vision engineer, I want my created or imported plugin to appear in the workflow builder palette as a regular node with ports, parameters, and help text, so that anyone in my use case can use the custom node like a built-in node.

#### Acceptance Criteria

1. WHEN a user registers a Custom_Node_Type for a plugin, THE Node_Designer SHALL collect the display name, palette category, input and output Ports with declared Port types, parameters with types, defaults, constraints, descriptions, and examples, the hardware-dependence flag, and the mapping from the plugin's element and its properties to the declared parameters for each built Target_Architecture.
2. WHEN a Custom_Node_Type registration is completed and the backing Plugin_Record is approved, THE Node_Type_Catalog SHALL include the Custom_Node_Type with the same declaration structure as built-in Node types, scoped to the Use_Cases selected at registration.
3. WHEN a Custom_Node_Type is included in the Node_Type_Catalog for a Use_Case, THE Node_Palette SHALL display the Custom_Node_Type in its declared category for users of that Use_Case.
4. WHEN a user places a Custom_Node_Type Node on the canvas, THE Workflow_Builder SHALL provide the same configuration panel behavior as for built-in Node types, including parameter validation, field-level descriptions, and example values.
5. IF a Custom_Node_Type registration declares a Port type outside the Port types defined by the Node_Type_Catalog, THEN THE Node_Designer SHALL reject the registration and identify the invalid Port declaration.
6. WHEN a Custom_Node_Type is registered, THE Node_Type_Catalog SHALL record the plugin dependency of the Custom_Node_Type so that the Workflow_Compiler includes the plugin in the compiled pipeline's dependency list for each Target_Architecture.

### Requirement 7: Security Review and Approval of Native Plugins

**User Story:** As a portal administrator, I want every created or imported plugin reviewed and approved before it can be used in workflows, so that untrusted native code cannot reach edge devices without an explicit trust decision.

#### Acceptance Criteria

1. WHEN a Plugin_Record is created, THE Portal SHALL set the Plugin_Record approval status to pending.
2. WHILE a Plugin_Record's approval status is pending or rejected, THE Node_Type_Catalog SHALL exclude Custom_Node_Types backed by that Plugin_Record from the Node_Palette.
3. WHEN a PortalAdmin reviews a pending Plugin_Record, THE Portal SHALL display the Plugin_Record provenance (source repository URL and revision or scaffold origin, importing or creating user, timestamps), the per-architecture Plugin_Artifact checksums, and the plugin source for inspection.
4. WHEN a PortalAdmin approves or rejects a Plugin_Record, THE Portal SHALL record the decision, the acting PortalAdmin, and a timestamp in the existing audit log.
5. IF a user requests packaging of a workflow containing a Custom_Node_Type whose backing Plugin_Record is not approved, THEN THE Component_Packager SHALL reject the packaging request and identify the unapproved Custom_Node_Type.
6. WHEN the Component_Packager includes a Plugin_Artifact in a Workflow_Component, THE Component_Packager SHALL verify the Plugin_Artifact against the checksum recorded in the Plugin_Record and SHALL reject the packaging request when the checksum verification fails.
7. WHEN a new version of an approved plugin is imported or rebuilt from changed source, THE Portal SHALL set the new Plugin_Record version's approval status to pending independently of prior approvals.

### Requirement 8: Packaging and Delivery of Custom Node Plugins

**User Story:** As an operator, I want workflows that use custom nodes packaged with the required plugin binaries per architecture, so that a single deployment delivers everything the edge device needs to run the custom node.

#### Acceptance Criteria

1. WHEN a user requests packaging of a workflow containing a Custom_Node_Type, THE Component_Packager SHALL include the Custom_Node_Type's Plugin_Artifact from the Plugin_Library for each Target_Architecture selected for packaging.
2. IF the Plugin_Library contains no Plugin_Artifact for a Custom_Node_Type on a Target_Architecture selected for packaging, THEN THE Component_Packager SHALL reject the packaging request and identify the Custom_Node_Type and the missing Target_Architecture.
3. WHEN a Workflow_Component containing a Custom_Node_Type's Plugin_Artifact is deployed to a device, THE LocalServer SHALL load the delivered plugin and execute the Custom_Node_Type's element within the compiled pipeline.

### Requirement 9: Custom Nodes in Cloud Test Runs

**User Story:** As a computer vision engineer, I want to test workflows containing custom nodes in the cloud test sandbox, so that I can verify custom node behavior before deploying to a device.

#### Acceptance Criteria

1. WHEN a test run executes a workflow containing a Custom_Node_Type that has an x86_64 Plugin_Artifact, THE Workflow_Test_Runner SHALL load the x86_64 Plugin_Artifact and execute the Custom_Node_Type within the simulated pipeline.
2. IF a test run executes a workflow containing a Custom_Node_Type that has no x86_64 Plugin_Artifact, THEN THE Workflow_Test_Runner SHALL substitute a stub for that Node that records the data the Node would have consumed and passes input frames through unchanged, and SHALL identify the Node as stubbed in the test run report.
3. WHEN a test run report includes a stubbed Custom_Node_Type, THE Workflow_Builder SHALL describe the limitation that the Custom_Node_Type was simulated because no x86_64 build exists.

### Requirement 10: Access Control for Node Designer Operations

**User Story:** As a portal administrator, I want custom node creation, import, and library management restricted to administrative roles, so that only trusted users can introduce native code into the platform.

#### Acceptance Criteria

1. THE Portal SHALL permit users with the UseCaseAdmin role in a Use_Case or the PortalAdmin role to create Plugin_Scaffolds, import plugins, register Custom_Node_Types, and manage Plugin_Records scoped to that Use_Case.
2. THE Portal SHALL permit only users with the PortalAdmin role to approve or reject Plugin_Records.
3. THE Portal SHALL permit users with the DataScientist, Operator, or Viewer role in a Use_Case to view the Custom_Node_Types and Plugin_Records of that Use_Case in read-only form.
4. IF a user without a permitted role attempts a create, import, register, approve, update, or remove operation on a Plugin_Record or Custom_Node_Type, THEN THE Portal SHALL deny the operation and return an authorization error.
5. WHEN a Plugin_Record or Custom_Node_Type is created, imported, registered, approved, rejected, updated, deprecated, or removed, THE Portal SHALL record the action, the acting user, and a timestamp in the existing audit log.

### Requirement 11: Custom Node Type Lifecycle

**User Story:** As a use case administrator, I want to version, deprecate, and remove custom node types, so that I can evolve the node library without breaking saved workflows.

#### Acceptance Criteria

1. WHEN plugin source or a Custom_Node_Type declaration is updated, THE Node_Designer SHALL create a new Custom_Node_Type version and retain prior versions.
2. WHEN a workflow is saved with a Custom_Node_Type Node, THE Workflow_Store SHALL record the Custom_Node_Type version used, and packaging of that workflow version SHALL use the recorded Custom_Node_Type version.
3. WHEN a UseCaseAdmin deprecates a Custom_Node_Type, THE Node_Palette SHALL stop offering the Custom_Node_Type for new placement WHILE existing saved workflows referencing the Custom_Node_Type remain loadable, packagable, and deployable.
4. WHEN a UseCaseAdmin requests removal of a Custom_Node_Type that no saved workflow references, THE Node_Designer SHALL remove the Custom_Node_Type from the Node_Type_Catalog and its Plugin_Artifacts from the Plugin_Library.
5. IF a UseCaseAdmin requests removal of a Custom_Node_Type that at least one saved workflow references, THEN THE Node_Designer SHALL reject the removal and identify the referencing workflows.
