# Implementation Plan

## Overview

The design's audit established that most of this feature shipped in commit `086c251`
(`vlm-anomaly-reference-parity`) and its predecessors. This plan therefore does **not**
re-implement shipped code. It closes the four real gaps and adds the property/preservation
suites that turn the audit's "already satisfied" rows into enforced contracts:

- **G1** — `LlmInferenceProcessor` fail-closed semantics for a fed-but-unreadable `reference`
  frame (Requirement 3.2), plus retargeting the shipped test that encodes the opposite rule.
- **G2** — `llm_inference` `prompt_template` comparison guidance in the catalog (Requirement 4.1),
  applied to the portal copy, re-vendored, baseline regenerated with a reviewed diff.
- **G3** — port-generic node-image surfacing: `run_artifacts.list_node_images`, additive
  `{"kind": "node", ...}` `/results` entries, a token-in-query `node-image` route, and the
  `RunResults.tsx` node sections (Requirement 4.3).
- **G4** — a tree-wide dual-copy byte-equality guard plus the documented re-vendor mechanism
  (Requirements 5.1, 5.2).
- **Guarantee suites** — all 15 correctness properties from design §Correctness Properties and
  the Requirement 6 preservation set.

Language: Python (device/portal backend) and TypeScript (device frontend) — the design uses
real code, not pseudocode.

**Authoritative decision on the design's open question (Requirement 3.2):** FAIL CLOSED. A
`reference` port that IS fed but whose resolved capture file cannot be read produces a contained
node error naming the node, the `reference` port and the resolved path; no model is invoked for
that binding; remaining bindings still process. This deliberately diverges from
`bedrock_inference`'s degrade-to-single-image behavior and **supersedes** the shipped
`vlm-anomaly-reference-parity` Requirement 4.2 rule. An *unfed* `reference` port (`None`/absent)
still means single-image inference, unchanged.

Property tests use Hypothesis with a minimum of 100 iterations and are tagged
`**Feature: vlm-bedrock-parity, Property {n}: {title}**`. Preservation tests follow the repo's
observation-first pattern (`edge-cv-portal/backend/tests/test_property_bedrock_sampling_preservation.py`,
`test_vision_model_packaging_preservation.py`): written so they pass **before and after** the
change, asserting identity by byte/structural equality rather than re-deriving expected values.

## Tasks

