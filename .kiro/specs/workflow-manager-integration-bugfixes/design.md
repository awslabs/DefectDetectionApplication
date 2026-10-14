# Workflow Manager Integration Bugfixes — Bugfix Design

## Overview

This design addresses five independent defects found while integration-testing the Workflow
Manager (the drag-and-drop workflow designer in
`edge-cv-portal/frontend/src/pages/workflows`, its node catalog source of truth in
`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`, and the
edge workflow engine in `src/backend/workflow_engine`). Each defect is treated as its own bug
condition so it can be fixed and validated in isolation; the five share no code paths.

| # | Defect | Layer | Fix locus |
|---|--------|-------|-----------|
| 1 | `llm_inference` input port is `InferenceMeta`, should be `VideoFrames` | Node catalog (data) | `catalog/nodes.py` `LLM_INFERENCE.inputs` (+ vendored copy) |
| 2 | `mqtt_publish` has no zero-config Greengrass path (only topic) | Catalog + validator + edge executor | `catalog/nodes.py`, `validator/checks.py`, `workflow_engine/output_bindings.py` |
| 3 | Model-inference output port cannot fan out to multiple downstream nodes in the designer | Frontend designer (React Flow interaction) | `pages/workflows` (WorkflowBuilder / BuilderNodeComponent / builderGraph) |
| 4 | `llm_inference` label reads "LLM Inference", should read "VLM/LLM Inference" | Node catalog (data) | `catalog/nodes.py` `LLM_INFERENCE.display_name` (+ vendored copy) |
| 5 | Workflow name is not shown at the top while viewing/editing | Frontend builder page | `pages/workflows/WorkflowBuilder.tsx` |

The strategy for each defect is the same: (1) confirm the current defective behavior with a test
that fails on the unfixed code, (2) apply the smallest change that makes the fix-checking property
hold, and (3) prove — with property-based tests where feasible — that every input outside the bug
condition behaves exactly as it did before (preservation).

Two defects (1 and 4) are pure data edits to the node-catalog descriptor and are the lowest risk.
Defect 2 is additive (a new, off-by-default publishing option) touching the catalog, validator, and
edge executor. Defect 3 is frontend-only interaction behavior; the backend compiler already realizes
fan-out with `tee`/`queue` (`compiler/compiler.py`, Requirement 6.3). Defect 5 is a display-only
change to the builder page header.

## Glossary

- **Bug_Condition (C)**: The set of inputs that trigger a given defect. Each of the five defects has
  its own predicate `isBugConditionN`.
- **Property (P)**: The desired behavior on inputs where the bug condition holds (fix-checking).
- **Preservation**: Behavior that must remain byte-for-byte identical for every input where no bug
  condition holds.
