# Requirements Document

## Introduction

This feature is **Sub-feature A** of the larger, documentation-only assessment at
`.kiro/specs/workflow-triggers-and-input-overhaul/` (see that folder's `requirements.md`,
`design.md`, and `tasks.md` — item "Sub-feature A"). It is a separate, schedulable
implementation spec and does **not** modify that assessment folder.

Sub-feature A is **portal + designer only**. It introduces the structural scaffolding
for a Triggers stage and a unified Input node without introducing any new device-runtime
subscription or activation model. Concretely, it:

1. Adds a new `CATEGORY_TRIGGER` node category to the shared Node_Type_Catalog, presented
   in the Workflow_Designer palette as a distinct section ordered **before** Inputs.
2. Recategorizes the existing `digital_input` node from `CATEGORY_INPUT` to
   `CATEGORY_TRIGGER` as a **metadata-only** move with byte-identical runtime behavior.
   The relocated `digital_input` is the only trigger node this spec introduces.
3. Adds a `Unified_Input_Node` that **coexists** with the four retained source descriptors
   (`csi_camera_source`, `icam_source`, `aravis_camera_source`, `folder_source`), selects
   among the non-digital sources via a `source_kind` enum, gates its other parameters on
   that selection, emits `PORT_TYPE_VIDEO_FRAMES`, and compiles to the **same** device
   bindings as the corresponding existing source. It carries an optional activation input
   port that is designer/validator scaffolding only.
4. Extends the Workflow_Validator to enforce stage-ordering legality (Trigger → Input →
   existing downstream stages).
5. Updates the frontend designer: new palette section before Inputs; renders saved
   workflows referencing `digital_input` under the Triggers section without mutating the
   stored definition; `source_kind` gating UX; activation-port wiring UX (scaffolding).

### Out of scope (deferred to sibling sub-features)

- MQTT/OPC UA subscribe triggers and the trigger-driven device-activation runtime
  (**Sub-feature B**).
- The `Trigger_Transform` custom-code node (**Sub-feature C**).
- Any new device-runtime subscription/activation model, any long-lived listener layer,
  any new Greengrass recipe accessControl, and any functional trigger-driven activation.

### Grounded artifacts

- Catalog (both copies MUST stay byte-in-sync):
  - `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`
  - `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`
- Category constants: `.../workflow_core/catalog/models.py`
  (`CATEGORY_INPUT`, `CATEGORY_PREPROCESSING`, `CATEGORY_INFERENCE`,
  `CATEGORY_POST_PROCESSING`, `CATEGORY_OUTPUT`, `CATEGORIES`).
- Validator: `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/validator/checks.py`
  (`validate`, check codes `V1`–`V6`, `W1`).
- Frontend catalog mirror + palette: `edge-cv-portal/frontend/src/pages/workflows/types.ts`
  (`CATEGORY_*`, `CATEGORIES`) and the workflow designer under
  `edge-cv-portal/frontend/src/pages/workflows/`.

## Glossary

- **Node_Type_Catalog**: the data-only list of `NodeTypeDescriptor` records defining every
  workflow node type. Two byte-identical copies exist: the portal layer copy
  (`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`)
  and the device-vendored copy (`src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`).
- **CATEGORY_TRIGGER**: the new node category constant added to `catalog/models.py` and the
  `CATEGORIES` tuple, identifying nodes that emit an activation event.
- **Workflow_Designer**: the frontend workflow builder under
  `edge-cv-portal/frontend/src/pages/workflows/`, including the Node_Palette.
- **Node_Palette**: the Workflow_Designer sidebar that groups selectable node types by
  category section.
- **Workflow_Validator**: the pure `validate(graph, catalog, ...)` function in
  `workflow_core/validator/checks.py` that returns `ValidationFinding` records.
- **digital_input**: the existing GPIO edge node (`type_id="digital_input"`) emitting
  `PORT_TYPE_EVENT_SIGNAL` with an executor-level binding and a simulation appsrc stub.
- **Unified_Input_Node**: the new input node type selecting among non-digital sources via
  a `source_kind` enum; emits `PORT_TYPE_VIDEO_FRAMES`.
- **source_kind**: the enum parameter on the Unified_Input_Node selecting the underlying
  source (`csi_camera`, `icam`, `aravis_camera`, `folder`).
- **Activation_Input_Port**: an optional input port on the Unified_Input_Node accepting an
  `PORT_TYPE_EVENT_SIGNAL` connection, reserved for future trigger wiring. In this spec it
  is designer/validator scaffolding only, with no functional runtime effect.
- **Device_Binding**: the compiled realization of a node for an architecture (the
  `GstMapping` element chain and/or `executor_binding`) produced by the compiler.