- [x] 1. G1 — fail-closed `reference` semantics in the LLM_Inference_Processor
  - [x] 1.1 Retarget the shipped unreadable-reference test to the fail-closed rule
    - `test/backend-test/workflow_engine/test_llm_reference_attachment.py`: rename
      `test_unreadable_reference_is_never_a_node_error` →
      `test_unreadable_reference_is_a_contained_node_error` and invert its assertions — the
      outcome is `{"error": ...}` mentioning the node id, the `reference` port and the resolved
      path; the invoker is never called (`calls == []`); no `generated_text` key
    - Add a module/test comment recording that this rule **supersedes**
      `vlm-anomaly-reference-parity` Requirement 4.2 (degrade-to-single-image) and cites
      `vlm-bedrock-parity` Requirement 3.2 as the authority; update the module docstring's
      "never a node error" sentence accordingly
    - Keep `test_reference_none_is_single_image_inference`,
      `test_reference_key_absent_is_single_image_inference`,
      `test_no_frames_keeps_prefeature_three_argument_arity`,
      `test_unreadable_in_frame_still_errors_before_reference` and
      `test_reference_invariance_of_anomaly_mode_handling` unchanged — the unfed and `in`-frame
      contracts do not move
    - This task runs RED (it fails against the shipped degrade path); 1.2 turns it green
    - _Requirements: 3.2, 3.3, 6.1_

  - [x] 1.2 Implement the fail-closed reference branch
    - `src/backend/workflow_engine/output_bindings.py`, `LlmInferenceProcessor._run_one`
      (reference-attachment block, currently ~lines 1358–1395): replace the `except OSError`
      warn-and-continue path with a contained node error returned **before** any invoker call:
      `{"error": "LLM inference node '<id>' could not read the captured 'reference' frame from
      <resolved path>: <reason>"}`, mirroring the wording/shape of the existing `in`-frame error
      so the two ports produce structurally identical records; log at error level naming node,
      port and resolved path
    - Leave the unfed branch (`not reference_path` → warning + single-image), the `{work_dir}`
      resolution, the invoker arity staging (5/4/3 args) and `_default_llm_invoker`'s
      `reference_image` additivity untouched
    - `process()` continues to store the record under `metadata['llm'][nodeId]` and keeps
      iterating the remaining bindings; no exception crosses the processor boundary
    - Do NOT touch `BedrockInferenceProcessor`'s reference block — Bedrock keeps degrading
    - _Requirements: 3.2, 3.1, 3.3, 6.4_

  - [ ]* 1.3 Write property test for the reference attachment trichotomy
    - **Feature: vlm-bedrock-parity, Property 5: Reference attachment trichotomy**
    - **Validates: Requirements 3.1, 3.2, 3.3**
    - New `test/backend-test/workflow_engine/test_property_llm_reference_trichotomy.py`:
      generate binding sets over the three reference shapes (readable path, `None`/absent,
      missing/unreadable path — unreadable via missing file and `chmod 000`), an injected
      invoker recording arity + argument values, tmp work dirs holding real JPEG bytes; assert
      (a) one invocation with the reference bytes base64 in the reference position, (b) one
      invocation with no reference argument, (c) zero invocations and an error naming node/port/
      path; in all three shapes assert every other binding in the document still processes and
      prompt rendering, instruction appending, verdict parsing and the flat merge are unaffected
    - _Requirements: 3.1, 3.2, 3.3_

  - [ ]* 1.4 Write property test for Anomaly_Mode instruction appending
    - **Feature: vlm-bedrock-parity, Property 4: Anomaly_Mode instruction appended exactly once**
    - **Validates: Requirements 1.4**
    - New `test/backend-test/workflow_engine/test_property_llm_anomaly_instruction.py`: generate
      prompt templates, satisfying metadata and answer texts; assert the invoked prompt equals
      the rendered template followed by exactly one occurrence of `BEDROCK_JSON_INSTRUCTION`
      (count the substring), and that the raw answer is recorded as `generated_text`
    - _Requirements: 1.4, 1.5_

  - [ ]* 1.5 Write property test for verdict merge shape equality across node types
    - **Feature: vlm-bedrock-parity, Property 9: Verdict merge shape equality across node types**
    - **Validates: Requirements 4.2**
    - New `test/backend-test/workflow_engine/test_property_verdict_merge_parity.py`: run both
      processors over equivalent documents (one `llm_inference` node, one `bedrock_inference`
      node, same generated Anomaly_Mode answer text, reference present and absent); assert the
      Run_Metadata projections carry the same flat `is_anomalous`/`confidence` keys with the same
      values, and that evaluating generated downstream filter/conditional expressions over those
      keys yields identical outcomes for the two node types
    - _Requirements: 4.2_

