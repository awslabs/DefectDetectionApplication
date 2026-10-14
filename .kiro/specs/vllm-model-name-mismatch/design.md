# vLLM Model Name Mismatch Bugfix Design

## Overview

The vLLM publish pipeline sanitizes the portal registry model name
(`_safe_model_name`: lowercase, every character outside `[a-zA-Z0-9-]` becomes
`-`) when deriving the Greengrass component name and the Triton repository
directory — so the device serves the model under the sanitized name. Workflow
packaging, however, compiles the `llm_inference` node's `modelName` parameter
verbatim from the registry name into the packaged artifacts, and the device
workflow engine (`output_bindings.py::_run_one`) sends that verbatim name to
`http://localhost:5000/text-generation/{model_name}/generate`. For any registry
name that is not sanitization-stable, the packaged name and the served name
differ and every LLM inference gets a 409.

The fix is a single-transform rewrite in the portal packaging Lambda: when
packaging a workflow, rewrite each `llm_inference` node's `modelName` to
`safe_model_name(registry_name)` in both packaged artifacts (`workflow.json`
and `compiled_pipeline.json`), sharing the transform with the publish/packaging
code paths instead of duplicating the regex a third time. Model-component
dependency resolution keeps using the original registry name.

Scope is portal Lambda code only (`edge-cv-portal/backend/functions/`). A
device-side alias in the Text_Generation_API (accepting unsanitized names) is
explicitly out of scope for this bugfix — future hardening, since it requires
a ~2h LocalServer build.

## Glossary

- **Bug_Condition (C)**: a workflow containing an `llm_inference` node whose referenced registry model name is not sanitization-stable — `safe_model_name(name) != name` — is packaged
- **Property (P)**: the packaged artifacts carry the llm node's `modelName` equal to the name the device serves the model under, i.e. `safe_model_name(registry_name)`
- **Preservation**: packaging output for non-LLM nodes, for sanitization-stable model names, and for model-component dependency resolution is byte-for-byte unchanged
- **safe_model_name / `_safe_model_name`**: the sanitization transform `re.sub(r'[^a-zA-Z0-9-]', '-', str(name).lower())`, currently duplicated in `greengrass_publish.py` (~line 289) and `packaging.py` (~line 356)
- **Sanitization-stable**: a name for which `safe_model_name(name) == name` (e.g. `opt125m-smoke`)
- **`workflow_packaging.py::package_workflow`**: the Component_Packager Lambda handler that loads the stored Workflow_Definition, compiles it per architecture, and writes `workflow.json` + `compiled_pipeline.json` into the per-arch artifact zip
- **`gather_model_references`**: collects effective `model_ref` parameter values (verbatim) from the definition; feeds `resolve_model_components`, which looks records up in the Model_Registry snapshot **keyed by the original registry name**
- **`output_bindings.py::_run_one`** (device side, `src/backend/workflow_engine/`): passes `parameters.get("modelName")` verbatim into the text-generation URL — unchanged by this fix
- **Served name**: the model name segment of the device Text_Generation_API route, equal to the Triton repository directory name, i.e. `safe_model_name(registry_name)`

## Bug Details

### Bug Condition

The bug manifests when workflow packaging compiles an `llm_inference` node
whose `modelName` (registry name) contains any character outside
`[a-z0-9-]` — capitals, dots, underscores, etc. The publish pipeline and the
workflow packager disagree on the name: publisher sanitizes, packager does not.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type PackagingRequest
         (a Workflow_Definition + its referenced registry model names)
  OUTPUT: boolean

  RETURN EXISTS node IN input.definition.nodes
         WHERE node.type = 'llm_inference'
           AND safe_model_name(effectiveModelName(node)) != effectiveModelName(node)
END FUNCTION

FUNCTION safe_model_name(name)
  RETURN regex_replace(lowercase(name), '[^a-zA-Z0-9-]', '-')
