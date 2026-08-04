# Workflow Triggers + Input Overhaul — Phased Outline (future todo)

> Status: DOCUMENTATION / NOT SCHEDULED. Read requirements.md + design.md first. The design's "Open design decisions" MUST be resolved before Phase 1. This outline splits the work into three schedulable sub-features (A/B/C) plus a spike; each becomes its own spec when scheduled.

## Phase 0 — Design review + OPC UA subscription spike (DECISION GATE)

- [ ] 0.1 Resolve the 8 open design decisions in design.md (concurrency/debounce, multi-trigger semantics, retain-vs-replace sources, mixed model, subscription lifecycle/reconnect, override contract, OPC UA subscription vs poll, backpressure). Write the answers into requirements.md as firm criteria.
- [ ] 0.2 Spike the `opcua` client's subscription/monitored-item support on a real device (does the packaged client do true data-change subscriptions reliably at the edge, or must we poll?). Spike Greengrass IPC `SubscribeToIoTCore` message-callback behavior + subscribe accessControl.
- Exit: decisions frozen; subscription mechanism proven; sub-feature boundaries A/B/C confirmed.

## Sub-feature A — Triggers category, digital-input move, unified input (portal + designer)

- [ ] A.1 Add `CATEGORY_TRIGGER` to shared catalog constants (both copies in sync); designer palette section ordered before Inputs.
- [ ] A.2 Recategorize `digital_input` to `CATEGORY_TRIGGER` (metadata-only, behavior byte-identical); ensure saved graphs load/validate/compile/run unchanged.
- [ ] A.3 Add `Unified_Input_Node` (`source_kind` enum; gated params; optional activation input port; emits video frames). Decide retain-vs-replace of the four source descriptors per 0.1.
- [ ] A.4 Validator: stage-ordering rules (Trigger → optional Transform → Input) and wiring legality; per-arch support.
- [ ] A.5 Frontend: palette section, activation-port wiring UX, `source_kind` gating, render saved digital_input under Triggers without mutating the graph.
- [ ] A.6 Tests (workflow_core both copies, validator, frontend) + backward-compat property (zero-trigger workflows byte-identical). Portal deploy.

## Sub-feature B — MQTT/OPC UA subscribe triggers + device activation model (device runtime, the hard part)

- [ ] B.1 Catalog: `mqtt_subscribe` (mirror mqtt_publish greengrass/aws_iot/plain surface, subscribe) and `opcua_subscribe` (mirror opcua_write endpoint/security) trigger descriptors + simulation stubs.
- [ ] B.2 Device Workflow_Engine: long-lived subscription/listener layer (injectable for tests) that starts subscriptions on component start, builds Trigger_Context, and enqueues Run_Activations per the concurrency/debounce policy from 0.1; reconnect/backoff + health surfacing.
- [ ] B.3 MQTT subscribe via Greengrass IPC `SubscribeToIoTCore`; OPC UA subscription/monitored-item (or poll per 0.2).
- [ ] B.4 Packager + recipe accessControl: add `aws.greengrass#SubscribeToIoTCore` authorization for subscribed topics; actionable subscribe-denial diagnostics (mirror publish-denial).
- [ ] B.5 Compiler: emit trigger executor bindings + activation edges; input parameter-override injection points.
- [ ] B.6 Tests: stubbed subscription layer, Trigger_Context fidelity + activation-isolation + authorization-truthfulness properties; packaging accessControl tests; simulation. Rides a LocalServer device build.
- [ ] B.7 On-hardware (gated): real MQTT-subscribe activation and OPC UA subscription on device.

## Sub-feature C — Trigger_Transform (custom code between trigger and input)

- [ ] C.1 Define the Trigger_Transform I/O contract (Trigger_Context in → whitelisted input-parameter overrides out) reusing custom-node runtime/packaging/code-assist.
- [ ] C.2 Device: run the transform on activation, apply overrides to the input, fail-closed per-activation on error (no input start), never affect other activations.
- [ ] C.3 Simple_Trigger link (no transform) activates the input with static params.
- [ ] C.4 Tests + on-hardware smoke.

## Notes

- Both catalog copies must stay byte-in-sync (established discipline).
- Zero-trigger backward compatibility is a hard invariant across all phases.
- Device-side pieces (B, C) ride LocalServer builds; A is portal-only.
- This is a multi-spec effort; when scheduling, promote A/B/C to their own `.kiro/specs/` entries with full requirements/design/tasks.
