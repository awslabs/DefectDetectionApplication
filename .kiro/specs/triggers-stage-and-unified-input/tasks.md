# Implementation Plan

## Overview

This plan implements **Sub-feature A** (portal + designer only, no device runtime) of the
`workflow-triggers-and-input-overhaul` assessment. It follows the design's C1–C6 components
and validates the P1–P7 correctness properties. The work is sequenced so the two Python
catalog copies stay byte-in-sync at every step:

1. **C1 — category constant** in `catalog/models.py` (`CATEGORY_TRIGGER`, `CATEGORIES`
   trigger-first), mirrored to the vendor copy.
2. **C2 + C4 — `digital_input` relocation** (one-field `category` change) plus the
   validator adjustments (V1 presence + V5 root set widened to
   `CATEGORY_INPUT ∪ CATEGORY_TRIGGER`, new `V7_STAGE_ORDER` check).
3. **C3 — `Unified_Input_Node`** descriptor + `SOURCE_KIND_TO_SOURCE_TYPE` map, parameters
   built by reusing the four retained source descriptors.
4. **C5 — compiler `expand_unified_inputs`** pure pre-pass that rewrites unified nodes into
   their underlying source node and drops activation edges.
5. **C6 — frontend designer** mirror (`types.ts`), palette section ordering, `source_kind`
   config gating, activation-port wiring, and `inlineChecks` parity.
6. **Property tests P1–P7** using the existing tooling (Hypothesis for `workflow_core`,
   fast-check for the frontend), reusing existing generators, ≥100 iterations each.
7. **Checkpoint** running the `workflow_core` suites, the portal backend suite, the device
   vendored-mirror test, and the frontend suite; then a **portal deploy** (portal-side; no
   device / LocalServer build is required for Sub-feature A).

Behavior preservation is the guiding constraint: any workflow that does not use the new
scaffolding must compile, package, validate, and render exactly as it does today.

This spec does **not** modify the parent assessment folder
(`.kiro/specs/workflow-triggers-and-input-overhaul/`).

## Tasks

- [x] 1. Add the `CATEGORY_TRIGGER` category constant (C1)
  - [x] 1.1 Add `CATEGORY_TRIGGER` to `catalog/models.py` and widen `CATEGORIES` (trigger first)
    - In `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/models.py`,
      add `CATEGORY_TRIGGER = "trigger"` and rebuild `CATEGORIES` as
      `(CATEGORY_TRIGGER, CATEGORY_INPUT, CATEGORY_PREPROCESSING, CATEGORY_INFERENCE,
      CATEGORY_POST_PROCESSING, CATEGORY_OUTPUT)` so consumers that iterate `CATEGORIES`
      present Triggers ahead of Inputs
    - Export `CATEGORY_TRIGGER` from `catalog/__init__.py` (`__all__`) alongside the existing
      category constants; make no `NodeTypeDescriptor` schema change
    - Copy the edited `models.py` verbatim to the vendor copy
      `src/backend/workflow_engine/vendor/workflow_core/catalog/models.py` so the two Python
      copies remain byte-identical
    - _Requirements: 1.1, 1.2, 1.3, 6.5_
    - _Property: P7_

  - [x]* 1.2 Write example unit tests for the category constant
    - Assert `CATEGORY_TRIGGER == "trigger"`, that it is a member of `CATEGORIES`, and that it
      appears **before** `CATEGORY_INPUT` in the tuple (add to a new
      `test_catalog_trigger_category.py` to avoid colliding with existing catalog test files)
    - _Requirements: 1.1, 1.2_