- **F / F'**: The system before (`F`) and after (`F'`) the five fixes.
- **Node catalog**: The list of `NodeTypeDescriptor` records in `catalog/nodes.py` — the single
  source of truth served to the frontend palette via the node-catalog API and consumed by the
  validator and compiler.
- **`NodeTypeDescriptor`**: A node type declaration (`type_id`, `category`, `display_name`,
  `inputs`, `outputs`, `parameters`, `mappings`, `hardware_dependent`) in `catalog/models.py`.
- **`PortDescriptor` / port type**: A typed attachment point (`name`, `port_type`); port types are
  `VideoFrames`, `InferenceMeta`, `EventSignal` (`PORT_TYPE_*` constants).
- **Port coercion**: The one declared coercion in `catalog/compatibility.py` (and its TS mirror
  `compatibility.ts`): an `InferenceMeta` output may feed a `VideoFrames` input.
- **`LLM_INFERENCE`**: The `llm_inference` descriptor in `catalog/nodes.py` (defects 1 and 4).
- **`MQTT_PUBLISH`**: The `mqtt_publish` output descriptor in `catalog/nodes.py` (defect 2).
- **Executor binding**: A node realized by the LocalServer `WorkflowExecutor` rather than a
  GStreamer element; `mqtt_publish` binds to `"mqtt_publish"` and is handled by
  `OutputBindingProcessor._run_mqtt_publish` in `workflow_engine/output_bindings.py`.
- **Greengrass-managed publish**: Publishing through the device's Greengrass IPC (to AWS IoT Core via
  the Greengrass nucleus) so the user configures only a topic — no broker host/port and no
  certificate file paths.
- **Model inference node**: A node in the `inference` category — Model Inference (`model_inference`),
  Bedrock Inference (`bedrock_inference`), and VLM/LLM Inference (`llm_inference`).
- **Fan-out**: A single output port connected to more than one downstream node.
- **Workflow_Builder**: The React Flow canvas page `WorkflowBuilder.tsx`; `WorkflowMeta` is the
  loaded workflow's identity (`workflowId`, `name`, `description`, `version`).
- **`connectionRejectionReason` / `onConnect` / `edgeIdFor`**: The pure connection helpers in
  `builderGraph.ts` that decide whether a dragged connection is acceptable and create the edge.

## Bug Details

### Bug 1 — VLM/LLM inference should take video frames

Today `LLM_INFERENCE` declares `inputs=[PortDescriptor("in", PORT_TYPE_INFERENCE_META)]`, so the node
consumes upstream inference metadata and a video-frame source cannot connect directly into its `in`
port (a `VideoFrames` output → `InferenceMeta` input is not compatible: coercion only runs the other
direction). As a vision-language node it should take video frames.

**Formal Specification:**
```
FUNCTION isBugCondition1(X)
  INPUT: X of type NodeTypeDescriptor
  OUTPUT: boolean

  RETURN X.type_id = "llm_inference"
         AND inputPortType(X, "in") = PORT_TYPE_INFERENCE_META
END FUNCTION
```

#### Examples
- A `csi_camera_source` (`out : VideoFrames`) cannot connect into `llm_inference.in` today; expected:
  it connects directly (exact type match).
- The catalog descriptor's `in` port reports `InferenceMeta`; expected: `VideoFrames`.
- The `out` port reports `InferenceMeta` and must stay `InferenceMeta` (unchanged).
- An `inference_filter` (`out : InferenceMeta`) still connects into `llm_inference.in` after the fix
  via the declared `InferenceMeta → VideoFrames` coercion (edge case: coercion preserved).

### Bug 2 — MQTT publish through Greengrass with only a topic

`MQTT_PUBLISH` requires `broker_host` (`required=True`) for the plain-broker path, or `aws_iot=True`
plus device certificate file paths for AWS IoT Core over mutual TLS. There is no zero-configuration
path that publishes through the device's Greengrass-managed MQTT so that only the topic must be
supplied. The edge executor `_run_mqtt_publish` unconditionally reads `parameters["broker_host"]`.

**Formal Specification:**
```
FUNCTION isBugCondition2(X)
  INPUT: X of type MqttPublishConfig
  OUTPUT: boolean

  RETURN wantsGreengrassManagedPublish(X)
         AND NOT existsValidConfig(topicOnly(X))
END FUNCTION
```

#### Examples
- A user wanting Greengrass publishing must still fill in `broker_host`; expected: a topic-only
  config validates and packages.
- A topic-only config fails validation today (`V4_MISSING_REQUIRED_PARAMETER` for `broker_host`);
  expected: it passes when the Greengrass option is on.
- Existing plain-broker config (host + topic) still validates and publishes (unchanged).
- Existing `aws_iot` config (thing name + cert paths) still validates and publishes (unchanged).

### Bug 3 — Model inference output fan-out

A model inference node's single output port cannot be connected to more than one downstream node in
the designer (for example fanning out to both a conditional and an output). The backend compiler
already realizes fan-out with `tee`/`queue` (`compiler/compiler.py`, Requirement 6.3), so this is a
designer-interaction gap, not a compile gap.

**Formal Specification:**
```
FUNCTION isBugCondition3(X)
  INPUT: X of type ConnectionAttempt
  OUTPUT: boolean

  RETURN isModelInferenceNode(X.sourceNode)
         AND isOutputPort(X.sourceNode, X.sourceHandle)
         AND portsCompatible(X)
         AND outgoingCount(X.sourceNode, X.sourceHandle) >= 1
         AND NOT connectionCreated(X)
