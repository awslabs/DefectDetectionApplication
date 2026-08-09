# Workflow Triggers + Input Overhaul — Design Assessment (future todo)

> Status: DOCUMENTATION / NOT SCHEDULED. Architecture analysis + open questions. This is the largest workflow-model change since the node catalog was introduced; it touches the designer, catalog, validator, compiler, packager, device workflow engine, and the Greengrass recipe accessControl. The "Open design decisions" section was a required pre-design review before scheduling. **Update 2026-08-04: ALL 8 open decisions are now RESOLVED by the user** — see the resolutions inline below and the corresponding firm requirements criteria (1.5, 1.6, 2.5, 2.9, 2.10, 3.4, 3.5, 3.6, 3.7, 3.8, 5.5). A firm MQTT transport-parity requirement (greengrass / aws_iot / plain broker, mirroring `mqtt_publish`) was also added as requirements criteria 2.1, 2.6–2.8. Phase 0.1 (decision resolution) is complete; only the Phase 0.2 OPC UA subscription spike remains before sub-feature B can be scheduled.

## Current state (grounded)

- **Catalog** (`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`, mirrored to `src/backend/workflow_engine/vendor/...`): input sources are `csi_camera_source`, `icam_source`, `aravis_camera_source`, `folder_source` (all emit `PORT_TYPE_VIDEO_FRAMES` via GStreamer element chains) and `digital_input` (emits `PORT_TYPE_EVENT_SIGNAL`, executor-level binding, hardware-dependent).
- **MQTT / OPC UA today are OUTPUT-only**: `mqtt_publish` (with a zero-config `greengrass` path using Greengrass IPC `PublishToIoTCore`, plus `aws_iot` mutual-TLS and plain-broker paths) and `opcua_write` (anonymous/user-token/cert security). The device output processor (`src/backend/workflow_engine/output_bindings.py`) drives them. Recipe accessControl authorizes `aws.greengrass#PublishToIoTCore` for the publish topic.
- **Run model**: source-driven. The pipeline executor stages a source and runs the compiled GStreamer pipeline; `digital_input` is the one event-shaped entry today (executor binding, appsrc sim stub).
- **Custom code** nodes already have packaging, sandbox execution, and code-assist — reusable for Trigger_Transform.

## Target architecture

Introduce a **Triggers** stage before Inputs. Conceptually the workflow gains an *activation graph* (trigger → optional transform → input) in front of the existing *dataflow graph* (input → preprocessing → inference → output).

### Components to add

1. **`CATEGORY_TRIGGER`** in the shared catalog constants; designer palette section ordered before Inputs.
2. **`mqtt_subscribe` trigger** — mirror `mqtt_publish`'s parameter surface (greengrass/aws_iot/plain), but subscribe. Device side: a long-lived subscription (Greengrass IPC `SubscribeToIoTCore`) whose message callback enqueues a Run_Activation carrying Trigger_Context.
3. **`opcua_subscribe` trigger** — mirror `opcua_write`'s endpoint/security surface; device side creates an OPC UA subscription + monitored item; data-change notification enqueues a Run_Activation.
4. **`digital_input` recategorized** to `CATEGORY_TRIGGER` (metadata-only move; behavior identical).
5. **Trigger_Transform** — a custom-code node whose I/O contract is Trigger_Context in → input-parameter-overrides out; reuses custom-node runtime.
6. **`Unified_Input_Node`** — `source_kind` enum selecting camera/aravis/folder/csi; parameters gate on selection; optional activation input port; emits video frames.

### Device runtime: the central change

The biggest shift is the **Workflow_Engine run model** on device. Today a deployed workflow runs its pipeline continuously (or per source semantics). Trigger-driven activation requires a **long-lived subscription/listener layer** that:
- starts the MQTT/OPC UA subscriptions (and the existing GPIO poll) when the workflow component starts,
- on a trigger firing, builds Trigger_Context, optionally runs the Trigger_Transform to compute input overrides, then activates ONE pipeline run of the downstream input+dataflow graph with those overrides,
- serializes or bounds concurrent activations (debounce/queue policy — see open questions),
- surfaces subscription/auth failures as actionable run/component errors.