- [x] 2. Relocate `digital_input` to Triggers and extend the validator (C2, C4)
  - [x] 2.1 Relocate `digital_input` to `CATEGORY_TRIGGER` (metadata-only)
    - In `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`,
      change only the `DIGITAL_INPUT` descriptor's `category` from `CATEGORY_INPUT` to
      `CATEGORY_TRIGGER`; leave `pin`, `trigger_edge`, `poll_interval_ms`, the single
      `PORT_TYPE_EVENT_SIGNAL` `out` port, the `executor_binding="digital_input"` device
      mappings, and the `ARCH_SIM` appsrc simulation stub untouched
    - Copy the edited `nodes.py` verbatim to
      `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py` to keep the copies
      byte-identical
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 6.5_
    - _Property: P2, P7_

  - [x] 2.2 Widen V1/V5 root set and add the `V7_STAGE_ORDER` check
    - In `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/validator/checks.py`,
      widen the V1 presence test from `CATEGORY_INPUT in categories` to
      `categories & {CATEGORY_INPUT, CATEGORY_TRIGGER}`, and widen the V5 reachability roots
      from `category == CATEGORY_INPUT` to `category in (CATEGORY_INPUT, CATEGORY_TRIGGER)`
    - Add `CODE_V7_STAGE_ORDER = "V7_STAGE_ORDER"` and `_check_v7(graph, typed_nodes)`: for
      every connection whose **target** resolves to a `CATEGORY_TRIGGER` node, emit a
      `SEVERITY_ERROR` finding naming the offending connection (target-category check, so a
      legal `trigger → unified activation port` connection — whose target is the
      `CATEGORY_INPUT` unified node — passes automatically)
    - Add exactly one line to `validate()` — `findings.extend(_check_v7(graph, typed_nodes))`
      after `_check_w1` — preserving the "run every check, never short-circuit, return the
      full list" contract
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 2.7_
    - _Property: P5, P6_

  - [x] 2.3 Write example unit tests for relocation and validator changes
    - Assert the relocated `digital_input` descriptor's `category == CATEGORY_TRIGGER` and that
      its parameters, output port, executor binding, and sim stub are unchanged (2.2–2.4)
    - Assert `_check_v7` emits a `V7_STAGE_ORDER` error for a connection targeting a trigger
      node and no `V7` finding for a legal `trigger → unified activation` edge (4.2–4.4); assert
      a `digital_input`-only graph still satisfies V1 and keeps its downstream reachable under
      V5 (2.7, 4.5)
    - _Requirements: 2.2, 2.3, 2.4, 4.2, 4.3, 4.4, 4.5, 4.7, 2.7_

- [x] 3. Add the `Unified_Input_Node` descriptor and source-kind map (C3)
  - [x] 3.1 Add `SOURCE_KIND_TO_SOURCE_TYPE` and the `UNIFIED_INPUT` descriptor
    - In `catalog/nodes.py`, add `SOURCE_KIND_TO_SOURCE_TYPE = {"csi_camera":
      "csi_camera_source", "icam": "icam_source", "aravis_camera": "aravis_camera_source",
      "folder": "folder_source"}` (excludes `digital_input`, satisfying 3.3)
    - Add `_unified_source_parameters()` that unions the four source descriptors' parameters
      by reusing the live descriptor objects via `dataclasses.replace(p, required=False)`
      (de-duplicated by name; only the identical `gain`/`exposure` collide), so names/types/
      defaults/constraints cannot drift from the originals (3.4)
    - Add `UNIFIED_INPUT = NodeTypeDescriptor(type_id="unified_input",
      category=CATEGORY_INPUT, ...)` with a required `source_kind` enum
      (`constraints={"values": list(SOURCE_KIND_TO_SOURCE_TYPE)}`, default `"folder"`)
      prepended to the union parameters, one optional `PORT_TYPE_EVENT_SIGNAL` `activation`
      input port, one `PORT_TYPE_VIDEO_FRAMES` `out` output port, and an
      empty-but-present `mappings` placeholder (expansion is the sole compile path, C5)
    - Append `UNIFIED_INPUT` to `NODE_CATALOG` after the existing entries (mirroring the
      `LLM_INFERENCE` append convention so no pre-existing descriptor shifts); leave the four
      source descriptors and the `mqtt_publish`/`opcua_write` descriptors unchanged
    - Copy the edited `nodes.py` verbatim to the vendor copy to keep the copies byte-identical
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.8, 3.11, 6.4, 6.5_
    - _Property: P3, P7_

  - [x]* 3.2 Write example unit tests for the unified descriptor
    - Assert `UNIFIED_INPUT.category == CATEGORY_INPUT`; `source_kind` enum values are exactly
      `csi_camera, icam, aravis_camera, folder` with no digital option (3.1, 3.3); exactly one
      `PORT_TYPE_VIDEO_FRAMES` output (3.5) and one optional `PORT_TYPE_EVENT_SIGNAL`
      `activation` input (3.8)
    - Assert, per `source_kind`, that the unified node's gated parameter subset matches the
      underlying source descriptor on name/type/default/constraints (3.4); assert the four
      source descriptors and `mqtt_publish`/`opcua_write` descriptors are unchanged (3.2, 6.4)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.8, 6.4_