END FUNCTION
```

#### Examples
- `model_inference.out` already connected to `conditional.in`; a second drag to `mqtt_publish.in`
  produces no edge; expected: the second edge is created.
- `bedrock_inference.out` fanning out to `inference_filter.in` and `capture.in`; expected: both.
- `llm_inference.out` fanning out to two downstream nodes; expected: both.
- Edge case: the second target has an incompatible port type — still rejected with the existing
  reason (fan-out must not relax compatibility).

**Investigation note (confirmed against the code; informs the root-cause hypothesis below):** every
layer that could gate a second outgoing edge currently permits fan-out:
- `builderGraph.ts` — `edgeIdFor`/`isSameConnection` key on the full
  source+sourceHandle+target+targetHandle tuple, so two edges from one output to two different targets
  have distinct ids. `connectionRejectionReason` rejects only self-connections, unknown handles, and
  incompatible port types; it never inspects the source's out-degree.
- `WorkflowBuilder.tsx` `onConnect` — appends any connection whose exact tuple is not already present
  (`existing.some(isSameConnection)` dedup is per-tuple, not per-source), so a second compatible target
  is appended.
- `BuilderNodeComponent.tsx` — the output (`source`) `<Handle>` sets **no** `isConnectable` /
  connection-count cap, and the `<ReactFlow>` element in `WorkflowBuilder.tsx` sets no connection
  limits either. React Flow's default allows multiple connections from a source handle.

In other words, the confirmed code contains no single-connection constraint on a model-inference
output at any layer, so at the unit/component level fan-out appears already supported. The exploratory
Bug 3 test is therefore expected to be an **unexpected pass** at the pure-logic layer (and likely at
the component layer). The exploratory step exists precisely to confirm or refute this: if fan-out
already works, the fix reduces to (a) locking the behavior in with a regression test and (b) confirming
no `<Handle>`/`<ReactFlow>` cap is (re)introduced; if a real reproduction is found (e.g. via a specific
React Flow interaction), it will pin the exact locus before any code change is written.

### Bug 4 — Node label reads "VLM/LLM Inference"

`LLM_INFERENCE.display_name` is `"LLM Inference"`. The palette and canvas render `display_name`
directly (`NodePalette`, `BuilderNodeComponent`), so the label reads "LLM Inference". It should read
"VLM/LLM Inference"; the `type_id` must stay `llm_inference`.

**Formal Specification:**
```
FUNCTION isBugCondition4(X)
  INPUT: X of type NodeTypeDescriptor
  OUTPUT: boolean

  RETURN X.type_id = "llm_inference" AND X.display_name = "LLM Inference"
END FUNCTION
```

#### Examples
- Palette entry reads "LLM Inference"; expected: "VLM/LLM Inference".
- Canvas node title reads "LLM Inference"; expected: "VLM/LLM Inference".
- `type_id` stays `llm_inference` (unchanged — saved workflows and compiler bindings key on it).
- Edge case: every other node type's label is untouched.

### Bug 5 — Workflow name shown while editing

The builder page renders a static `Header` reading "Workflow Builder" and only surfaces the open
workflow's name as small secondary text inside the toolbar
(`${workflow.name} (v${workflow.version})`). There is no prominent workflow-name display at the top
of the screen, so a user viewing/editing cannot readily tell which workflow is open.

**Formal Specification:**
```
FUNCTION isBugCondition5(X)
  INPUT: X of type BuilderView
  OUTPUT: boolean

  RETURN hasOpenWorkflow(X)
         AND NOT displaysWorkflowName(X)
