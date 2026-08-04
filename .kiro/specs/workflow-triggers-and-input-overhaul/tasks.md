# Workflow Triggers + Input Overhaul — Phased Outline (future todo)

> Status: DOCUMENTATION / NOT SCHEDULED. Read requirements.md + design.md first. The design's "Open design decisions" MUST be resolved before Phase 1. This outline splits the work into three schedulable sub-features (A/B/C) plus a spike; each becomes its own spec when scheduled.
>
> **Progress note (2026-08-04):** Sub-feature A has been **delivered** as its own spec (`.kiro/specs/triggers-stage-and-unified-input/` — portal deployed, riding the current LocalServer build). Sub-feature B is the next schedulable spec.

## Phase 0 — Design review + OPC UA subscription spike (DECISION GATE)

- [ ] 0.1 Resolve the 8 open design decisions in design.md (concurrency/debounce, multi-trigger semantics, retain-vs-replace sources, mixed model, subscription lifecycle/reconnect, override contract, OPC UA subscription vs poll, backpressure). Write the answers into requirements.md as firm criteria.
  - **RESOLVED (user, 2026-08-04): #1** concurrency/debounce — configurable **per trigger node**: `concurrency_policy` enum (`queue` default | `drop` | `debounce`) with gated `debounce_ms` and bounded `queue_depth` companions on each `mqtt_subscribe`/`opcua_subscribe` node instance; `digital_input` unchanged for now (criteria 2.5, 3.4).
  - **RESOLVED (shipped): #3** retain-vs-replace sources — COEXIST, implemented in `triggers-stage-and-unified-input` (sub-feature A).
  - **RESOLVED (user, 2026-08-04): #7** OPC UA subscription vs poll — spike true subscriptions first; polling fallback is a REQUIRED node capability either way via the `mode` parameter (`subscribe` default | `poll`, gated `poll_interval_ms`) plus runtime auto-fallback surfaced in health (criteria 3.5, 3.6).
  - **Also firm (user, 2026-08-04):** `mqtt_subscribe` transport parity with `mqtt_publish` — greengrass / aws_iot mutual TLS / plain broker, exact parameter mirror + must-declare-a-target validation (criteria 2.1, 2.6–2.8).
  - **5 decisions remain open: #2, #4, #5, #6, #8.**
- [ ] 0.2 Spike the `opcua` client's subscription/monitored-item support on a real device — per the #7 resolution, spike TRUE subscriptions first: the spike **validates the subscription path** (does the packaged client do reliable data-change subscriptions at the edge?) and **characterizes when the runtime auto-fallback to polling should trigger**. If the spike shows subscriptions are unreliable at the edge, polling becomes the practical default — but the polling fallback (`mode` param `subscribe`|`poll`, `poll_interval_ms`, auto-fallback surfaced in health) is a REQUIRED capability of `opcua_subscribe` regardless of spike outcome (criteria 3.5, 3.6). Spike Greengrass IPC `SubscribeToIoTCore` message-callback behavior + subscribe accessControl.
- Exit: remaining decisions frozen; subscription path validated and fallback conditions characterized; sub-feature boundaries A/B/C confirmed (A already delivered).

## Sub-feature A — Triggers category, digital-input move, unified input (portal + designer) — **DONE**

> **Delivered by `.kiro/specs/triggers-stage-and-unified-input/`** (Triggers category, digital_input relocation, unified input with COEXIST decision, validator stage-ordering, designer support; portal deployed, riding the current LocalServer build). Also resolves open decision #3 (retain vs replace: coexist).

- [x] A.1 Add `CATEGORY_TRIGGER` to shared catalog constants (both copies in sync); designer palette section ordered before Inputs. *(delivered by .kiro/specs/triggers-stage-and-unified-input)*
- [x] A.2 Recategorize `digital_input` to `CATEGORY_TRIGGER` (metadata-only, behavior byte-identical); ensure saved graphs load/validate/compile/run unchanged. *(delivered by .kiro/specs/triggers-stage-and-unified-input)*
- [x] A.3 Add `Unified_Input_Node` (`source_kind` enum; gated params; optional activation input port; emits video frames). Retain-vs-replace resolved as COEXIST (decision #3). *(delivered by .kiro/specs/triggers-stage-and-unified-input)*
- [x] A.4 Validator: stage-ordering rules (Trigger → optional Transform → Input) and wiring legality; per-arch support. *(delivered by .kiro/specs/triggers-stage-and-unified-input)*
- [x] A.5 Frontend: palette section, activation-port wiring UX, `source_kind` gating, render saved digital_input under Triggers without mutating the graph. *(delivered by .kiro/specs/triggers-stage-and-unified-input)*
- [x] A.6 Tests (workflow_core both copies, validator, frontend) + backward-compat property (zero-trigger workflows byte-identical). Portal deploy. *(delivered by .kiro/specs/triggers-stage-and-unified-input)*

## Sub-feature B — MQTT/OPC UA subscribe triggers + device activation model (device runtime, the hard part)

- [ ] B.1 Catalog: `mqtt_subscribe` (exact `mqtt_publish` parameter mirror — `topic`, `qos`, `greengrass`, `aws_iot` + `iot_thing_name`/`iot_ca_cert_path`/`iot_client_cert_path`/`iot_private_key_path` gated on `aws_iot`, `broker_host`/`broker_port` — plus must-declare-a-target validation per criteria 2.1/2.6–2.8) and `opcua_subscribe` (mirror opcua_write endpoint/security; `mode` enum `subscribe`|`poll` with `poll_interval_ms` gated on `poll` per criterion 3.5) trigger descriptors + simulation stubs. Both descriptors expose the per-node `concurrency_policy` enum parameter (`queue` default, `drop`, `debounce`; `debounce_ms` gated on `debounce`, bounded `queue_depth` gated on `queue`) per criteria 2.5/3.4.
- [ ] B.2 Device Workflow_Engine: long-lived subscription/listener layer (injectable for tests) that starts subscriptions on component start, builds Trigger_Context, and enqueues Run_Activations per each trigger node's own `concurrency_policy` (default `queue` bounded by `queue_depth`; `drop`; `debounce` coalescing at `debounce_ms`) per criteria 2.5/3.4; reconnect/backoff + health surfacing. For OPC UA, honor the `mode` parameter — true subscriptions (monitored item / data-change notification) by default, Polling_Fallback at `poll_interval_ms` when `poll` is selected or on auto-fallback — identical Trigger_Context either way, logging/surfacing the active mechanism (including fallback transitions) in run/component health per criteria 3.5/3.6.
- [ ] B.3 MQTT subscribe transports: Greengrass IPC `SubscribeToIoTCore` (greengrass path), AWS IoT Core over mutual TLS (aws_iot path), and plain local broker via paho-mqtt (broker_host/broker_port) per criteria 2.2/2.6/2.7; OPC UA subscription/monitored-item with polling fallback per criteria 3.5/3.6 (auto-fallback conditions characterized by the 0.2 spike).
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
