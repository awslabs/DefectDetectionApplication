# Requirements Document

## Introduction

The VLM/LLM Inference node (`llm_inference`) should offer the same authoring surface as the Bedrock Inference node (`bedrock_inference`): an `anomaly_mode` checkbox that switches between the anomaly verdict contract (executor-appended JSON instruction, parsed `{is_anomalous, confidence}` driving downstream filters/conditionals/outputs) and freeform text, plus a `reference` VideoFrames input port whose frame is sent to the VLM alongside the input frame for comparison.

Current state (verified in the tree):

- The device executor (`LlmInferenceProcessor` in `src/backend/workflow_engine/output_bindings.py`) ALREADY implements anomaly-mode parity (vlm-parity-run-results Requirement 1: `anomaly_mode` truthy appends `BEDROCK_JSON_INSTRUCTION` to the rendered prompt, parses the verdict with the shared parser, merges `{is_anomalous, confidence}` flat; unparseable answers record `{'error', 'generated_text'}` without raising) and single `in`-frame attachment (edge-vlm-image-inference: `capturePaths["in"]` read, base64-encoded, sent as the Text_Generation_API `image` field).
- The catalog `llm_inference` descriptor (both copies) carries NEITHER the `anomaly_mode` parameter NOR a `reference` input port, so neither capability is reachable from the workflow designer.
- The compiler's frame-capture plan (`_bedrock_capture_plan`) already covers `llm_inference` bindings and iterates a descriptor's input ports generically — a new `reference` port joins the plan without new compiler logic.
- The Text_Generation_API (`src/backend/endpoints/text_generation.py`), the Triton generate-extension server (`src/backend/vllm_runtime/server.py`), and the vLLM runtime manager (`src/backend/vllm_runtime/manager.py`) support exactly ONE image per request; the reference image needs a second optional field end to end and multi-image prompt construction.

This feature closes those gaps: catalog exposure of `anomaly_mode` and the `reference` port, executor reference-frame attachment mirroring Bedrock's optional-reference semantics, and reference-image transport through the Text_Generation_API into the vLLM engine.

## Glossary

- **VLM_Node**: the `llm_inference` node type — catalog descriptor in `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py` and its byte-identical vendored copy at `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`.
- **Bedrock_Node**: the `bedrock_inference` node type — the parity model for this feature.
- **Anomaly mode**: `anomaly_mode` parameter truthy — the executor appends the Canonical_JSON_Instruction to the rendered prompt, parses the answer with the shared verdict parser, and merges `{is_anomalous, confidence}` flat into the run metadata (in addition to the nested `llm.{nodeId}` record).
- **Freeform mode**: `anomaly_mode` absent or false — the rendered prompt is sent as-is and the raw text is recorded at `llm.{nodeId}.generated_text` (today's behavior; the VLM analog of `bedrock_text`).
- **Canonical_JSON_Instruction**: `BEDROCK_JSON_INSTRUCTION` in `src/backend/workflow_engine/output_bindings.py` — the single source of truth for the verdict answer shape.
- **Reference frame**: the JPEG captured from the branch feeding the VLM_Node's `reference` input port, persisted by the compiler's synthetic capture sink and recorded in the binding's `capturePaths["reference"]`.
- **LLM_Processor**: `LlmInferenceProcessor` in `src/backend/workflow_engine/output_bindings.py` — runs `llm_inference` bindings after a pipeline run; binding failures are recorded (`{'error': reason}`), never raised.
- **Text_Generation_API**: the device-local generate endpoint in `src/backend/endpoints/text_generation.py` (`/text-generation/{model_name}/generate`).
- **Runtime_Manager**: `VllmRuntimeManager` in `src/backend/vllm_runtime/manager.py` — owns loaded vLLM engines and builds engine prompts (including the multimodal prompt dict).
- **Capture plan**: the compiler's synthetic frame-capture sinks (`videoconvert ! jpegenc ! multifilesink`) plus per-binding `capturePaths` emission in `workflow_core/compiler/compiler.py` (both copies).
- **Multimodal_Model**: a loaded vLLM model whose architecture accepts image input (Qwen2-VL / Qwen2.5-VL families at minimum), as detected by the Runtime_Manager.

## Requirements

### Requirement 1: Catalog — anomaly_mode parameter on the VLM node

**User Story:** As a workflow author, I want the VLM/LLM node to show the same anomaly/freeform checkbox as the Bedrock node, so I can build anomaly workflows on local VLMs interchangeably with Bedrock.

#### Acceptance Criteria

1.1 WHEN the `llm_inference` NodeTypeDescriptor is defined THEN it SHALL carry a `ParameterDescriptor("anomaly_mode", "bool", required=False, default=False)` whose description mirrors the Bedrock_Node's (checked: executor-appended Canonical_JSON_Instruction, parsed verdict drives downstream filters/conditionals/outputs; unchecked: freeform — prompt sent as-is, raw text at `llm.{nodeId}.generated_text`)

1.2 WHEN the `prompt_template` parameter description is read THEN it SHALL state that in anomaly mode the executor automatically appends the JSON-format instruction and the parsed verdict becomes the inference metadata, and that freeform mode sends the rendered prompt as-is

1.3 WHEN a packaged workflow carries `anomaly_mode: true` on an `llm_inference` binding THEN the LLM_Processor SHALL apply the already-implemented anomaly-mode contract (instruction appended to the rendered prompt exactly once, verdict parsed and merged flat, raw text still recorded at `llm.{nodeId}.generated_text`; unparseable answer → `{'error': <reason with excerpt>, 'generated_text': <text>}` recorded, never raised)

### Requirement 2: Catalog — reference input port on the VLM node

**User Story:** As a workflow author, I want to wire a reference image into the VLM/LLM node like I do on the Bedrock node, so the model can compare the inspected frame against a known-good example.

#### Acceptance Criteria

2.1 WHEN the `llm_inference` NodeTypeDescriptor is defined THEN its inputs SHALL be `PortDescriptor("in", PORT_TYPE_VIDEO_FRAMES)` followed by `PortDescriptor("reference", PORT_TYPE_VIDEO_FRAMES)` — the same port names and types as the Bedrock_Node

2.2 WHEN a workflow definition leaves the VLM_Node's `reference` port unconnected THEN the validator SHALL accept the definition (the port is optional, exactly like the Bedrock_Node's `reference` port)

