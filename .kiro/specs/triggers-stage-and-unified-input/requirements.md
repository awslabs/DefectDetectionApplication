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
- **Legacy_Source_Node**: any of the four retained source descriptors
  (`csi_camera_source`, `icam_source`, `aravis_camera_source`, `folder_source`).
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

1. THE Node_Type_Catalog SHALL define a `CATEGORY_TRIGGER` category constant with the string
   value `"trigger"` in `catalog/models.py`, alongside the existing category constants
   (`CATEGORY_INPUT`, `CATEGORY_PREPROCESSING`, `CATEGORY_INFERENCE`,
   `CATEGORY_POST_PROCESSING`, `CATEGORY_OUTPUT`).
2. THE Node_Type_Catalog SHALL include `CATEGORY_TRIGGER` in the `CATEGORIES` tuple,
   positioned immediately before `CATEGORY_INPUT`, with the relative order of all existing
   entries unchanged.
3. THE portal catalog copy
   (`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/models.py`)
   and the device-vendored catalog copy
   (`src/backend/workflow_engine/vendor/workflow_core/catalog/models.py`) SHALL remain
   byte-identical after the `CATEGORY_TRIGGER` addition.
4. THE frontend catalog mirror (`edge-cv-portal/frontend/src/pages/workflows/types.ts`)
   SHALL define a `CATEGORY_TRIGGER` constant with the same string value (`'trigger'`) as
   the backend constant and SHALL include it in its `CATEGORIES` list at the same relative
   position (immediately before `CATEGORY_INPUT`), with the relative order of all existing
   entries unchanged.
5. THE Workflow_Designer SHALL present a Triggers Node_Palette section, ordered before the
   Inputs section, containing exactly the node types whose descriptor declares
   `category=CATEGORY_TRIGGER` and no node types of any other category.
6. WHEN the Node_Palette renders, THE Workflow_Designer SHALL preserve the existing relative
   ordering of the Inputs, Preprocessing, Inference, Post-processing, and Output sections.

### Requirement 2: Relocate digital_input to Triggers (metadata-only, behavior-preserving)

**User Story:** As a workflow author, I want the existing digital_input node listed under
Triggers, so that its event-shaped role is represented correctly without changing how it runs.

#### Acceptance Criteria

1. THE `digital_input` descriptor SHALL declare `category=CATEGORY_TRIGGER` in place of
   `CATEGORY_INPUT` in both the portal catalog copy and the device-vendored catalog copy of
   the Node_Type_Catalog.
2. THE relocated `digital_input` descriptor SHALL retain identical parameters: `pin`
   (int, required, no default, constraints min 0 / max 255), `trigger_edge` (enum, optional,
   default `rising`, allowed values `rising`, `falling`, `both`), and `poll_interval_ms`
   (int, optional, default 100, constraints min 10 / max 60000), with parameter descriptions
   and examples unchanged.
3. THE relocated `digital_input` descriptor SHALL retain exactly one output port of type
   `PORT_TYPE_EVENT_SIGNAL` and zero input ports, unchanged from the pre-relocation
   descriptor.
4. THE relocated `digital_input` descriptor SHALL retain its device-architecture
   `executor_binding="digital_input"` mappings, its `ARCH_SIM` appsrc simulation stub, and
   its `hardware_dependent=True` flag unchanged, such that the `category` field is the only
   descriptor field that differs from the pre-relocation descriptor.
5. FOR ALL architectures (every device architecture and `ARCH_SIM`) and FOR ALL parameter
   values valid under the constraints in criterion 2, THE compiled Device_Binding produced
   for a `digital_input` node after relocation SHALL be identical to the Device_Binding
   produced before relocation.
6. WHEN a pre-existing saved workflow references `digital_input`, THE Workflow_Designer SHALL
   resolve the node's category from the Node_Type_Catalog at render time and SHALL render the
   node under the Triggers section, leaving the stored workflow definition byte-identical
   (no category or other field is written to the stored definition).
7. WHEN a pre-existing saved workflow referencing `digital_input` is revalidated, THE
   Workflow_Validator SHALL produce a finding set identical to the pre-relocation finding set
   for that workflow (same check codes, same severities, same referenced node and connection
   identifiers).
8. THE Node_Palette SHALL list `digital_input` under the Triggers section only and SHALL NOT
   list it under the Inputs section.

### Requirement 3: Add the Unified Input node (coexisting with existing sources)

**User Story:** As a workflow author, I want one configurable Input node that can represent any
camera or folder source, so that the palette is simpler while existing source nodes keep working.

#### Acceptance Criteria