END FUNCTION
```

#### Examples
- Open "Line inspection"; the top-of-page header still reads only "Workflow Builder"; expected: the
  header shows "Line inspection".
- Deep-link `/workflows/builder/{id}`: once loaded, the name appears at the top.
- Edge case: a new/unsaved workflow has no name — the header shows a neutral placeholder and no crash.
- Save, Open, Validate, Package, Duplicate, Delete behavior is unchanged (display-only).

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- **Bug 1**: `llm_inference` still emits `InferenceMeta` on `out`; its parameters (`modelName`,
  `prompt_template`, `max_tokens`, `temperature`, `top_p`) and its architecture mappings
  (vLLM-capable device archs plus the `sim` stub `sim_llm_inference`) are unchanged.
- **Bug 2**: the plain-broker and `aws_iot` publishing paths still accept, validate, package
  (paho-mqtt dependency), and publish exactly as before; the Greengrass option is additive and off by
  default.
- **Bug 3**: incompatible-type connections, self-connections, and unknown-port handles are still
  rejected with the existing reason; a single-downstream inference output and every non-inference
  node type behave exactly as before; compiled pipelines still reference every node exactly once with
  fan-out realized via `tee`/`queue`.
- **Bug 4**: every other node type's label is unchanged and `llm_inference`'s `type_id` stays
  `llm_inference`.
- **Bug 5**: New/Open/Save/Validate/Duplicate/Delete/Package/Generate/Test all behave as before;
  showing the name is display-only and does not change save, load, or validation.

**Scope:**
All inputs where no bug condition holds should be completely unaffected. Concretely, that includes:
- Every node type other than `llm_inference` (defects 1 and 4).
- Every `mqtt_publish` config that does not request Greengrass publishing (defect 2).
- Every connection attempt that is not a valid additional outgoing edge from a model-inference output
  port — incompatible types, self-connections, unknown handles, and first/only connections (defect 3).
- Every builder action other than rendering the workflow-name label (defect 5).

The actual corrected behaviors are specified in the Correctness Properties section (Properties 1–5).

## Hypothesized Root Cause

1. **Bug 1 — declared input port type (confirmed by reading the catalog).** `LLM_INFERENCE.inputs`
   hard-codes `PORT_TYPE_INFERENCE_META`. This is a one-line data value, not a code-path issue. The
   descriptor is duplicated in the vendored device copy
   (`src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`), which must be re-vendored.

2. **Bug 2 — no Greengrass branch anywhere in the MQTT path.**
   - Catalog: `broker_host` is `required=True`, so `V4` in `validator/checks.py` unconditionally
     demands it; there is no `greengrass` parameter.
   - Executor: `_run_mqtt_publish` reads `parameters["broker_host"]` before any mode branch and only
     forks on `aws_iot`.
   - There is no Greengrass IPC publisher. The fix must add the option in all three places while
     keeping the broker and `aws_iot` paths intact.

3. **Bug 3 — no single-connection constraint exists in the current code (confirmed by reading it).**
   The pure connection helpers (`connectionRejectionReason`, `onConnect`, `edgeIdFor`,
   `isSameConnection`) already support fan-out, the output `<Handle>` in `BuilderNodeComponent.tsx`
   sets no connection cap, the `<ReactFlow>` element sets no connection limits, and the backend
   compiler already emits `tee`/`queue` (see the investigation note under Bug Details). The most
   likely explanations for the reported inability to draw a second edge are, in priority order:
   - (a) the defect does not reproduce against the current code at the unit/component level (fan-out
     already works) — an unexpected pass — because the constraint was already removed or never existed
     in this layer; the fix then reduces to a regression test plus guaranteeing no cap is
     (re)introduced;
   - (b) the defect reproduces only through a specific React Flow interaction (e.g. a drag from an
     already-connected handle handled by a default that the current props do not override), which a
     component/interaction-level test must surface and pin;
   - (c) the block is a `isValidConnection` / `connectionRejectionReason` rejection unrelated to port
     compatibility (contradicted by the code, but the exploratory test checks it explicitly).
   The exploratory tests (below) start at the pure-logic layer to confirm (a) and, if it holds, drive
   an actual second drag on the canvas to check (b) before any change is written.

4. **Bug 4 — declared display name (confirmed by reading the catalog).** `display_name = "LLM
   Inference"`. One-line data value; also present in the vendored copy.

5. **Bug 5 — the header is static and detached from `WorkflowMeta`.** `WorkflowBuilder.tsx` renders
   the `Header` "Workflow Builder" in the outer component, while the loaded `workflow` (`WorkflowMeta`)
   lives in the inner `BuilderCanvas`. The name is only shown as small toolbar text. The fix must
   surface the loaded name in a prominent top-of-page element, which requires the header to have
   access to `workflow`.

## Correctness Properties

Property 1: Bug Condition — VLM/LLM inference takes video frames