- [x] 2. Checkpoint — LLM/Bedrock executor suites
  - `PYTHONPATH=src/backend:test/backend-test:test/backend-test/workflow_engine python3 -m pytest
    test/backend-test/workflow_engine/test_llm_reference_attachment.py
    test/backend-test/workflow_engine/test_llm_anomaly_mode.py
    test/backend-test/workflow_engine/test_workflow_llm_inference.py
    test/backend-test/workflow_engine/test_workflow_bedrock_inference.py
    test/backend-test/workflow_engine/test_property_llm_reference_trichotomy.py
    test/backend-test/workflow_engine/test_property_llm_anomaly_instruction.py
    test/backend-test/workflow_engine/test_property_verdict_merge_parity.py -q`
  - Per `.kiro/steering/builds.md`, run this family standalone (known moto leakage in full sweeps)
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. G2 — catalog `prompt_template` comparison guidance (both copies)
  - [x] 3.1 Extend the `llm_inference` `prompt_template` descriptor (portal copy)
    - `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`,
      `LLM_INFERENCE`: append to `prompt_template`'s `description` a sentence stating that when
      the `reference` port is connected the captured reference image is sent with the prompt so
      the model can compare the inspected frame against it; add a comparison prompt to
      `examples` that is consistent with `BEDROCK_DEFAULT_PROMPT` (same comparison framing and
      `is_anomalous` wording), keeping the existing metadata-placeholder example
    - Add nothing else: no parameter added, renamed or retyped, no default changed
      (`anomaly_mode` stays `default=False` per design decision 2), no port change
    - _Requirements: 4.1, 1.7, 6.2_

  - [x] 3.2 Re-vendor the device copy and regenerate the catalog baseline
    - Run `src/backend/workflow_engine/vendor/re_vendor.sh`; never hand-edit
      `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`
    - Regenerate `edge-cv-portal/backend/layers/workflow_core/tests/catalog_baseline.json` per the
      documented maintenance path (`dataclasses.asdict` per descriptor, keyed by `type_id`, same
      JSON formatting/sort order as the committed file)
    - Review both diffs and confirm they contain **only** the `llm_inference` `prompt_template`
      `description`/`examples` change — any other descriptor delta means stop and investigate
    - `diff -r` the portal `workflow_core` tree against the vendored copy (excluding
      `__pycache__`) and confirm no differences
    - _Requirements: 5.1, 5.2, 6.2_

  - [ ]* 3.3 Extend the catalog content example tests
    - `edge-cv-portal/backend/layers/workflow_core/tests/test_catalog_content.py`: assert on
      `llm_inference` — `anomaly_mode` present, type `bool`, `required=False`, `default=False`,
      description covering both modes; `prompt_template` description mentions the appended JSON
      instruction, the as-is freeform send, and the `reference` comparison sentence; `examples`
      contains a comparison prompt consistent with `BEDROCK_DEFAULT_PROMPT`; `inputs` are exactly
      `in` then `reference`, both VideoFrames, matching `bedrock_inference`'s port shape
    - _Requirements: 1.1, 1.7, 2.1, 4.1_

  - [ ]* 3.4 Write property test for binding parameter pass-through
    - **Feature: vlm-bedrock-parity, Property 1: Binding parameter pass-through**
    - **Validates: Requirements 1.3**
    - New `edge-cv-portal/backend/layers/workflow_core/tests/test_property_llm_binding_parameters.py`:
      generate definitions with LLM_Inference_Nodes carrying arbitrary parameter maps (including
      `anomaly_mode` true/false/absent) using the existing `generators.py` helpers; compile for a
      vLLM-capable architecture; assert one `llm_inference` binding per node whose `parameters`
      contain every set value with booleans preserved exactly, and no `anomaly_mode` key when the
      node set none
    - _Requirements: 1.3_

  - [ ]* 3.5 Write property test for Frame_Capture_Plan correctness
    - **Feature: vlm-bedrock-parity, Property 2: Frame_Capture_Plan correctness**
    - **Validates: Requirements 2.4, 2.5, 2.6**
    - New `edge-cv-portal/backend/layers/workflow_core/tests/test_property_reference_capture_plan.py`
      (reusing the `test_compiler_bedrock.py` generators): for definitions mixing
      `llm_inference`/`bedrock_inference` nodes and shared feeders, assert each descriptor input
      port gets a `{work_dir}`-rooted JPEG `capturePaths` entry iff transitively fed by a
      GStreamer video source and `None` otherwise; count `multifilesink` elements and assert one
      per distinct feeder; assert every consuming binding's entry for a shared feeder is the
      identical path string; assert `llm_inference` feeders keep their downstream fan-out branch
    - _Requirements: 2.4, 2.5, 2.6_

  - [ ]* 3.6 Write property test for reference optionality and validator parity
    - **Feature: vlm-bedrock-parity, Property 3: Reference port optionality and validator parity**
    - **Validates: Requirements 2.2, 2.3, 6.2**
    - New `edge-cv-portal/backend/layers/workflow_core/tests/test_property_reference_validator_parity.py`:
      for generated definitions containing an LLM_Inference_Node, assert no finding is
      attributable to an unconnected `reference` port, no port-compatibility finding when a
      VideoFrames producer feeds it, and that the finding set equals (modulo node-type
      identifiers) the set produced by substituting a `bedrock_inference` node
    - _Requirements: 2.2, 2.3, 6.2_

