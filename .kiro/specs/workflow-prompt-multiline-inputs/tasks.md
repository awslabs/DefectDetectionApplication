# Implementation Plan: Workflow Prompt Multiline Inputs

## Overview

Implement the Multiline_Hint end to end: add the `multiline` field to `ParameterDescriptor` (both byte-identical catalog copies), finalize the `system_prompt` descriptors and hint the five Target_Parameters in `nodes.py` (both copies), rebaseline the CSI preservation golden, emit the hint conditionally in `parameter_to_wire`, and render hinted string parameters as a Cloudscape `Textarea rows={4}` in `NodeConfigPanel.tsx`. Backend tests use pytest + hypothesis; frontend tests use vitest + fast-check + testing-library. Every property test runs at least 100 iterations and is tagged `Feature: workflow-prompt-multiline-inputs, Property N: {title}`.

## Tasks

- [x] 1. Catalog data model and wire serialization
  - [x] 1.1 Add the `multiline` field to `ParameterDescriptor` in both catalog copies
    - Append `multiline: bool = False` after `examples` in the frozen dataclass in `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/models.py`
    - Add the docstring prose describing the Multiline_Hint (rendering hint for string parameters; `False` keeps every existing descriptor and wire form unchanged)
    - Apply the identical bytes to `src/backend/workflow_engine/vendor/workflow_core/catalog/models.py` (mirror tests require byte identity)
    - _Requirements: 2.1_

  - [x] 1.2 Emit the hint conditionally in `parameter_to_wire`
    - In `edge-cv-portal/backend/functions/workflow_validation.py::parameter_to_wire`, add `wire['multiline'] = True` only when `parameter.multiline` is truthy, per the design snippet
    - Do not touch any other key; descriptors without the hint must serialize byte-identically to the pre-feature form, and the existing `test_parameter_wire_shape` in `edge-cv-portal/backend/tests/test_node_catalog_wire.py` must stay green unmodified
    - _Requirements: 2.3, 2.4, 2.5, 1.3_

  - [ ]* 1.3 Write property test for wire serialization exactness
    - **Property 1: Wire serialization marks exactly the hinted descriptors and preserves the pre-feature form otherwise**
    - **Validates: Requirements 2.3, 2.4, 2.5**
    - New file `edge-cv-portal/backend/tests/test_property_multiline_wire.py` (hypothesis, min 100 examples): generate random descriptors (names, types, required, defaults, constraints with snake_case keys, depends_on, description, examples) with a random hinted subset; compare `parameter_to_wire` output against an inlined pre-feature reference serializer for unhinted descriptors; assert `multiline: true` on exactly the hinted subset

