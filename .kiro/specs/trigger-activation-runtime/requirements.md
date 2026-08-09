# Requirements Document

## Introduction

This feature is **Sub-feature B** of the assessment at
`.kiro/specs/workflow-triggers-and-input-overhaul/` (outline items B.1–B.7). It is a
separate, schedulable implementation spec and does **not** modify that assessment folder
or the delivered Sub-feature A spec (`.kiro/specs/triggers-stage-and-unified-input/`).

Sub-feature B delivers the two subscribe-side trigger node types and the device
activation runtime that makes trigger-driven workflows actually run:

1. **Catalog**: `mqtt_subscribe` and `opcua_subscribe` trigger descriptors (with the
   per-node policy parameter family resolved in Phase 0.1), simulation stubs, and the
   no-mixed-activation-model validator rule — in both byte-identical catalog copies and
   the frontend mirror.
2. **Device Workflow_Engine**: a long-lived, injectable Trigger_Subscription_Manager that
   starts subscriptions with the workflow, builds Trigger_Context, enqueues
   Run_Activations per each node's Concurrency_Policy, drains the Activation_Queue by
   priority-then-FIFO (OR semantics with a reserved activation-group extension point),
   reconnects with backoff up to `retry_limit`, runs the OPC UA liveness watchdog with
   subscribe→poll auto-fallback (per the 2026-08-05 Phase 0.2 spike findings), and
   surfaces trigger health.
3. **MQTT transports**: Greengrass IPC `SubscribeToIoTCore`, AWS IoT Core mutual TLS, and
   plain paho-mqtt broker — mirroring how `mqtt_publish` connects today.
4. **Packaging/deployment**: `aws.greengrass#SubscribeToIoTCore` accessControl for
   subscribed topics plus subscribe-denial diagnostics mirroring the publish-denial
   diagnostics.
5. **Compiler**: real trigger executor bindings and activation edges for the two new
   trigger types, while zero-trigger and `digital_input` compilation stay byte-identical.

### Out of scope (deferred)

- The `Trigger_Transform` custom-code node between trigger and input (**Sub-feature C**).
- AND/correlation multi-trigger semantics (only the extension point is reserved).
- Adding the policy parameter family to `digital_input` (parent criterion 4.4 keeps it
  unchanged).

### Grounded artifacts

- Catalog (both copies MUST stay byte-in-sync):
  `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/` (source of truth)
  and `src/backend/workflow_engine/vendor/workflow_core/` (device vendored copy).
- Validator: `workflow_core/validator/checks.py` (codes `V1`–`V7`, `W1`).
- Compiler: `workflow_core/compiler/compiler.py` (`expand_unified_inputs`,
  `INERT_ACTIVATION_TYPE_IDS`).
- Device runtime: `src/backend/workflow_engine/` (`runtime.py`, `watcher.py`,
  `executor.py`, `pipeline_executor.py`, `output_bindings.py`, `api.py`).
- Packaging/deployment: `edge-cv-portal/backend/functions/workflow_packaging.py`,
  `edge-cv-portal/backend/functions/deployments.py`, LocalServer recipes
  (`recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`,
  `recipe-amd64.yaml`).
- Frontend mirror: `edge-cv-portal/frontend/src/pages/workflows/` (`types.ts`,
  `builderGraph.ts`, `NodeConfigPanel.tsx`, `inlineChecks.ts`).
- Goldens/baselines: `layers/workflow_core/tests/catalog_baseline.json`,
  `layers/workflow_core/tests/golden_zero_trigger_compilation.json`.

### Phase 0.2 spike constraints (2026-08-05, ryan-orin-nano JP6, real device)

- The packaged OPC UA client is `python-opcua 0.98.13` (synchronous; `asyncua` is NOT
  available). True subscriptions work on device (10/10 data changes, 2.9–27.2 ms latency);
  `subscribe` stays the default mode.
