# Requirements Document

## Introduction

This feature adds support for serving large language models (LLMs) on edge devices through the NVIDIA Triton Inference Server vLLM backend, integrated end-to-end with the Edge CV Portal and the on-device LocalServer runtime.

Today the portal supports a bring-your-own-model (BYOM) path where a pre-trained vision model bypasses the labeling and training pipeline and is published directly as a Greengrass model component that deploys to edge devices. This feature mirrors that mechanism for vLLM models: a user registers an LLM (by Hugging Face model identifier or S3 artifact), publishes it directly (no labeling, no training), and the portal packages it as a Greengrass component containing a Triton vLLM-backend model repository (`model.json` engine configuration plus `config.pbtxt`). On the device, the LocalServer's Triton runtime loads the model with the vLLM backend and exposes a text-generation inference API.

The Workflow Designer gains a new inference node type for LLM inference (prompt template, generation parameters) that composes with all existing node types, workflows, and input types (cameras, folder sources, custom Python nodes, custom plugin nodes, and output bindings). The Workflow Compiler, Component Packager, and on-device Workflow Engine are extended to compile, package, and execute the new node type.

Platform scope: JetPack 6 (arm64_jp6) support is required. JetPack 5 (arm64_jp5) support is best-effort and must not compromise JetPack 6 function. JetPack 4 (arm64_jp4) is out of scope for vLLM execution; JP4 devices and their existing capabilities remain fully functional and unaffected.

## Glossary

- **Portal**: The Edge CV Portal — the cloud application (Lambda backend, React frontend) that manages models, workflows, devices, and Greengrass deployments.
- **Model_Registry**: The Portal subsystem that stores model records (training jobs and imported models) per Use_Case.
- **vLLM_Model_Record**: A Model_Registry record of model type `vllm` referencing an LLM by Hugging_Face_Model_ID or S3_Model_Artifact, together with its vLLM_Engine_Configuration.
- **Hugging_Face_Model_ID**: A model identifier on the Hugging Face Hub (for example `facebook/opt-125m`) resolvable by the vLLM engine.
- **S3_Model_Artifact**: An archive of LLM weights and tokenizer files stored in S3 and owned by the Use_Case account.
- **vLLM_Engine_Configuration**: The engine arguments serialized into `model.json` for the Triton vLLM backend (model reference, dtype, gpu_memory_utilization, max_model_len, tensor parallelism, enforce_eager, and related settings).
- **Model_Packager**: The Portal subsystem that assembles model artifacts into Greengrass model components (the existing `packaging.py` mechanism).
- **vLLM_Model_Component**: A Greengrass component produced by the Model_Packager for a vLLM_Model_Record, delivering a Triton_vLLM_Repository to the device.
- **Triton_vLLM_Repository**: A Triton model repository directory for one model using the vLLM backend: `{model_name}/1/model.json` and `{model_name}/config.pbtxt` with `backend: "vllm"`.
- **LocalServer**: The Greengrass component (`aws.edgeml.dda.LocalServer.<arch>`) running on edge devices that hosts the Triton inference runtime, the Workflow_Engine, and device APIs.
- **Triton_vLLM_Runtime**: The Triton Inference Server instance (or companion runtime process) on the device with the vLLM backend available, serving loaded vLLM models.
- **Text_Generation_API**: The device-local inference interface exposed by LocalServer that accepts a prompt and generation parameters and returns generated text for a loaded vLLM model.
- **Deployment_Service**: The Portal subsystem (`deployments.py`) that creates Greengrass deployments targeting devices and validates component/device compatibility.
- **Target_Architecture**: A device platform identifier used by packaging and deployment: `x86_64`, `x86_64_nvidia`, `arm64_jp4` (JetPack 4), `arm64_jp5` (JetPack 5), `arm64_jp6` (JetPack 6).
- **Workflow_Designer**: The Portal frontend node-graph editor for building Workflow_Definitions.
- **Node_Type_Catalog**: The shared catalog of workflow node type descriptors (ports, parameters, per-architecture mappings) used by the Workflow_Designer, Workflow_Validator, and Workflow_Compiler.
- **LLM_Inference_Node**: The new Node_Type_Catalog inference node type that invokes a deployed vLLM model with a rendered prompt and emits the generated text as inference metadata.
- **Prompt_Template**: The LLM_Inference_Node parameter containing the prompt text, optionally with placeholders substituted from upstream node metadata at execution time.
- **Workflow_Validator**: The Portal subsystem that validates a Workflow_Definition against the Node_Type_Catalog and compatibility rules.
- **Workflow_Compiler**: The Portal subsystem that compiles a validated Workflow_Definition into per-architecture pipeline documents.
- **Component_Packager**: The Portal subsystem (`workflow_packaging.py`) that packages compiled workflows into `dda.workflow.*` Greengrass components.
- **Workflow_Engine**: The LocalServer subsystem that executes deployed workflow components on the device.
- **Inference_Metadata**: The structured per-run metadata record that inference nodes produce and downstream nodes (filters, conditionals, outputs, custom Python) consume.