- [x] 4. Checkpoint — workflow_core suite and catalog sync
  - `cd edge-cv-portal/backend && python3 -m pytest layers/workflow_core/tests/ -q`
  - Confirm the regenerated `catalog_baseline.json` diff is limited to the `prompt_template`
    change and both `workflow_core` copies are byte-identical (`diff -r`, `__pycache__` excluded)
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. G4 — dual-copy byte-equality guard and documented re-vendor mechanism
  - [ ]* 5.1 Write property test for dual-copy byte equality (tree-wide)
    - **Feature: vlm-bedrock-parity, Property 11: Dual-copy byte equality**
    - **Validates: Requirements 5.1, 5.2**
    - Extend `test/backend-test/workflow_engine/test_vendored_catalog_mirror.py` beyond its
      current two-file (`catalog/nodes.py`, `catalog/models.py`) scope: walk every
      `workflow_core/**/*.py` under the portal layer copy (`__pycache__` excluded), SHA-256 the
      bytes, and assert equality with the vendored counterpart — failing with the offending
      relative paths and a missing-file list; the Node_Type_Catalog and the
      Workflow_Compiler are covered by construction, and future shared modules need no test edit
    - _Requirements: 5.1, 5.2_

  - [x] 5.2 Document the re-vendor mechanism
    - `src/backend/workflow_engine/vendor/README.md`: state that the portal copy is the single
      source of truth, that the device copy is produced only by `re_vendor.sh` (rsync mirror,
      `__pycache__`/`*.pyc` excluded) and is never hand-edited, and that the Property 11 guard in
      `test_vendored_catalog_mirror.py` fails the suite on drift; include the catalog-baseline
      regeneration step for descriptor edits
    - _Requirements: 5.1, 5.2_

