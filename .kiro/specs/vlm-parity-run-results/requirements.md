# Requirements Document

## Introduction

Two related gaps after the bedrock-response-mode feature:

1. The VLM/LLM Inference node (`llm_inference`) lacks the Bedrock node's returned-value options: there is no `anomaly_mode` toggle, no verdict parsing, and its text is only reachable at `llm.{nodeId}.generated_text` — so a VLM cannot drive anomaly filters/outputs the way Bedrock can.
2. The run results view ("View results") shows nothing useful for Bedrock/VLM runs: the frames sent to Bedrock are deleted with the run's temp dir, runs without a File_Output_Node terminal get no artifact directory at all (so even the metadata JSON is not persisted), and the results screen renders only a single File_Output image with no metadata.

This feature gives `llm_inference` the same returned-value contract as `bedrock_inference`, persists the inference nodes' sent frames into the run's artifact directory, and upgrades the run results screen to show the 1–2 sent images with an anomaly-verdict overlay and the returned metadata below (anomaly mode), or the single sent image with the metadata below (freeform mode).

## Glossary

- **Sent frame**: the JPEG a bedrock_inference capture branch persisted for a port (`in`/`reference`), or the frame feeding the llm_inference node's `in` port.
- **Verdict overlay**: a frontend-rendered anomaly result presentation over/beside the image (e.g. ANOMALOUS/NORMAL badge with confidence and colored border) — NOT a pixel mask; Bedrock/VLM verdicts have no mask.
- **Node images**: per-inference-node persisted frames `{capture_id}.node.{nodeId}.{port}.jpg` in the run's `output_dir`.
- **Anomaly mode / freeform mode**: as defined by bedrock-response-mode (`anomaly_mode` parameter).

## Requirements

### Requirement 1: VLM returned-value parity with Bedrock

**User Story:** As a workflow author, I want the VLM node's `anomaly_mode` checkbox and returned values to behave like the Bedrock node's, so I can build anomaly workflows on local VLMs interchangeably with Bedrock.

#### Acceptance Criteria

1.1 WHEN the `llm_inference` catalog descriptor is defined THEN it SHALL carry an `anomaly_mode` bool parameter — default FALSE (today's behavior: freeform text generation) — with a description mirroring the Bedrock node's (checked: verdict JSON contract with the executor-appended instruction; unchecked: freeform text)

1.2 WHEN an `llm_inference` binding runs with `anomaly_mode` true THEN the executor SHALL append the canonical JSON instruction (`BEDROCK_JSON_INSTRUCTION`) to the rendered prompt, parse the generated text with the shared verdict parser, merge {is_anomalous, confidence} into the run's flat metadata (driving downstream filters/conditionals/outputs exactly like Bedrock), and STILL record the raw text at `llm.{nodeId}.generated_text`

1.3 WHEN an anomaly-mode `llm_inference` answer is unparseable as the verdict JSON THEN the executor SHALL record `{'error': <reason including an answer excerpt>, 'generated_text': <text>}` for the node and SHALL NOT merge verdict keys — preserving the llm contract that a binding failure is recorded, never raised (unlike Bedrock)

1.4 WHEN `anomaly_mode` is absent or false THEN the `llm_inference` behavior SHALL be byte-identical to today (rendered prompt sent as-is, `{generated_text}` recorded, no verdict)

### Requirement 2: Sent frames persist into the run's artifacts

**User Story:** As a workflow operator, I want the images that were sent to Bedrock/VLM nodes preserved with the run, so the results view can show what the model actually saw.

#### Acceptance Criteria

2.1 WHEN the compiler processes an `llm_inference` node THEN it SHALL emit a frame-capture plan for the node's `in` port exactly like `bedrock_inference` (synthetic capture sink on the feeding branch, `capturePaths` on the binding) — safe because `llm_inference` outputs only InferenceMeta, so no frames flow beyond it

2.2 WHEN a run with bedrock_inference or llm_inference bindings completes (success or output-binding failure) THEN the executor SHALL copy each binding's captured frames from the work dir into the run's `output_dir` as `{capture_id}.node.{nodeId}.{port}.jpg` BEFORE the work dir is deleted (best-effort, contained)

2.3 WHEN a run has inference-node captures or run metadata to persist but no File_Output_Node terminal THEN the executor SHALL still assign and record the run's `output_dir`/`capture_id` (creating the directory), so the metadata JSON and node images have a destination — fixing the current behavior where Bedrock-only runs persist nothing

2.4 WHEN node images were persisted THEN the run SHALL be marked as having viewable image results (the "View results" link appears)

### Requirement 3: Results view shows sent images, verdict overlay, and metadata

**User Story:** As a workflow operator, I want the run results screen to show the sent image(s) with the anomaly outcome and the model's returned metadata, so I can judge results at a glance.

#### Acceptance Criteria

3.1 WHEN the results endpoint reports a run's images THEN it SHALL include the node images (`kind: "node"`, with `nodeId` and `port`) alongside the existing output-image entry, and a serving route SHALL return each node image (token-in-query pattern, like the existing output-image route)

3.2 WHEN the results screen renders a run with node images from an ANOMALY-mode inference node THEN it SHALL show the node's 1–2 sent images with a verdict overlay (ANOMALOUS/NORMAL + confidence, visually distinct e.g. colored border/badge) and the returned metadata below (verdict fields plus the model's raw text)

3.3 WHEN the results screen renders a run whose inference node ran in FREEFORM mode THEN it SHALL show the single sent image with the returned metadata (the raw text) below it

3.4 WHEN a run also produced the existing File_Output image/mask THEN that display SHALL CONTINUE TO render exactly as today, with the node-image sections shown in addition

3.5 WHEN metadata or images are missing/partial THEN the screen SHALL degrade gracefully (existing best-effort/empty-state discipline; no crashes, no 500s)

### Requirement 4: Unchanged behavior

#### Acceptance Criteria

4.1 WHEN existing packaged workflows run (no `anomaly_mode` on llm bindings, no repackage) THEN llm behavior, bedrock behavior, artifact routing for File_Output runs, and the existing results/overlay/metadata/node-status API shapes SHALL CONTINUE TO work unchanged (API extensions are additive)

4.2 WHEN the compiler processes non-inference nodes THEN compilation output SHALL CONTINUE TO be byte-identical; existing bedrock capture plans unchanged

4.3 WHEN the simulation (sandbox) path compiles llm_inference THEN the sim stub behavior SHALL CONTINUE TO be unchanged

4.4 NOTE: existing packaged workflows will not have llm capturePaths until repackaged — the executor and results view SHALL tolerate their absence (llm runs without captures simply show no image, metadata only)