- When the OPC UA server dies, the subscribed client receives **no signal** — the
  subscription goes silent; loss surfaces only as `TimeoutError` on the next read and
  `BrokenPipeError`/`CancelledError` on channel operations. The runtime MUST run an active
  liveness watchdog per subscription; reconnect/fallback triggers on (a) failed
  `create_subscription`/monitored-item setup and (b) keepalive timeout / broken-channel
  exceptions; reconnect must tear down and rebuild the entire client session.
- Greengrass IPC `SubscribeToIoTCore` works from the backend container via
  `on_stream_event` callbacks (no polling needed on the MQTT path).
- Subscribe accessControl IS enforced: the current LocalServer recipes only authorize
  subscribe on `$aws/things/*/shadow/name/*`; the packager/deployment accessControl work
  is confirmed required.

## Glossary

- **Node_Type_Catalog**: the data-only list of `NodeTypeDescriptor` records defining every
  workflow node type, existing in two byte-identical Python copies (portal layer and
  device vendored copy) plus a hand-maintained frontend mirror in `types.ts`.
- **MQTT_Subscribe_Trigger**: the new `mqtt_subscribe` trigger node type
  (`CATEGORY_TRIGGER`): fires a Run_Activation when a message arrives on the subscribed
  topic filter over one of three transports (greengrass, aws_iot, plain broker).
- **OPCUA_Subscribe_Trigger**: the new `opcua_subscribe` trigger node type
  (`CATEGORY_TRIGGER`): fires a Run_Activation when the monitored OPC UA node's value
  changes, via a true subscription or Polling_Fallback.
- **Trigger_Context**: the structured payload a trigger firing carries —
  `{topic, payload, qos, timestamp}` for MQTT; `{endpoint, node_id, value,
  source_timestamp}` for OPC UA.
- **Run_Activation**: one request to start a workflow run, produced by a trigger firing
  and consumed by the Activation_Dispatcher, which creates a `WorkflowExecution` record
  and hands it to the registered WorkflowExecutor (the same run path the manual trigger
  endpoint uses today).
- **Activation_Queue**: the per-workflow set of pending Run_Activations, populated per
  each trigger node's Concurrency_Policy and drained in Activation_Priority order, FIFO
  within equal priority.
- **Activation_Dispatcher**: the device-runtime loop that drains the Activation_Queue and
  starts runs sequentially for a workflow.
- **Activation_Group**: the reserved extension point for future AND/correlation
  semantics: every Run_Activation records the identifier of the group it satisfies; in
  this feature every trigger node forms its own implicit single-member group, which
  yields OR semantics.
- **Concurrency_Policy**: the per-trigger-node `concurrency_policy` enum parameter
  (`queue` default | `drop` | `debounce`) governing what happens when the trigger fires
  while a run it activated is still in flight, with companion `queue_depth` (gated on
  `queue`) and `debounce_ms` (gated on `debounce`) parameters.
- **Activation_Priority**: the per-trigger-node `priority` int parameter; lower value =
  higher priority; default 100; ties served FIFO by firing time.
- **Reconnect_Limit**: the per-trigger-node `retry_limit` int parameter bounding automatic
  reconnect attempts; `0` = retry forever (the default sentinel); values ≥ 1 bound the
  attempts.
- **Trigger_Subscription_Manager**: the new long-lived device-runtime layer that owns all
  trigger subscriptions for registered trigger-driven workflows: transport connections,
  Trigger_Context construction, enqueueing, dispatching, reconnect, watchdog, and
  Trigger_Health. Its transports are injectable for tests.
- **Trigger_Health**: the per-trigger-node health record the Trigger_Subscription_Manager
  maintains and the workflow API surfaces: connection state (connecting / subscribed /
  reconnecting / failed), the active OPC UA mechanism (`subscribe` or `poll`, including
  auto-fallback), reconnect attempt counts, and the last actionable error.
- **Polling_Fallback**: the OPCUA_Subscribe_Trigger's alternate value-change detection —
  periodic reads at `poll_interval_ms` — selected via the `mode` parameter or entered
  automatically when a true subscription cannot be established.