## Requirements

### Requirement 1: vLLM Model Onboarding

**User Story:** As a use case administrator, I want to register an LLM in the portal by Hugging Face model ID or S3 artifact and publish it directly, so that I can deploy LLMs to edge devices without going through the labeling and training pipeline.

#### Acceptance Criteria

1. THE Portal SHALL provide a registration operation that creates a vLLM_Model_Record from exactly one source, either a Hugging_Face_Model_ID or an S3_Model_Artifact reference but not both, and SHALL restrict this operation to users with access to the owning Use_Case.
2. WHEN a user registers a vLLM_Model_Record, THE Portal SHALL accept a vLLM_Engine_Configuration with the registration and SHALL apply defined default values for any omitted engine settings.
3. WHEN a vLLM_Model_Record is registered, THE Model_Registry SHALL store the record with model type `vllm`, its source reference, and its complete vLLM_Engine_Configuration including applied default values, scoped to the owning Use_Case.
4. WHEN a vLLM_Model_Record registration completes successfully, THE Portal SHALL mark the record eligible for publish with zero labeling steps and zero training steps, and SHALL return the record identifier and publish-eligibility status to the caller.
5. IF a registration request fails validation, THEN THE Portal SHALL create no vLLM_Model_Record and SHALL mark nothing eligible for publish.
6. IF a registration request omits both a Hugging_Face_Model_ID and an S3_Model_Artifact reference, THEN THE Portal SHALL reject the request with a validation error identifying the missing source reference.
7. IF a registration request references an S3_Model_Artifact that is not readable from the Use_Case account, THEN THE Portal SHALL reject the request with an error identifying the unreadable S3 location.
8. WHEN a user lists models for a Use_Case, THE Portal SHALL include vLLM_Model_Records with a model type indicator distinguishing them from vision model records.
9. IF a registration request supplies both a Hugging_Face_Model_ID and an S3_Model_Artifact reference, THEN THE Portal SHALL reject the request with a validation error stating that exactly one source must be provided.
10. IF a supplied vLLM_Engine_Configuration setting is outside its accepted range, THEN THE Portal SHALL reject the request with a validation error identifying the offending setting.
11. IF a supplied Hugging_Face_Model_ID is malformed, THEN THE Portal SHALL reject the request with a validation error identifying the malformed value.

### Requirement 2: vLLM Model Component Packaging

**User Story:** As a use case administrator, I want a published vLLM model packaged as a Greengrass component containing a Triton vLLM model repository, so that deploying the component delivers a servable model to the device.

#### Acceptance Criteria