_For any_ node-type descriptor where the bug condition holds (`isBugCondition1` returns true —
`type_id = "llm_inference"` with an `InferenceMeta` input port), the fixed catalog SHALL declare the
`llm_inference` node's `in` port as `VideoFrames` and SHALL keep its `out` port as `InferenceMeta`,
so a `VideoFrames` source connects directly into it.

**Validates: Requirements 2.1**

Property 2: Bug Condition — MQTT publish through Greengrass with only a topic

_For any_ `mqtt_publish` configuration where the bug condition holds (`isBugCondition2` returns true —
the user wants Greengrass-managed publishing and no topic-only config is currently valid), the fixed
system SHALL accept a Greengrass publish configuration supplying only the topic — valid without
`broker_host`, `broker_port`, or any `iot_*` certificate path — and SHALL package it as a valid
output node.

**Validates: Requirements 2.2**

Property 3: Bug Condition — Model inference output fan-out

_For any_ connection attempt where the bug condition holds (`isBugCondition3` returns true — an
additional type-compatible outgoing connection from a model-inference node's output port that is not
being created), the fixed designer SHALL create the connection, increasing that output port's
outgoing-connection count by one, so a model-inference output supports fan-out to multiple downstream
nodes.

**Validates: Requirements 2.3**

Property 4: Bug Condition — Node label reads "VLM/LLM Inference"

_For any_ node-type descriptor where the bug condition holds (`isBugCondition4` returns true —
`type_id = "llm_inference"` labelled "LLM Inference"), the fixed catalog SHALL present its
`display_name` as "VLM/LLM Inference" while keeping its `type_id` equal to `llm_inference`.

**Validates: Requirements 2.4**

Property 5: Bug Condition — Workflow name shown while editing

_For any_ builder view where the bug condition holds (`isBugCondition5` returns true — a workflow is
open but its name is not displayed at the top), the fixed builder SHALL display the open workflow's
name at the top of the screen, and the displayed name SHALL equal the open workflow's name.

**Validates: Requirements 2.5**

Property 6: Preservation — Behavior unchanged outside every bug condition

_For any_ input where none of the bug conditions hold (`isBugCondition1..5` all return false), the
fixed system SHALL produce exactly the same result as the original system, preserving: the
`llm_inference` output type, parameters, and architecture mappings; the plain-broker and `aws_iot`
MQTT paths (acceptance, validation, packaging, publish); connection rejection for
incompatible/self/unknown-port attempts and single-downstream/other-node connection behavior together
with the compiler's exactly-once, `tee`/`queue` fan-out realization; every other node type's label and
`llm_inference`'s `type_id`; and all builder actions (New/Open/Save/Validate/Duplicate/Delete/
Package/Generate/Test) as display-only, non-behavioral.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Bug 1 — VLM/LLM inference input type

**File**: `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`
**Descriptor**: `LLM_INFERENCE`

1. Change `inputs=[PortDescriptor("in", PORT_TYPE_INFERENCE_META)]` to
   `inputs=[PortDescriptor("in", PORT_TYPE_VIDEO_FRAMES)]`.
2. Leave `outputs`, `parameters`, `mappings`, and `hardware_dependent` untouched.
3. Update the adjacent comment that says the node "consumes upstream inference metadata" so the
   code documentation matches the new frame input (the `prompt_template` help text that references
   upstream metadata placeholders is a parameter description and is out of scope for this change; note
   the semantic interaction for the implementer but do not alter the parameter set — preservation 3.1).
4. Re-vendor the device copy `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`
   via the existing `vendor/re_vendor.sh` so the LocalServer/compiler copy matches.

### Bug 2 — Greengrass MQTT publishing option

**Files**: `catalog/nodes.py` (`MQTT_PUBLISH`) and `validator/checks.py` in the source-of-truth layer
`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/`, the LocalServer engine
`src/backend/workflow_engine/output_bindings.py` (`OutputBindingProcessor`), and the re-vendored
copies of the catalog **and** validator under `src/backend/workflow_engine/vendor/workflow_core/`.
Note the executor `output_bindings.py` is the real engine (not vendored) and is edited directly; the
catalog and validator have vendored copies that must be regenerated with `re_vendor.sh` (see Bug 1
step 4).