- **Liveness_Watchdog**: the periodic keepalive read the device runtime performs per
  active OPC UA subscription to detect silent subscription death (the key Phase 0.2 spike
  finding), triggering session teardown and reconnect on timeout or broken-channel
  exceptions.
- **Workflow_Validator**: the pure `validate(graph, catalog, ...)` function in
  `workflow_core/validator/checks.py`.
- **Workflow_Compiler**: the pure `compile(graph, target_arch, ...)` function in
  `workflow_core/compiler/compiler.py`.
- **Trigger_Binding**: the compiled realization of a trigger node — an executor-binding
  entry carrying the trigger's parameters and the node ids of the input/source nodes its
  activation edges feed.
- **Activation_Edge**: a connection from a new trigger node's `PORT_TYPE_EVENT_SIGNAL`
  output to an input/source node's `activation` port; compiled into the Trigger_Binding
  (no longer dropped as inert for the two new trigger types).
- **Zero_Trigger_Workflow**: a workflow whose node set contains no `CATEGORY_TRIGGER`
  node. Compiles, packages, and runs byte-identically to today (hard invariant).
- **Subscription_Access_Control**: the `aws.greengrass.ipc.mqttproxy` accessControl policy
  authorizing `aws.greengrass#SubscribeToIoTCore` for the workflow's subscribed topics,
  applied to the LocalServer component (whose process makes the IPC call) at deployment
  time.
- **Dependent_Parameter_Gating**: the `depends_on` visibility mechanism on
  `ParameterDescriptor`, extended by this feature from bool-only gating to also support
  the form `"name=value"` (visible while the named parameter's effective value equals the
  literal), used for enum-selection gating.

## Requirements

### Requirement 1: mqtt_subscribe trigger descriptor

**User Story:** As a workflow author, I want an MQTT subscribe trigger node with the same
connection surface as the MQTT publish output, so that a message arriving on a topic can
start a workflow run over any of the three transports I already use for publishing.

#### Acceptance Criteria

1. THE Node_Type_Catalog SHALL define an `mqtt_subscribe` descriptor with
   `category=CATEGORY_TRIGGER`, zero input ports, and exactly one
   `PORT_TYPE_EVENT_SIGNAL` output port.
2. THE `mqtt_subscribe` descriptor SHALL declare connection parameters mirroring
   `mqtt_publish` exactly in names, types, defaults, and constraints: `topic` (string,
   required, min_length 1; documented as a topic filter), `qos` (enum, optional, default
   0, values 0/1/2), `greengrass` (bool, optional, default false), `aws_iot` (bool,
   optional, default false), `iot_thing_name` / `iot_ca_cert_path` /
   `iot_client_cert_path` / `iot_private_key_path` (strings, optional, min_length 1, each
   `depends_on` `aws_iot`), `broker_host` (string, optional, min_length 1), and
   `broker_port` (int, optional, default 1883, min 1, max 65535).
3. THE `mqtt_subscribe` descriptor SHALL declare a `concurrency_policy` enum parameter
   (values `queue`, `drop`, `debounce`; optional, default `queue`) plus a `queue_depth`
   int parameter (optional, default 10, min 1, max 1000) gated on the `queue` selection
   and a `debounce_ms` int parameter (optional, default 500, min 1, max 60000) gated on
   the `debounce` selection, via Dependent_Parameter_Gating.
4. THE `mqtt_subscribe` descriptor SHALL declare a `retry_limit` int parameter (optional,
   default 0, min 0, max 1000) with the documented convention `0` = retry forever, and a
   `priority` int parameter (optional, default 100, min 0, max 1000) with the documented
   convention lower value = higher priority.
5. THE `mqtt_subscribe` descriptor SHALL declare device-architecture mappings with
   `executor_binding="mqtt_subscribe"` and plugin dependencies `python:paho-mqtt` and
   `python:awsiotsdk`, plus an `ARCH_SIM` appsrc simulation stub mirroring the
   `digital_input` simulation stub form, and SHALL set `hardware_dependent=True`.