END FUNCTION
```

### Examples

- Registry name `Qwen2.5-7B-Instruct-AWQ` → served as `qwen2-5-7b-instruct-awq`; packaged artifact 6.0.0 carries `"modelName": "Qwen2.5-7B-Instruct-AWQ"` → device POST to `/text-generation/Qwen2.5-7B-Instruct-AWQ/generate` returns 409 `{'model_name': 'Qwen2.5-7B-Instruct-AWQ', 'state': 'unknown'}`. Expected: packaged `modelName` = `qwen2-5-7b-instruct-awq` → 200 with generated text (both verified live on JP6).
- Registry name `my_model.v2` → served as `my-model-v2`; packaged verbatim → guaranteed 409. Expected: packaged as `my-model-v2`.
- Registry name `opt125m-smoke` (sanitization-stable) → served as `opt125m-smoke`; packaged verbatim happens to match → works today. This is why the historic smoke tests never caught the bug. Expected: unchanged.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- `llm_inference` nodes with sanitization-stable model names package to identical artifacts (rewrite is a no-op)
- Workflows with no `llm_inference` node package byte-identically — non-LLM nodes, including `model_inference`, are never rewritten
- Model-component dependency resolution (`gather_model_references` → `resolve_model_components`) continues to look up the Model_Registry by the **original** registry name; the recipe's `ComponentDependencies` entries are unchanged
- `greengrass_publish.py` component naming (`model-vllm-{safe_name}`) and `packaging.py` Triton repository naming (`{safe_name}`) produce the same output as before the shared-transform refactor
- The device workflow engine is untouched

**Scope:**
All packaging inputs that do NOT contain an `llm_inference` node with a
non-sanitization-stable model name are completely unaffected. This includes:
- LLM workflows whose model names are already safe
- Vision-only workflows (`model_inference`, camera nodes, plugins, custom Python nodes)
- The publish pipeline for vLLM and vision model components

**Note:** The expected correct behavior for buggy inputs is defined in
Correctness Properties (Property 1). This section is about what must NOT change.

## Hypothesized Root Cause

Fully diagnosed and verified on real JP6 hardware — this is a confirmed root
cause chain, not a hypothesis:

1. **Publisher sanitizes**: `greengrass_publish.py::_safe_model_name` (~line 289) and `packaging.py::_safe_model_name` (~line 356) apply `re.sub(r'[^a-zA-Z0-9-]', '-', name.lower())`, so component `model-vllm-qwen2-5-7b-instruct-awq` serves the model under `qwen2-5-7b-instruct-awq`.
2. **Packager does not**: `workflow_packaging.py` compiles the llm node's `modelName` verbatim from the registry name — the stored `definition_json` is written into the zip as `workflow.json` unmodified, and the compiler output (`compiled_pipeline.json`) carries the same verbatim parameter.
3. **Device passes through**: `output_bindings.py::_run_one` (~line 902) sends `parameters.get("modelName")` verbatim into `TEXT_GENERATION_URL`.
4. **Result**: for any non-sanitization-stable registry name, packaged name ≠ served name → guaranteed 409 `state: 'unknown'`.

The mismatch went unnoticed because the previous smoke model `opt125m-smoke`
was sanitization-stable.

## Correctness Properties

Property 1: Bug Condition - Packaged LLM modelName Equals Served Name

_For any_ registry model name (mixed case, dots, underscores, arbitrary unsafe
characters) referenced by an `llm_inference` node, the packaged artifacts
produced by the fixed packaging path SHALL carry that node's `modelName` equal
to `safe_model_name(registry_name)` — the exact name the publish pipeline
serves the model under — in both `workflow.json` and `compiled_pipeline.json`.

**Validates: Requirements 2.1, 2.2**

Property 2: Preservation - Non-LLM Nodes and Stable Names Unchanged

_For any_ packaging input where the bug condition does NOT hold — workflows
with no `llm_inference` node, or LLM workflows whose referenced model names are
sanitization-stable — the fixed packaging path SHALL produce the same artifact
content as the original path, and model-component dependency resolution SHALL
continue to receive the original registry names.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

## Fix Implementation

### Changes Required

**1. Single source of truth for the transform**

**File**: `edge-cv-portal/backend/functions/model_naming.py` (new, same Lambda bundle directory as its consumers)

- `def safe_model_name(model_name: str) -> str` containing the one regex: `re.sub(r'[^a-zA-Z0-9-]', '-', str(model_name).lower())`
- `greengrass_publish.py` and `packaging.py` replace their private `_safe_model_name` bodies with delegation to (or direct import of) the shared function — behavior identical, verified by Property 2 / existing publish tests

**2. Rewrite llm node modelName at packaging time**

**File**: `edge-cv-portal/backend/functions/workflow_packaging.py`

**Function**: `package_workflow` (plus a small pure helper, e.g. `rewrite_llm_model_names(definition_dict) -> Dict`)

**Specific Changes**:
1. **Gather model references from the ORIGINAL definition first**: `gather_model_references` / `resolve_model_components` must keep seeing the original registry names (registry snapshot is keyed by them — Requirement 3.3). Compute the references before any rewrite, or run the rewrite only on the serialized artifact copies.
2. **Rewrite the definition**: for each node with `type == 'llm_inference'` (`LLM_INFERENCE_TYPE_ID`), replace the effective `modelName` parameter value with `safe_model_name(value)`. Serialize the rewritten definition as the zip's `workflow.json` (today the zip writes the loaded `definition_json` string verbatim).
3. **Rewrite the compiled documents**: ensure each arch's `compiled_pipeline.json` carries the sanitized `modelName` for llm nodes — either by compiling from the rewritten definition/graph or by applying the same pure rewrite to the compiled document before `compiled_document_json` serialization. One rewrite point applied before both serializations is preferred.
4. **No other node types touched**: the rewrite is keyed strictly on the llm node type; `model_inference` and all other parameters pass through untouched (Requirement 3.2).

### Out of Scope (future hardening)

- Device-side alias: Text_Generation_API accepting unsanitized names (requires ~2h LocalServer build)
- Renaming already-published model components or migrating registry names

## Testing Strategy

### Validation Approach

Two-phase: first surface counterexamples proving the packaged `modelName`
diverges from the served name on UNFIXED code, then verify the fix produces
the served name for all unsafe registry names while preserving byte-identical
output for everything else.

Tests live under `edge-cv-portal/backend/tests/` following the existing
patterns there (moto-backed `aws_stack`/`env` fixtures from `conftest.py`, or
pure-function property tests against the packaging helpers as in
`test_property_custom_python_gathering.py`; Hypothesis profile `portal-fast`
caps at 25 examples). Known pre-existing failures to ignore: IAM CDK-synth
statement-count test, cdk.out drift guard, portal workflow test-runner
failures.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples demonstrating the mismatch BEFORE the fix.
The root cause is already verified on hardware; this test encodes it as an
executable property and will validate the fix when it passes afterward.

**Test Plan**: Property-based test generating registry names with mixed case,
dots, and underscores; build a minimal LLM workflow definition referencing the
name; run it through the packaging compile/serialization path; assert the llm
node's `modelName` in the produced `workflow.json` and `compiled_pipeline.json`
equals `safe_model_name(registry_name)` (the name the publisher serves under).
Run on UNFIXED code — expect FAILURE for every non-sanitization-stable name.

**Test Cases**:
1. **Live counterexample**: registry name `Qwen2.5-7B-Instruct-AWQ` → packaged `modelName` must equal `qwen2-5-7b-instruct-awq` (fails on unfixed code)
2. **Generated unsafe names**: Hypothesis-generated names containing `[A-Z._]` (fails on unfixed code)
3. **Stable-name subcase**: generated names already in `[a-z0-9-]` pass even on unfixed code (documents why `opt125m-smoke` masked the bug)

**Expected Counterexamples**:
- Any name with a capital, dot, or underscore: packaged `modelName` = verbatim registry name ≠ served name
- Cause: `workflow_packaging.py` never applies `_safe_model_name`

### Fix Checking

**Goal**: For all inputs where the bug condition holds, the fixed packaging path produces the served name.

**Pseudocode:**
```
FOR ALL request WHERE isBugCondition(request) DO
  artifacts := package_fixed(request)
  ASSERT ∀ llm node n: artifacts.workflow_json.modelName(n)
           = artifacts.compiled_pipeline.modelName(n)
           = safe_model_name(registryName(n))