- **Zero_Trigger_Workflow**: a workflow whose node set contains no `CATEGORY_TRIGGER` node.
- **PORT_TYPE_VIDEO_FRAMES / PORT_TYPE_EVENT_SIGNAL**: the existing port-type constants for
  video-frame streams and event signals respectively.

## Requirements

### Requirement 1: Add the Triggers node category

**User Story:** As a workflow author, I want a distinct Triggers category in the catalog and
palette, so that event-shaped nodes are grouped separately from image sources and appear
before Inputs.

#### Acceptance Criteria

1. THE Node_Type_Catalog SHALL define a `CATEGORY_TRIGGER` category constant in
   `catalog/models.py` and SHALL include `CATEGORY_TRIGGER` in the `CATEGORIES` tuple.
2. THE portal catalog copy and the device-vendored catalog copy SHALL remain byte-identical
   after the `CATEGORY_TRIGGER` addition.
3. THE frontend catalog mirror (`types.ts`) SHALL define a matching `CATEGORY_TRIGGER`
   constant and SHALL include it in its `CATEGORIES` list.
4. THE Workflow_Designer SHALL present `CATEGORY_TRIGGER` as a distinct Node_Palette section
   ordered before the Inputs section.

### Requirement 2: Relocate digital_input to Triggers (metadata-only, behavior-preserving)

**User Story:** As a workflow author, I want the existing digital_input node listed under
Triggers, so that its event-shaped role is represented correctly without changing how it runs.

#### Acceptance Criteria

1. THE `digital_input` descriptor SHALL declare `category=CATEGORY_TRIGGER` in place of
   `CATEGORY_INPUT`.
2. THE relocated `digital_input` descriptor SHALL retain identical `pin`, `trigger_edge`,
   and `poll_interval_ms` parameters (same types, defaults, and constraints).
3. THE relocated `digital_input` descriptor SHALL retain its single `PORT_TYPE_EVENT_SIGNAL`
   output port unchanged.
4. THE relocated `digital_input` descriptor SHALL retain its device-architecture
   `executor_binding="digital_input"` and its `ARCH_SIM` appsrc simulation stub unchanged.
5. FOR ALL architectures, THE compiled Device_Binding produced for a `digital_input` node
   after relocation SHALL be identical to the Device_Binding produced before relocation.
6. WHEN a pre-existing saved workflow references `digital_input`, THE Workflow_Designer SHALL
   load and render the node under the Triggers section without modifying the stored workflow
   definition.
7. WHEN a pre-existing saved workflow referencing `digital_input` is revalidated, THE
   Workflow_Validator SHALL produce a finding set equivalent to the pre-relocation outcome
   for that workflow.

### Requirement 3: Add the Unified Input node (coexisting with existing sources)

**User Story:** As a workflow author, I want one configurable Input node that can represent any
camera or folder source, so that the palette is simpler while existing source nodes keep working.

#### Acceptance Criteria

1. THE Node_Type_Catalog SHALL define a `Unified_Input_Node` descriptor in
   `CATEGORY_INPUT` with a `source_kind` enum parameter whose allowed values are
   `csi_camera`, `icam`, `aravis_camera`, and `folder`.
2. THE four existing source descriptors (`csi_camera_source`, `icam_source`,
   `aravis_camera_source`, `folder_source`) SHALL be retained unchanged in the catalog.
3. THE `source_kind` enum SHALL NOT offer digital input as a selectable value.
4. THE Unified_Input_Node SHALL expose, for the selected `source_kind`, the same parameters
   (names, types, defaults, and constraints) that the corresponding existing source
   descriptor exposes.
5. THE Unified_Input_Node SHALL emit exactly one `PORT_TYPE_VIDEO_FRAMES` output port,
   matching the output of the existing source descriptors.
6. FOR ALL architectures, THE Device_Binding compiled for a Unified_Input_Node with a given
   `source_kind` SHALL be identical to the Device_Binding compiled for the corresponding
   existing source descriptor with equivalent parameter values.
7. THE Unified_Input_Node SHALL declare one optional `PORT_TYPE_EVENT_SIGNAL`
   Activation_Input_Port.
8. WHEN the Activation_Input_Port is unconnected, THE Unified_Input_Node SHALL compile and
   run identically to the corresponding existing source (always-running behavior).
9. THE Activation_Input_Port SHALL be designer and validator scaffolding only; THE compiler
   SHALL NOT emit any trigger-driven activation binding for it in this feature.

### Requirement 4: Validator stage-ordering legality

**User Story:** As a workflow author, I want the validator to reject illegal Trigger/Input
orderings, so that graphs conform to the Trigger → Input → downstream stage model.

#### Acceptance Criteria

1. THE Workflow_Validator SHALL enforce the stage ordering Trigger → Input → (existing
   preprocessing / inference / post-processing / output stages).
2. IF a graph wires a `CATEGORY_TRIGGER` node downstream of a `CATEGORY_INPUT` node, THEN THE
   Workflow_Validator SHALL produce an error finding identifying the offending connection.