6. IF an `mqtt_subscribe` node configuration declares no connection target (no
   `greengrass`, no `aws_iot`, and no non-empty `broker_host`), THEN THE
   Workflow_Validator SHALL produce one error finding for that node under a dedicated
   stable check code, mirroring the existing `mqtt_publish` `V6_MQTT_NO_TARGET` rule.

### Requirement 2: opcua_subscribe trigger descriptor

**User Story:** As a workflow author, I want an OPC UA subscribe trigger node with the
same endpoint and security surface as the OPC UA write output, so that a monitored node's
value change can start a workflow run.

#### Acceptance Criteria

1. THE Node_Type_Catalog SHALL define an `opcua_subscribe` descriptor with
   `category=CATEGORY_TRIGGER`, zero input ports, and exactly one
   `PORT_TYPE_EVENT_SIGNAL` output port.
2. THE `opcua_subscribe` descriptor SHALL declare `endpoint` (string, required,
   min_length 1, regex `^opc\.tcp://.+`) and `node_id` (string, required, min_length 1)
   parameters mirroring `opcua_write`, plus a `sampling_interval_ms` int parameter
   (optional, default 100, min 10, max 60000) for the subscription's
   sampling/publishing interval.
3. THE `opcua_subscribe` descriptor SHALL declare the same optional security parameters
   as `opcua_write`, identical in names, types, defaults, and constraints: `username`,
   `password`, `security_policy`, `security_mode`, `client_cert_path`, `client_key_path`,
   `server_cert_path`.
4. THE `opcua_subscribe` descriptor SHALL declare a `mode` enum parameter (values
   `subscribe`, `poll`; optional, default `subscribe`) and a `poll_interval_ms` int
   parameter (optional, default 500, min 10, max 60000) gated on the `poll` selection via
   Dependent_Parameter_Gating.
5. THE `opcua_subscribe` descriptor SHALL declare the same policy parameter family as
   criteria 1.3 and 1.4 (`concurrency_policy` with gated `queue_depth`/`debounce_ms`,
   `retry_limit`, `priority`), with identical types, defaults, constraints, and gating.
6. THE `opcua_subscribe` descriptor SHALL declare device-architecture mappings with
   `executor_binding="opcua_subscribe"` and plugin dependency `python:opcua` (the client
   the `opcua_write` node already packages), plus an `ARCH_SIM` appsrc simulation stub
   mirroring the `digital_input` simulation stub form, and SHALL set
   `hardware_dependent=True`.

### Requirement 3: Catalog integration, gating extension, and copy discipline

**User Story:** As a platform maintainer, I want the new descriptors added additively with
the gating mechanism extended in one place, so that both catalog copies, the baseline, and
the frontend mirror stay consistent and no existing descriptor changes.

#### Acceptance Criteria

1. THE Node_Type_Catalog SHALL support Dependent_Parameter_Gating in the `"name=value"`
   form on `ParameterDescriptor.depends_on`, WHERE a bare parameter name retains the
   existing bool-truthy gating semantics unchanged for all existing descriptors.
2. THE `mqtt_subscribe` and `opcua_subscribe` descriptors SHALL be appended to
   `NODE_CATALOG` after all existing entries, with every pre-existing descriptor
   (including `mqtt_publish`, `opcua_write`, `digital_input`, `unified_input`, and the
   four legacy sources) byte-identical to its pre-feature content.
3. THE portal catalog copy and the device-vendored catalog copy SHALL remain
   byte-identical after all changes in this feature.
4. WHEN the catalog baseline (`catalog_baseline.json`) is updated for this feature, THE
   updated baseline SHALL differ from the pre-feature baseline only in the
   `mqtt_subscribe` and `opcua_subscribe` descriptor additions, with all other descriptor
   entries byte-identical.
5. THE frontend catalog mirror (`types.ts`) SHALL define `mqtt_subscribe` and
   `opcua_subscribe` descriptors matching the backend descriptors in type ids, category,
   ports, and parameter names, types, defaults, constraints, and gating, and THE
   Node_Palette SHALL list both under the Triggers section.
