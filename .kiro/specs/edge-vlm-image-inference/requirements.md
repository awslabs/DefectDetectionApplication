# Requirements Document

## Introduction

The workflow engine's "VLM/LLM Inference" node (type_id `llm_inference`) declares a VideoFrames input port but is text-only end to end: the compiler emits no frame captures for `llm_inference` executor bindings, the on-device processor invokes the Text_Generation_API with prompt text alone, the API schema has no image field, and the vLLM runtime passes a bare prompt string to the engine with no multimodal data. A deployed workflow (folder_source → llm_inference → capture + mqtt_publish) on a JP6 device serving a quantized Qwen VL model completes successfully, but the model answers "I don't see any image" because the frame is never attached to the inference request.

This feature adds image (multimodal) input to the edge VLM inference path by mirroring the existing `bedrock_inference` frame-capture mechanism: the Workflow_Compiler emits `capturePaths` for `llm_inference` bindings whose video input port is fed, the LLM_Inference_Processor reads the captured JPEG and attaches it (base64-encoded) to the Text_Generation_API request, the API forwards decoded image bytes to the vLLM runtime, and the runtime builds a multimodal vLLM prompt (chat-templated text with the model's image placeholder plus `multi_modal_data`) for vision-language models. Text-only prompt flows, existing deployed workflow packages, and text-only models keep byte-identical behavior.

## Glossary

- **Workflow_Compiler**: The shared `workflow_core` compiler (`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/compiler/compiler.py`, vendored identically at `src/backend/workflow_engine/vendor/workflow_core/compiler/compiler.py`) that compiles a workflow definition into per-architecture pipeline documents with `executorBindings`.
- **LLM_Inference_Binding**: A compiled `executorBindings` entry with `binding == "llm_inference"` carrying the bound model name, Prompt_Template, and generation parameters.
- **Capture_Paths**: The `capturePaths` key on an executor binding — a mapping from input port name to a `{work_dir}`-rooted JPEG file path (or `None` when the port is not fed by any video source), produced by the compiler's synthetic frame-capture sink planning.
- **Frame_Capture_Sink**: The synthetic GStreamer sink chain (`videoconvert → jpegenc → multifilesink`) the compiler appends to a feeding branch so the branch's latest frame persists to a Capture_Paths file.
- **LLM_Inference_Processor**: The device-side `LlmInferenceProcessor` in `src/backend/workflow_engine/output_bindings.py` that runs LLM_Inference_Bindings after a pipeline run and merges each node's outcome into the run metadata under `metadata['llm'][nodeId]`.
- **Pipeline_Executor**: The device-side `WorkflowExecutor` in `src/backend/workflow_engine/pipeline_executor.py` that runs compiled documents, prepares the per-run work directory, and invokes the LLM_Inference_Processor.
- **Text_Generation_API**: The device-local FastAPI endpoints in `src/backend/endpoints/text_generation.py` (`POST /text-generation/{model_name}/generate` and `/generate-stream`) fronting the vLLM runtime.
- **Vllm_Runtime_Manager**: The `VllmRuntimeManager` in `src/backend/vllm_runtime/manager.py` that owns per-model vLLM engines and serves `generate`/`generate_stream`.
- **Triton_Generate_Server**: The loopback Triton generate-extension HTTP server in `src/backend/vllm_runtime/server.py` (`GenerateRequest` = `{text_input, parameters}`).
- **Multimodal_Model**: A loaded vLLM model whose architecture accepts image input (a vision-language model such as Qwen2-VL / Qwen2.5-VL), as detectable from the model's configuration.
- **Image_Payload**: An optional request field carrying one base64-encoded JPEG image for the inference request.
- **Node_Error_Record**: The per-node contained failure record (`{'error': reason}` merged under `metadata['llm'][nodeId]`) that marks a node failed without terminating the run or affecting other bindings.

## Requirements

### Requirement 1: Compiler Frame Capture for LLM Inference Bindings

**User Story:** As a workflow author, I want the compiler to capture the frames feeding my VLM inference node, so that the on-device processor has an image file to attach to the inference request.

#### Acceptance Criteria

1. WHEN the Workflow_Compiler compiles a workflow containing an `llm_inference` node whose `in` port is fed by at least one video source, THE Workflow_Compiler SHALL append a Frame_Capture_Sink to each feeding GStreamer branch and SHALL emit Capture_Paths on the node's LLM_Inference_Binding mapping the `in` port to the feeder's `{work_dir}`-rooted capture file path.
2. WHEN the Workflow_Compiler compiles a workflow containing an `llm_inference` node whose `in` port is not fed by any video source, THE Workflow_Compiler SHALL emit Capture_Paths on the node's LLM_Inference_Binding mapping the `in` port to `None` and SHALL append no Frame_Capture_Sink for that node.
3. WHEN one GStreamer feeder branch feeds input ports of multiple `llm_inference` or `bedrock_inference` nodes, THE Workflow_Compiler SHALL plan exactly one Frame_Capture_Sink and one capture file for that feeder, shared by every consuming binding's Capture_Paths.
4. WHEN the Workflow_Compiler compiles a workflow containing an `llm_inference` node, THE Workflow_Compiler SHALL preserve the node's existing non-opaque stream treatment, so that frames continue to flow through the collapsed executor-level node to downstream pipeline elements exactly as before this feature.
5. WHEN the Workflow_Compiler compiles a workflow containing no `llm_inference` node, THE Workflow_Compiler SHALL produce a compiled pipeline document identical to pre-feature output.
6. THE Workflow_Compiler copies at `edge-cv-portal/backend/layers/workflow_core` and `src/backend/workflow_engine/vendor/workflow_core` SHALL remain byte-identical after this feature.

### Requirement 2: Processor Frame Attachment

**User Story:** As a workflow author, I want the on-device processor to attach the captured frame to the inference request, so that the vision-language model sees the image my workflow feeds it.

#### Acceptance Criteria

1. WHEN the LLM_Inference_Processor runs an LLM_Inference_Binding whose Capture_Paths maps the `in` port to a path and the resolved file is readable, THE LLM_Inference_Processor SHALL read the captured JPEG, substitute the per-run work directory for the `{work_dir}` placeholder before reading, and include the frame as a base64-encoded Image_Payload in the Text_Generation_API request.
2. WHEN the LLM_Inference_Processor runs an LLM_Inference_Binding whose Capture_Paths is absent or maps the `in` port to `None`, THE LLM_Inference_Processor SHALL invoke the Text_Generation_API with no Image_Payload and a request otherwise identical to pre-feature behavior.
3. IF an LLM_Inference_Binding's Capture_Paths maps the `in` port to a path and the resolved file cannot be read, THEN THE LLM_Inference_Processor SHALL record a Node_Error_Record identifying the node, the port, and the unreadable path, and SHALL invoke the Text_Generation_API zero times for that binding.
4. WHEN the Pipeline_Executor invokes the LLM_Inference_Processor for a document containing LLM_Inference_Bindings, THE Pipeline_Executor SHALL pass the per-run work directory to the LLM_Inference_Processor.
5. WHEN a Node_Error_Record is recorded for one LLM_Inference_Binding, THE LLM_Inference_Processor SHALL continue processing the document's remaining bindings and THE Pipeline_Executor SHALL continue the run per the existing per-node error containment behavior.

### Requirement 3: Text Generation API Image Support

**User Story:** As a device-side integrator, I want the Text_Generation_API to accept an optional image with a generation request, so that callers can request multimodal inference over the local HTTP contract.

#### Acceptance Criteria

1. THE Text_Generation_API SHALL accept an optional `image` field on generate and generate-stream request bodies containing a base64-encoded JPEG Image_Payload.
2. WHEN a generate request contains a valid Image_Payload, THE Text_Generation_API SHALL decode the base64 content and forward the decoded image bytes to the Vllm_Runtime_Manager generate invocation together with the prompt and sampling parameters.
3. WHEN a generate request omits the `image` field, THE Text_Generation_API SHALL produce a normalized request and a runtime invocation identical to pre-feature behavior.
4. IF a generate request's `image` field is not a string, is not decodable as base64, or decodes to zero bytes, THEN THE Text_Generation_API SHALL return a validation error naming the `image` field and the reason, and SHALL invoke the Vllm_Runtime_Manager zero times for that request.
5. IF a generate request's Image_Payload decodes to more than the configured maximum image size, with a default of 16 MiB, THEN THE Text_Generation_API SHALL return a validation error naming the `image` field and the size bound, and SHALL invoke the Vllm_Runtime_Manager zero times for that request.
6. WHEN a generate response is returned for a request that carried an Image_Payload, THE Text_Generation_API SHALL include in the response an indication of whether the image was consumed by the model.

### Requirement 4: vLLM Runtime Multimodal Generation

**User Story:** As an edge device operator, I want the on-device vLLM runtime to run vision-language inference with the supplied image, so that VLM models answer about the actual frame instead of reporting no image.

#### Acceptance Criteria

1. WHEN the Vllm_Runtime_Manager generate interface is invoked with image bytes for a Multimodal_Model, THE Vllm_Runtime_Manager SHALL construct a multimodal engine prompt consisting of chat-templated prompt text containing the model's image placeholder tokens and `multi_modal_data` carrying the decoded image, and SHALL generate with that prompt.
2. THE Vllm_Runtime_Manager SHALL determine whether a loaded model is a Multimodal_Model from the model's configuration without requiring additional per-model operator settings.
3. WHEN the Vllm_Runtime_Manager generate interface is invoked with image bytes for a model that is not a Multimodal_Model, THE Vllm_Runtime_Manager SHALL log the omission, generate with the text-only prompt exactly as pre-feature behavior, and report that the image was not consumed.
4. WHEN the Vllm_Runtime_Manager generate interface is invoked without image bytes, THE Vllm_Runtime_Manager SHALL construct the engine request identically to pre-feature behavior.
5. THE Vllm_Runtime_Manager SHALL support multimodal generation for Qwen VL model families (Qwen2-VL and Qwen2.5-VL) at minimum.
6. IF the engine reports an error while generating with a multimodal prompt, THEN THE Vllm_Runtime_Manager SHALL raise the existing generation error carrying the model name and the backend reason, and SHALL leave every other loaded model unaffected.
7. IF the supplied image bytes cannot be decoded into an image object, THEN THE Vllm_Runtime_Manager SHALL raise the existing generation error identifying the image decoding failure without invoking the engine.
8. THE Triton_Generate_Server SHALL accept an optional base64-encoded `image` field on its generate request schema and forward the decoded image bytes to the Vllm_Runtime_Manager generate invocation.

### Requirement 5: Error Surfacing and Containment

**User Story:** As a workflow operator, I want image-path failures surfaced in the node's run metadata without killing the run, so that diagnosis stays consistent with the existing per-node containment contract.

#### Acceptance Criteria

1. IF the Text_Generation_API returns an error for an LLM_Inference_Binding invocation that carried an Image_Payload, THEN THE LLM_Inference_Processor SHALL record a Node_Error_Record containing the failure reason and SHALL continue processing the remaining bindings.
2. WHEN a Node_Error_Record is recorded for an LLM_Inference_Binding, THE Pipeline_Executor SHALL mark that node failed in the run's node status map while leaving nodes that do not depend on the failed node's output running to completion.

### Requirement 6: Backward Compatibility

**User Story:** As an existing user of deployed VLM/LLM workflows, I want text-only prompt flows and already-deployed packages to keep working unchanged, so that this feature carries no regression risk.

#### Acceptance Criteria

1. WHEN the LLM_Inference_Processor runs a compiled document produced before this feature (LLM_Inference_Bindings carrying no Capture_Paths), THE LLM_Inference_Processor SHALL produce outcomes identical to pre-feature behavior.
2. WHEN the Text_Generation_API receives a request without an `image` field, THE Text_Generation_API SHALL apply validation, defaults, retry, and timeout behavior identical to pre-feature behavior.
3. WHEN a text-only model serves a generation request without image bytes, THE Vllm_Runtime_Manager SHALL produce engine invocations identical to pre-feature behavior.
4. WHEN the LLM_Inference_Processor renders a Prompt_Template, THE LLM_Inference_Processor SHALL apply the existing placeholder substitution, anomaly-mode handling, and 409-loading retry behavior unchanged regardless of whether an Image_Payload is attached.