1. WHEN a user publishes a vLLM_Model_Record, THE Model_Packager SHALL generate a Triton_vLLM_Repository laid out as `{model_name}/1/model.json` and `{model_name}/config.pbtxt`, where `model.json` contains a serialization of every setting in the record's vLLM_Engine_Configuration and `config.pbtxt` declares `backend: "vllm"`.
2. WHERE the vLLM_Model_Record references an S3_Model_Artifact, THE Model_Packager SHALL declare the S3_Model_Artifact as a downloadable artifact in the component recipe and SHALL serialize the model reference in `model.json` so that it resolves to the artifact's device-local path after component installation.
3. WHERE the vLLM_Model_Record references a Hugging_Face_Model_ID, THE Model_Packager SHALL serialize the Hugging_Face_Model_ID into `model.json` as the vLLM engine model reference.
4. WHEN all packaging artifacts for a vLLM_Model_Record have been assembled and uploaded successfully, THE Model_Packager SHALL register a Greengrass component version for the vLLM_Model_Record following the existing model component naming and versioning conventions, assigning on each repeat publish of the same record the next component version determined by those conventions.
5. WHEN packaging a vLLM_Model_Component, THE Model_Packager SHALL record on the component metadata a supported Target_Architecture set that includes `arm64_jp6`, excludes `arm64_jp4`, and includes `arm64_jp5` only where JetPack 5 support is implemented.
6. IF artifact upload or Greengrass component registration fails, THEN THE Model_Packager SHALL report the failure identifying the failing artifact or registration step, SHALL register no partial component version, and SHALL leave the vLLM_Model_Record in its pre-publish state so that the publish operation can be retried.
7. WHEN a vLLM_Model_Component is installed on a device, THE component recipe SHALL place the Triton_vLLM_Repository where the LocalServer Triton model preparation discovers model artifacts, without restarting LocalServer.
8. IF Triton_vLLM_Repository generation or vLLM_Engine_Configuration serialization fails, THEN THE Model_Packager SHALL upload no packaging artifacts, SHALL register no Greengrass component version, and SHALL leave the vLLM_Model_Record's published state unchanged.
9. WHEN Greengrass component registration for a vLLM_Model_Record completes successfully, THE Portal SHALL mark the vLLM_Model_Record as published and SHALL make the registered component version available for inclusion in deployments.

### Requirement 3: Platform Gating and Deployment Validation

**User Story:** As a use case administrator, I want deployments of vLLM components validated against the target device's platform, so that vLLM models are only deployed to devices that can run them.

#### Acceptance Criteria

1. THE Deployment_Service SHALL treat `arm64_jp6` as a supported Target_Architecture for vLLM_Model_Components.
2. WHERE JetPack 5 support is implemented, THE Deployment_Service SHALL treat `arm64_jp5` as a supported Target_Architecture for vLLM_Model_Components.
3. WHEN a deployment includes a vLLM_Model_Component or a workflow component containing an LLM_Inference_Node, THE Deployment_Service SHALL compare each target device's recorded Target_Architecture against the component's supported Target_Architectures by exact architecture name, with no cross-architecture fallback, before creating the deployment, where the supported set is the Target_Architectures recorded on the vLLM_Model_Component metadata or, for a workflow component, the Target_Architectures for which the Component_Packager produced workflow artifacts.
4. IF one or more targeted devices have a Target_Architecture that is not in the component's supported set, THEN THE Deployment_Service SHALL reject the deployment with an error identifying every incompatible device, each device's Target_Architecture, and the supported set, and SHALL create no deployment for any targeted device.
5. IF a deployment of a vLLM_Model_Component targets an `arm64_jp4` device, THEN THE Deployment_Service SHALL reject the deployment with an error stating that JetPack 4 does not support vLLM inference.
6. IF a targeted device has no recorded Target_Architecture in the devices table, THEN THE Deployment_Service SHALL treat that device as incompatible and reject the deployment with an error identifying the device as having no recorded Target_Architecture.
7. WHEN every targeted device's Target_Architecture is in the component's supported set and all other deployment validation checks pass, THE Deployment_Service SHALL create the deployment.
8. THE Portal frontend SHALL display the supported Target_Architectures of a vLLM_Model_Component on the model and deployment views.
9. WHEN a user composes a deployment in the Portal frontend, THE Portal frontend SHALL indicate, before the deployment request is submitted, each selected device that is incompatible with the selected vLLM_Model_Component, showing the device's Target_Architecture (or its absence) alongside the component's supported set.

### Requirement 4: Edge Runtime — Triton vLLM Backend and Model Loading

**User Story:** As an edge device operator, I want the device runtime to load deployed vLLM models into a Triton vLLM backend, so that text generation is served locally on the device.

#### Acceptance Criteria