- [x] 2. Finalize catalog declarations in `nodes.py` (both copies)
  - [x] 2.1 Finalize the `system_prompt` descriptors on both inference nodes
    - In `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`, keep the working-tree `system_prompt` descriptors' name, string type, `required=False`, `default=""`, and description text exactly as the device declarations, adding only the required non-empty `examples` and `multiline=True`, per the design's descriptor snippets (BEDROCK_INFERENCE after `max_tokens`, LLM_INFERENCE after `top_p`)
    - Apply the identical bytes to `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`
    - _Requirements: 1.1, 1.2, 1.6, 2.2_

  - [x] 2.2 Add `multiline=True` to the three existing long-text descriptors
    - In both `nodes.py` copies, add only `multiline=True` to `bedrock_inference.prompt`, `llm_inference.prompt_template`, and `mqtt_publish.payload_template`; leave names, types, defaults (including `payload_template`'s `{inference_json}`), constraints, descriptions, and visibility gating untouched
    - Confirm the two copies remain byte-identical (e.g. `cp` the portal file over the vendored one or `cmp` them)
    - _Requirements: 2.2, 2.6, 5.2_

  - [x] 2.3 Rebaseline the CSI preservation golden for the vendored `nodes.py`
    - Recompute `sha256sum src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py` and update only that entry in `test/backend-test/csi_nvargus_optional/goldens/backend_csi_consumers_sha256.json`
    - _Requirements: 6.2_

  - [ ]* 2.4 Write example-based tests pinning the catalog data
    - Backend test asserting the five Target_Parameters (and no other parameter in the catalog) declare `multiline=True`
    - Pin the `system_prompt` descriptors on both inference nodes: name, string type, `required=False`, `default=""`, description, non-empty examples
    - _Requirements: 1.1, 1.2, 2.2, 2.6, 5.2_

  - [ ]* 2.5 Write example-based tests for the served catalog wire shapes
    - Assert the `get_node_catalog` response includes both `system_prompt` wire descriptors with `multiline: true` and the other three Target_Parameters hinted
    - Verify the existing `test_parameter_wire_shape` and non-empty-examples tests pass unmodified against the updated catalog
    - _Requirements: 1.3, 2.3, 2.4_

- [x] 3. Checkpoint - Backend catalog gates
  - Ensure all tests pass, ask the user if questions arise.
  - Run `test/backend-test/workflow_engine/test_vendored_catalog_mirror.py`, the portal-layer `test_catalog_content.py` mirror check, the `test/backend-test/csi_nvargus_optional/` preservation suite, the full `workflow_core` layer test suite, and `edge-cv-portal/backend/tests/`

- [ ] 4. Backend graph-level property tests
  - [ ]* 4.1 Write property test for workflow definition round trip
    - **Property 4: Workflow definition round trip preserves multi-line values**
    - **Validates: Requirements 4.3**
    - In `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/tests/`, new file extending the existing graph generators (pattern of `test_serializer_parse.py`) to place multi-line strings in Target_Parameters; assert `parse(serialize(g))` yields byte-identical parameter values (hypothesis, min 100 examples)

  - [ ]* 4.2 Write property test for omitted system_prompt validity
    - **Property 5: Definitions omitting system_prompt stay valid**
    - **Validates: Requirements 1.5**
    - New workflow_core test file: generate valid graphs with `bedrock_inference` and/or `llm_inference` nodes omitting `system_prompt`; assert catalog validation produces no finding referencing `system_prompt` (hypothesis, min 100 examples)

  - [ ]* 4.3 Write property test for newline acceptance in validation
    - **Property 6: Newlines are ordinary string content to validation**
    - **Validates: Requirements 4.4**
    - New workflow_core test file: generate otherwise-valid graphs and multi-line string values satisfying each Target_Parameter's declared constraints; assert no validation finding for those parameters (hypothesis, min 100 examples)

  - [ ]* 4.4 Write property test for compilation neutrality of the hint
    - **Property 7: The hint is compilation-neutral**
    - **Validates: Requirements 6.3**
    - New workflow_core test file: generate compilable graphs; compile against the catalog and against a hint-stripped copy (`dataclasses.replace(p, multiline=False)` on every descriptor); assert identical compiler output (hypothesis, min 100 examples)

- [x] 5. Frontend multiline rendering
  - [x] 5.1 Add the `multiline` field to the frontend wire type
    - In `edge-cv-portal/frontend/src/pages/workflows/types.ts`, add optional `multiline?: boolean | null` to the `ParameterDescriptor` interface with the doc comment from the design; leave the hand-mirrored descriptors (`MQTT_SUBSCRIBE_DESCRIPTOR`, `OPCUA_SUBSCRIBE_DESCRIPTOR`, `MODBUS_WRITE_DESCRIPTOR`) unchanged
    - _Requirements: 2.3_

  - [x] 5.2 Render hinted string parameters as a Textarea in `NodeConfigPanel.tsx`
    - In `ParameterControl`, add the new branch `paramType === 'string' && descriptor.multiline === true` rendering a Cloudscape `Textarea` with `rows={4}`, `value={textValue(value)}`, verbatim `onChange(detail.value)`, and `ariaLabel={descriptor.name}`, placed immediately before the single-line string fallback (after the `requirements`/`bool`/numeric/`code`/`model_ref`/`enum` branches)
    - Leave `ParameterField` chrome (label, `- optional` marker, description, example chips, constraint violations, `dependsOn` gating) and the single-line fallback untouched
    - _Requirements: 3.1, 3.2, 3.3, 3.6, 3.8, 1.4, 4.6_

  - [ ]* 5.3 Write property test for control kind selection
    - **Property 2: Control kind follows the hint**
    - **Validates: Requirements 3.1, 3.2, 3.6**
    - In `NodeConfigPanel.test.tsx` (fast-check + testing-library, min 100 runs): generate string descriptors with and without `multiline: true`; render the panel; assert `textarea[aria-label=name]` with `rows >= 4` for hinted descriptors and a single-line `input` otherwise

  - [ ]* 5.4 Write property test for value fidelity
    - **Property 3: Controls are value-faithful, newlines included**
    - **Validates: Requirements 3.4, 3.5, 3.7, 6.1**
    - In `NodeConfigPanel.test.tsx` (fast-check, min 100 runs): generate strings containing newlines; assert the control displays the stored value verbatim and change events commit the exact string through `onParametersChange` for both control kinds

  - [ ]* 5.5 Write example-based panel scenario tests
    - Using the real `bedrock_inference`, `llm_inference`, and `mqtt_publish` wire fixtures: `prompt` + `system_prompt`, `prompt_template` + `system_prompt`, and `payload_template` all render as `rows=4` textareas of the same style
    - A hinted descriptor renders the same label/description/examples/violation/dependsOn chrome as an unhinted one
    - A builder node-duplicate carries a multi-line Target_Parameter value verbatim
    - _Requirements: 1.4, 4.1, 4.2, 4.5, 5.1, 3.8_

- [x] 6. Final checkpoint - Full gating suites
  - Ensure all tests pass, ask the user if questions arise.
  - Run the vendored catalog mirror tests, the `csi_nvargus_optional` preservation suite, the full `workflow_core` layer suite, `edge-cv-portal/backend/tests/`, and the `edge-cv-portal/frontend` vitest suite in single-run mode (`vitest --run`)

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Every `nodes.py`/`models.py` edit is applied identically to both catalog copies; `test_vendored_catalog_mirror.py` and `test_catalog_content.py` enforce byte identity
- The vendored `nodes.py` sha256 must be rebaselined in `test/backend-test/csi_nvargus_optional/goldens/backend_csi_consumers_sha256.json` in the same change (task 2.3)
- The pinned `test_parameter_wire_shape` test is Requirement 2.4's backward-compatibility anchor and must stay green unmodified
- No compiler, validator, or device executor changes: the hint is consumed only by the configuration panel
- Actual drag-resizing (Requirement 3.3) is guaranteed by using a native Cloudscape `Textarea`; jsdom cannot exercise CSS resize

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "5.1"] },
    { "id": 1, "tasks": ["1.2", "2.1", "5.2"] },
    { "id": 2, "tasks": ["1.3", "2.2", "5.3"] },
    { "id": 3, "tasks": ["2.3", "2.4", "2.5", "4.1", "4.2", "4.3", "4.4", "5.4"] },
    { "id": 4, "tasks": ["5.5"] }
  ]
}
```