This generalizes the existing digital-input executor path (which already turns a GPIO edge into work) into a uniform trigger→activation mechanism, with the camera/folder sources becoming *activatable* rather than only free-running. The subscription/listener layer applies each trigger node's own `concurrency_policy` (default `queue`, bounded by `queue_depth`; `drop`; or `debounce` at `debounce_ms`) when enqueuing Run_Activations, and the OPC UA listener implements the `mode` parameter (`subscribe` default | `poll`) with auto-fallback to polling surfaced in run/component health (resolved decision #7). Multiple triggers activate on OR semantics — any firing activates a run — with the activation model reserving an explicit **extension point for future AND/correlation semantics**: the Run_Activation data model leaves room for an *activation-group* (correlation) concept, so a workflow-level or trigger-group AND can be added later without changing OR workflows' behavior; AND is NOT implemented in sub-feature B (resolved decision #2). A workflow with ≥1 trigger is wholly trigger-driven and the validator rejects mixed triggered/always-running graphs with an actionable finding (resolved decision #4); pending queued activations across triggers are dequeued by per-trigger `priority` (lower value = higher priority, catalog-style default 100; ties FIFO by firing time; no further global ordering guarantee — resolved decision #8); and subscriptions auto-reconnect with backoff up to a per-node `retry_limit` (0 = retry forever, the default sentinel), surfacing reconnect state in health and marking the trigger failed with an actionable error naming the trigger node and endpoint/topic on exhaustion (resolved decision #5). Per activation, Trigger_Transform overrides may target ANY downstream input parameter, validated against the input descriptor's constraints; invalid overrides fail that activation only (resolved decision #6).

### Portal side

- Catalog: new category + 3 trigger descriptors + unified input; both copies (portal layer + device vendored) byte-in-sync (the established discipline).
- Validator: stage-ordering rules (Req 1.3), trigger/transform/input wiring legality, per-arch support.
- Compiler: emit trigger bindings (executor-level, like digital_input today) + the activation edges; the input's GStreamer chain is unchanged but gains parameter-override injection points for the Trigger_Transform outputs.
- Packager: recipe accessControl now must include **subscribe** authorization (`aws.greengrass#SubscribeToIoTCore`) for mqtt_subscribe topics, alongside the existing publish authorization; per-arch workflow artifacts carry the trigger bindings.
- Frontend designer: new palette section, the activation-port wiring UX, unified-input `source_kind` gating, and rendering saved `digital_input` under Triggers without mutating the stored graph.

## Correctness properties (to formalize at design time)

- **P1 Backward compatibility**: a workflow with zero triggers produces byte-identical compiled/packaged artifacts and identical run behavior to pre-feature (excluding new-node code paths). Saved graphs with `digital_input`/sources load and revalidate identically.
- **P2 Activation isolation**: one trigger firing produces exactly one Run_Activation; a Trigger_Transform error fails that activation only and never starts the input, and never affects concurrent/subsequent activations.
- **P3 Authorization truthfulness**: an mqtt_subscribe on a topic the recipe accessControl does not authorize fails with an actionable error naming the topic and the recipe location (mirrors the publish-denial diagnostics already implemented).
- **P4 Context fidelity**: Trigger_Context delivered to the Trigger_Transform / input matches the received event (topic+payload / node+value / pin+edge) with no cross-activation leakage.
- **P5 Simulation parity**: each trigger has a harness-fed stub; a triggered workflow simulates without a real broker/OPC UA server, like digital_input today.

## Open design decisions — ALL 8 RESOLVED (2026-08-04)

1. **Concurrency / debounce policy** — **RESOLVED (user, 2026-08-04)**: the policy is configurable **per trigger node** — each `mqtt_subscribe`/`opcua_subscribe` node instance carries its own `concurrency_policy` enum parameter (`queue` DEFAULT | `drop` | `debounce`) with gated companion parameters `debounce_ms` (int, gated on `debounce`) and a bounded `queue_depth` (int, gated on `queue`), catalog-convention defaults. `digital_input` keeps its existing behavior; adding the same parameters to it later is a follow-on decision, not required now. See requirements criteria 2.5 and 3.4.
2. **Multiple triggers, one workflow** — **RESOLVED (user, 2026-08-04)**: **OR semantics** — any trigger firing activates a run. The activation model (data model and runtime) RESERVES ROOM for future AND/correlation semantics — an *activation-group* concept noted as an extension point in the target architecture — so a workflow-level or trigger-group correlation can be added later without breaking OR workflows. AND is NOT implemented in sub-feature B. See requirements criterion 1.5.
3. **Retain vs replace source node types** — **RESOLVED (shipped)**: COEXIST — Unified_Input_Node coexists with the four source descriptors as a designer convenience compiling to the same bindings; delivered by `.kiro/specs/triggers-stage-and-unified-input/` (sub-feature A).
4. **Always-running vs triggered inputs in the same workflow** — **RESOLVED (user, 2026-08-04)**: **only one model at a time per workflow** — a workflow is either wholly trigger-driven (has ≥1 trigger) or wholly source-driven (zero triggers). Mixed always-running + triggered inputs in the same workflow are REJECTED by the Workflow_Validator with an actionable finding. See requirements criterion 1.6.
5. **Subscription lifecycle & reconnect** — **RESOLVED (user, 2026-08-04)**: **automatic reconnect with a configurable retry limit** — the device runtime auto-reconnects dropped MQTT/OPC UA subscriptions with backoff; each trigger node exposes a `retry_limit` int parameter (catalog-convention default; documented sentinel `0` = retry forever, the default). Exhausting the limit surfaces as an actionable component/run health error naming the trigger node and endpoint/topic. Ties into edge-deploy-reliability's health model. See requirements criteria 2.9 and 3.7.
6. **Trigger_Context → input override contract** — **RESOLVED (user, 2026-08-04)**: **any input parameter is overridable** — NO whitelist. The Trigger_Transform MAY override ANY parameter of the downstream input node; validation happens per activation against the input descriptor's declared parameter constraints (same check_parameter_value rules as the designer), and invalid overrides fail that activation only (consistent with criterion 5.4). See requirements criterion 5.5.
7. **OPC UA subscription vs polling** — **RESOLVED (user, 2026-08-04)**: spike TRUE subscriptions (monitored items / data-change notifications) FIRST; IF the spike shows the packaged `opcua` client cannot do reliable subscriptions at the edge, FALL BACK to polling. Regardless of spike outcome, the `opcua_subscribe` node supports a polling fallback mode via a `mode` enum parameter (`subscribe` DEFAULT | `poll`, with `poll_interval_ms` gated on `poll`); the device runtime MAY auto-fall-back to polling on subscription failure, surfacing the fallback in run/component health. Trigger_Context is identical under either mechanism. See criteria 3.5 and 3.6. Note the Phase 0.2 spike still validates the subscription path — the resolution removes the either/or ambiguity by making the polling fallback a REQUIRED node capability either way, with subscription preferred.
8. **Backpressure & ordering guarantees** — **RESOLVED (user, 2026-08-04)**: **per-trigger priority** — each trigger node exposes a `priority` int parameter (documented convention: lower value = higher priority; catalog-style default `100`). When multiple activations are queued (per each node's `concurrency_policy`), the runtime dequeues by priority, then FIFO by firing time within equal priority. The user decides relative importance via this parameter; NO additional global ordering guarantee is promised across triggers beyond priority-then-FIFO. See requirements criteria 2.10 and 3.8.

## Testing strategy (high level)

- Catalog/compiler/validator unit + property tests in the workflow_core suites (both copies in sync); stage-ordering legality as properties.
- Device runtime: stubbed subscription layer (injectable, like the injectable invokers/publishers already used) for MQTT/OPC UA; Trigger_Context fidelity and activation-isolation properties; digital-input-move regression (byte-identical behavior).
- Packaging: recipe accessControl now includes subscribe authorization — extend the greengrass recipe/accessControl tests.
- Simulation stubs for each trigger.
- On-hardware: MQTT-subscribe activation via Greengrass IPC and OPC UA subscription on a real device (gated, rides a LocalServer build).

## Effort shape

Large. Roughly: catalog+validator+designer (portal, multi-week), compiler+packager+recipe accessControl (portal), device runtime subscription/activation layer (the hard, novel part — new long-lived listener + run-activation model in the Workflow_Engine, device build), plus simulation and on-hardware validation. Recommend splitting into at least three schedulable specs once the open decisions are resolved: (A) Triggers category + digital-input move + unified input (portal + designer, no new device runtime beyond recategorization), (B) MQTT/OPC UA subscribe device runtime + activation model, (C) Trigger_Transform wiring. See tasks.md for the phased outline.
