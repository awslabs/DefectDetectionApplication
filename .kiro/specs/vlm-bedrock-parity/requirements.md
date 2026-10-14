# Requirements Document

## Introduction

The VLM/LLM Inference node (`llm_inference`) lags the Bedrock Inference node (`bedrock_inference`) in two user-visible ways:

1. **No mode toggle in the designer.** The Bedrock node carries an `anomaly_mode` checkbox: checked (anomaly mode), the executor auto-appends the canonical JSON instruction and the model's parsed {is_anomalous, confidence} verdict drives downstream filters, conditionals, and outputs; unchecked (freeform mode), the prompt is sent as-is and the raw model text is recorded in the run metadata. The device executor's `LlmInferenceProcessor` already implements anomaly-mode handling for `llm_inference` bindings (from the vlm-parity-run-results spec), but the catalog descriptor never gained the `anomaly_mode` parameter — so the checkbox does not exist in the Workflow Designer, packaged bindings never carry the parameter, and the executor code path is unreachable.

2. **No reference image input.** The Bedrock node declares two VideoFrames input ports (`in` and `reference`) so the model can compare the inspected frame against a reference image. The `llm_inference` node declares only `in`. The single-image path already works end to end (edge-vlm-image-inference: compiler capture plan for `in`, base64 `image` field on the Text_Generation_API, multimodal prompt in the vLLM runtime), but there is no way to feed a second, reference frame to the on-device VLM.

This feature brings `llm_inference` to functional parity with `bedrock_inference`: the `anomaly_mode` checkbox in the designer catalog, and an optional `reference` VideoFrames input port carried end to end — catalog → compiler frame-capture plan → device processor → Text_Generation_API → vLLM multimodal generation with two images.

## Glossary

- **LLM_Inference_Node**: The `llm_inference` node type in the Node_Type_Catalog (display name "VLM/LLM Inference") that invokes an on-device vLLM model, compiled to the `llm_inference` executor binding on vLLM-capable architectures.
- **Bedrock_Inference_Node**: The `bedrock_inference` node type — the reference behavior for this feature: `in` + `reference` VideoFrames ports, `anomaly_mode` checkbox, anomaly/freeform executor handling.
- **Node_Type_Catalog**: The shared node type descriptor catalog. Two copies must stay byte-identical: the portal copy (`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`) and the vendored device copy (`src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`).
- **Workflow_Compiler**: The workflow_core compiler (`workflow_core/compiler/compiler.py`, portal copy plus vendored device copy) that compiles workflow definitions into per-architecture pipeline documents with executor bindings.
- **Frame_Capture_Plan**: The compiler mechanism that terminates a frame-consuming binding's feeding branch in a synthetic capture sink chain (`videoconvert → jpegenc → multifilesink`) and emits `capturePaths` (port name → `{work_dir}`-rooted JPEG path, or `None` for an unfed port) on the binding.
- **LLM_Inference_Processor**: The device-side `LlmInferenceProcessor` in `src/backend/workflow_engine/output_bindings.py` that runs `llm_inference` bindings after a pipeline run and merges each node's outcome into the run metadata under `metadata['llm'][nodeId]`.
- **Anomaly_Mode**: The mode in which the executor appends the canonical JSON instruction (`BEDROCK_JSON_INSTRUCTION`) to the rendered prompt, parses the model's answer with the shared verdict parser, and merges {is_anomalous, confidence} flat into the run metadata to drive downstream filters, conditionals, and outputs.
- **Freeform_Mode**: The mode in which the rendered prompt is sent as-is (no appended instruction) and the raw model text is recorded as `generated_text` in the node's metadata record with no JSON parsing.
- **Verdict**: The parsed {is_anomalous, confidence} pair produced by the shared `parse_bedrock_answer` parser from an Anomaly_Mode answer.
- **Reference_Image**: The optional second frame, captured from the LLM_Inference_Node's `reference` input port, that the model compares the inspected `in` frame against per the configured prompt.
- **Text_Generation_API**: The device-local endpoints in `src/backend/endpoints/text_generation.py` (`POST /text-generation/{model_name}/generate` and `/generate-stream`) fronting the vLLM runtime; already accepts an optional base64 `image` field for the `in` frame.
- **vLLM_Runtime_Manager**: The device-side `VllmRuntimeManager` in `src/backend/vllm_runtime/manager.py` that builds the vLLM engine prompt — bare string for text-only, or a multimodal prompt dict with `multi_modal_data` when an image accompanies a multimodal-capable model.
- **Multimodal_Model**: A loaded vLLM model whose model configuration declares image input capability (e.g. Qwen2-VL / Qwen2.5-VL architectures), as detected by the vLLM_Runtime_Manager's existing capability check.
- **Workflow_Validator**: The portal subsystem that validates workflow definitions against the Node_Type_Catalog port and parameter rules.
- **Run_Metadata**: The per-run inference metadata dictionary that inference-node outcomes merge into and that downstream filters, conditionals, output bindings, and the run results view consume.