1. THE LocalServer JetPack 6 (`arm64_jp6`) image SHALL include a Triton_vLLM_Runtime that loads a Triton_vLLM_Repository whose `config.pbtxt` declares `backend: "vllm"` and serves it for inference.
2. WHERE JetPack 5 vLLM support is implemented, THE LocalServer JetPack 5 (`arm64_jp5`) image SHALL include a Triton_vLLM_Runtime that loads and serves a Triton_vLLM_Repository, and all existing JetPack 5 vision model conversion, loading, and inference functions SHALL produce results identical to pre-feature behavior.
3. THE LocalServer JetPack 6 image build SHALL preserve all existing JetPack 6 vision model conversion, loading, and inference function, producing results identical to pre-feature behavior for every previously supported vision model type.
4. WHEN a vLLM_Model_Component is installed on a device whose Target_Architecture is in the component's supported set, THE LocalServer model preparation SHALL stage the Triton_vLLM_Repository into the Triton model repository without restarting the LocalServer component.
5. WHERE the vLLM_Model_Record references an S3_Model_Artifact, THE LocalServer model preparation SHALL rewrite the `model.json` model reference to the device-local artifact path before requesting the model load.
6. IF the Triton_vLLM_Runtime reports an error while loading or serving a vLLM model, THEN THE LocalServer SHALL log the failure with the model name and the backend error, SHALL report the model's status as failed through the existing device model status mechanisms, and SHALL continue serving all other loaded models without interruption.
7. WHILE a vLLM model load is in progress, THE LocalServer SHALL report the model's status as loading through the existing device model status mechanisms.
8. WHEN staging of a Triton_vLLM_Repository completes, THE LocalServer model preparation SHALL request the Triton_vLLM_Runtime to load the staged model.
9. IF the device-local artifact path resolved from an S3_Model_Artifact does not exist or is not readable at load time, THEN THE LocalServer model preparation SHALL not request the model load, SHALL report the model's status as failed with an error identifying the model name and the unresolved path, and SHALL continue preparing all other installed models.
10. WHEN the Triton_vLLM_Runtime reports a vLLM model as ready, THE LocalServer SHALL report the model's status as ready through the existing device model status mechanisms within 30 seconds of the runtime state change.

### Requirement 5: Edge Runtime — Text Generation Inference API

**User Story:** As a workflow or application developer, I want a device-local text generation API over the loaded vLLM model, so that workflows and device applications can obtain LLM completions.

#### Acceptance Criteria