6. WHEN a workflow author selects a `concurrency_policy` or `mode` value on a new trigger
   node, THE Workflow_Designer configuration panel SHALL show exactly the companion
   parameters gated on that selection (`queue_depth` under `queue`, `debounce_ms` under
   `debounce`, `poll_interval_ms` under `poll`) and SHALL hide them otherwise, while
   keeping the existing bool-based gating behavior of all other node types unchanged.

### Requirement 4: One activation model per workflow (validator)

**User Story:** As a workflow author, I want the validator to reject graphs that mix
trigger-driven and always-running inputs, so that a workflow has exactly one activation
model.

#### Acceptance Criteria

1. WHEN `validate` is called on a graph containing at least one `mqtt_subscribe` or
   `opcua_subscribe` node, THE Workflow_Validator SHALL require every `CATEGORY_INPUT`
   node in the graph to have its `activation` input port connected, and SHALL produce one
   error finding per unconnected input node under a single new stable check code
   (following the existing next-available-`V`-code convention), with a message naming the
   node and stating that a workflow with subscription triggers must drive every input
   from a trigger.
2. WHEN `validate` is called on a graph containing zero `mqtt_subscribe` and zero
   `opcua_subscribe` nodes, THE Workflow_Validator SHALL produce zero findings under the
   new mixed-model check code, and THE finding set SHALL equal the pre-feature finding
   set for the same graph (same codes, severities, and node/connection identifiers).
3. THE Workflow_Validator SHALL run the new checks (mixed-model and the criterion-1.6
   mqtt_subscribe target check) together with all existing checks (`V1`–`V7`, `W1`)
   without short-circuiting, returning the complete finding list in a single result.
4. WHEN a graph wires an `mqtt_subscribe` or `opcua_subscribe`
   `PORT_TYPE_EVENT_SIGNAL` output to an input/source node's `activation` port, THE
   Workflow_Validator SHALL treat that connection as legal stage ordering (zero
   `V7_STAGE_ORDER` findings for that connection).
5. THE frontend inline checks (`inlineChecks.ts`) SHALL mirror the two new validator
   checks so that inline findings identify the same offending node, severity, and check
   code as the Workflow_Validator findings for the same graph.

### Requirement 5: Compiler emits trigger bindings and real activation edges

**User Story:** As a platform maintainer, I want compiled documents to carry the trigger
executor bindings and their activation targets, so that the device runtime knows which
triggers drive which inputs — without changing any existing compiled output.

#### Acceptance Criteria

1. WHEN a graph containing `mqtt_subscribe` or `opcua_subscribe` nodes is compiled for a
   device architecture, THE Workflow_Compiler SHALL emit one Trigger_Binding per trigger
   node carrying the node id, the executor binding name, the node's effective parameters
   (declared defaults applied), and the ordered list of input/source node ids reachable
   through that trigger's Activation_Edges.
2. WHEN resolving Activation_Edges, THE Workflow_Compiler SHALL treat a connection from a
   new trigger node's output to an `activation` port on a `unified_input` node or on any
   of the four legacy source nodes as an activation target declaration, SHALL record the
   target in the Trigger_Binding (resolving `unified_input` targets to the same node id
   the expansion pre-pass preserves), and SHALL exclude Activation_Edges from GStreamer
   stream-topology resolution (segments and element chains are computed as if the
   Activation_Edges were absent).
3. WHEN a graph contains a connection from a `digital_input` output to an `activation`
   port, THE Workflow_Compiler SHALL drop that connection as inert exactly as it does
   today (parent criterion 4.4: `digital_input` keeps its existing behavior), producing
   compiled output identical to the pre-feature compiler for the same graph.
4. FOR ALL Zero_Trigger_Workflows and all supported architectures, THE compiled document
   emitted by the extended Workflow_Compiler SHALL be byte-identical to the pre-feature
   compiled document for the same graph (verified against
   `golden_zero_trigger_compilation.json`), and THE compiled-document serialization SHALL
   omit every new trigger-related field when the graph contains no `mqtt_subscribe` or
   `opcua_subscribe` node.
