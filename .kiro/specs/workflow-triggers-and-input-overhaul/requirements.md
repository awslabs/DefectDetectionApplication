# Workflow Triggers + Input Overhaul — Requirements (future todo)

> Status: **DOCUMENTATION / NOT SCHEDULED**. Captures the intended overhaul of the workflow input model: a new **Triggers** stage that runs before Inputs, subscribe-side MQTT/OPC UA triggers, relocation of Digital Input into Triggers, and a unified Input node. Read `design.md` for the architecture analysis and open questions before scheduling.
>
> **Resolved design decisions — ALL 8 RESOLVED (Phase 0.1 complete, 2026-08-04):** every open decision in design.md is now a firm requirement. **#1** concurrency/debounce — configurable **per trigger node**: `concurrency_policy` enum (`queue` default | `drop` | `debounce`) with gated `debounce_ms` and bounded `queue_depth` companions on each `mqtt_subscribe`/`opcua_subscribe` node instance (criteria 2.5, 3.4). **#2** multi-trigger semantics — **OR**: any trigger firing activates a run; the activation model reserves room for future AND/correlation semantics without breaking OR workflows (criterion 1.5). **#3** retain vs replace sources — **coexist**, delivered by `.kiro/specs/triggers-stage-and-unified-input/`. **#4** mixed model — **one activation model at a time per workflow**: ≥1 trigger ⇒ all inputs trigger-driven; the Workflow_Validator rejects mixed graphs (criterion 1.6). **#5** subscription lifecycle/reconnect — **automatic reconnect with a configurable retry limit**: per-node `retry_limit` (0 = retry forever, the default), backoff, health surfacing, trigger marked failed with an actionable error naming the trigger node and endpoint/topic on exhaustion (criteria 2.9, 3.7). **#6** override contract — **ANY input parameter is overridable**, validated against the input descriptor's constraints per activation; no whitelist (criterion 5.5). **#7** OPC UA subscription vs polling — spike true subscriptions first; polling fallback is a required node capability either way via the `mode` parameter (`subscribe` default | `poll`, gated `poll_interval_ms`) with auto-fallback surfaced in run/component health (criteria 3.5, 3.6). **#8** backpressure/ordering — per-trigger `priority` int parameter (lower value = higher priority; catalog-style default 100; ties FIFO by firing time): the user decides relative importance; no global ordering guarantee beyond priority-then-FIFO (criteria 2.10, 3.8). A firm **MQTT transport-parity** requirement also stands: `mqtt_subscribe` supports the same three connection paths as `mqtt_publish` — greengrass, aws_iot mutual TLS, plain broker (criteria 2.1, 2.6–2.8). Only the Phase 0.2 OPC UA subscription spike remains before sub-feature B can be scheduled.

## Motivation

Today a deployed workflow's first stage is an **Input** node (`csi_camera_source`, `icam_source`, `aravis_camera_source`, `folder_source`, `digital_input`) in `CATEGORY_INPUT`. Two problems:

1. **`digital_input` is miscategorized.** It already emits `PORT_TYPE_EVENT_SIGNAL` (not video frames) and runs at the executor level — it is structurally a *trigger* (an event that starts work), not an image source. It sits awkwardly among the camera/folder sources.
2. **There is no event-driven activation.** A workflow cannot say "when an MQTT message arrives on topic X (or an OPC UA node changes), start a run — optionally using data from that event to decide what to capture." Inputs are the only entry point and they are source-shaped, not event-shaped.

This feature introduces a **Triggers** stage that precedes Inputs, with MQTT-subscribe, OPC UA-subscribe, and Digital Input triggers; an optional custom-code step between a trigger and an input to transform trigger context into input parameters; and a unified Input node covering every source except digital input.

## Glossary