- [x] 4. Add the compiler `expand_unified_inputs` pre-pass (C5)
  - [x] 4.1 Implement `expand_unified_inputs` and wire it into `compile()`
    - In `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/compiler/compiler.py`,
      add `expand_unified_inputs(graph, catalog)` returning a **new** `WorkflowGraph` (never
      mutating the input): each `unified_input` node becomes a synthetic node with the **same
      `id`** and `position`, `type = SOURCE_KIND_TO_SOURCE_TYPE[source_kind]`, carrying only
      the parameters whose names appear on the underlying source descriptor (dropping
      `source_kind` and non-applicable union parameters)
    - Drop every connection whose `target` is a unified node's `activation` port; pass all
      other connections and all non-unified nodes through untouched
    - Invoke `expand_unified_inputs(graph, catalog)` at `compile()`'s entry, before the
      validation re-run and mapping resolution, so the `unified_input` `type_id` never reaches
      `mapping_for` and the expanded source flows through the existing resolution path
    - Vendor copy note: the compiler is not part of the byte-mirrored `catalog/` package, but
      if the device-vendored `vendor/workflow_core/compiler/compiler.py` exists, apply the
      same edit so device-side expansion matches the portal
    - _Requirements: 3.6, 3.7, 3.9, 3.10, 3.11, 2.6_
    - _Property: P3, P4_

  - [x] 4.2 Write example unit tests for expansion
    - Assert a `unified_input` node with `source_kind=X` compiles to the same
      `segments`/`executorBindings`/`pluginDependencies` as a hand-placed source-`X` node with
      the same id and equivalent params (3.6); assert an unconnected activation port emits no
      activation binding (3.9, 3.11); assert a connected activation edge is dropped, the
      compiled output is identical to the unconnected case, and the feeding `digital_input`
      still emits its ordinary executor binding (3.10, 3.11)
    - Assert that where the underlying source descriptor defines no Device_Binding for an
      architecture, the unified node with that `source_kind` is unsupported on that
      architecture in the same way (3.7)
    - Assert `folder` selected with blank `location` is deferred to the compile-time validation
      re-run (`V4_MISSING_REQUIRED_PARAMETER` on the expanded `folder_source`), not raised on
      the unified graph
    - _Requirements: 3.6, 3.7, 3.9, 3.10, 3.11_

