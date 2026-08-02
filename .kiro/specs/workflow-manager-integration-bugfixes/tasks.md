# Implementation Plan

## Overview

This plan follows the exploratory bugfix methodology: write tests that fail on the **unfixed**
code to confirm each bug condition, capture the baseline behavior that must be preserved, then
apply the smallest fix for each of the five independent defects and re-run the same tests. The
five defects share no code paths and are fixed in isolation. Property numbering matches the
design's Correctness Properties (Properties 1–5 are the per-bug fix-checking properties;
Property 6 is preservation across all five).

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Write tests against UNFIXED code: task 1 (bug conditions) — subtasks 1.1/1.2/1.4/1.5 FAIL, 1.3 is an unexpected pass at the pure-logic layer; task 2 (preservation) PASSES. Independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["3", "4", "5", "6", "7"],
      "description": "Implement the five independent fixes. Each depends on wave 1 but the five are independent of each other (no shared code paths) and may proceed in any order or in parallel."
    },
    {
      "wave": 3,
      "tasks": ["8"],
      "description": "Checkpoint - full suite passes and vendored copies are in sync. Depends on wave 2."
    }
  ]
}
```

- Tasks 1 and 2 are independent and must be completed BEFORE any fix (tests written against unfixed code).
- Tasks 3–7 each depend on tasks 1 and 2. Within each fix task, the verify sub-tasks depend on the implement sub-tasks.
- Tasks 3–7 are mutually independent (the five defects share no code paths) and may be done in any order.
- Task 8 depends on tasks 3–7.

## Tasks

- [x] 1. Write bug condition exploration tests for all five defects (BEFORE any fix)
  - **CRITICAL**: These tests MUST run on UNFIXED code. Tests for Bugs 1, 2, 4, and 5 MUST FAIL
    (failure confirms the bug exists). The Bug 3 pure-logic test is EXPECTED to be an
    **unexpected pass** (the pure connection helpers already support fan-out — see the design's
    investigation note); an unexpected pass here refutes the pure-logic hypothesis and redirects
    the fix to the React Flow interaction / `<Handle>` layer.
  - **DO NOT attempt to fix the test or the code when a test fails** — failures/counterexamples
    are the goal of this step.
  - **GOAL**: Surface counterexamples that demonstrate each defect and confirm or refute each
    root-cause hypothesis.

  - [x] 1.1 Bug 1 exploration — VLM/LLM inference input type
    - **Property 1: Bug Condition** - VLM/LLM inference takes video frames
    - Assert `LLM_INFERENCE` `in` port type is `PORT_TYPE_VIDEO_FRAMES` (backend catalog test)
    - Companion designer check: a `VideoFrames` source (e.g. `csi_camera_source.out`) is rejected
      into `llm_inference.in` today
    - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (`in` port is currently `InferenceMeta`)
    - Document counterexample: "`llm_inference.in` reports `InferenceMeta`; a `VideoFrames` source
      cannot connect"
    - _Requirements: 1.1, 2.1_

  - [x] 1.2 Bug 2 exploration — topic-only Greengrass MQTT config
    - **Property 2: Bug Condition** - MQTT publish through Greengrass with only a topic
    - Build an `mqtt_publish` node with only a `topic` plus the (not-yet-existing) `greengrass`
      option and run `validate` (backend)
    - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (`V4_MISSING_REQUIRED_PARAMETER` for
      `broker_host`; no `greengrass` parameter exists)
    - Document counterexample: "a topic-only config is rejected; no Greengrass parameter exists"
    - _Requirements: 1.2, 2.2_

  - [x] 1.3 Bug 3 exploration — model inference output fan-out
    - **Property 3: Bug Condition** - Model inference output fan-out
    - Pure-logic layer (frontend): create a `model_inference` node with `out` connected to one
      target, invoke the connection path (`connectionRejectionReason` / `onConnect` / `edgeIdFor`)
      for a second compatible target, and assert a second edge exists
    - Run on UNFIXED code — **EXPECTED OUTCOME**: **UNEXPECTED PASS** at the pure-logic layer (the
      helpers already create the second edge). Record this via the PBT status tool as
      `unexpected_pass` — it refutes the pure-logic hypothesis
    - Follow-up: add a component/interaction-level test that drives an actual second drag on the
      `<ReactFlow>` canvas from a model-inference output `<Handle>` to reproduce the real defect (if
      any); if fan-out works end to end, note that the fix reduces to regression coverage
    - Document findings (which layer, if any, blocks the second edge)
    - _Requirements: 1.3, 2.3_

  - [x] 1.4 Bug 4 exploration — node label
    - **Property 4: Bug Condition** - Node label reads "VLM/LLM Inference"
    - Assert `LLM_INFERENCE.display_name == "VLM/LLM Inference"` (backend catalog test)
    - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (label is currently "LLM Inference")
    - Document counterexample: "palette/canvas label reads 'LLM Inference'"
    - _Requirements: 1.4, 2.4_

  - [x] 1.5 Bug 5 exploration — workflow name display
    - **Property 5: Bug Condition** - Workflow name shown while editing
    - Render `WorkflowBuilder` with a loaded workflow and assert its name appears in the
      top-of-page header (frontend component test)
    - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (header is the static "Workflow Builder";
      the name only appears as small toolbar text)
    - Document counterexample: "the workflow name is absent from the top header"
    - _Requirements: 1.5, 2.5_

- [x] 2. Write preservation property tests capturing baseline behavior (BEFORE any fix)
  - **Property 6: Preservation** - Behavior unchanged outside every bug condition
  - **IMPORTANT**: Follow the observation-first methodology — run the UNFIXED code, record actual
    outputs for non-bug-condition inputs, then encode them as property-based tests
  - Property-based testing is used wherever the input domain is large (node descriptors, MQTT
    configs, connection attempts) for stronger regression guarantees
  - **EXPECTED OUTCOME**: All tests PASS on UNFIXED code (this establishes the baseline to preserve)

  - [x] 2.1 Catalog preservation (backend, property-based over all descriptors)
    - For every node type other than `llm_inference`: `display_name`, ports, parameters, and
      mappings are byte-identical to the observed baseline
    - For `llm_inference`: `type_id`, `out` port type (`InferenceMeta`), parameters (`modelName`,
      `prompt_template`, `max_tokens`, `temperature`, `top_p`), and architecture mappings
      (vLLM-capable archs plus the `sim` stub `sim_llm_inference`) are unchanged
    - Verify tests PASS on UNFIXED code
    - _Requirements: 3.1, 3.5_

  - [x] 2.2 MQTT plain-broker / `aws_iot` preservation (backend, property-based, `greengrass` off)
    - Observe and assert validation accept/reject, packaged dependencies (paho-mqtt), and the
      executor's `_run_mqtt_publish` publish-call arguments for plain-broker and `aws_iot` configs
    - Verify tests PASS on UNFIXED code
    - _Requirements: 3.2_

  - [x] 2.3 Connection-acceptance preservation (frontend, property-based)
    - Extend `connectionAcceptance.property.test.ts`: `connectionRejectionReason` accepts a
      connection iff source-output/target-input types are compatible, with a non-empty reason on
      every rejection; single-downstream connections and self/unknown-handle rejections are unchanged
    - Verify tests PASS on UNFIXED code
    - _Requirements: 3.3, 3.4_

  - [x] 2.4 Builder-action preservation (frontend)
    - Observe New / Open / Save / Validate / Duplicate / Delete / Package / Generate / Test flows
      and assert they behave identically (display-only changes must not affect them)
    - Verify tests PASS on UNFIXED code
    - _Requirements: 3.6_

- [x] 3. Fix Bug 1 — VLM/LLM inference input type

  - [x] 3.1 Change the `llm_inference` input port to `VideoFrames`
    - In `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`,
      change `LLM_INFERENCE.inputs` from `PortDescriptor("in", PORT_TYPE_INFERENCE_META)` to
      `PortDescriptor("in", PORT_TYPE_VIDEO_FRAMES)`
    - Leave `outputs`, `parameters`, `mappings`, and `hardware_dependent` untouched
    - Update the adjacent "consumes upstream inference metadata" comment to match the new frame
      input; do NOT alter the parameter set
    - _Bug_Condition: isBugCondition1(X) where X.type_id = "llm_inference" AND inputPortType(X,"in") = PORT_TYPE_INFERENCE_META_
    - _Expected_Behavior: inputPortType(d',"in") = PORT_TYPE_VIDEO_FRAMES AND outputPortType(d',"out") = PORT_TYPE_INFERENCE_META_
    - _Preservation: llm_inference out type, parameters, and architecture mappings unchanged_
    - _Requirements: 2.1, 3.1_

  - [x] 3.2 Re-vendor the device catalog copy
    - Run `vendor/re_vendor.sh` to regenerate
      `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py` so the LocalServer/compiler
      copy matches the source-of-truth catalog
    - _Requirements: 2.1, 3.1_

  - [x] 3.3 Verify Bug 1 exploration test now passes
    - **Property 1: Expected Behavior** - VLM/LLM inference takes video frames
    - **IMPORTANT**: Re-run the SAME test from task 1.1 — do NOT write a new test
    - Add/confirm the integration check: a `csi_camera_source → llm_inference → mqtt_publish` graph
      validates and compiles (frame input accepted end to end); an `inference_filter → llm_inference`
      edge still connects via the declared `InferenceMeta → VideoFrames` coercion
    - **EXPECTED OUTCOME**: Test PASSES (confirms the bug is fixed)
    - _Requirements: 2.1_

  - [x] 3.4 Verify catalog preservation still holds
    - **Property 6: Preservation** - Behavior unchanged outside every bug condition
    - Re-run the SAME preservation tests from task 2.1
    - **EXPECTED OUTCOME**: Tests PASS (no regressions to other descriptors or to `llm_inference`
      out type / parameters / mappings)
    - _Requirements: 3.1, 3.5_

- [x] 4. Fix Bug 2 — Greengrass MQTT publishing option

  - [x] 4.1 Add the additive, off-by-default Greengrass option to the catalog
    - In `catalog/nodes.py` `MQTT_PUBLISH`: add
      `ParameterDescriptor("greengrass", "bool", required=False, default=False, ...)` describing
      zero-config publishing via the device's Greengrass-managed MQTT (only the topic is needed)
    - Change `broker_host` from `required=True` to `required=False`; keep `topic` `required=True`
    - Add the Greengrass IPC runtime dependency (e.g. `python:awsiotsdk`) to the device mappings'
      `plugin_dependencies` alongside `python:paho-mqtt`; keep the `sim` recording binding unchanged
    - _Bug_Condition: isBugCondition2(X) where wantsGreengrassManagedPublish(X) AND NOT existsValidConfig(topicOnly(X))_
    - _Expected_Behavior: isValidMqttPublishConfig'(greengrassPublish(topic=X.topic)) AND NOT requires(cfg',"broker_host") AND NOT requires(cfg',"iot_ca_cert_path")_
    - _Preservation: plain-broker and aws_iot paths accept/validate/package/publish as before_
    - _Requirements: 2.2, 3.2_

  - [x] 4.2 Preserve broker-config rejection in the validator under the relaxed `required`
    - In `validator/checks.py`, add a small `mqtt_publish`-specific check (new stable code, e.g.
      `CODE_V*_MQTT_NO_TARGET`) reporting an error when a `mqtt_publish` node has neither
      `greengrass` enabled, nor `aws_iot` enabled, nor a non-empty `broker_host`
    - Mirror the rule in the frontend inline checks only if the existing mirror already covers
      `mqtt_publish`; otherwise leave inline checks unchanged and rely on full backend validation
    - _Preservation: host-less plain-broker config stays rejected (same accept/reject outcome)_
    - _Requirements: 2.2, 3.2_

  - [x] 4.3 Add the Greengrass branch to the edge executor
    - In `src/backend/workflow_engine/output_bindings.py` `_run_mqtt_publish`
      (`OutputBindingProcessor`), before reading `broker_host`, branch on
      `parameters.get("greengrass")`; render the payload as today and publish only
      `topic`/`payload`/`qos` through a new injectable `_greengrass_publisher`
    - Default publisher uses the Greengrass IPC `PublishToIoTCore` operation via a lazily-imported
      `awsiot.greengrasscoreipc` client (matching the existing paho/opcua lazy-import pattern)
    - Add the new publisher to `OutputBindingProcessor.__init__` injectable set so tests run without
      a Greengrass runtime; leave the non-`aws_iot`, `aws_iot`, `_default_mqtt_publisher`, and
      `AWS_IOT_*` handling exactly as-is
    - Note: `output_bindings.py` is the real engine (not vendored) and is edited directly
    - _Requirements: 2.2, 3.2_

  - [x] 4.4 Re-vendor the device catalog and validator copies
    - Run `vendor/re_vendor.sh` to regenerate the vendored `catalog/nodes.py` AND
      `validator/checks.py` under `src/backend/workflow_engine/vendor/workflow_core/`
    - _Requirements: 2.2, 3.2_

  - [x] 4.5 Verify Bug 2 exploration test now passes
    - **Property 2: Expected Behavior** - MQTT publish through Greengrass with only a topic
    - Re-run the SAME test from task 1.2 — do NOT write a new test
    - Add fix-checking coverage: a topic-only Greengrass config validates and packages; the executor
      dispatches to the injected Greengrass publisher when `greengrass` is set
    - **EXPECTED OUTCOME**: Test PASSES (topic-only Greengrass config is valid; no `broker_host` or
      `iot_ca_cert_path` required)
    - _Requirements: 2.2_

  - [x] 4.6 Verify MQTT plain-broker / `aws_iot` preservation still holds
    - **Property 6: Preservation** - Behavior unchanged outside every bug condition
    - Re-run the SAME preservation tests from task 2.2; confirm host-less plain config is still an
      error, `aws_iot` config unchanged, packaged dependencies and executor publish-call arguments
      identical for `greengrass`-off configs
    - **EXPECTED OUTCOME**: Tests PASS (no regressions; new check only fires when there is no target)
    - _Requirements: 3.2_

- [x] 5. Fix Bug 3 — Model inference output fan-out

  - [x] 5.1 Apply the fix indicated by the exploration results
    - **If fan-out already works (expected — hypothesis 3a):** the change is preventative. Add
      regression coverage (`builderGraph`/component test) asserting a second compatible outgoing edge
      from a model-inference output is created and both edges coexist; explicitly guarantee the
      output `<Handle>` in `BuilderNodeComponent.tsx` and the `<ReactFlow>` element stay uncapped
      (no `isConnectable` / `maxConnections` / `connectionCount` limit); confirm `onConnect` keeps
      appending per full source+sourceHandle+target+targetHandle tuple (not source-only dedup)
    - **If a real reproduction is found (hypothesis 3b):** apply the smallest change at the exact
      interaction locus the component/interaction test surfaced
    - Do NOT touch `connectionRejectionReason` / `incompatibilityReason`; no backend/compiler change
      (the compiler already realizes fan-out via `tee`/`queue`)
    - _Bug_Condition: isBugCondition3(X) where isModelInferenceNode(X.sourceNode) AND isOutputPort(...) AND portsCompatible(X) AND outgoingCount(...) >= 1 AND NOT connectionCreated(X)_
    - _Expected_Behavior: connectionCreated'(X) = TRUE AND outgoingCount'(...) = outgoingCount(...) + 1_
    - _Preservation: incompatible/self/unknown-handle rejections and single-downstream behavior unchanged; compiler references every node exactly once via tee/queue_
    - _Requirements: 2.3, 3.3, 3.4_

  - [x] 5.2 Verify Bug 3 fix-checking / regression test passes
    - **Property 3: Expected Behavior** - Model inference output fan-out
    - Re-run/finalize the test from task 1.3: a second compatible outgoing edge from a
      model-inference output is created and both edges coexist; an incompatible second drag is still
      rejected with the existing reason
    - Add the integration check: a graph where a model-inference output fans out to a conditional and
      an output node validates and the compiler linearizes it with `tee`/`queue`, referencing every
      node exactly once
    - **EXPECTED OUTCOME**: Test PASSES (fan-out supported and locked in by regression coverage)
    - _Requirements: 2.3_

  - [x] 5.3 Verify connection-acceptance preservation still holds
    - **Property 6: Preservation** - Behavior unchanged outside every bug condition
    - Re-run the SAME preservation tests from task 2.3
    - **EXPECTED OUTCOME**: Tests PASS (no accept/reject outcome changed; compatibility, self, and
      unknown-handle rules intact)
    - _Requirements: 3.3, 3.4_

- [x] 6. Fix Bug 4 — Node label

  - [x] 6.1 Change the `llm_inference` display name
    - In `catalog/nodes.py` `LLM_INFERENCE`, change `display_name="LLM Inference"` to
      `display_name="VLM/LLM Inference"`; leave `type_id="llm_inference"` and everything else
      unchanged (no frontend change — palette and canvas render `display_name` from the catalog API)
    - _Bug_Condition: isBugCondition4(X) where X.type_id = "llm_inference" AND X.display_name = "LLM Inference"_
    - _Expected_Behavior: displayName(d') = "VLM/LLM Inference" AND typeId(d') = "llm_inference"_
    - _Preservation: every other node type's label unchanged; type_id stays llm_inference_
    - _Requirements: 2.4, 3.5_

  - [x] 6.2 Re-vendor the device catalog copy
    - Run `vendor/re_vendor.sh` to regenerate the vendored `catalog/nodes.py` under
      `src/backend/workflow_engine/vendor/workflow_core/`
    - _Requirements: 2.4, 3.5_

  - [x] 6.3 Verify Bug 4 exploration test now passes
    - **Property 4: Expected Behavior** - Node label reads "VLM/LLM Inference"
    - Re-run the SAME test from task 1.4 — do NOT write a new test
    - Add the integration check: the node-catalog API response and rendered palette show
      "VLM/LLM Inference"
    - **EXPECTED OUTCOME**: Test PASSES (`display_name` is "VLM/LLM Inference"; `type_id` unchanged)
    - _Requirements: 2.4_

  - [x] 6.4 Verify catalog preservation still holds
    - **Property 6: Preservation** - Behavior unchanged outside every bug condition
    - Re-run the SAME preservation tests from task 2.1
    - **EXPECTED OUTCOME**: Tests PASS (all other labels unchanged; `llm_inference` `type_id` intact)
    - _Requirements: 3.5_

- [x] 7. Fix Bug 5 — Workflow name display

  - [x] 7.1 Surface the open workflow's name at the top of the builder
    - In `pages/workflows/WorkflowBuilder.tsx`, give the top-of-page header access to the loaded
      `WorkflowMeta` (move the `Header` into `BuilderCanvas`, which holds `workflow` state, or lift
      the loaded name up)
    - Render the open workflow's name prominently at the top when loaded; show a neutral placeholder
      (e.g. "Untitled workflow") for a new/unsaved canvas and never crash
    - Keep it display-only: do not alter `loadWorkflow`, `onSaved`, `resetCanvas`, toolbar actions,
      or validation; existing toolbar status text may stay or be de-duplicated but its behavior must
      not change
    - _Bug_Condition: isBugCondition5(X) where hasOpenWorkflow(X) AND NOT displaysWorkflowName(X)_
    - _Expected_Behavior: displaysWorkflowName(view') = TRUE AND displayedName(view') = openWorkflowName(X)_
    - _Preservation: New/Open/Save/Validate/Duplicate/Delete/Package/Generate/Test unchanged (display-only)_
    - _Requirements: 2.5, 3.6_

  - [x] 7.2 Verify Bug 5 exploration test now passes
    - **Property 5: Expected Behavior** - Workflow name shown while editing
    - Re-run the SAME test from task 1.5 — do NOT write a new test
    - Add checks: an unsaved canvas shows the placeholder; a deep-link `/workflows/builder/{id}`
      shows the name at the top once loaded
    - **EXPECTED OUTCOME**: Test PASSES (the loaded workflow's name appears at the top)
    - _Requirements: 2.5_

  - [x] 7.3 Verify builder-action preservation still holds
    - **Property 6: Preservation** - Behavior unchanged outside every bug condition
    - Re-run the SAME preservation tests from task 2.4
    - **EXPECTED OUTCOME**: Tests PASS (all toolbar/builder actions behave as before; change is
      display-only)
    - _Requirements: 3.6_

- [x] 8. Checkpoint — ensure the full suite passes
  - Run the complete backend and frontend test suites (exploration, fix-checking, and preservation
    property-based tests for all five defects)
  - Confirm every fix-checking property (Properties 1–5) passes and preservation (Property 6) holds
  - Confirm the re-vendored device copies (`catalog/nodes.py`, `validator/checks.py`) are in sync
    with the source-of-truth layer
  - Ensure all tests pass; ask the user if questions arise

## Notes

- **Test-first ordering is mandatory**: Tasks 1 and 2 must be written and run against the UNFIXED
  code before any fix. Bugs 1, 2, 4, 5 exploration tests must FAIL; the Bug 3 pure-logic test is
  expected to be an **unexpected pass** (record it via the PBT status tool as `unexpected_pass`),
  which redirects Bug 3's root cause to the React Flow interaction / `<Handle>` layer. Preservation
  tests (task 2) must PASS on unfixed code.
- **Five independent defects**: The defects share no code paths, so tasks 3–7 can proceed in any
  order. Each fix task re-runs the SAME exploration and preservation tests from tasks 1 and 2 — do
  NOT write new copies.
- **Re-vendoring**: Bugs 1, 2, and 4 edit the source-of-truth `workflow_core` layer and require
  `vendor/re_vendor.sh` to regenerate the device copies under
  `src/backend/workflow_engine/vendor/workflow_core/` (catalog for Bugs 1/4; catalog + validator for
  Bug 2). The edge executor `output_bindings.py` is the real engine (not vendored) and is edited
  directly.
- **Property references**: Property 1 → Requirement 2.1; Property 2 → 2.2; Property 3 → 2.3;
  Property 4 → 2.4; Property 5 → 2.5; Property 6 (preservation) → 3.1, 3.2, 3.3, 3.4, 3.5, 3.6.
- **Preservation discipline**: Fan-out (Bug 3) must not relax any compatibility, self-connection, or
  unknown-handle rule; the Greengrass option (Bug 2) is additive and off by default; the label
  change (Bug 4) keeps `type_id` = `llm_inference`; the name display (Bug 5) is display-only.