- **Trigger**: a new node category (`CATEGORY_TRIGGER`) whose nodes emit an **activation event** that starts a workflow run. Triggers replace "always-running input" as the activation mechanism when present.
- **Trigger_Context**: the structured payload a trigger carries when it fires (MQTT topic + message body; OPC UA node id + new value; digital input pin + edge). Available to a downstream custom-code node and/or the input node.
- **MQTT_Subscribe_Trigger**: fires when a message arrives on a subscribed topic over the device's Greengrass-managed MQTT (the subscribe counterpart of the existing `mqtt_publish` greengrass path).
- **OPCUA_Subscribe_Trigger**: fires when a subscribed OPC UA node's value changes (the subscribe counterpart of the existing `opcua_write` node).
- **Digital_Input_Trigger**: the relocated `digital_input`, unchanged in behavior — GPIO edge fires an activation event.
- **Trigger_Transform**: an optional custom-code node (reusing the custom Python node machinery) placed between a trigger and an input that maps Trigger_Context → input parameters (e.g. topic payload → folder path / capture selection).
- **Simple_Trigger link**: a trigger wired directly to an input with no Trigger_Transform — fires the input with its statically configured parameters, ignoring context.
- **Unified_Input_Node**: a single input node type that can be configured as any non-digital source (camera / aravis / folder / CSI), replacing the need for distinct source node types in the designer palette; parameters gate on the selected source kind.
- **Run_Activation**: the device Workflow_Engine's notion of starting one pipeline run; today implicitly source-driven, extended here to be trigger-driven.
- **Concurrency_Policy**: a per-trigger-node enum parameter `concurrency_policy` governing what happens when the trigger fires while a run activated by that trigger is still in flight. Each `mqtt_subscribe`/`opcua_subscribe` node instance carries its own policy. Values: `queue` (default — firings queue, bounded by a companion `queue_depth` int parameter gated on `queue`, and activate sequentially; firings beyond the bound are discarded), `drop` (firing is discarded), `debounce` (firings within the companion `debounce_ms` interval, gated on `debounce`, coalesce into one activation).
- **Polling_Fallback**: the OPCUA_Subscribe_Trigger's alternate value-change detection mechanism — periodic reads of the monitored node at a configurable `poll_interval_ms` — selected explicitly via the node's `mode` parameter (`poll`) or entered automatically by the device runtime when a true subscription cannot be established or proves unreliable. Trigger semantics are identical either way; only the detection mechanism differs.
- **Activation_Priority**: a per-trigger-node `priority` int parameter. Convention (documented): **lower value = higher priority**, catalog-style default `100`. WHEN multiple queued activations are pending across a workflow's triggers, the device runtime dequeues lower-`priority`-value triggers' work first; ties are served FIFO by firing time. No additional global ordering guarantee is promised across triggers beyond priority-then-FIFO. Part of the per-node policy parameter family alongside `concurrency_policy`/`debounce_ms`/`queue_depth`.
- **Activation_Queue**: the device runtime's set of pending Run_Activations awaiting execution — populated per each trigger node's Concurrency_Policy (e.g. bounded by `queue_depth` under `queue`) and drained in Activation_Priority order, FIFO within equal priority.
- **Reconnect_Limit**: a per-trigger-node `retry_limit` int parameter bounding automatic reconnect attempts after broker disconnects / OPC UA session drops. Convention (documented): `0` = retry forever (the default sentinel); values ≥ 1 bound the attempts. The device runtime reconnects with backoff, surfaces reconnect state in run/component health, and marks the trigger failed with an actionable error naming the trigger node and endpoint/topic when the limit is exhausted.

## Requirements

### Requirement 1: Triggers node category and stage

1.1 THE Node_Type_Catalog SHALL define a new `CATEGORY_TRIGGER` category and the Workflow_Designer SHALL present it as a distinct palette section positioned before Inputs.

1.2 A workflow MAY have zero or more Trigger nodes. WHEN a workflow has at least one Trigger, THE run model SHALL be trigger-driven: a run activates only when a trigger fires. WHEN a workflow has no Trigger, THE existing source-driven activation SHALL be preserved unchanged (backward compatibility).

1.3 THE Workflow_Validator SHALL enforce the stage ordering Trigger → (optional Trigger_Transform) → Input → (existing preprocessing/inference/output stages), and SHALL reject graphs that wire a Trigger downstream of an Input or that place a non-trigger source upstream of a Trigger.

1.4 A Trigger node SHALL emit `PORT_TYPE_EVENT_SIGNAL` carrying Trigger_Context, consumable by a Trigger_Transform node or directly by an Input node's activation port.