- [x] 6. G3 — port-generic node-image surfacing
  - [x] 6.1 Add `run_artifacts.list_node_images`
    - `src/backend/workflow_engine/run_artifacts.py`: `list_node_images(output_dir, capture_id)`
      parsing `{capture_id}.node.{nodeId}.{port}.jpg` into `[{"nodeId": ..., "port": ...}]` plus a
      resolver returning the on-disk path for a `(nodeId, port)` pair; sorted deterministically
      (node id, then port with `in` before `reference`); no node-type or port-name allow-list;
      best-effort and contained (missing dir / unreadable listing / `None` inputs → `[]`, never
      raises), matching the module's existing helper style
    - _Requirements: 4.3_

  - [x] 6.2 Add additive node entries to the run-results payload
    - `src/backend/workflow_engine/api.py`, `get_workflow_execution_results`: append
      `{"kind": "node", "nodeId": ..., "port": ..., "hasOverlay": false}` entries after the
      existing `output` entry, from `list_node_images`; keep `hasImageResults`/`captureId`
      meanings
    - Fix the latent inconsistency the audit found: emit the `{"kind": "output", ...}` entry only
      when the base output artifact actually exists, so a node-image-only run no longer reports an
      `output` entry with no file behind it; `hasImageResults` stays true when either kind exists
    - _Requirements: 4.3_

  - [x] 6.3 Add the `node-image` serving route
    - `src/backend/endpoints/download_file.py`:
      `GET /workflows/executions/{execution_id}/node-image?nodeId=&port=&token=` on the
      `unauthenticated_router` with `validate_token_in_query_param`, mirroring the shipped
      `output-image` route; serve the JPEG via `FileResponse` only when `(nodeId, port)` appears in
      `run_artifacts.list_node_images` for that execution — 404 otherwise (which rejects traversal
      shapes and fabricated names by construction), 404 for an unknown execution
    - _Requirements: 4.3_

  - [ ]* 6.4 Write property test for node-image round trip and enumeration
    - **Feature: vlm-bedrock-parity, Property 10: Node image round trip and enumeration**
    - **Validates: Requirements 4.3**
    - New `test/backend-test/workflow_engine/test_property_node_image_surfacing.py` (standalone
      FastAPI app + in-memory DB pattern from `test_workflow_run_results_api.py`): generate
      `(nodeId, port)` sets — node types and port names varied, traversal-shaped and fabricated
      inputs included — write files into a tmp artifact dir; assert the listing reports each pair
      exactly once and nothing else, the route returns byte-identical content for every reported
      pair and 404 for every unreported one, `/results` node entries match the listing, and the
      existing `output`/`hasImageResults`/`captureId` fields keep their shape
    - _Requirements: 4.3_

  - [x] 6.5 Render node sections in the device run-results view
    - `src/frontend/src/api/WorkflowRegistrationAPI.ts`: extend
      `WorkflowExecutionResultImage` additively (`kind: "output" | "input" | "node"`, optional
      `nodeId`/`port`) and add `workflowExecutionNodeImageUrl(executionId, nodeId, port, token?)`
    - `src/frontend/src/components/deployed-workflow/results/RunResults.tsx`: leave the existing
      output-image container untouched; add one section per inference node that has images,
      rendering its 1–2 frames side by side labeled "Input"/"Reference", the run's verdict badge
      when run metadata carries `is_anomalous`, and the node's returned text below
      (`llm.{nodeId}.generated_text` or `bedrock.{nodeId}.text`); graceful empty/partial states;
      `llm_inference` and `bedrock_inference` render through the same code path
    - _Requirements: 4.3, 4.2_

  - [ ]* 6.6 Write unit tests for the run-results node sections
    - `src/frontend/src/components/deployed-workflow/results/RunResults.test.tsx`: two node images
      render side by side labeled Input/Reference with the verdict badge in Anomaly_Mode; a single
      `in` image renders alone in Freeform_Mode with text only; the existing output-image
      container and overlay toggle behavior are unchanged; empty/partial listings and a 404 node
      image degrade gracefully
    - _Requirements: 4.3_

- [x] 7. Checkpoint — run-results API and device frontend
  - `PYTHONPATH=src/backend:test/backend-test:test/backend-test/workflow_engine python3 -m pytest
    test/backend-test/workflow_engine/test_workflow_run_results_api.py
    test/backend-test/workflow_engine/test_node_frame_persistence.py
    test/backend-test/workflow_engine/test_property_node_image_surfacing.py -q`
  - From `src/frontend`: `CI=true npm test -- --watchAll=false --testPathPattern=deployed-workflow`
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Guarantee suites for the shipped request/runtime path
  - [ ]* 8.1 Write property test for generate request body additivity
    - **Feature: vlm-bedrock-parity, Property 6: Generate request body additivity**
    - **Validates: Requirements 3.4**
    - New `test/backend-test/workflow_engine/test_property_llm_request_body_additivity.py`:
      `_default_llm_invoker` with a stubbed `requests.post` capturing the body; generate model
      names, prompts, generation parameters and optional image/reference base64 payloads; assert
      `image`/`reference_image` present exactly when supplied and equal to the supplied value, and
      that any reference-less body is byte-identical (`json` dump equality) to the pre-feature
      body for the same inputs
    - _Requirements: 3.4_

  - [ ]* 8.2 Write property test for `reference_image` validation exactness
    - **Feature: vlm-bedrock-parity, Property 7: `reference_image` validation exactness**
    - **Validates: Requirements 3.5**
    - New `test/backend-test/text_generation/test_property_reference_image_validation.py`:
      Hypothesis directly over `normalize_generation_request` (pure) with candidate values
      (non-strings, invalid base64, empty decode, oversized decode, valid payloads); assert a
      value is accepted/rejected as `reference_image` exactly as it is as `image`, a
      `reference_image` without a valid `image` is rejected, every finding for that field names
      `reference_image`, and the endpoint flow (shipped dependency-override fake runtime) invokes
      no runtime when any finding exists
    - _Requirements: 3.5_

  - [ ]* 8.3 Write property test for multimodal prompt construction and ordering
    - **Feature: vlm-bedrock-parity, Property 8: Multimodal prompt construction and ordering**
    - **Validates: Requirements 3.6, 3.7**
    - New `test/backend-test/vllm_runtime/test_property_multimodal_prompt_ordering.py`: fake engine
      capturing the prompt argument, multimodal-detection stubs, in-memory PIL images; assert the
      four cases — bare string with no image; pre-feature single-image dict; two-image dict whose
      `multi_modal_data["image"]` is `[input, reference]` in order with the input content block
      before the reference block; bare string plus a logged warning for a non-multimodal model —
      and assert image-placeholder count equals image count on both the chat-template and the
      Qwen-VL literal-fallback paths
    - _Requirements: 3.6, 3.7_

  - [ ]* 8.4 Write preservation property test for the API and runtime without image fields
    - **Feature: vlm-bedrock-parity, Property 13: Preservation — API and runtime without image fields**
    - **Validates: Requirements 3.8, 6.5**
    - New `test/backend-test/text_generation/test_property_reference_image_preservation.py`,
      observation-first: for generated bodies carrying neither `image` nor `reference_image`,
      assert the normalization dict equals the recorded pre-feature normalization for the same
      body and the runtime invocation carries no image-related keyword arguments; for bodies
      carrying `image` but no `reference_image`, assert normalization and runtime kwargs are
      identical to pre-feature; assertions must pass before and after any change to this path
    - _Requirements: 3.8, 6.5_

  - [ ]* 8.5 Write the Triton generate-route schema parity test
    - `test/backend-test/vllm_runtime/test_vllm_reference_image_units.py`: assert
      `src/backend/vllm_runtime/server.py`'s `GenerateRequest` accepts an optional
      `reference_image` with the same optionality/typing as `image`, and that omitting it produces
      the pre-feature request object
    - _Requirements: 3.4, 3.8_