1. THE Node_Type_Catalog SHALL define a `Unified_Input_Node` descriptor in `CATEGORY_INPUT`
   with a required `source_kind` enum parameter whose allowed values are `csi_camera`,
   `icam`, `aravis_camera`, and `folder`, and whose default value is `csi_camera`.
2. THE four existing source descriptors (`csi_camera_source`, `icam_source`,
   `aravis_camera_source`, `folder_source`) SHALL be retained in the catalog with identical
   type_id, category, parameters, and per-architecture Device_Binding mappings, and with
   identical output ports, plus the single optional activation input port added by
   Requirement 7. *(amended 2026-08-04 — see Requirement 7)*
3. THE `source_kind` enum SHALL NOT offer digital input as a selectable value.
4. THE Unified_Input_Node descriptor SHALL declare the union of the four existing source
   descriptors' parameters, each tagged with the `source_kind` values it applies to, such
   that for each `source_kind` value the applicable parameter subset equals, in names,
   types, defaults, and constraints, the parameters of the corresponding existing source
   descriptor.
5. THE Unified_Input_Node SHALL emit exactly one `PORT_TYPE_VIDEO_FRAMES` output port,
   matching the output of the existing source descriptors.
6. FOR ALL architectures for which the corresponding existing source descriptor defines a
   Device_Binding, THE Device_Binding compiled for a Unified_Input_Node with a given
   `source_kind` SHALL be identical to the Device_Binding compiled for the corresponding
   existing source descriptor with equivalent parameter values.
7. WHERE the corresponding existing source descriptor defines no Device_Binding for an
   architecture, THE Unified_Input_Node with that `source_kind` SHALL be unsupported on
   that architecture in the same way.
8. THE Unified_Input_Node SHALL declare one optional `PORT_TYPE_EVENT_SIGNAL`
   Activation_Input_Port that accepts zero or one connection.
9. WHEN the Activation_Input_Port is unconnected, THE Unified_Input_Node SHALL compile and
   run identically to the corresponding existing source (always-running behavior).
10. WHEN the Activation_Input_Port is connected to a `CATEGORY_TRIGGER` node output, THE
    compiled output SHALL be identical to the unconnected case (the port is inert in this
    feature).
11. THE compiler SHALL NOT emit any trigger-driven activation binding for the
    Activation_Input_Port, regardless of connection state.

### Requirement 4: Validator stage-ordering legality

**User Story:** As a workflow author, I want the validator to reject illegal Trigger/Input
orderings, so that graphs conform to the Trigger → Input → downstream stage model.

#### Acceptance Criteria

1. WHEN `validate` is called on a graph, THE Workflow_Validator SHALL evaluate a
   stage-ordering check against every connection in the graph, enforcing the ordering
   Trigger → Input → (existing preprocessing / inference / post-processing / output stages)
   as defined by criteria 4.2–4.4.
2. IF a connection's source endpoint is a port on a `CATEGORY_INPUT` node and its target
   endpoint is a port on a `CATEGORY_TRIGGER` node, THEN THE Workflow_Validator SHALL
   produce one error finding for that connection carrying the connection's identifier.
3. IF a connection's source endpoint is a port on a node of any non-`CATEGORY_TRIGGER`
   category and its target endpoint is a port on a `CATEGORY_TRIGGER` node, THEN THE
   Workflow_Validator SHALL produce one error finding for that connection carrying the
   connection's identifier, so that a `CATEGORY_TRIGGER` node downstream of any
   non-trigger node (directly or via intermediate nodes) is reported at each offending
   connection into the trigger node.
4. IF a connection joins a `CATEGORY_TRIGGER` node `PORT_TYPE_EVENT_SIGNAL` output port to
   a Unified_Input_Node Activation_Input_Port, THEN THE Workflow_Validator SHALL produce
   zero stage-ordering findings for that connection.
5. WHEN a Zero_Trigger_Workflow is validated, THE Workflow_Validator SHALL return a
   finding set equal to the finding set the pre-feature validator returns for the same
   graph (same finding codes, severities, and associated node/connection identifiers),
   with zero findings carrying the stage-ordering check code.
6. WHEN `validate` is called, THE Workflow_Validator SHALL run every existing check
   (`V1`–`V6`, `W1`) and the stage-ordering check without short-circuiting and SHALL
   return the complete list of findings from all checks in a single result.
7. THE stage-ordering check SHALL report every stage-ordering finding under exactly one
   new stable finding code following the existing check-code convention (the next
   available `V` code, e.g. `V7`) with severity error, and SHALL NOT alter the codes or
   severities of any existing check's findings.

### Requirement 5: Frontend designer support