1. THE LocalServer SHALL expose a Text_Generation_API that accepts a model name, a prompt of at least 1 character, and generation parameters (max_tokens as an integer of at least 1 and no greater than the loaded model's configured max_model_len, temperature in the range 0.0 to 2.0 inclusive, top_p greater than 0.0 and no greater than 1.0) and returns the generated text.
2. WHEN the Text_Generation_API receives a valid request for a vLLM model in the READY serving state, THE LocalServer SHALL invoke the Triton_vLLM_Runtime generate interface and return a response containing the generated text for that request.
3. WHERE streaming is requested, THE Text_Generation_API SHALL deliver generated tokens incrementally in generation order as the Triton_vLLM_Runtime produces them, and SHALL signal end-of-stream to the caller when generation completes.
4. IF an error occurs during a streaming response, THEN THE Text_Generation_API SHALL stop token delivery, SHALL deliver an in-stream error indication containing the failure reason, and SHALL NOT retry generation or retract tokens already delivered.
5. IF a Text_Generation_API request names a model that is not in the READY serving state, THEN THE LocalServer SHALL return, without invoking generation, an error identifying the requested model name and distinguishing whether the model is still loading, failed to load, or is unknown to the device.
6. IF the Triton_vLLM_Runtime returns a transient inference error (temporary runtime unavailability or a runtime-reported retryable failure) for a non-streaming request, THEN THE Text_Generation_API SHALL retry the request up to a configured retry limit, with a default of 2 retries, before returning an error.
7. IF retries are exhausted or the inference error is non-transient, THEN THE Text_Generation_API SHALL return an error containing the requested model name and the backend failure reason.
8. WHEN a Text_Generation_API request omits max_tokens, temperature, or top_p, THE LocalServer SHALL apply that parameter's documented default value and process the request.
9. IF a Text_Generation_API request contains an empty or missing prompt, an empty or missing model name, or a generation parameter outside its accepted range, THEN THE LocalServer SHALL reject the request without invoking the Triton_vLLM_Runtime and SHALL return a validation error identifying each invalid or missing field.
10. WHEN the Text_Generation_API receives concurrent requests for one or more loaded vLLM models, THE LocalServer SHALL process each request independently, so that an error in one request does not alter the response of any concurrent request.
11. IF a non-streaming generation request does not complete within a configured request timeout, with a default of 120 seconds, THEN THE Text_Generation_API SHALL stop waiting on the Triton_vLLM_Runtime and return an error identifying the requested model name and indicating the timeout.

### Requirement 6: Workflow Designer — LLM Inference Node Type

**User Story:** As a workflow author, I want a new LLM inference node in the Workflow Designer, so that I can add local LLM text generation to my workflows.

#### Acceptance Criteria

1. THE Node_Type_Catalog SHALL define an LLM_Inference_Node type in the inference category with declared typed input and output ports, per-architecture mappings, and parameters for model selection, Prompt_Template, max_tokens, temperature, and top_p.
2. WHEN a workflow author configures an LLM_Inference_Node, THE Workflow_Designer SHALL populate the model selection parameter with exactly the Use_Case's registered vLLM_Model_Records and no other model records.
3. THE LLM_Inference_Node SHALL declare its output port with the inference metadata port type and emit the generated text as Inference_Metadata consumable by downstream nodes that accept inference metadata.
4. THE LLM_Inference_Node SHALL accept input connections from every existing node type whose output port type is accepted by the LLM_Inference_Node's declared input ports, under the existing port compatibility rules including the declared port type coercions.
5. WHEN a workflow containing an LLM_Inference_Node is validated, THE Workflow_Validator SHALL apply the same structural and parameter validation checks applied to existing inference node types.
6. IF an LLM_Inference_Node has an empty Prompt_Template, has no model selected, or has a generation parameter value outside its declared bounds, THEN THE Workflow_Validator SHALL report a validation error identifying the node, the offending parameter, and the reason.
7. WHILE a workflow version has unresolved validation errors on an LLM_Inference_Node, THE Portal SHALL block compilation and packaging of that workflow version.
8. WHEN the Workflow_Compiler compiles a workflow containing an LLM_Inference_Node for a Target_Architecture that does not support vLLM, THE Workflow_Compiler SHALL report an error identifying the node and the unsupported Target_Architecture, and SHALL produce no compiled pipeline document for that Target_Architecture.
9. WHEN a workflow containing an LLM_Inference_Node is compiled for simulation, THE Workflow_Compiler SHALL emit for that node the same pass-through stub form emitted for existing hardware-dependent inference node types in simulation, with no binding that invokes a vLLM model.
10. THE LLM_Inference_Node type descriptor SHALL declare model selection and Prompt_Template as required parameters, and SHALL declare max_tokens, temperature, and top_p as optional parameters, each with a default value and declared bounds of: max_tokens at least 1, temperature between 0.0 and 2.0 inclusive, and top_p greater than 0.0 and at most 1.0.
11. IF the Use_Case has no registered vLLM_Model_Records, THEN THE Workflow_Designer SHALL present the LLM_Inference_Node model selection parameter with an empty option list and an indication that no vLLM models are registered for the Use_Case.
12. IF an LLM_Inference_Node's selected model does not resolve to an existing vLLM_Model_Record in the Use_Case's Model_Registry at validation time, THEN THE Workflow_Validator SHALL report a validation error identifying the node and the unresolvable model reference.

### Requirement 7: Workflow Packaging and On-Device Execution

**User Story:** As a workflow author, I want workflows containing LLM inference nodes to package and run on devices like any other workflow, so that LLM steps execute as part of my deployed pipelines.

#### Acceptance Criteria

1. WHEN packaging a validated workflow containing an LLM_Inference_Node, THE Component_Packager SHALL include the node's compiled binding (model name, Prompt_Template, and the generation parameters max_tokens, temperature, and top_p) in the per-architecture workflow artifacts for each Target_Architecture in the workflow component's supported set.
2. IF a packaging request for a workflow containing an LLM_Inference_Node includes a Target_Architecture that does not support vLLM execution, THEN THE Component_Packager SHALL reject the request with an error identifying the LLM_Inference_Node and each unsupported Target_Architecture, and SHALL register no workflow component version for that request.
3. WHEN the Workflow_Engine executes an LLM_Inference_Node, THE Workflow_Engine SHALL render the Prompt_Template by substituting each placeholder with the corresponding value from upstream Inference_Metadata and SHALL invoke the Text_Generation_API with the rendered prompt, the bound model name, and the bound generation parameters.
4. WHEN the Text_Generation_API returns generated text for an LLM_Inference_Node invocation, THE Workflow_Engine SHALL record the generated text in the node's Inference_Metadata output before any downstream node consumes that output.
5. IF a Prompt_Template placeholder references a value that is absent from the upstream Inference_Metadata at execution time, THEN THE Workflow_Engine SHALL record the node execution as failed in the node's Inference_Metadata with an error indication identifying the unresolved placeholder, and SHALL NOT invoke the Text_Generation_API for that run.
6. IF the Text_Generation_API returns an error or does not respond within the configured generation timeout during workflow execution, THEN THE Workflow_Engine SHALL record the failure in the node's Inference_Metadata with an error indication containing the failure reason, SHALL continue the workflow run per the workflow's existing per-node error handling behavior, and SHALL NOT terminate execution of nodes that do not depend on the failed node's output.
7. WHEN an LLM_Inference_Node execution completes, THE Workflow_Engine SHALL make the node's Inference_Metadata output (generated text or recorded failure) available to downstream nodes (filters, conditionals, outputs, custom Python nodes) through the existing Inference_Metadata mechanisms.

### Requirement 8: Backward Compatibility

**User Story:** As an existing user of the platform, I want all current model types, workflows, deployments, and input types to keep working unchanged, so that adopting this feature carries no regression risk.

#### Acceptance Criteria

1. WHEN a workflow containing no LLM_Inference_Node is validated, compiled, or packaged, THE Portal SHALL produce the same validation outcomes, the same compiled per-architecture pipeline documents, and packaged component artifacts with the same content as pre-feature behavior, excluding non-deterministic fields such as timestamps and version identifiers.
2. WHEN an existing vision model (trained or BYOM) is published, packaged, or deployed, THE Portal SHALL process it through the existing publish, packaging, and deployment paths, SHALL apply no vLLM-specific validation to it, and SHALL produce a model component with the same structure and content as pre-feature behavior.
3. THE Workflow_Designer SHALL continue to support all existing node types, input types (camera sources, Aravis camera sources, folder sources, digital inputs), custom Python nodes, custom plugin node types, and output bindings alongside the LLM_Inference_Node.
4. WHEN a Workflow_Definition created before this feature is opened in the Workflow_Designer, THE Workflow_Designer SHALL load and render it without modifying its stored definition, and revalidation SHALL produce the same validation outcome as pre-feature behavior.
5. WHEN a deployment contains no vLLM_Model_Component and no workflow with an LLM_Inference_Node, THE Deployment_Service SHALL apply pre-feature validation behavior for every Target_Architecture including `arm64_jp4`, and SHALL reject no deployment that pre-feature validation would have accepted.
6. WHEN a deployment contains a vLLM_Model_Component, THE Deployment_Service SHALL apply the platform gating validation of Requirement 3 regardless of the deployment's workflow content.
7. WHEN a vision model is deployed to a JetPack 4 device after this feature is delivered, THE LocalServer JetPack 4 image SHALL load and serve the model with the same inference behavior, device APIs, and workflow execution function as before this feature.
8. WHILE a vLLM model and a vision model are both loaded on the same device, THE LocalServer SHALL serve inference requests for both model types without unloading either model.
9. IF device GPU memory is insufficient to load a vLLM model and a vision model together, THEN THE LocalServer SHALL log a load failure identifying the model that could not be loaded and SHALL continue serving every model already loaded.