- [x] 5. Frontend designer support (C6)
  - [x] 5.1 Mirror the catalog constants in `types.ts`
    - In `edge-cv-portal/frontend/src/pages/workflows/types.ts`, add
      `export const CATEGORY_TRIGGER = 'trigger';` and place it **first** in the `CATEGORIES`
      array (`NodeCategory` derives from it); add a mirrored `SOURCE_KIND_TO_SOURCE_TYPE`
      constant matching the Python map for gating
    - _Requirements: 1.4_

  - [x] 5.2 Add the Triggers palette section and preserve saved-graph rendering
    - In `builderGraph.ts` `CATEGORY_META`, add a `trigger: { label: 'Triggers', color: … }`
      entry so the Triggers section has a label/color (avoids the `UNKNOWN_CATEGORY_META`
      fallback); rely on `NodePalette.tsx` already mapping `CATEGORIES` in order so `trigger`
      first renders the Triggers section before Inputs (no `NodePalette` logic change)
    - Confirm `fromWorkflowDefinition`/`toWorkflowDefinition` still rebuild nodes purely from
      `node.type` + served descriptor so a saved `digital_input` renders under Triggers with no
      mutation of the stored definition (no migration)
    - _Requirements: 1.5, 1.6, 2.8, 5.1, 5.2, 5.3, 2.6_

  - [x] 5.3 Gate `source_kind` parameters in `NodeConfigPanel.tsx`
    - For a `unified_input` node, compute visible parameters as `source_kind` plus the parameter
      names of `SOURCE_KIND_TO_SOURCE_TYPE[source_kind]`'s served descriptor; reuse the existing
      per-field rendering; keep `source_kind` always visible
    - _Requirements: 5.4_

  - [x] 5.4 Render and wire the activation port in `BuilderNodeComponent.tsx`
    - Render the optional `activation` `EventSignal` input port on the unified node and allow a
      `CATEGORY_TRIGGER` output → activation-port edge; verify the existing
      `connectionRejectionReason`/`incompatibilityReason` path already accepts
      EventSignal↔EventSignal (no rule change needed) and rejects non-EventSignal outputs
      wired to the activation port, leaving the graph unchanged
    - _Requirements: 5.5, 5.6_

  - [x] 5.5 Widen `inlineChecks.ts` roots and add the stage-order mirror
    - Widen `checkV5` roots to `CATEGORY_INPUT ∪ CATEGORY_TRIGGER` and add a target-is-trigger
      stage-order mirror so inline markers match the backend validator findings for the same
      graph, including stage-ordering findings; an unconnected activation port produces no
      inline finding
    - _Requirements: 5.7, 5.8_

  - [x] 5.6 Write frontend example/interaction tests
    - Assert NodePalette renders Triggers before Inputs with the existing section order
      preserved (1.5, 1.6, 5.1) and lists `digital_input` under Triggers only, not Inputs
      (2.8); a saved `digital_input` renders under Triggers and
      `fromWorkflowDefinition → toWorkflowDefinition` preserves the stored node byte-identically
      on an edit-free save (2.6, 5.2, 5.3); `NodeConfigPanel` shows only the selected
      `source_kind`'s parameters (5.4); `connectionRejectionReason(trigger.out →
      unified.activation)` is `null` (5.5); a non-`EventSignal` output wired to the activation
      port is rejected and the graph is unchanged (5.6); an unconnected activation port shows
      no inline finding (5.8); and `types.ts` mirror defines `CATEGORY_TRIGGER` in
      `CATEGORIES` (1.4)
    - _Requirements: 1.4, 1.5, 1.6, 2.8, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.8_

- [x] 6. Checkpoint - Ensure catalog, validator, compiler, and designer are consistent
  - Ensure all tests pass, ask the user if questions arise.
  - Covered here: the catalog baseline (`catalog_baseline.json`, verified by
    `test_catalog_content.py`) was updated with a diff scoped to exactly the
    `CATEGORY_TRIGGER` addition, the `digital_input` category value, and the
    `unified_input` descriptor addition, all other descriptor entries byte-identical (6.8)

- [x] 7. Property-based tests for P1–P7
  - Use the existing tooling — **Hypothesis** for the `workflow_core` Python suite and
    **fast-check** for the frontend `*.property.test.ts` modules. Reuse the existing graph/
    parameter generators (`layers/workflow_core/tests/generators.py` and the frontend property
    suites), extended with a `unified_input`/`digital_input`/trigger-edge generator. Each test
    runs **≥100 iterations** and carries the tag comment
    `Feature: triggers-stage-and-unified-input, Property {n}: {property text}`.

  - [x] 7.1 P1 — zero-trigger workflows compile and package byte-identically
    - Generate Zero_Trigger_Workflows over the pre-existing node types; for each architecture
      assert `compile(...).to_dict()` bytes and the packaged artifact bytes equal a captured
      pre-feature baseline (reuse `tests/catalog_baseline.json`-style golden capture); cover
      legacy workflows referencing the existing source nodes so their compiled/packaged
      artifacts stay byte-identical too (6.7)
    - **Property 1: Zero-trigger workflows compile and package byte-identically**
    - _Requirements: 6.1, 6.2, 6.3, 6.7_

  - [x] 7.2 P2 — `digital_input` relocation is binding-preserving
    - Generate valid `digital_input` parameter combos (`pin`, `trigger_edge`,
      `poll_interval_ms`) × architectures; assert the emitted executor binding + parameters +
      sim stub equal the pre-relocation baseline (category has no compiler effect)
    - **Property 2: digital_input relocation is binding-preserving**
    - _Requirements: 2.5, 6.2_

  - [x] 7.3 P3 — unified node compiles to its underlying source binding
    - Generate `source_kind` × valid params × architecture; compile a `unified_input` node and
      an underlying source node with the same id/params; assert equal
      `segments`/`executorBindings`/`pluginDependencies`, and assert per-`source_kind` param
      equivalence (name/type/default/constraints) against the underlying descriptor; where the
      underlying source has no Device_Binding for an architecture, assert the unified node is
      unsupported on that architecture in the same way (3.7)
    - **Property 3: Unified node compiles to its underlying source binding**
    - _Requirements: 3.4, 3.6, 3.7_

  - [x] 7.4 P4 — the activation port is inert
    - Generate unified nodes with the activation port unconnected (3.9) and connected to a
      `digital_input` output (3.10); assert both compile to the same source output, that the
      connected case is identical to the unconnected case, and that no trigger-driven
      activation binding is emitted for the activation port regardless of connection
      state (3.11)
    - **Property 4: The activation port is inert**
    - _Requirements: 3.9, 3.10, 3.11_

  - [x] 7.5 P5 — validator enforces stage-ordering legality
    - Generate graphs with a connection targeting a trigger (expect a `V7_STAGE_ORDER` finding
      naming that connection, with severity error under the single stable code, 4.7) and
      graphs with legal `trigger → unified activation` edges (expect no `V7` finding)
    - **Property 5: Validator enforces stage-ordering legality**
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.7_

  - [x] 7.6 P6 — validator finding-set equivalence (backend + frontend parity)
    - Generate Zero_Trigger_Workflows and `digital_input` graphs (including legacy saved
      workflows referencing the existing source nodes, 6.6); assert the extended validator
      finding set equals the pre-feature baseline and that restricting to non-`V7` codes never
      drops or suppresses any V1–V6/W1 finding; mirror the parity in the frontend
      `inlineChecks` property test (5.7)
    - **Property 6: Validator finding-set equivalence for zero-trigger and digital_input graphs**
    - _Requirements: 2.7, 4.5, 4.6, 5.7, 6.6_

  - [x] 7.7 P7 — catalog copies stay byte-identical (extend both mirror tests)
    - Extend `TestCatalogMirrorEquality` in
      `edge-cv-portal/backend/layers/workflow_core/tests/test_catalog_content.py` and
      `test/backend-test/workflow_engine/test_vendored_catalog_mirror.py` so each byte/sha256
      compares **both** `catalog/nodes.py` and `catalog/models.py` (today they only cover
      `nodes.py`); on mismatch print the exact `cp` re-sync command
    - **Property 7: Catalog copies stay byte-identical**
    - _Requirements: 1.3, 6.5_

- [x] 8. Checkpoint - Run the full suite across portal, device-vendored mirror, and frontend
  - Run the portal catalog suite: `cd edge-cv-portal/backend && python3 -m pytest layers/workflow_core/tests/`
  - Run the portal backend suite: `cd edge-cv-portal/backend && python3 -m pytest tests/`
  - Run the device vendored-mirror test:
    `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/test_vendored_catalog_mirror.py`
  - Run the frontend tests via the frontend package test runner (the workflow + node-designer
    property/unit suites under `edge-cv-portal/frontend/src/pages/workflows/`)
  - Ignore these known pre-existing failures (per repo steering, unrelated to this feature):
    portal workflow test-runner `test_workflow_testing_errors` and `test_workflow_model_staging`;
    `test_property_setup_command_wellformed` collection-order failure;
    `test_property_aravis_type_compatibility`; and the `awsiot`/`panorama` collection errors
  - Ensure all other tests pass, ask the user if questions arise.

- [~] 9. Deploy the portal (portal-side; no device / LocalServer build) — REQUIRES USER COORDINATION
  - This change is portal + designer only; it goes live via a **portal deploy**. Sub-feature A
    introduces no device-runtime change, so **no** device / LocalServer gdk build is required
    and none should be run
  - Deploy the updated portal (backend `workflow_core` layer + frontend) using the repo's
    standard portal deploy path; run only with the user's explicit go-ahead
  - _Requirements: 1.5, 5.1, 5.2, 5.4, 5.5_

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP;
  core implementation tasks are never optional.
- **Byte-in-sync discipline**: every catalog edit (1.1 `models.py`, 2.1 and 3.1 `nodes.py`)
  ends by copying the portal source into
  `src/backend/workflow_engine/vendor/workflow_core/catalog/`, and 7.7 extends both mirror
  tests to cover `models.py` in addition to `nodes.py`.
- **Property → requirements traceability**: P1 (6.1–6.3, 6.7), P2 (2.5, 6.2), P3 (3.4, 3.6, 3.7),
  P4 (3.9, 3.10, 3.11), P5 (4.1–4.4, 4.7), P6 (2.7, 4.5, 4.6, 5.7, 6.6), P7 (1.3, 6.5).
- **Behavior preservation**: `category` is validator/presentation metadata only — `compile()`
  keys off `type_id` + `mappings`, so the `digital_input` relocation is a compile/runtime
  no-op, and the unified node compiles by expansion into an existing source descriptor.
- This spec does **not** touch the parent assessment folder
  `.kiro/specs/workflow-triggers-and-input-overhaul/`.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "2.2"] },
    { "id": 2, "tasks": ["3.1"] },
    { "id": 3, "tasks": ["4.1", "5.1"] },
    { "id": 4, "tasks": ["5.2", "5.3", "5.4", "5.5"] },
    { "id": 5, "tasks": ["1.2", "2.3", "3.2", "4.2", "5.6"] },
    { "id": 6, "tasks": ["7.1", "7.2", "7.3", "7.4", "7.5", "7.6", "7.7"] }
  ]
}
```