- [ ] 9. Preservation suites (Requirement 6) and designer regression
  - [ ]* 9.1 Write preservation property test for pre-feature bindings and Freeform_Mode
    - **Feature: vlm-bedrock-parity, Property 12: Preservation — processor behavior for pre-feature bindings and Freeform_Mode**
    - **Validates: Requirements 1.6, 6.1**
    - New `test/backend-test/workflow_engine/test_property_llm_prefeature_preservation.py`,
      observation-first: for generated bindings with `anomaly_mode` absent/`None`/false and no
      `reference` capture path, compare the recorded invocation tuples (prompt string and
      argument arity) and the returned record against the pre-feature observation — prompt
      unmodified, pre-feature arity, record exactly `{"generated_text": <answer>}`, no verdict
      keys merged
    - _Requirements: 1.6, 6.1_

  - [ ]* 9.2 Write preservation property test for the Bedrock executor
    - **Feature: vlm-bedrock-parity, Property 14: Preservation — Bedrock executor behavior**
    - **Validates: Requirements 6.4**
    - New `test/backend-test/workflow_engine/test_property_bedrock_executor_preservation.py`,
      observation-first: for generated `bedrock_inference` bindings and answer texts in both
      modes, assert the returned metadata, the raise behavior for an unreadable `in` frame and an
      unparseable answer, and the degrade-to-single-image behavior for an unreadable `reference`
      frame all equal pre-feature behavior — pinning that G1 changed only the llm node
    - _Requirements: 6.4_

  - [ ]* 9.3 Write preservation property test for compilation non-interference
    - **Feature: vlm-bedrock-parity, Property 15: Preservation — compilation non-interference**
    - **Validates: Requirements 6.3, 6.6**
    - New `edge-cv-portal/backend/layers/workflow_core/tests/test_property_llm_compilation_preservation.py`,
      observation-first: for definitions containing no LLM_Inference_Node, compare compiled
      per-architecture documents against baselines captured from the pre-change compiler
      (`bedrock_inference` capture plans included); for definitions containing
      LLM_Inference_Nodes, assert the simulation document binds them to `sim_llm_inference` with
      no `capturePaths` key and no synthetic capture chain anywhere in the document
    - _Requirements: 6.3, 6.6_

  - [ ]* 9.4 Add the designer rendering regression test
    - `edge-cv-portal/frontend/src/pages/workflows/BuilderNodeComponent.test.tsx` (or the node
      config-panel suite it drives): assert the `llm_inference` config panel renders
      `anomaly_mode` as a checkbox through the generic bool-parameter path, unchecked by default,
      and that the node renders both `in` and `reference` input handles
    - _Requirements: 1.2, 2.1_