3. IF a graph wires a non-trigger source (`CATEGORY_INPUT` node) upstream of a
   `CATEGORY_TRIGGER` node, THEN THE Workflow_Validator SHALL produce an error finding
   identifying the offending connection.
4. WHERE a `CATEGORY_TRIGGER` node connects to a Unified_Input_Node Activation_Input_Port via
   `PORT_TYPE_EVENT_SIGNAL`, THE Workflow_Validator SHALL accept the connection as legal
   stage ordering.
5. WHEN a graph contains no `CATEGORY_TRIGGER` node, THE Workflow_Validator SHALL produce the
   same finding set it produces today for that graph (no new findings from the stage-ordering
   check).
6. THE Workflow_Validator SHALL continue to run every existing check (`V1`–`V6`, `W1`) without
   short-circuiting and SHALL return the complete list of findings.

### Requirement 5: Frontend designer support

**User Story:** As a workflow author, I want the designer to present Triggers, configure the
unified node, and wire activation ports, so that I can build the new node shapes visually.

#### Acceptance Criteria

1. THE Workflow_Designer SHALL render a Triggers Node_Palette section ordered before the
   Inputs section.
2. WHEN a saved workflow references `digital_input`, THE Workflow_Designer SHALL render the
   node under the Triggers section without mutating the stored workflow definition.
3. WHEN a workflow author selects a `source_kind` on a Unified_Input_Node, THE Workflow_Designer
   SHALL display only the parameters that apply to the selected `source_kind`.
4. THE Workflow_Designer SHALL render the Unified_Input_Node Activation_Input_Port and SHALL
   allow wiring a `CATEGORY_TRIGGER` node output to that port.
5. WHEN the Workflow_Designer displays inline validation, THE displayed findings SHALL match
   the Workflow_Validator findings for the same graph, including stage-ordering findings.

### Requirement 6: Backward compatibility invariants

**User Story:** As an operator of existing deployments, I want current workflows to behave
exactly as before, so that adding the Triggers scaffolding introduces no regressions.

#### Acceptance Criteria

1. FOR ALL Zero_Trigger_Workflows, THE compiled and packaged artifacts SHALL be byte-identical
   to the artifacts produced before this feature.
2. FOR ALL Zero_Trigger_Workflows, THE run behavior SHALL be identical to the pre-feature run
   behavior.
3. WHEN an existing saved workflow referencing `digital_input`, `folder_source`,
   `csi_camera_source`, `icam_source`, or `aravis_camera_source` is loaded, THE system SHALL
   load, validate, compile, and run it unchanged.
4. THE existing `mqtt_publish` and `opcua_write` OUTPUT node descriptors SHALL remain unchanged.
5. THE portal catalog copy and the device-vendored catalog copy SHALL remain byte-identical
   after all changes in this feature.

## Correctness properties (for property-based testing)

The following properties formalize the backward-compatibility and behavior-preservation
invariants and are suitable for property-based tests over generated workflow graphs.

- **P1 — Zero-trigger byte-identical (Round-trip / Invariant).** For all generated
  Zero_Trigger_Workflows, compiling and packaging with this feature produces artifacts
  byte-identical to the pre-feature baseline. (Requirements 6.1, 6.2)
- **P2 — digital_input relocation is behavior-preserving (Metamorphic).** For all
  architectures and all valid `digital_input` parameter combinations, the compiled
  Device_Binding after relocation equals the Device_Binding before relocation.
  (Requirements 2.5, 2.7)
- **P3 — Unified node compiles to the underlying source binding (Model-based / Metamorphic).**
  For every `source_kind` and every equivalent parameter set, the Unified_Input_Node compiles
  to the same Device_Binding as the corresponding existing source descriptor.
  (Requirement 3.6)
- **P4 — Unconnected activation port is inert (Invariant).** For all Unified_Input_Node
  configurations with an unconnected Activation_Input_Port, the compiled output equals the
  corresponding always-running source output; no activation binding is emitted.
  (Requirements 3.8, 3.9)
- **P5 — Validator stage-ordering legality (Invariant / Error-condition).** For all generated
  graphs: a Trigger downstream of an Input, or a non-trigger source upstream of a Trigger,
  always yields an error finding; legal Trigger → Input → downstream orderings never yield a
  stage-ordering error. (Requirements 4.1–4.4)
- **P6 — Validator zero-trigger equivalence (Metamorphic).** For all generated
  Zero_Trigger_Workflows, the finding set from the extended validator equals the finding set
  from the pre-feature validator. (Requirements 4.5, 2.7)
- **P7 — Catalog copies stay in sync (Invariant).** The portal catalog copy and the
  device-vendored catalog copy are byte-identical. (Requirements 1.2, 6.5)