## Requirements

### Requirement 1: Anomaly/freeform mode toggle on the VLM/LLM node

**User Story:** As a workflow author, I want the VLM/LLM Inference node to offer the same anomaly-mode checkbox as the Bedrock node, so that an on-device VLM can drive anomaly filters, conditionals, and outputs — or produce freeform text — interchangeably with Bedrock.

#### Acceptance Criteria

1. THE Node_Type_Catalog SHALL define an `anomaly_mode` bool parameter on the LLM_Inference_Node descriptor with `required=False` and default `False`, with a description explaining both modes in the same terms as the Bedrock_Inference_Node's `anomaly_mode` description.
2. WHEN a workflow author opens an LLM_Inference_Node's configuration in the Workflow Designer, THE Workflow Designer SHALL render the `anomaly_mode` parameter as a checkbox through the existing bool-parameter rendering.
3. WHEN a workflow containing an LLM_Inference_Node with `anomaly_mode` set is packaged, THE Workflow_Compiler SHALL carry the `anomaly_mode` parameter value on the compiled `llm_inference` executor binding.
4. WHEN an `llm_inference` binding runs with `anomaly_mode` true, THE LLM_Inference_Processor SHALL append the canonical JSON instruction to the rendered prompt exactly once before invoking the model, SHALL parse the answer with the shared verdict parser, SHALL merge the Verdict flat into the Run_Metadata, and SHALL record the raw answer text as `generated_text` in the node's metadata record.
5. IF an Anomaly_Mode answer is unparseable as the Verdict JSON, THEN THE LLM_Inference_Processor SHALL record `{'error': <reason including an answer excerpt>, 'generated_text': <text>}` for the node, SHALL merge no Verdict keys, and SHALL continue processing the remaining bindings.
6. WHEN an `llm_inference` binding runs with `anomaly_mode` absent or false, THE LLM_Inference_Processor SHALL send the rendered prompt unchanged and record the raw model text as `generated_text` with no JSON parsing, identical to pre-feature Freeform_Mode behavior.
7. THE LLM_Inference_Node descriptor's `prompt_template` parameter description SHALL state that the executor appends the JSON instruction automatically in Anomaly_Mode and that Freeform_Mode sends the rendered prompt as-is.

### Requirement 2: Reference image input port

**User Story:** As a workflow author, I want to connect a reference image into the VLM/LLM Inference node like I can on the Bedrock node, so that the on-device VLM compares the inspected frame against a known-good reference.

#### Acceptance Criteria

1. THE Node_Type_Catalog SHALL define a `reference` input port of type VideoFrames on the LLM_Inference_Node descriptor, declared after the existing `in` port, mirroring the Bedrock_Inference_Node's port shape.
2. WHEN a workflow author connects a VideoFrames-producing node to the LLM_Inference_Node's `reference` port, THE Workflow_Validator SHALL accept the connection under the same port compatibility rules that apply to the Bedrock_Inference_Node's `reference` port.
3. WHEN a workflow containing an LLM_Inference_Node with an unconnected `reference` port is validated, THE Workflow_Validator SHALL treat the `reference` port as optional and report no validation error for the unconnected port, matching the Bedrock_Inference_Node's optional-reference behavior.
4. WHEN the Workflow_Compiler compiles a workflow in which a video source feeds an LLM_Inference_Node's `reference` port, THE Workflow_Compiler SHALL extend the Frame_Capture_Plan so the feeding branch terminates in a synthetic capture sink and the binding's `capturePaths` maps `reference` to the capture file path.
5. WHEN the Workflow_Compiler compiles a workflow in which an LLM_Inference_Node's `reference` port is unfed, THE Workflow_Compiler SHALL emit the binding's `capturePaths` with `reference` mapped to `None` or absent, so the executor treats the run as single-image.
6. WHEN one video source feeds ports of multiple frame-consuming bindings (`bedrock_inference` or `llm_inference`, any port), THE Workflow_Compiler SHALL plan exactly one capture sink for that feeder and reference the single shared capture file from every consuming binding's `capturePaths`.

### Requirement 3: Reference image carried through the device execution path

**User Story:** As a workflow operator, I want the reference image actually delivered to the on-device VLM at inference time, so that the model's comparison answer reflects both images.

#### Acceptance Criteria