5. WHEN a graph containing the new trigger nodes is compiled with `simulation=True`, THE
   Workflow_Compiler SHALL resolve the trigger nodes to their `ARCH_SIM` simulation
   stubs, mirroring how `digital_input` is simulated today.

### Requirement 6: Device subscription lifecycle and transports

**User Story:** As a workflow operator, I want the device to hold live subscriptions for
every deployed trigger-driven workflow over the configured transport, so that external
events reach the workflow engine without polling.

#### Acceptance Criteria

1. WHEN a workflow registration containing Trigger_Bindings becomes registered, THE
   Trigger_Subscription_Manager SHALL start one subscription per trigger node within 10
   seconds, and WHEN the registration is removed or becomes invalid, THE
   Trigger_Subscription_Manager SHALL stop and release that workflow's subscriptions.
2. THE Trigger_Subscription_Manager SHALL accept its transport implementations by
   injection (constructor arguments with production defaults), so tests can substitute
   stub transports without network access.
3. WHEN an `mqtt_subscribe` node has `greengrass` enabled, THE
   Trigger_Subscription_Manager SHALL subscribe via the Greengrass IPC
   `SubscribeToIoTCore` operation with `on_stream_event` callback delivery, clamping
   `qos` to the Greengrass maximum of 1 as the publish path does.
4. WHEN an `mqtt_subscribe` node has `aws_iot` enabled, THE Trigger_Subscription_Manager
   SHALL subscribe to AWS IoT Core over mutual TLS via a paho-mqtt client configured with
   `iot_thing_name` as the client id and `iot_ca_cert_path` / `iot_client_cert_path` /
   `iot_private_key_path` as the TLS material, mirroring the `mqtt_publish` aws_iot
   connection configuration.
5. WHEN an `mqtt_subscribe` node has neither `greengrass` nor `aws_iot` enabled and
   `broker_host` is set, THE Trigger_Subscription_Manager SHALL subscribe to the plain
   broker at `broker_host`:`broker_port` via a paho-mqtt client, mirroring the
   `mqtt_publish` plain-broker connection configuration.
6. WHEN an `opcua_subscribe` node has `mode` `subscribe`, THE
   Trigger_Subscription_Manager SHALL connect a `python-opcua` client session (applying
   the same security configuration as the `opcua_write` executor for the same
   parameters), create a subscription at `sampling_interval_ms`, and register a
   data-change monitored item on `node_id`.
7. WHEN an `opcua_subscribe` node has `mode` `poll`, THE Trigger_Subscription_Manager
   SHALL detect value changes by reading `node_id` every `poll_interval_ms` milliseconds
   and firing WHEN the read value differs from the previously read value.
8. WHEN a message or data change arrives, THE Trigger_Subscription_Manager SHALL build
   Trigger_Context as `{topic, payload, qos, timestamp}` for MQTT triggers and
   `{endpoint, node_id, value, source_timestamp}` for OPC UA triggers, identical in shape
   under `subscribe` and `poll` mechanisms, and SHALL record the Trigger_Context on the
   resulting Run_Activation and persist it with the run record.

### Requirement 7: Activation semantics — OR, policies, priority

**User Story:** As a workflow author, I want each trigger node's firing behavior governed
by its own concurrency policy and priority, so that I control queueing, dropping,
debouncing, and relative importance per trigger.

#### Acceptance Criteria

1. WHEN any single trigger node of a workflow fires, THE device runtime SHALL activate a
   run for that workflow (OR semantics), and THE Run_Activation record SHALL carry an
   Activation_Group identifier (one implicit single-member group per trigger node in this
   feature) so future AND/correlation semantics can be added without changing OR
   behavior.
2. WHILE a trigger node with `concurrency_policy` `queue` has a Run_Activation in flight
   or pending, WHEN that trigger fires, THE device runtime SHALL append the new
   Run_Activation to the Activation_Queue bounded at `queue_depth` pending entries for
   that trigger node; IF the bound is reached, THEN THE device runtime SHALL discard the
   firing and log the discard naming the trigger node.
3. WHILE a trigger node with `concurrency_policy` `drop` has a Run_Activation in flight
   or pending, WHEN that trigger fires, THE device runtime SHALL discard the firing.