1. **Catalog — add the option (additive, off by default):**
   - Add a `greengrass` parameter: `ParameterDescriptor("greengrass", "bool", required=False,
     default=False, ...)` describing zero-config publishing via the device's Greengrass-managed MQTT
     (only the topic is needed).
   - Change `broker_host` from `required=True` to `required=False` so a topic-only Greengrass config
     is not force-failed by `V4`. `topic` stays `required=True` (needed in every mode).
   - Optionally scope the broker/`aws_iot` fields' visibility to the non-Greengrass modes in the
     config UI; this is presentational and must not change validation outcomes.
   - Add the Greengrass IPC runtime dependency to the device mappings' `plugin_dependencies`
     (e.g. `python:awsiotsdk`) alongside the existing `python:paho-mqtt`; keep the `sim` recording
     binding unchanged.

2. **Validator — preserve broker-config rejection under the relaxed `required`:** because
   `broker_host` is no longer statically required, add a small, `mqtt_publish`-specific check to
   `validator/checks.py` (new stable code, e.g. `CODE_V*_MQTT_NO_TARGET`) that reports an error when a
   `mqtt_publish` node has **neither** `greengrass` enabled **nor** `aws_iot` enabled **nor** a
   non-empty `broker_host`. This keeps a host-less plain-broker config rejected (same accept/reject
   outcome as today, under a dedicated code) while allowing the topic-only Greengrass config. Mirror
   the rule in the frontend inline checks only if the existing mirror covers `mqtt_publish` (V4/V5
   mirror scope); otherwise leave inline checks unchanged and rely on full backend validation.

3. **Edge executor — add the Greengrass branch first:** in `_run_mqtt_publish`
   (`output_bindings.py`), before reading `broker_host`, branch on `parameters.get("greengrass")`. In
   that branch, render the payload as today and publish only `topic`/`payload`/`qos` through a new
   injectable `_greengrass_publisher` (default implementation uses the Greengrass IPC
   `PublishToIoTCore` operation via a lazily-imported `awsiot.greengrasscoreipc` client, matching the
   lazy-import pattern already used for paho/opcua). Leave the existing non-`aws_iot` and `aws_iot`
   branches, `_default_mqtt_publisher`, and the `AWS_IOT_*` handling exactly as they are. Add the new
   publisher to the `OutputBindingProcessor.__init__` injectable set so tests can run without a
   Greengrass runtime.

### Bug 3 — Model inference output fan-out

**Files**: `pages/workflows/BuilderNodeComponent.tsx`, `WorkflowBuilder.tsx`, `builderGraph.ts`
(final locus confirmed by the exploratory tests).

The current code contains no single-connection constraint at any layer (confirmed — see the
investigation note and root-cause hypothesis). The fix is therefore driven by what the exploratory
tests reveal:

- **If fan-out already works (expected — hypothesis 3a):** the change is preventative, not corrective.
  1. Add regression coverage: a `builderGraph`/component test asserting a second compatible outgoing
     edge from a model-inference output is created and both edges coexist, so any future
     `maxConnections`/`isConnectable` cap or source-only dedup is caught.
  2. Explicitly guarantee the output `<Handle>` in `BuilderNodeComponent.tsx` and the `<ReactFlow>`
     element stay uncapped (no `isConnectable`/connection-count limit added); if desired, set the
     source handle's cap intent explicitly (e.g. no `connectionCount` limit) so the intent is
     documented in code.
  3. Confirm `onConnect` keeps appending per full tuple (it does today) and does not dedup on source
     alone.
- **If a real reproduction is found (hypothesis 3b):** re-hypothesize toward the specific interaction
  path the component/interaction test surfaces (e.g. a React Flow default that only bites during an
  actual second drag) and apply the smallest change at that exact locus.

In both cases, do not touch `connectionRejectionReason`/`incompatibilityReason`: fan-out must keep
every existing compatibility, self-connection, and unknown-handle rejection (preservation 3.3). No
backend/compiler change is needed — the compiler already realizes fan-out via `tee`/`queue`.

### Bug 4 — Node label

**File**: `catalog/nodes.py`, descriptor `LLM_INFERENCE`.
1. Change `display_name="LLM Inference"` to `display_name="VLM/LLM Inference"`.
2. Leave `type_id="llm_inference"` and everything else unchanged.
3. Re-vendor the device catalog copy. No frontend change is needed — the palette and canvas render
   `display_name` from the catalog API.