1.5 WHEN a workflow contains multiple Trigger nodes, ANY single trigger firing SHALL activate a run (OR semantics). THE activation model (data model and device runtime) SHALL be structured so that correlation/AND semantics (e.g. a workflow-level or trigger-group correlation concept) can be added later without changing the behavior of OR workflows; AND semantics SHALL NOT be implemented now. *(Resolves design.md open decision #2: OR now, room reserved for future AND/correlation.)*

1.6 WHEN a workflow contains at least one Trigger node, ALL input/source nodes in that workflow SHALL be trigger-driven (no always-running source in the same workflow). IF a graph mixes a triggered input and an untriggered (always-running) input, THEN THE Workflow_Validator SHALL reject the workflow with an actionable finding. Zero-trigger workflows remain fully source-driven per criterion 1.2. *(Resolves design.md open decision #4: only one activation model at a time per workflow.)*

### Requirement 2: MQTT subscribe trigger

2.1 THE catalog SHALL define an `mqtt_subscribe` Trigger that supports the SAME three connection paths as the existing `mqtt_publish` output node — `greengrass` zero-config, `aws_iot` mutual TLS, and plain local broker — mirroring `mqtt_publish`'s parameter surface exactly: `topic` (string, required; topic filter), `qos` (enum), `greengrass` (bool, default false), `aws_iot` (bool, default false), `iot_thing_name`/`iot_ca_cert_path`/`iot_client_cert_path`/`iot_private_key_path` (strings, each `depends_on` `aws_iot`), and `broker_host`/`broker_port` (plain-broker path; `broker_port` default 1883). *(Transport parity with `mqtt_publish`; firm decision, user 2026-08-04.)*

2.2 WHEN the workflow runs on device and `greengrass` is enabled, THE device runtime SHALL subscribe via the Greengrass IPC `SubscribeToIoTCore` operation, and a received message SHALL fire the trigger with Trigger_Context = {topic, payload, qos, timestamp}.

2.3 THE Greengrass component recipe SHALL declare the `aws.greengrass.ipc.mqttproxy` **subscribe** authorization for the configured topic(s) (accessControl for `aws.greengrass#SubscribeToIoTCore`), analogous to how publish authorization is handled today, and a denial SHALL surface as an actionable run error naming the topic and the recipe accessControl location.

2.4 THE feature SHALL support at-least-once (qos 1) and at-most-once (qos 0) as Greengrass allows, clamping unsupported qos as the publish path does.

2.5 THE `mqtt_subscribe` Trigger descriptor SHALL declare a per-trigger-node `concurrency_policy` enum parameter (values `queue`, `drop`, `debounce`; default `queue`) governing Run_Activation when the trigger fires while a run it activated is still in flight. WHERE `debounce` is selected, THE node SHALL expose a companion `debounce_ms` int parameter gated on that selection (`depends_on`); WHERE `queue` is selected, THE node SHALL expose a companion bounded `queue_depth` int parameter gated on that selection, with catalog-convention defaults. Each node instance carries its own policy values. *(Resolves design.md open decision #1: policy is configurable per trigger node.)*

2.6 WHEN `aws_iot` is enabled, THE device runtime SHALL subscribe to AWS IoT Core over mutual TLS using the `iot_thing_name`/`iot_ca_cert_path`/`iot_client_cert_path`/`iot_private_key_path` parameters, identically to how `mqtt_publish` connects for publishing.

2.7 WHEN neither `greengrass` nor `aws_iot` is enabled and `broker_host` is set, THE device runtime SHALL subscribe to the plain local broker at `broker_host`:`broker_port` via paho-mqtt, identically to how `mqtt_publish` connects for publishing.

2.8 IF an `mqtt_subscribe` node configuration declares no connection target (no `greengrass`, no `aws_iot`, no `broker_host`), THEN THE Workflow_Validator SHALL reject the workflow with an actionable per-node error, mirroring the existing `mqtt_publish` must-declare-a-target validation rule.

2.9 THE `mqtt_subscribe` Trigger descriptor SHALL declare a `retry_limit` int parameter with the documented convention `0` = retry forever (the default sentinel), values ≥ 1 bounding the attempts, and catalog-convention bounds. WHEN the broker connection or Greengrass session drops, THE device runtime SHALL automatically reconnect and re-subscribe with backoff up to the configured limit, surfacing reconnect state in run/component health; WHEN the limit is exhausted, THE device runtime SHALL mark the trigger failed in component/run health with an actionable error naming the trigger node and topic. *(Resolves design.md open decision #5: automatic reconnect with a configurable retry limit.)*

2.10 THE `mqtt_subscribe` Trigger descriptor SHALL declare a `priority` int parameter (documented convention: lower value = higher priority; catalog-style default `100`), joining the per-node policy parameter family of criterion 2.5 (`concurrency_policy`/`debounce_ms`/`queue_depth`). WHEN multiple queued activations are pending across the triggers of a workflow (per each node's `concurrency_policy`), THE device runtime SHALL dequeue by `priority`, then FIFO by firing time within equal priority; THE feature SHALL promise no additional global ordering guarantee across triggers beyond priority-then-FIFO. *(Resolves design.md open decision #8: the user decides relative importance via per-trigger priority.)*

### Requirement 3: OPC UA subscribe trigger

3.1 THE catalog SHALL define an `opcua_subscribe` Trigger with parameters: endpoint, node id (the monitored item), sampling/publishing interval, and the same optional security parameters (username/password and/or certificate-based signing) as the existing `opcua_write` node.

3.2 WHEN the workflow runs on device, THE device runtime SHALL create an OPC UA subscription with a monitored item on the configured node, and a value change (data-change notification) SHALL fire the trigger with Trigger_Context = {endpoint, node_id, value, source_timestamp}.

3.3 THE feature SHALL reuse the `opcua` client dependency the output node already packages; security configuration SHALL behave identically (anonymous default; user-token and/or cert signing when configured).

3.4 THE `opcua_subscribe` Trigger descriptor SHALL declare the same per-trigger-node `concurrency_policy` enum parameter as criterion 2.5 (values `queue`, `drop`, `debounce`; default `queue`), with the `debounce_ms` parameter gated on the `debounce` selection and the bounded `queue_depth` parameter gated on the `queue` selection. Each node instance carries its own policy values. *(Resolves design.md open decision #1: policy is configurable per trigger node.)*

3.5 THE `opcua_subscribe` Trigger descriptor SHALL declare a `mode` enum parameter (values `subscribe`, `poll`; default `subscribe`) selecting the value-change detection mechanism, with a `poll_interval_ms` int parameter gated on the `poll` selection (`depends_on`). WHERE `mode` is `subscribe`, THE device runtime SHALL create a true OPC UA subscription (monitored item / data-change notification); WHERE `mode` is `poll`, THE device runtime SHALL perform Polling_Fallback (periodic reads at `poll_interval_ms`). THE trigger SHALL fire with identical Trigger_Context under either mechanism. *(Resolves design.md open decision #7: spike true subscriptions first; polling fallback is a required node capability regardless of spike outcome.)*

3.6 WHILE `mode` is `subscribe`, IF the device runtime cannot establish a reliable OPC UA subscription (subscription creation or monitored-item registration fails, or the subscription proves unreliable at runtime), THEN THE device runtime MAY automatically fall back to Polling_Fallback, and WHEN such a fallback occurs, THE device runtime SHALL log the transition and surface the active mechanism (including the fallback) in run/component health.

3.7 THE `opcua_subscribe` Trigger descriptor SHALL declare the same `retry_limit` parameter and reconnect behavior as criterion 2.9, applied to OPC UA session drops and subscription/monitored-item loss: THE device runtime SHALL reconnect with backoff up to the limit and re-establish the subscription (or Polling_Fallback per criteria 3.5/3.6) after reconnecting, surfacing reconnect state in run/component health and marking the trigger failed with an actionable error naming the trigger node and endpoint when the limit is exhausted. *(Resolves design.md open decision #5.)*

3.8 THE `opcua_subscribe` Trigger descriptor SHALL declare the same `priority` parameter and activation-ordering behavior as criterion 2.10 (lower value = higher priority; catalog-style default `100`; ties FIFO by firing time; priority-then-FIFO is the only cross-trigger ordering guarantee). *(Resolves design.md open decision #8.)*

### Requirement 4: Digital Input relocated to Triggers

4.1 THE `digital_input` node SHALL move from `CATEGORY_INPUT` to `CATEGORY_TRIGGER` with NO behavior change — same pin/trigger_edge/poll_interval_ms parameters, same `PORT_TYPE_EVENT_SIGNAL` output, same executor binding, same simulation stub.

4.2 WHEN a pre-existing workflow references `digital_input`, THE Workflow_Designer SHALL load and render it under the Triggers section without modifying the stored definition, and revalidation SHALL produce the same outcome (backward compatibility with saved graphs).

4.3 Trigger_Context for Digital_Input_Trigger SHALL be {pin, edge, timestamp}.

4.4 THE Digital_Input_Trigger SHALL keep its existing activation behavior without the per-node policy parameters of criteria 2.5/2.9/2.10 (`concurrency_policy`/`debounce_ms`/`queue_depth`/`retry_limit`/`priority`). *(Follow-on decision, not required now: whether to later add the same per-node policy parameter family to `digital_input` remains open.)*

### Requirement 5: Trigger_Transform (optional custom code before input)

5.1 A workflow author MAY place a Trigger_Transform (custom Python node) between a Trigger and an Input. It SHALL receive Trigger_Context and produce a mapping of input parameter overrides (e.g. folder path, file selection, capture count) applied to the downstream Input node for that activation.

5.2 WHEN no Trigger_Transform is present (Simple_Trigger link), THE Input SHALL activate with its statically configured parameters and ignore Trigger_Context.

5.3 THE Trigger_Transform SHALL reuse the existing custom-node machinery (packaging, sandboxing, code-assist) so no new execution runtime is introduced; only its position (trigger→input) and its I/O contract (Trigger_Context in, parameter overrides out) are new.

5.4 IF a Trigger_Transform errors or returns invalid overrides, THEN THE run SHALL be recorded failed with an actionable per-node error and the Input SHALL NOT activate for that trigger firing.

5.5 A Trigger_Transform MAY override ANY parameter of the downstream Input node — there is no override whitelist. THE device runtime SHALL validate each activation's overrides against the input descriptor's parameter constraints (types, enums, bounds, gating) per activation; invalid overrides SHALL fail that activation only, per criterion 5.4. *(Resolves design.md open decision #6: any input parameter is overridable, validated per activation.)*

### Requirement 6: Unified Input node

6.1 THE catalog SHALL define a `Unified_Input_Node` covering every non-digital source (CSI camera, industrial/icam camera, aravis camera, folder source) selected via a `source_kind` parameter, with the other parameters gated on the selection. THE existing distinct source node types MAY be retained for backward compatibility or aliased to the unified node (design decision, see design.md).

6.2 Digital input SHALL NOT be a selectable `source_kind` (it is a Trigger now).

6.3 THE Unified_Input_Node SHALL accept an optional activation input port fed by a Trigger or Trigger_Transform; when connected, it activates per firing; when unconnected (no triggers in the workflow), it behaves as the always-running source it does today.

6.4 THE Unified_Input_Node SHALL emit `PORT_TYPE_VIDEO_FRAMES` exactly as the current sources do, so all downstream preprocessing/inference/output nodes are unaffected.

### Requirement 7: Backward compatibility and scope

7.1 Workflows with no Trigger nodes SHALL compile, package, deploy, and run byte-identically to today (source-driven).

7.2 Existing saved workflows referencing `digital_input`, `folder_source`, and the camera sources SHALL continue to load, validate, compile, and run unchanged.

7.3 THE existing `mqtt_publish` and `opcua_write` OUTPUT nodes SHALL be unchanged; the new triggers are additive subscribe-side counterparts.

7.4 Simulation SHALL provide stub forms for the new triggers (harness-fed activation events) mirroring how `digital_input` is simulated today.

7.5 Platform scope, packaging conventions, and the security accessControl model SHALL follow the existing per-arch and Greengrass patterns; no new deployment mechanism is introduced.