2.3 WHEN the workflow designer renders a VLM_Node THEN the `reference` input handle SHALL appear via the existing catalog-driven port rendering (no type-specific frontend code)

### Requirement 3: Compiler — reference frame capture

**User Story:** As a workflow author, I want the frame feeding the VLM node's reference port captured for the executor, so the model actually receives the reference image at run time.

#### Acceptance Criteria

3.1 WHEN a compiled `llm_inference` binding is emitted for a vLLM-capable architecture and the node's `reference` port is fed by a video source THEN the binding's `capturePaths` SHALL map `"reference"` to a `{work_dir}`-rooted capture file path, and the compiled document SHALL contain a synthetic capture sink chain on the feeding branch persisting that file

3.2 WHEN the VLM_Node's `reference` port is not fed THEN the binding's `capturePaths` SHALL map `"reference"` to `None` (the same shape the Bedrock_Node's unfed reference produces)

3.3 WHEN one video source feeds both a VLM_Node port and a Bedrock_Node port (or several ports of either kind) THEN the compiled document SHALL contain exactly one capture sink chain for that feeder and every consuming binding SHALL reference the shared capture file

3.4 WHEN the simulation architecture is compiled THEN the `sim_llm_inference` stub SHALL CONTINUE TO be emitted unchanged (no capture plan on the sim path)

### Requirement 4: Executor — reference frame attachment

**User Story:** As a workflow operator, I want the captured reference frame sent to the VLM alongside the input frame, so the model performs the comparison my prompt describes.

#### Acceptance Criteria

4.1 WHEN an `llm_inference` binding's `capturePaths` maps `"reference"` to a path and the resolved file is readable THEN the LLM_Processor SHALL base64-encode the Reference frame and pass it to the invoker alongside the input frame's image

4.2 WHEN `capturePaths` maps `"reference"` to `None`, omits the key, or the resolved reference file is missing or unreadable THEN the LLM_Processor SHALL log the omission and proceed with single-image inference on the input frame alone (the Bedrock_Node's optional-reference semantics; a missing reference is never a node error)

4.3 WHEN the reference image rides an invocation THEN the request body sent by the default invoker SHALL carry it as an optional `reference_image` field beside the existing `image` field, and invocations without a reference SHALL produce a request body byte-identical to today's

4.4 WHEN a reference frame is attached THEN the prompt rendering, anomaly-mode instruction appending, verdict parsing, and error containment SHALL be identical to the no-reference invocation — the reference only adds the `reference_image` field

### Requirement 5: Text_Generation_API — reference image transport

**User Story:** As a device integrator, I want the generate endpoint to accept a reference image with the same validation discipline as the existing image field, so the executor's two-image request reaches the runtime safely.

#### Acceptance Criteria

5.1 WHEN a generate request body carries a `reference_image` field THEN `normalize_generation_request` SHALL validate it with exactly the rules applied to `image` (string, valid base64, decodes to 1..MAX_IMAGE_BYTES bytes) and record failures as findings naming the `reference_image` field

5.2 WHEN a request carries a valid `reference_image` THEN the Text_Generation_API SHALL forward the decoded bytes to the Runtime_Manager's generate invocation alongside the decoded `image` bytes

5.3 WHEN a request omits `reference_image` THEN normalization, the runtime invocation, and the response SHALL be identical to pre-feature behavior

5.4 IF a request carries a `reference_image` without an `image` THEN the Text_Generation_API SHALL reject the request with a finding explaining that a reference image requires a primary image

5.5 WHEN the Triton generate-extension server receives its optional `reference_image` field THEN it SHALL base64-decode it and pass it through to the Runtime_Manager exactly like the existing `image` field (schema parity between the two HTTP surfaces)

### Requirement 6: vLLM runtime — two-image multimodal generation

**User Story:** As a workflow operator, I want the loaded VLM to receive both images in one prompt, so its answer reflects the comparison.

#### Acceptance Criteria

6.1 WHEN the Runtime_Manager generates for a Multimodal_Model with an image and a reference image THEN it SHALL build an engine prompt whose chat-templated text labels and places two image content parts (input first, reference second) and whose `multi_modal_data` carries both decoded images in that order

6.2 WHEN the Runtime_Manager generates with only an image (no reference) THEN the engine prompt SHALL be identical to pre-feature single-image behavior

6.3 WHEN the Runtime_Manager generates with no image THEN the bare prompt string SHALL be passed exactly as today

6.4 IF the model is not multimodal and images are supplied THEN the Runtime_Manager SHALL log a warning and generate text-only (the existing degradation), with `image_used` reporting false

6.5 IF the reference image bytes cannot be decoded as an image THEN the Runtime_Manager SHALL raise the existing `GenerationError` naming the decode failure before the engine is invoked

6.6 WHEN a vLLM engine is constructed for a Multimodal_Model THEN the engine arguments SHALL allow at least two images per prompt (`limit_mm_per_prompt`), while an explicit `model.json` value for that argument SHALL be honored unchanged

### Requirement 7: Non-interference

**User Story:** As an operator of existing deployments, I want workflows that don't use the new capabilities to behave exactly as before, so this feature is safe to roll out.

#### Acceptance Criteria

7.1 WHEN an existing packaged workflow's `llm_inference` binding carries no `anomaly_mode` parameter and no `reference` capture path THEN the LLM_Processor's behavior (prompt, invocation body, recorded outcome) SHALL be byte-identical to today's

7.2 WHEN a workflow definition contains `llm_inference` nodes with only the `in` port connected THEN validation, compilation output (per architecture, including capture plan and `capturePaths` shapes), packaging serialization round-trips, and the sim binding SHALL be identical to pre-feature output

7.3 WHEN a workflow definition contains no `llm_inference` node THEN compilation output SHALL be byte-identical to pre-feature output, including Bedrock_Node capture plans

7.4 WHEN the catalog copies are compared after the change THEN the portal copy and the vendored device copy SHALL be byte-identical (re-vendor via `src/backend/workflow_engine/vendor/re_vendor.sh`), and the catalog content tests (`EXPECTED_TYPE_IDS`, `catalog_baseline.json` maintenance path) SHALL pass

7.5 WHEN generate requests without any image field reach the Text_Generation_API or the Triton generate-extension server THEN request validation, runtime invocation, and responses SHALL be identical to pre-feature behavior