**User Story:** As a workflow author, I want the designer to present Triggers, configure the
unified node, and wire activation ports, so that I can build the new node shapes visually.

#### Acceptance Criteria

1. THE Workflow_Designer SHALL render a Triggers Node_Palette section that contains every
   `CATEGORY_TRIGGER` node descriptor in the frontend catalog mirror and is ordered before
   the Inputs section.
2. WHEN a saved workflow referencing `digital_input` is loaded, THE Workflow_Designer SHALL
   render the `digital_input` node under the Triggers section.
3. WHEN a workflow author saves a loaded workflow without making edits, THE Workflow_Designer
   SHALL persist a workflow definition identical to the loaded definition.
4. WHEN a workflow author selects or changes the `source_kind` on a Unified_Input_Node, THE
   Workflow_Designer SHALL keep the `source_kind` parameter visible and SHALL display only
   the parameters of the existing source descriptor corresponding to the selected
   `source_kind`, with the same names, types, defaults, and constraints as that descriptor.
5. THE Workflow_Designer SHALL render the Unified_Input_Node Activation_Input_Port as an
   optional `PORT_TYPE_EVENT_SIGNAL` input port and SHALL allow wiring a `CATEGORY_TRIGGER`
   node `PORT_TYPE_EVENT_SIGNAL` output to that port.
6. IF a workflow author attempts to wire a node output whose port type is not
   `PORT_TYPE_EVENT_SIGNAL` to the Activation_Input_Port, THEN THE Workflow_Designer SHALL
   reject the connection and SHALL leave the workflow graph unchanged.
7. WHEN the workflow graph is mutated (a node or connection is added or removed, or a node
   parameter is changed), THE Workflow_Designer SHALL display inline validation findings that
   identify the same offending node or connection, the same severity, and the same check code
   as the Workflow_Validator findings for that graph, including stage-ordering findings.
8. IF a Unified_Input_Node Activation_Input_Port is unconnected, THEN THE Workflow_Designer
   SHALL display no inline validation finding for that port.

### Requirement 6: Backward compatibility invariants

**User Story:** As an operator of existing deployments, I want current workflows to behave
exactly as before, so that adding the Triggers scaffolding introduces no regressions.

#### Acceptance Criteria

1. FOR ALL Zero_Trigger_Workflows and FOR ALL supported device architectures, THE compiled
   and packaged artifacts (`compiled_pipeline.json`, `workflow.json`, `manifest.json`, and
   the Greengrass recipe emitted by `workflow_packaging.py`) SHALL be byte-identical to the
   artifacts produced from the pre-feature baseline for the same workflow definition and
   equivalent parameter values.
2. FOR ALL Zero_Trigger_Workflows, THE device runtime SHALL execute the same Device_Bindings
   and SHALL emit the same outputs at each OUTPUT node for equivalent input sequences as the
   pre-feature runtime.
3. WHEN an existing saved workflow referencing `digital_input`, `folder_source`,
   `csi_camera_source`, `icam_source`, or `aravis_camera_source` is loaded, THE system SHALL
   load and render it without modifying the stored workflow definition.
4. THE existing `mqtt_publish` and `opcua_write` OUTPUT node descriptors SHALL remain
   byte-identical in both catalog copies, retaining the same parameters (names, types,
   defaults, and constraints), ports, category, and per-architecture bindings.
5. THE portal catalog copy and the device-vendored catalog copy SHALL remain byte-identical
   after all changes in this feature.
6. WHEN an existing saved workflow referencing `digital_input`, `folder_source`,
   `csi_camera_source`, `icam_source`, or `aravis_camera_source` is validated, THE
   Workflow_Validator SHALL produce a finding set equivalent to the pre-feature finding set
   for that workflow.
7. WHEN an existing saved workflow referencing `digital_input`, `folder_source`,
   `csi_camera_source`, `icam_source`, or `aravis_camera_source` is compiled and packaged,
   THE resulting artifacts (`compiled_pipeline.json`, `workflow.json`, `manifest.json`, and
   the Greengrass recipe emitted by `workflow_packaging.py`) SHALL be byte-identical, on
   every supported architecture, to the pre-feature artifacts for the same workflow
   definition.
8. WHEN the catalog baseline (`catalog_baseline.json`, verified by
   `test_catalog_content.py`) is updated for this feature, THE updated baseline SHALL differ
   from the pre-feature baseline only in the `CATEGORY_TRIGGER` addition, the
   `digital_input` category value, the `Unified_Input_Node` descriptor addition, and the
   four Legacy_Source_Node activation input port declarations added by Requirement 7, with
   all other descriptor entries byte-identical. *(amended 2026-08-04 — see Requirement 7)*