1. WHEN an `llm_inference` binding's `capturePaths` maps `reference` to a path and the resolved file is readable, THE LLM_Inference_Processor SHALL base64-encode the captured reference frame and include it in the model invocation alongside the `in` frame.
2. IF an `llm_inference` binding's `capturePaths` maps `reference` to a path but the resolved file cannot be read, THEN THE LLM_Inference_Processor SHALL record a node error naming the node, the `reference` port, and the path, SHALL invoke no model for that binding, and SHALL continue processing the remaining bindings.
3. WHEN an `llm_inference` binding's `capturePaths` omits `reference` or maps it to `None`, THE LLM_Inference_Processor SHALL issue the invocation with no reference image, identical to pre-feature single-image behavior.
4. WHEN the LLM_Inference_Processor invokes the model with a Reference_Image, THE Text_Generation_API request SHALL carry the reference frame as an optional base64 `reference_image` field alongside the existing `image` field.
5. WHEN a generate request carries a `reference_image` field, THE Text_Generation_API SHALL validate it with the same rules as the existing `image` field (base64 string decoding to between 1 byte and the configured maximum), and IF validation fails, THEN THE Text_Generation_API SHALL reject the request with a finding naming the `reference_image` field before any runtime invocation.
6. WHEN a valid generate request carries both `image` and `reference_image` for a Multimodal_Model, THE vLLM_Runtime_Manager SHALL build a multimodal prompt whose `multi_modal_data` carries both decoded images in a defined order (input frame first, reference second) with the prompt text containing a matching number of image placeholders.
7. IF a generate request carries a `reference_image` for a model that is not a Multimodal_Model, THEN THE vLLM_Runtime_Manager SHALL generate text-only with a logged warning, matching the existing single-image degradation behavior.
8. WHEN a generate request carries no `reference_image` field, THE Text_Generation_API and vLLM_Runtime_Manager SHALL process the request identically to pre-feature behavior.

### Requirement 4: Comparison semantics parity

**User Story:** As a workflow author, I want the VLM node's default guidance to match the Bedrock node's comparison framing, so that switching a workflow between Bedrock and an on-device VLM needs no prompt rework.

#### Acceptance Criteria

1. THE LLM_Inference_Node descriptor's `prompt_template` parameter description SHALL explain that a connected `reference` port sends the reference image with the prompt for comparison, and SHALL provide a comparison example consistent with the Bedrock_Inference_Node's default comparison prompt.
2. WHEN an Anomaly_Mode `llm_inference` run with a Reference_Image produces a parseable Verdict, THE LLM_Inference_Processor SHALL merge the Verdict into the Run_Metadata in the same shape as a Bedrock_Inference_Node anomaly run (flat `is_anomalous`/`confidence` plus the node's nested record), so downstream filters, conditionals, and output bindings evaluate identically for either node type.
3. WHEN the run results view renders an `llm_inference` run whose `in` and `reference` frames were persisted as node images, THE run results view SHALL display both sent images through the existing node-image sections, matching the Bedrock two-image presentation.

### Requirement 5: Catalog copy synchronization

**User Story:** As a maintainer, I want the catalog and compiler changes applied to both the portal and vendored device copies, so that designer, validator, compiler, and device executor agree on the node's shape.

#### Acceptance Criteria

1. WHEN the LLM_Inference_Node descriptor is modified, THE portal Node_Type_Catalog copy and the vendored device copy SHALL receive identical edits and remain byte-identical.
2. WHEN the Workflow_Compiler's Frame_Capture_Plan is modified, THE portal compiler copy and the vendored device compiler copy SHALL receive identical edits and remain byte-identical.

### Requirement 6: Unchanged behavior

**User Story:** As an existing user, I want current workflows, the Bedrock node, and the single-image VLM path to keep working unchanged, so that this parity feature carries no regression risk.

#### Acceptance Criteria

1. WHEN an existing packaged workflow's `llm_inference` binding carries no `anomaly_mode` parameter and no `reference` capture path, THE LLM_Inference_Processor SHALL run it in Freeform_Mode with single-image or text-only behavior identical to pre-feature behavior.
2. WHEN a workflow definition created before this feature contains an LLM_Inference_Node, THE Workflow Designer SHALL load and render it without modification, and revalidation SHALL produce the same validation outcome as pre-feature behavior.
3. WHEN a workflow contains no LLM_Inference_Node, THE Workflow_Compiler SHALL produce per-architecture pipeline documents identical to pre-feature compilation, including unchanged `bedrock_inference` capture plans.
4. WHEN a `bedrock_inference` binding runs, THE executor SHALL behave identically to pre-feature behavior in both modes — this feature touches no Bedrock executor code path.
5. WHEN a generate request carries neither `image` nor `reference_image`, THE Text_Generation_API SHALL produce a response identical to pre-feature behavior.
6. WHEN the simulation architecture compiles an LLM_Inference_Node, THE Workflow_Compiler SHALL emit the existing `sim_llm_inference` stub unchanged, with no capture plan and no model invocation.
