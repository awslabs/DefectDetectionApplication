# Requirements Document

## Introduction

The Bedrock Inference node's JSON answer contract ({"is_anomalous": ..., "confidence": ...}) currently lives only in the default prompt text (`BEDROCK_DEFAULT_PROMPT`). A user-customized prompt that omits the JSON instruction makes `parse_bedrock_answer` fail and the run finalize FAILED, even though the model answered fine. This feature (a) hardens the anomaly contract by making the executor auto-append a canonical JSON-format instruction to every anomaly-mode prompt, and (b) adds a checkbox to the node toggling between "anomaly action" (today's JSON verdict driving downstream filters) and "freeform action" (the raw model text recorded into the run metadata, no JSON parsing).

## Glossary

- **Anomaly mode**: the current behavior — the model must answer the JSON verdict; is_anomalous/confidence merge into the run's inference metadata. Default.
- **Freeform mode**: new — the raw model text is recorded into the metadata (flat `bedrock_text` plus per-node `bedrock.{nodeId}.text`), no JSON parsing, no is_anomalous/confidence contribution from this node.
- **Canonical JSON instruction**: a fixed executor-owned suffix instructing the model to answer with the verdict JSON shape (single source of truth in `output_bindings.py`).
- **`anomaly_mode` parameter**: new bool ParameterDescriptor on the `bedrock_inference` node (default true → anomaly mode; unchecked → freeform mode). Bool parameters render as checkboxes in the workflow designer (same as the mqtt node's `greengrass`/`aws_iot`).

## Requirements

### Requirement 1: Anomaly-mode prompt hardening

**User Story:** As a workflow author, I want my custom Bedrock prompt to never break the JSON verdict contract, so that prompt wording changes cannot fail my runs.

#### Acceptance Criteria

1.1 WHEN a `bedrock_inference` binding runs in anomaly mode (parameter `anomaly_mode` absent or true) THEN the executor SHALL append the canonical JSON instruction to the configured prompt (exactly once, separated by a blank line) before invoking the model

1.2 WHEN the prompt already contains arbitrary user text (including text resembling the instruction) THEN the executor SHALL still append the canonical instruction deterministically — appending is unconditional and idempotent per invocation

1.3 WHEN the model answers in anomaly mode THEN the executor SHALL parse and merge {is_anomalous, confidence} into the run metadata exactly as today

### Requirement 2: Freeform mode

**User Story:** As a workflow author, I want a checkbox that switches the Bedrock node to freeform text output, so I can use vision-language models for descriptions/OCR/reports instead of only anomaly verdicts.

#### Acceptance Criteria

2.1 WHEN the `bedrock_inference` node's `anomaly_mode` checkbox is unchecked (parameter false) THEN the executor SHALL send the configured prompt unchanged (no JSON instruction appended)

2.2 WHEN a freeform-mode invocation returns THEN the executor SHALL record the raw model text into the run metadata as flat `bedrock_text` and per-node `bedrock.{nodeId}.text`, SHALL NOT run `parse_bedrock_answer`, and SHALL NOT add is_anomalous/confidence

2.3 WHEN a freeform-mode answer is any text at all (including text that would be unparseable as the verdict JSON) THEN the run SHALL NOT fail on account of answer format

2.4 WHEN downstream output bindings render templates THEN `{bedrock_text}` SHALL be available as a placeholder (via the existing metadata-driven `render_template` flow, no new template machinery)

### Requirement 3: Catalog parameter

**User Story:** As a workflow author, I want the toggle visible on the node in the designer, so the mode is explicit per node.

#### Acceptance Criteria

3.1 WHEN the `bedrock_inference` NodeTypeDescriptor is defined THEN it SHALL carry a new `ParameterDescriptor("anomaly_mode", "bool", required=False, default=True)` with a description explaining both modes

3.2 WHEN the catalog is edited THEN the portal copy (`edge-cv-portal/backend/layers/workflow_core/.../catalog/nodes.py`) and the vendored device copy (`src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`) SHALL receive identical edits and remain byte-in-sync

3.3 WHEN the `prompt` parameter description mentions the JSON contract THEN it SHALL be updated to state that the executor appends the JSON instruction automatically in anomaly mode and that freeform mode sends the prompt as-is

### Requirement 5: Anomaly mode preserves the model's textual notes (added after on-device verification)

**User Story:** As a workflow author, I want the model's full textual answer available even in anomaly mode, so prompts that ask for notes/explanations alongside the verdict don't silently lose that text.

#### Acceptance Criteria

5.1 WHEN an anomaly-mode invocation returns THEN the executor SHALL record the raw model answer text into the run metadata as flat `bedrock_text` and per-node `bedrock.{nodeId}.text` (the same keys freeform mode uses), IN ADDITION TO merging the parsed {is_anomalous, confidence} verdict

5.2 WHEN the run metadata is read by the run-observability API THEN the anomaly-mode raw text SHALL be visible in the output preview card (it renders the run metadata JSON) and usable as the `{bedrock_text}` template placeholder in output bindings

5.3 WHEN the anomaly-mode answer is unparseable as the verdict JSON THEN the existing BedrockInferenceError failure SHALL CONTINUE TO apply (the raw text still need not be recorded on the failure path — the error message already carries the answer excerpt)

### Requirement 4: Unchanged behavior

#### Acceptance Criteria

4.1 WHEN an existing packaged workflow's binding carries no `anomaly_mode` parameter THEN the executor SHALL run it in anomaly mode (default true) — existing workflows keep working, now hardened

4.2 WHEN the primary `in` frame is unavailable, the invoker raises, or (in anomaly mode) the answer is unparseable THEN the existing `BedrockInferenceError` surfacing (node id, run FAILED) SHALL CONTINUE TO apply; the single-image reference fallback SHALL CONTINUE TO work identically in both modes

4.3 WHEN documents contain no `bedrock_inference` bindings THEN tag_values SHALL CONTINUE TO pass through unchanged

4.4 WHEN other catalog node types and the compiler's bedrock capture-path emission are exercised THEN they SHALL CONTINUE TO behave identically — this feature touches only the bedrock node's parameters list and the executor's prompt/answer handling