### Bug 5 — Workflow name display

**File**: `pages/workflows/WorkflowBuilder.tsx`.
1. Give the top-of-page header access to the loaded `WorkflowMeta`. Either move the `Header` into
   `BuilderCanvas` (which already holds `workflow` state) or lift the loaded name up to
   `WorkflowBuilder`.
2. Render the open workflow's name prominently at the top: when a workflow is loaded, show its name
   (e.g. as the header text or a title beside "Workflow Builder"); when the canvas is new/unsaved,
   show a neutral placeholder (e.g. "Untitled workflow") and never crash.
3. Keep this display-only: do not alter `loadWorkflow`, `onSaved`, `resetCanvas`, the toolbar
   actions, or validation (preservation 3.6). The existing toolbar status text may stay or be
   de-duplicated with the new header, but its behavior must not change.

## Testing Strategy

### Validation Approach

Two phases per defect: first surface counterexamples that demonstrate the defect on the **unfixed**
code (confirming or refuting the root-cause hypothesis), then verify the fix makes the fix-checking
property hold and that every non-bug input is preserved. Preservation is checked with property-based
tests wherever the input domain is large (connection attempts, node descriptors, MQTT configs).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples demonstrating each defect before implementing the fix, and confirm
or refute each root-cause hypothesis. Where a hypothesis is refuted (notably Bug 3), re-hypothesize
before writing code.

**Test Plan**: Write focused tests against the unfixed code for each defect and observe the failures.

**Test Cases**:
1. **Bug 1 — catalog input type** (backend): assert `LLM_INFERENCE` `in` port is `VideoFrames` — fails
   on unfixed code (currently `InferenceMeta`). Companion designer check: a `VideoFrames` source is
   rejected into `llm_inference.in` today.
2. **Bug 2 — topic-only Greengrass config** (backend): build an `mqtt_publish` node with only a topic
   plus the (not-yet-existing) Greengrass option and run `validate` — fails on unfixed code
   (`V4_MISSING_REQUIRED_PARAMETER` for `broker_host`, and no `greengrass` parameter exists).
3. **Bug 3 — second outgoing edge at the pure-logic layer** (frontend): create a `model_inference`
   node with `out` connected to one target, then invoke the connection path for a second compatible
   target and assert a second edge exists. Per the investigation note this may **pass** on the unfixed
   pure helpers (an unexpected pass), which refutes the pure-logic hypothesis and points the fix at the
   React Flow interaction/`<Handle>` layer; a component-level test that drives an actual second drag on
   the canvas is then used to reproduce the real defect.
4. **Bug 4 — label** (backend): assert `LLM_INFERENCE.display_name == "VLM/LLM Inference"` — fails on
   unfixed code (currently "LLM Inference").
