# Workflow Triggers + Input Overhaul — Design Assessment (future todo)

> Status: DOCUMENTATION / NOT SCHEDULED. Architecture analysis + open questions. This is the largest workflow-model change since the node catalog was introduced; it touches the designer, catalog, validator, compiler, packager, device workflow engine, and the Greengrass recipe accessControl. Treat the "Open design decisions" section as a required pre-design review before scheduling. **Update 2026-08-04:** open decisions #1 (concurrency/debounce policy) and #7 (OPC UA subscription vs polling) are now RESOLVED by the user — see the resolutions inline below and requirements criteria 2.5, 3.4, 3.5. Decisions 2–6 and 8 remain open.

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

This generalizes the existing digital-input executor path (which already turns a GPIO edge into work) into a uniform trigger→activation mechanism, with the camera/folder sources becoming *activatable* rather than only free-running. The subscription/listener layer applies each trigger's `concurrency_policy` (default `queue`) when enqueuing Run_Activations, and the OPC UA listener embeds the subscription-with-polling-fallback mechanism (resolved decision #7).

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

## Open design decisions (resolve before scheduling)

1. **Concurrency / debounce policy** — **RESOLVED (user, 2026-08-04)**: each `mqtt_subscribe`/`opcua_subscribe` trigger exposes a per-trigger `concurrency_policy` enum parameter — `queue` (DEFAULT), `drop`, `debounce` (with a gated debounce-interval companion parameter), `concurrent` — governing Run_Activation when the trigger fires while a run it activated is still in flight. See requirements criteria 2.5 and 3.4.
2. **Multiple triggers, one workflow**: AND vs OR semantics? Most likely OR (any trigger activates), but a future AND/correlation is worth leaving room for. Also: can two triggers feed different inputs in one workflow (multiple activation subgraphs)?
3. **Retain vs replace source node types**: does Unified_Input_Node *replace* the four source descriptors (migration of saved graphs) or coexist as a designer convenience that compiles to the same bindings? Migration cost vs palette clarity.
4. **Always-running vs triggered inputs in the same workflow**: is a mixed model allowed, or is a workflow wholly one or the other? Cleaner to make "has ≥1 trigger ⇒ fully trigger-driven."
5. **Subscription lifecycle & reconnect**: how do MQTT/OPC UA subscriptions behave across LocalServer restarts, broker disconnects, and OPC UA session drops? Reconnect/backoff policy and health surfacing (ties into edge-deploy-reliability's health model).
6. **Trigger_Context → input override contract**: which input parameters are overridable per activation (folder path, file selection, capture count, camera selection)? Define the whitelist and validation.
7. **OPC UA subscription vs polling** — **RESOLVED (user, 2026-08-04)**: spike true subscriptions (monitored items) FIRST; the runtime falls back to periodic polling at a configurable interval when subscriptions are unavailable or unreliable on the edge device. Trigger_Context is identical under either mechanism, and the active mechanism (including any fallback transition) is logged and surfaced. See criterion 3.5. Note the Phase 0.2 spike still validates the subscription path — the resolution removes the either/or ambiguity by mandating BOTH mechanisms with subscription preferred.
8. **Backpressure & ordering guarantees** for high-rate MQTT topics.

## Testing strategy (high level)

- Catalog/compiler/validator unit + property tests in the workflow_core suites (both copies in sync); stage-ordering legality as properties.
- Device runtime: stubbed subscription layer (injectable, like the injectable invokers/publishers already used) for MQTT/OPC UA; Trigger_Context fidelity and activation-isolation properties; digital-input-move regression (byte-identical behavior).
- Packaging: recipe accessControl now includes subscribe authorization — extend the greengrass recipe/accessControl tests.
- Simulation stubs for each trigger.
- On-hardware: MQTT-subscribe activation via Greengrass IPC and OPC UA subscription on a real device (gated, rides a LocalServer build).

## Effort shape

Large. Roughly: catalog+validator+designer (portal, multi-week), compiler+packager+recipe accessControl (portal), device runtime subscription/activation layer (the hard, novel part — new long-lived listener + run-activation model in the Workflow_Engine, device build), plus simulation and on-hardware validation. Recommend splitting into at least three schedulable specs once the open decisions are resolved: (A) Triggers category + digital-input move + unified input (portal + designer, no new device runtime beyond recategorization), (B) MQTT/OPC UA subscribe device runtime + activation model, (C) Trigger_Transform wiring. See tasks.md for the phased outline.
