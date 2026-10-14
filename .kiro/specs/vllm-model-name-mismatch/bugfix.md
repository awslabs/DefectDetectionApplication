# Bugfix Requirements Document

## Introduction

Workflows with an `llm_inference` node fail on-device with
`Text_Generation_API returned 409 for model 'Qwen2.5-7B-Instruct-AWQ': {'model_name': 'Qwen2.5-7B-Instruct-AWQ', 'state': 'unknown'}`
whenever the model's portal registry name is not already lowercase-hyphen-safe.

The publish pipeline sanitizes the registry name (`Qwen2.5-7B-Instruct-AWQ` →
`qwen2-5-7b-instruct-awq`) when building the vLLM model component and Triton
repository, so the device serves the model under the **sanitized** name. But
workflow packaging compiles the llm node's `modelName` parameter **verbatim**
from the registry name into `workflow.json` / `compiled_pipeline.json`, and the
device workflow engine passes that verbatim name into the text-generation URL.
For any registry name where `sanitize(name) != name`, the packaged name and the
served name are guaranteed to differ, producing a 409 on every LLM inference.

Verified live on JP6 hardware: POST to
`/text-generation/Qwen2.5-7B-Instruct-AWQ/generate` → 409 `state: 'unknown'`;
POST to `/text-generation/qwen2-5-7b-instruct-awq/generate` → 200 with
generated text. The previous smoke model `opt125m-smoke` never exposed this
because its registry name was sanitization-stable (`sanitize(name) == name`).

**Scope**: portal Lambda code only (`edge-cv-portal/backend/functions/`). A
device-side alias (accepting unsanitized names in the text-generation API) is
explicitly OUT of scope — noted as future hardening — because it requires a
~2h LocalServer build.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a workflow containing an `llm_inference` node is packaged and the referenced model's registry name is not sanitization-stable (`sanitize(name) != name`) THEN the system compiles the registry name verbatim as `modelName` into the packaged `workflow.json` and `compiled_pipeline.json` (verified in the published component 6.0.0 artifact: `"modelName": "Qwen2.5-7B-Instruct-AWQ"`)

1.2 WHEN such a packaged workflow executes an LLM inference binding on the device THEN the system requests `/text-generation/{verbatim registry name}/generate` and the Text_Generation_API returns 409 `state: 'unknown'`, because the model is served only under the sanitized name (`qwen2-5-7b-instruct-awq`)

### Expected Behavior (Correct)

2.1 WHEN a workflow containing an `llm_inference` node is packaged THEN the system SHALL rewrite the node's `modelName` in the packaged artifacts (`workflow.json` and `compiled_pipeline.json`) to the sanitized served name, using the same transform `_safe_model_name` applies at publish time (`re.sub(r'[^a-zA-Z0-9-]', '-', name.lower())`)

2.2 WHEN the sanitization transform is applied THEN the system SHALL use a single shared definition of the transform across `workflow_packaging.py`, `packaging.py`, and `greengrass_publish.py` (single source of truth, not a third copy of the regex)

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a workflow with an `llm_inference` node references a model whose registry name is already sanitization-stable (`sanitize(name) == name`, e.g. `opt125m-smoke`) THEN the system SHALL CONTINUE TO package that `modelName` unchanged

3.2 WHEN a workflow contains no `llm_inference` node THEN the system SHALL CONTINUE TO produce packaged artifacts identical to today's output (non-LLM nodes, including `model_inference`, are untouched)

3.3 WHEN packaging resolves the workflow's `model_ref` values to published model components for the recipe's `ComponentDependencies` THEN the system SHALL CONTINUE TO resolve them through the Model_Registry using the original registry name (resolution is keyed by registry name; the rewrite must not break it)

3.4 WHEN the model publisher derives the vLLM component name and Triton repository directory THEN the system SHALL CONTINUE TO produce the same names as before (`model-vllm-{safe_name}` / `{safe_name}`)