4. WHEN a trigger node with `concurrency_policy` `debounce` fires, THE device runtime
   SHALL coalesce all firings of that trigger within the trailing `debounce_ms`
   interval into one Run_Activation carrying the most recent Trigger_Context.
5. WHEN more than one Run_Activation is pending across a workflow's triggers, THE
   Activation_Dispatcher SHALL dequeue the pending Run_Activation with the lowest
   `priority` value first, breaking ties FIFO by firing time, and SHALL provide no other
   cross-trigger ordering guarantee.
6. THE Activation_Dispatcher SHALL run a workflow's activations sequentially (at most one
   in-flight run per workflow), creating a `WorkflowExecution` record and dispatching it
   through the registered executor hook exactly as the manual trigger endpoint does
   today.
7. WHEN a triggered run fails, THE device runtime SHALL record the failure on that run
   only (existing containment) and SHALL continue processing subsequent Run_Activations.

### Requirement 8: Reconnect, liveness watchdog, and auto-fallback

**User Story:** As a workflow operator, I want subscriptions to survive broker and server
outages with bounded automatic recovery, so that transient failures self-heal and
permanent failures are visible and actionable.

#### Acceptance Criteria

1. WHEN a trigger's transport connection drops (MQTT broker disconnect, Greengrass IPC
   stream error, or OPC UA session loss), THE Trigger_Subscription_Manager SHALL
   automatically reconnect and re-subscribe with exponential backoff (initial delay 1 s,
   doubling, capped at 60 s), counting attempts against the node's `retry_limit` WHERE
   `retry_limit` ≥ 1 and retrying without bound WHERE `retry_limit` is 0.
2. WHILE reconnecting, THE Trigger_Subscription_Manager SHALL set the node's
   Trigger_Health state to `reconnecting` with the attempt count, and WHEN the
   connection is re-established, THE Trigger_Subscription_Manager SHALL restore the
   state to `subscribed` and reset the attempt count.
3. IF the reconnect attempts exhaust a `retry_limit` ≥ 1, THEN THE
   Trigger_Subscription_Manager SHALL set the node's Trigger_Health state to `failed`
   with an actionable error message naming the trigger node id and the topic (MQTT) or
   endpoint (OPC UA), and SHALL stop reconnect attempts for that node until the
   workflow's subscriptions are restarted.
4. WHILE an OPC UA subscription is active, THE Trigger_Subscription_Manager SHALL run a
   Liveness_Watchdog performing a keepalive read against the session at an interval of
   5 seconds, and WHEN the keepalive raises `TimeoutError`, `BrokenPipeError`,
   `ConnectionError`, or `CancelledError`, THE Trigger_Subscription_Manager SHALL tear
   down the entire client session and enter the reconnect path of criterion 8.1
   (rebuilding the session, subscription, and monitored item from scratch).
5. WHILE an `opcua_subscribe` node's `mode` is `subscribe`, IF subscription creation or
   monitored-item registration fails during connection establishment, THEN THE
   Trigger_Subscription_Manager SHALL fall back to Polling_Fallback at
   `poll_interval_ms` for that session, SHALL log the transition naming the trigger
   node, and SHALL record the active mechanism as `poll` (auto-fallback) in
   Trigger_Health.
6. WHEN a session that auto-fell-back to Polling_Fallback is rebuilt by the reconnect
   path, THE Trigger_Subscription_Manager SHALL attempt a true subscription again before
   falling back.
7. IF a Greengrass IPC `SubscribeToIoTCore` request is denied with `UnauthorizedError`,
   THEN THE Trigger_Subscription_Manager SHALL set the node's Trigger_Health to `failed`
   with an actionable error naming the denied topic and the
   `aws.greengrass.ipc.mqttproxy` accessControl location to fix, mirroring the existing
   publish-denial diagnostic message form, and SHALL not count the denial against
   `retry_limit` (authorization does not self-heal by retrying).

### Requirement 9: Trigger health surfacing