### Requirement 7: Activation ports on legacy source nodes (amendment 2026-08-04)

**User Story:** As a workflow author, I want the four existing source nodes to expose the
same optional activation input port as the Unified_Input_Node, so that I can wire trigger
scaffolding to any source node today without changing how any workflow compiles or runs.

#### Acceptance Criteria

1. THE Node_Type_Catalog SHALL declare, on each of the four Legacy_Source_Node descriptors
   (`csi_camera_source`, `icam_source`, `aravis_camera_source`, `folder_source`), exactly
   one optional `PORT_TYPE_EVENT_SIGNAL` input port named `activation` that accepts zero or
   one connection, with semantics identical to the Unified_Input_Node
   Activation_Input_Port.
2. WHEN a workflow graph containing a connection targeting a Legacy_Source_Node
   `activation` port is compiled, THE compiler SHALL drop that connection before mapping
   resolution and SHALL emit zero trigger-driven activation bindings for that port,
   regardless of connection state.
3. FOR ALL Legacy_Source_Node configurations, whether the `activation` port is unconnected
   or connected to a `CATEGORY_TRIGGER` node output, THE compiled output SHALL be identical
   to the pre-amendment compiled output for the same workflow graph with the activation
   connection removed.
4. THE Workflow_Designer SHALL render the Legacy_Source_Node `activation` port as an
   optional `PORT_TYPE_EVENT_SIGNAL` input port and SHALL allow wiring a `CATEGORY_TRIGGER`
   node `PORT_TYPE_EVENT_SIGNAL` output to that port, and IF a workflow author attempts to
   wire a node output whose port type is not `PORT_TYPE_EVENT_SIGNAL` to that port, THEN
   THE Workflow_Designer SHALL reject the connection and SHALL leave the workflow graph
   unchanged.
5. WHEN a graph containing a connection from a `CATEGORY_TRIGGER` node
   `PORT_TYPE_EVENT_SIGNAL` output to a Legacy_Source_Node `activation` port is validated,
   THE Workflow_Validator SHALL treat that connection as legal stage ordering and SHALL
   produce zero stage-ordering findings for that connection.
6. WHEN the catalog baseline (`catalog_baseline.json`) is updated for this amendment, THE
   baseline delta attributable to this amendment SHALL consist of exactly the four
   `activation` input port declarations on the four Legacy_Source_Node descriptors, with
   all other descriptor content unchanged.

## Correctness properties (for property-based testing)

The following properties formalize the backward-compatibility and behavior-preservation
invariants and are suitable for property-based tests over generated workflow graphs.

- **P1 — Zero-trigger byte-identical (Round-trip / Invariant).** For all generated
  Zero_Trigger_Workflows, compiling and packaging with this feature produces artifacts
  byte-identical to the pre-feature baseline. (Requirements 6.1, 6.2)
  *Note (2026-08-04): unaffected by the Requirement 7 amendment — port declarations are
  not a compile input, and a Zero_Trigger_Workflow contains no activation edges.*
- **P2 — digital_input relocation is behavior-preserving (Metamorphic).** For all
  architectures and all valid `digital_input` parameter combinations, the compiled
  Device_Binding after relocation equals the Device_Binding before relocation.
  (Requirements 2.5, 2.7)
- **P3 — Unified node compiles to the underlying source binding (Model-based / Metamorphic).**
  For every `source_kind` and every equivalent parameter set, the Unified_Input_Node compiles
  to the same Device_Binding as the corresponding existing source descriptor.
  (Requirement 3.6)
- **P4 — Activation port is inert on the unified node AND the four legacy sources
  (Invariant).** For all Unified_Input_Node and Legacy_Source_Node configurations,
  connected or not, the compiled output equals the corresponding always-running source
  output; no activation binding is emitted. *(generalized 2026-08-04)*
  (Requirements 3.9, 3.10, 3.11, 7.2, 7.3)
- **P5 — Validator stage-ordering legality (Invariant / Error-condition).** For all generated
  graphs: a Trigger downstream of an Input, or a non-trigger source upstream of a Trigger,
  always yields an error finding; legal Trigger → Input → downstream orderings never yield a
  stage-ordering error. (Requirements 4.1–4.4)
- **P6 — Validator zero-trigger equivalence (Metamorphic).** For all generated
  Zero_Trigger_Workflows, the finding set from the extended validator equals the finding set
  from the pre-feature validator. (Requirements 4.5, 2.7)
- **P7 — Catalog copies stay in sync (Invariant).** The portal catalog copy and the
  device-vendored catalog copy are byte-identical. (Requirements 1.2, 6.5)