5. **Bug 5 — name display** (frontend): render `WorkflowBuilder` with a loaded workflow and assert its
   name appears in the top-of-page header — fails on unfixed code (header is the static "Workflow
   Builder").

**Expected Counterexamples**:
- Bug 1: `in` port type is `InferenceMeta`; a `VideoFrames` source cannot connect.
- Bug 2: a topic-only config is rejected; no Greengrass parameter exists.
- Bug 3: (likely) the pure helpers already create the second edge — an unexpected pass that redirects
  the root cause to the interaction layer.
- Bug 4: label is "LLM Inference".
- Bug 5: the workflow name is absent from the top header (only in small toolbar text).

### Fix Checking

**Goal**: For all inputs where a bug condition holds, the fixed system produces the expected behavior.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition1(X) DO
  d' := catalog'("llm_inference")
  ASSERT inputPortType(d', "in") = PORT_TYPE_VIDEO_FRAMES
  ASSERT outputPortType(d', "out") = PORT_TYPE_INFERENCE_META
END FOR

FOR ALL X WHERE isBugCondition2(X) DO
  cfg' := greengrassPublish(topic = X.topic)
  ASSERT isValidMqttPublishConfig'(cfg')
  ASSERT NOT requires(cfg', "broker_host") AND NOT requires(cfg', "iot_ca_cert_path")
END FOR

FOR ALL X WHERE isBugCondition3(X) DO
  graph' := attemptConnect'(X)
  ASSERT connectionCreated'(X) = TRUE
  ASSERT outgoingCount'(graph', X.sourceNode, X.sourceHandle) = outgoingCount(X...) + 1
END FOR

FOR ALL X WHERE isBugCondition4(X) DO
  d' := catalog'("llm_inference")
  ASSERT displayName(d') = "VLM/LLM Inference" AND typeId(d') = "llm_inference"
END FOR

FOR ALL X WHERE isBugCondition5(X) DO
  view' := render'(X)
  ASSERT displaysWorkflowName(view') = TRUE AND displayedName(view') = openWorkflowName(X)
END FOR
```

### Preservation Checking

**Goal**: For all inputs where no bug condition holds, the fixed system produces the same result as the
original.

**Pseudocode:**
```
FOR ALL X WHERE NOT (isBugCondition1(X) OR isBugCondition2(X) OR isBugCondition3(X)
                     OR isBugCondition4(X) OR isBugCondition5(X)) DO
  ASSERT F(X) = F'(X)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation because it exercises many
inputs across the domain automatically and catches edge cases manual tests miss. Establish the baseline
by observing behavior on the **unfixed** code first, then assert the fixed code matches for every
non-bug input.

**Test Plan / Cases**:
1. **Catalog preservation** (backend, property-based over all descriptors): every node type other than
   `llm_inference` has byte-identical `display_name`, ports, parameters, and mappings; `llm_inference`
   keeps `type_id`, `out` type, parameters, and mappings.
2. **MQTT plain-broker/`aws_iot` preservation** (backend, property-based over MQTT configs with
   `greengrass` off): validation acceptance/rejection, packaged dependencies, and the executor's
   publish call arguments are identical to the pre-fix behavior; the new check only fires when there is
   no target at all.
3. **Connection-acceptance preservation** (frontend, property-based — extends
   `connectionAcceptance.property.test.ts`): `connectionRejectionReason` still accepts a connection iff
   source-output/target-input types are compatible, with a non-empty reason on every rejection; adding
   fan-out must not change any accept/reject outcome, and single-downstream connections are unchanged.
4. **Builder-action preservation** (frontend): New/Open/Save/Validate/Duplicate/Delete/Package flows
   behave as before with the name-display change in place.

### Unit Tests

- Bug 1/4: catalog descriptor assertions for `llm_inference` `in` port type, `out` port type, `type_id`,
  and `display_name`.
- Bug 2: `validate` on topic-only Greengrass config (passes), host-less plain config (still an error),
  `aws_iot` config (unchanged); `_run_mqtt_publish` dispatches to the Greengrass publisher when
  `greengrass` is set and to the broker/`aws_iot` publishers otherwise (injected fakes).
- Bug 3: component-level canvas test that a second compatible drag from a model-inference output
  creates a second edge; an incompatible second drag is still rejected.
- Bug 5: `WorkflowBuilder` renders the loaded name at the top; an unsaved canvas shows the placeholder.

### Property-Based Tests

- Property 1/4 (catalog): generate/enumerate descriptors and assert the fix-checking and preservation
  invariants for `llm_inference` and all other types.
- Property 2 (MQTT): generate MQTT configs across modes and assert topic-only Greengrass validates,
  while plain-broker/`aws_iot` acceptance and executor dispatch are preserved.
- Property 3 (connections): generate node/port pairs and assert acceptance equals port compatibility
  with fan-out enabled (extends the existing connection-acceptance property).

### Integration Tests

- Bug 1: a `csi_camera_source → llm_inference → mqtt_publish` graph validates and compiles (frame
  input accepted end to end).
- Bug 2: a workflow with a topic-only Greengrass `mqtt_publish` node validates and packages; a device
  run routes the publish through the Greengrass IPC publisher (recording binding in `sim`).
- Bug 3: build a graph where a model-inference output fans out to a conditional and an output node;
  validate and confirm the compiler linearizes the fan-out with `tee`/`queue`, referencing every node
  exactly once.
- Bug 4: the node-catalog API response and rendered palette show "VLM/LLM Inference".
- Bug 5: opening a saved workflow via deep link shows its name at the top; New resets to the
  placeholder.