**User Story:** As a workflow operator, I want each trigger's connection state visible
through the workflow status API, so that I can diagnose silent subscriptions and failed
triggers without device shell access.

#### Acceptance Criteria

1. THE Trigger_Subscription_Manager SHALL maintain a Trigger_Health record per trigger
   node of every registered trigger-driven workflow, carrying: state (`connecting`,
   `subscribed`, `polling`, `reconnecting`, `failed`), the active mechanism for OPC UA
   nodes (`subscribe` or `poll`, flagged when entered by auto-fallback), the reconnect
   attempt count, and the last error message when present.
2. WHEN the workflow registration detail endpoint is queried, THE workflow API SHALL
   include the registration's Trigger_Health records as an additive field, leaving all
   existing response fields unchanged, and WHEN a registration has no trigger nodes, THE
   workflow API response SHALL be unchanged from today.

### Requirement 10: Subscription accessControl and denial diagnostics (packaging/deployment)

**User Story:** As a workflow operator, I want deploying a greengrass-subscribing workflow
to carry the required subscribe authorization, so that the trigger works after deployment
instead of failing with an opaque authorization error.

#### Acceptance Criteria

1. WHEN a workflow containing `mqtt_subscribe` nodes with `greengrass` enabled is
   packaged, THE workflow packaging SHALL record the set of subscribed topic filters on
   the workflow version item and in the packaged manifest.
2. WHEN a deployment's component set includes both a packaged workflow with recorded
   subscribed topics and the LocalServer component, THE deployment creation SHALL merge a
   Subscription_Access_Control policy into the LocalServer component's
   configurationUpdate, authorizing `aws.greengrass#SubscribeToIoTCore` on resources
   covering exactly the recorded topic filters, under a policy key unique to the
   workflow, and leaving the LocalServer recipe's existing accessControl policies
   untouched.
3. WHEN a deployment's component set includes a packaged workflow with recorded
   subscribed topics but not the LocalServer component, THE deployment creation SHALL
   include an actionable warning in the deployment response naming the workflow, the
   topics, and the need to deploy the LocalServer component with the subscribe
   authorization.
4. WHEN a workflow contains no greengrass-enabled `mqtt_subscribe` node, THE packaging
   and deployment outputs SHALL be byte-identical to the pre-feature outputs for the same
   inputs.

### Requirement 11: Simulation stubs

**User Story:** As a workflow author, I want the new triggers usable in simulation, so
that trigger-driven workflows can be tested in the sandbox without brokers or OPC UA
servers.

#### Acceptance Criteria

1. WHEN a workflow containing `mqtt_subscribe` or `opcua_subscribe` nodes is compiled in
   simulation mode, THE compiled document SHALL realize each trigger node as a
   harness-fed appsrc event-source stub mirroring the existing `digital_input` simulation
   stub, with no broker or server contact.

### Requirement 12: Backward compatibility invariants

**User Story:** As an operator of existing deployments, I want every workflow that does
not use the new triggers to behave exactly as before, so that this feature introduces no
regressions.

#### Acceptance Criteria

1. FOR ALL Zero_Trigger_Workflows and all supported architectures, THE compiled and
   packaged artifacts SHALL be byte-identical to the pre-feature artifacts for the same
   workflow definition (hard invariant, verified against
   `golden_zero_trigger_compilation.json` and the packaging goldens).
2. THE `digital_input` descriptor SHALL remain byte-identical (no policy parameter family
   added), and workflows referencing `digital_input` SHALL load, validate, compile,
   package, and run unchanged.
3. THE `mqtt_publish` and `opcua_write` descriptors and executors SHALL remain unchanged;
   the new triggers are additive subscribe-side counterparts.
4. WHEN LocalServer starts on a device with no trigger-driven workflow registered, THE
   Trigger_Subscription_Manager SHALL start no subscription, open no connection, and
   SHALL NOT alter LocalServer startup behavior; IF the Trigger_Subscription_Manager
   fails to start, THEN LocalServer SHALL continue startup with the failure logged and
   contained (the existing workflow-engine containment discipline).