- [ ] 10. Optional on-hardware integration coverage (JP6/JP7 harness)
  - [ ]* 10.1 Add a two-image generate integration example
    - `test/on-hardware/harness/stages/`: post a `generate` request carrying `image` +
      `reference_image` to a loaded multimodal model and assert a non-empty text response with
      `image_used` true
    - _Requirements: 3.5, 3.6_

  - [ ]* 10.2 Add an Anomaly_Mode llm workflow integration example
    - `test/on-hardware/harness/stages/test_30_workflows.py`: deploy/run a workflow whose
      `llm_inference` node has `anomaly_mode` checked and a fed `reference` port; assert the run
      metadata carries flat `is_anomalous`/`confidence`, that the verdict gates the configured
      output, and that both node images are listed by `/results`
    - _Requirements: 4.2, 4.3_

- [x] 11. Final checkpoint
  - `cd edge-cv-portal/backend && python3 -m pytest layers/workflow_core/tests/ -q`
  - `PYTHONPATH=src/backend:test/backend-test:test/backend-test/workflow_engine python3 -m pytest
    test/backend-test/workflow_engine/ -q`
  - `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/text_generation/
    test/backend-test/vllm_runtime/ test/backend-test/vllm_runtime_tests/ -q`
  - Frontend: from `src/frontend` `CI=true npm test -- --watchAll=false --testPathPattern=deployed-workflow`;
    from `edge-cv-portal/frontend` `npx vitest run` for the touched designer suites
  - `diff -r edge-cv-portal/backend/layers/workflow_core/python/workflow_core
    src/backend/workflow_engine/vendor/workflow_core -x '__pycache__'` — no differences; the
    Property 11 guard green
  - Run each backend family standalone per `.kiro/steering/builds.md` (known moto leakage makes
    full-sweep results unreliable); known pre-existing failures per steering apply
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked `*` are optional test tasks and can be skipped for a faster MVP — except that
  task 1.1 is deliberately **not** optional: it is the contract change for Requirement 3.2 and
  must be retargeted before 1.2 lands, so the supersession is recorded in the test tree rather
  than only in the spec.
- Property coverage (all 15): P1→3.4, P2→3.5, P3→3.6, P4→1.4, P5→1.3, P6→8.1, P7→8.2, P8→8.3,
  P9→1.5, P10→6.4, P11→5.1, P12→9.1, P13→8.4, P14→9.2, P15→9.3.
- Ship vehicle: G1, G3 and the vendored catalog copy ride the next LocalServer build; G2's portal
  copy needs a portal compute-stack deploy for the designer to show the new guidance, and
  workflows must be repackaged to gain `reference` capture paths (pre-feature packages stay valid
  per Requirement 6.1).
- No new compiler code: Requirement 2's capture-plan behavior is already generic over
  `descriptor.inputs`, so tasks 3.4–3.6 are proof tasks. If a property falsifies the claim, fix
  the portal copy and re-run `re_vendor.sh` — never hand-edit the vendored tree.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "3.1", "6.1", "8.1", "8.2", "8.3", "8.4", "8.5", "9.1", "9.2", "9.3"] },
    { "id": 1, "tasks": ["1.2", "3.2", "3.3", "6.2", "6.3", "9.4"] },
    { "id": 2, "tasks": ["1.3", "1.4", "1.5", "3.4", "3.5", "3.6", "5.1", "5.2", "6.4", "6.5"] },
    { "id": 3, "tasks": ["6.6", "10.1", "10.2"] }
  ]
}
```