END FOR
```

### Preservation Checking

**Goal**: For all inputs where the bug condition does NOT hold, the fixed path produces the same result as the original path.

**Pseudocode:**
```
FOR ALL request WHERE NOT isBugCondition(request) DO
  ASSERT package_original(request) = package_fixed(request)
END FOR
```

**Testing Approach**: Property-based testing — preservation is a universal
claim over the non-buggy input domain, and generated definitions (non-LLM
nodes, stable LLM names) catch edge cases manual cases would miss.

**Test Plan**: Observe on UNFIXED code first, then encode as properties that
must pass before AND after the fix.

**Test Cases**:
1. **Stable LLM name identity**: for generated sanitization-stable names, the rewrite is a no-op and packaged artifacts match the unfixed output
2. **Non-LLM workflow identity**: for generated definitions without `llm_inference` nodes, artifact serialization is identical to the unfixed output
3. **Model-reference resolution**: `gather_model_references` output (the names fed to registry resolution) equals the original registry names, LLM or not
4. **Publisher naming identity**: after the shared-transform refactor, `greengrass_publish.derive_vllm_component_name` and `packaging.generate_vllm_repository` produce the same names as the pre-refactor regex for all generated inputs

### Unit Tests

- `Qwen2.5-7B-Instruct-AWQ` end-to-end through the packaging serialization path → `qwen2-5-7b-instruct-awq` in both artifacts
- Shared `safe_model_name` matches the two pre-existing private copies on representative inputs
- llm node with `modelName` supplied via parameter default (not explicit value) is also rewritten

### Property-Based Tests

- Property 1 (exploration/fix): generated unsafe registry names → packaged `modelName` equals served name
- Property 2 (preservation): generated stable-name and non-LLM definitions → artifacts unchanged; resolution names unchanged; publisher naming unchanged

### Integration Tests

- Existing packaging suites (`test_workflow_packaging_*.py`, `test_vllm_packaging_dispatch.py`, `test_greengrass_publish_localserver.py`) still pass — regression net over recipe, dependency, and publish paths
- Post-deploy manual validation (delivery path, not CI): deploy via `edge-cv-portal/deploy-infrastructure.sh`, repackage the workflow (v7), deploy to the JP6 device, confirm LLM inference returns 200

## Amendment (vllm-multi-arch-publish-conflict)

Amended by `.kiro/specs/vllm-multi-arch-publish-conflict/` (branch `spec/jetpack7-support`), which introduced per-JetPack component name suffixes (e.g. `model-vllm-{safe}-jetson-xavier-jp7`). This spec's intent is not regressed:

- The Triton model identity travels on `--model_name`, which is still `_safe_model_name(model_name)`, unchanged. `--component_name` is logging-only in `src/backend/dda_triton/vllm_model_prep.py` (its argparse help says "(logging)"; `prepare()` binds it to `component` and only logs it), so suffixing the component name has zero device-side runtime impact.
- `derive_vllm_component_name` still returns `model-vllm-{safe_model_name}` verbatim; the per-target suffixing appends to that base name. This spec's transform-equality property test (`test_property_llm_model_name_preservation.py`) passes unmodified.
