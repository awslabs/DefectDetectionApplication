# Implementation Plan

## Overview

This plan implements **Sub-feature B** (MQTT/OPC UA subscribe triggers + the device
activation runtime) of the `workflow-triggers-and-input-overhaul` assessment, following
the design's components C1–C8 and validating properties P1–P17. Sequencing follows the
repo disciplines:

1. **Catalog first** (C1): the two trigger descriptors + the `depends_on` `"name=value"`
   gating extension, landed in both byte-identical catalog copies with the baseline
   updated.
2. **Validator** (C2): `V8_MQTT_SUB_NO_TARGET` and `V9_MIXED_ACTIVATION_MODEL`.
3. **Compiler** (C3): activation-edge extraction and `activates`-annotated
   Trigger_Bindings, with zero-trigger and `digital_input` output byte-identical
   (golden-guarded).
4. **Frontend mirror** (C8-adjacent): `types.ts` descriptors, config-panel enum gating,
   inline-check mirrors.
5. **Device runtime** (C4–C6): `trigger_runtime.py` — activation queue/dispatcher with
   per-node policies, the Trigger_Subscription_Manager lifecycle, the three MQTT
   transports, the OPC UA worker with Liveness_Watchdog and auto-fallback, Trigger_Health
   and its API surfacing.
6. **Packaging/deployment** (C7): subscribed-topic recording + the
   `SubscribeToIoTCore` accessControl deployment merge + denial diagnostics.
7. **Checkpoints** after each layer, a full-suite checkpoint, then the gated LocalServer
   build/portal integration and the gated on-hardware verification on the JP6 device.

Hard invariants throughout: zero-trigger workflows compile/package byte-identically
(`golden_zero_trigger_compilation.json` must pass unchanged), `digital_input` stays
byte-identical, and both catalog copies stay byte-in-sync at every step. This spec does
**not** modify `.kiro/specs/workflow-triggers-and-input-overhaul/` or
`.kiro/specs/triggers-stage-and-unified-input/`.

## Tasks

- [x] 1. Catalog: trigger descriptors and gating extension (C1)
  - [x] 1.1 Add `mqtt_subscribe` and `opcua_subscribe` descriptors + `depends_on` `"name=value"` form
    - In `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/models.py`,
      extend the `ParameterDescriptor.depends_on` docstring to document the
      `"name=value"` gating form (bare name keeps bool-truthy semantics; no field change)
    - In `.../catalog/nodes.py`, add a `_trigger_policy_parameters()` helper returning the
      shared policy family: `concurrency_policy` (enum `queue`|`drop`|`debounce`, default
      `queue`), `queue_depth` (int, default 10, 1–1000,
      `depends_on="concurrency_policy=queue"`), `debounce_ms` (int, default 500, 1–60000,
      `depends_on="concurrency_policy=debounce"`), `retry_limit` (int, default 0, 0–1000),
      `priority` (int, default 100, 0–1000)
    - Add `MQTT_SUBSCRIBE` (`category=CATEGORY_TRIGGER`, no inputs, one
      `PORT_TYPE_EVENT_SIGNAL` `out` port): connection parameters copied field-for-field
      from `MQTT_PUBLISH` (`topic` re-described as a topic filter; no
      `payload_template`), plus the policy family; mappings
      `_same_on_device_archs(executor_binding="mqtt_subscribe",
      plugin_dependencies=["python:paho-mqtt", "python:awsiotsdk"])` + the
      `digital_input`-form `ARCH_SIM` appsrc stub; `hardware_dependent=True`
    - Add `OPCUA_SUBSCRIBE`: `endpoint`/`node_id` + the seven security parameters copied
      field-for-field from `OPCUA_WRITE` (no `value_template`), new
      `sampling_interval_ms` (int, default 100, 10–60000), `mode` (enum
      `subscribe`|`poll`, default `subscribe`) with `poll_interval_ms` (int, default 500,
      10–60000, `depends_on="mode=poll"`), plus the policy family; mappings
      `_same_on_device_archs(executor_binding="opcua_subscribe",
      plugin_dependencies=["python:opcua"])` + `ARCH_SIM` appsrc stub;
      `hardware_dependent=True`
    - Append both to `NODE_CATALOG` after `UNIFIED_INPUT` (append-only; every
      pre-existing descriptor byte-identical)
    - Copy the edited `models.py` and `nodes.py` verbatim to
      `src/backend/workflow_engine/vendor/workflow_core/catalog/` (byte-in-sync
      discipline)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 3.3_

  - [x] 1.2 Write example unit tests for descriptor content
    - New `test_catalog_subscribe_triggers.py`: categories/ports (1.1, 2.1), the policy
      family and gating strings (1.3, 1.4, 2.4), `sampling_interval_ms`, mappings /
      plugin deps / sim stub / `hardware_dependent` (1.5, 2.6)
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 2.1, 2.4, 2.6_

  - [x] 1.3 Write property test for descriptor mirroring
    - **Property 1: Descriptor mirroring**
    - Iterate every mirrored parameter name: `mqtt_subscribe` vs `mqtt_publish`
      connection params, `opcua_subscribe` vs `opcua_write` endpoint/security params,
      and the policy family across the two trigger descriptors — field-for-field equality
    - **Validates: Requirements 1.2, 2.2, 2.3, 2.5**

  - [x] 1.4 Update the catalog baseline
    - Regenerate `layers/workflow_core/tests/catalog_baseline.json`; assert (in the
      existing `test_catalog_content.py` flow) that the delta is exactly the two new
      descriptor entries, with `digital_input`, `mqtt_publish`, `opcua_write`, and all
      other entries byte-identical
    - _Requirements: 3.2, 3.4, 12.2, 12.3_

- [x] 2. Validator: V8 target check and V9 mixed-model rule (C2)
  - [x] 2.1 Implement `_check_v8` and `_check_v9` in `validator/checks.py`
    - `CODE_V8_MQTT_SUB_NO_TARGET`: V6's target logic applied to `mqtt_subscribe` nodes
      (error when neither `greengrass` nor `aws_iot` is truthy and `broker_host` is
      blank), message naming the node and the three target options
    - `CODE_V9_MIXED_ACTIVATION_MODEL`: when the graph has ≥ 1
      `mqtt_subscribe`/`opcua_subscribe` node, one error per `CATEGORY_INPUT` node with
      no connection targeting its `activation` port; zero findings otherwise
      (`digital_input` alone does not engage V9)
    - Append both to `validate()` after `_check_w1` (no short-circuiting); mirror the
      edit to the vendored validator copy
    - _Requirements: 1.6, 4.1, 4.2, 4.3, 4.4_

  - [x] 2.2 Write example unit tests for the validator wiring
    - Multi-violation graph returns old + new codes together (4.3); a
      trigger-output→activation-port connection yields zero `V7_STAGE_ORDER` findings
      (4.4)
    - _Requirements: 4.3, 4.4_

  - [x] 2.3 Write property test for the mqtt_subscribe target rule
    - **Property 2: mqtt_subscribe must declare a target**
    - Hypothesis-generate target parameter combinations (bools set/unset, blank and
      whitespace hosts); assert one V8 finding iff no target
    - **Validates: Requirements 1.6**

  - [x] 2.4 Write property test for the mixed-model rule
    - **Property 3: Mixed activation model is rejected**
    - Generate graphs varying trigger presence and per-input activation wiring; assert
      V9 findings match the unconnected-input set exactly
    - **Validates: Requirements 4.1**

  - [x] 2.5 Write property test for validator equivalence
    - **Property 4: Validator equivalence without the new triggers**
    - Generate graphs without the new trigger types (incl. `digital_input` graphs);
      assert finding-set equality with the pre-feature validator and zero V8/V9 codes
    - **Validates: Requirements 4.2**

- [x] 3. Compiler: activation plan and Trigger_Bindings (C3)
  - [x] 3.1 Extract activation edges and emit `activates` on trigger bindings
    - In `compiler/compiler.py`: add `TRIGGER_EXECUTOR_TYPES`; change
      `expand_unified_inputs`'s inert-drop guard to keep activation edges whose source
      type is a new trigger type (drop all others, `digital_input` included); add
      `_extract_activation_plan(graph)` inside `compile()` (post-expansion,
      post-validation) removing those edges from the working connection set and
      recording `{trigger_node_id: [target_ids]}` in connection order; attach
      `"activates": plan[node_id]` to executor-binding entries whose binding is a
      trigger kind — no other entry or document field changes
    - Mirror the edit to the vendored compiler copy
      (`src/backend/workflow_engine/vendor/workflow_core/compiler/compiler.py`)
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [x] 3.2 Write example unit tests for simulation stubs
    - Compile a trigger graph with `simulation=True`; assert both trigger nodes resolve
      to appsrc stub chains with no executor network binding (mirroring `digital_input`)
    - _Requirements: 5.5, 11.1_

  - [x] 3.3 Write property test for trigger binding emission
    - **Property 7: Trigger bindings are emitted faithfully**
    - Generate trigger-driven graphs (extend `tests/generators.py` with trigger-node +
      activation-edge generators); assert one binding per trigger with kind, effective
      parameters, ordered `activates`, and segments equal to the activation-edge-stripped
      twin graph's segments
    - **Validates: Requirements 5.1, 5.2**

  - [x] 3.4 Write property test for no-new-trigger preservation
    - **Property 8: No-new-trigger compilation and packaging are preserved**
    - Generate graphs without new triggers, including `digital_input`→activation edges;
      assert compiled bytes equal pre-feature output with no `activates` key; run the
      existing `golden_zero_trigger_compilation.json` comparison unchanged
    - **Validates: Requirements 5.3, 5.4, 12.1**

- [x] 4. Checkpoint — portal catalog/validator/compiler suite
  - Run `cd edge-cv-portal/backend && python3 -m pytest layers/workflow_core/tests/`;
    the golden zero-trigger test and both catalog-mirror tests must pass unchanged.
    Ensure all tests pass, ask the user if questions arise.

- [x] 5. Frontend mirror: descriptors, gating, inline checks (C1/C2 frontend)
  - [x] 5.1 Mirror the two descriptors in `types.ts`
    - Hand-mirror `mqtt_subscribe` and `opcua_subscribe` (ids, category, ports, parameter
      names/types/defaults/constraints, `dependsOn` strings incl. `"name=value"`); the
      palette lists them under Triggers automatically (category-driven)
    - _Requirements: 3.5_

  - [x] 5.2 Extend `visibleWhenDependencyMet` in `NodeConfigPanel.tsx`
    - Parse `dependsOn`: bare name → existing bool check (unchanged); `name=value` →
      compare the controlling parameter's effective value (explicit, else declared
      default) as a string against the literal
    - _Requirements: 3.1, 3.6_

  - [x] 5.3 Add `checkV8`/`checkV9` mirrors to `inlineChecks.ts`
    - Mirror the backend V8/V9 semantics and codes; add both to `runInlineChecks`
    - _Requirements: 4.5_

  - [x] 5.4 Write frontend example tests
    - Palette lists both triggers under Triggers; `types.ts` descriptor content matches
      the backend descriptors (names/types/defaults/constraints/dependsOn); config panel
      shows/hides `queue_depth`/`debounce_ms`/`poll_interval_ms` per selection
    - _Requirements: 3.5, 3.6_

  - [x] 5.5 Write property test for gating semantics (fast-check)
    - **Property 6: Dependent-parameter gating semantics**
    - Generate controlling values (set/unset/default) for bare-name and `name=value`
      dependencies; assert bare-name behavior unchanged and equality gating exact
    - **Validates: Requirements 3.1, 3.6**

  - [x] 5.6 Write property test for inline-check parity (fast-check)
    - **Property 5: Inline-check parity for the new checks**
    - Generate graphs; assert inline V8/V9 finding (code, node) sets equal the mirrored
      backend semantics
    - **Validates: Requirements 4.5**

- [x] 6. Device runtime core: activation queue, policies, dispatcher (C4)
  - [x] 6.1 Create `src/backend/workflow_engine/trigger_runtime.py` core
    - `RunActivation` dataclass (trigger_node_id, activation_group=node id, priority,
      firing_seq, context); `ActivationQueue` (heapq by (priority, firing_seq));
      enqueue-time Concurrency_Policy handling (`queue` bounded by `queue_depth` with
      logged discard, `drop`, `debounce` trailing-timer coalescing keeping the latest
      context); per-workflow dispatcher thread running activations sequentially by
      creating a `WorkflowExecution` row (status pending) and calling
      `executor.dispatch`, mirroring `api.trigger_workflow`; failure containment (a
      failed run never blocks the next activation)
    - Additive alembic migration adding nullable `trigger_context_json` to
      `workflow_executions`; the dispatcher persists the Trigger_Context there
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 6.8_

  - [x] 6.2 Add `TriggerSubscriptionManager` lifecycle and wiring
    - Manager with injectable `mqtt_transport_factory`/`opcua_transport_factory`/
      `run_starter`/`clock`; `on_registrations_changed` diffs registered artifact sets,
      parses trigger bindings (`activates` entries) from compiled documents, and
      starts/stops `WorkflowTriggerGroup`s (workers + queue + dispatcher); Trigger_Health
      registry (state, mechanism, autoFallback, reconnectAttempts, lastError)
    - Wire in `runtime.start_workflow_engine` inside its own containment try/except;
      add an additive `registrations_listeners` hook to `WorkflowWatcher.sync_once`;
      no subscription or thread work when no trigger-driven workflow is registered
    - _Requirements: 6.1, 6.2, 9.1, 12.4_

  - [x] 6.3 Write property test for concurrency policies
    - **Property 9: Concurrency policy conformance**
    - Generate firing sequences with a controlled in-flight flag and fake clock; assert
      queue bound/overflow discard, drop semantics, and debounce coalescing with
      latest-context selection
    - **Validates: Requirements 7.2, 7.3, 7.4**

  - [x] 6.4 Write property test for dispatch ordering
    - **Property 10: Priority-then-FIFO dispatch order**
    - Generate pending activation sets; drain; assert order equals sorted
      (priority, firing_seq)
    - **Validates: Requirements 7.5**

  - [x] 6.5 Write property test for OR semantics and isolation
    - **Property 11: OR semantics with activation isolation**
    - Generate multi-trigger interleavings with injected run failures; assert one
      activation per non-discarded firing, activation_group = trigger node id, and
      continued dispatch after failures
    - **Validates: Requirements 7.1, 7.7**

  - [x] 6.6 Write example tests for lifecycle and dispatch wiring
    - Stub transports: groups start on registration and stop on removal/invalidation
      (6.1, 6.2); dispatcher creates a pending `WorkflowExecution` with
      `trigger_context_json` and at most one in-flight run (7.6); failing run
      containment (7.7); manager no-op with zero trigger workflows and contained
      constructor failure (12.4)
    - _Requirements: 6.1, 6.2, 7.6, 7.7, 12.4_

- [x] 7. MQTT transports and reconnect engine (C5)
  - [x] 7.1 Implement `GreengrassIpcSubscriber`
    - `awsiot.greengrasscoreipc` `SubscribeToIoTCoreRequest` + stream handler
      (`on_stream_event` → Trigger_Context `{topic, payload, qos, timestamp}`), qos
      clamped to `GREENGRASS_MAX_QOS` like the publish path; `UnauthorizedError` →
      Trigger_Health `failed` with the accessControl-naming diagnostic mirroring
      `_default_greengrass_publisher`'s denial text for `SubscribeToIoTCore`, not
      counted against `retry_limit`; stream error/close → reconnect path
    - _Requirements: 6.3, 6.8, 8.7_

  - [x] 7.2 Implement `AwsIotTlsSubscriber` and `PlainBrokerSubscriber`
    - Long-lived paho clients mirroring `_default_mqtt_publisher`'s connection config:
      aws_iot path with `client_id=iot_thing_name` + `tls_set(ca, cert, key)`; plain
      path with `connect(broker_host, broker_port)`; `subscribe(topic, qos)` on connect,
      `on_message` → Trigger_Context, `on_disconnect` → reconnect path
    - _Requirements: 6.4, 6.5, 6.8_

  - [x] 7.3 Implement the shared reconnect/backoff engine and health transitions
    - Per-worker `on_connection_lost`: exponential backoff 1 s doubling capped 60 s,
      attempts vs `retry_limit` (0 = forever); health `reconnecting` (attempt count) →
      `subscribed`/`polling` on success (count reset) → `failed` on exhaustion with the
      actionable message naming node id + topic/endpoint, worker parked
    - _Requirements: 8.1, 8.2, 8.3, 9.1_

  - [x] 7.4 Write property test for the qos clamp
    - **Property 14: Greengrass subscribe QoS clamp**
    - For all qos values, assert the subscribe request carries `min(qos, 1)`
    - **Validates: Requirements 6.3**

  - [x] 7.5 Write property test for reconnect behavior
    - **Property 12: Reconnect bounding, backoff, and failure marking**
    - Generate retry_limit values and failure sequences with injected clock/sleep;
      assert delay series, attempt bounds (unbounded when 0), and failed-state message
      contents on exhaustion
    - **Validates: Requirements 8.1, 8.3**

  - [x] 7.6 Write example tests for the MQTT transports
    - Stub IPC/paho clients: request/topic/qos wiring (6.3), aws_iot client_id + tls_set
      args (6.4), plain connect/subscribe args (6.5), health transitions on
      loss/restore (8.2), `UnauthorizedError` → failed + accessControl-naming message +
      no retries (8.7)
    - _Requirements: 6.3, 6.4, 6.5, 8.2, 8.7_

- [x] 8. OPC UA worker: subscription, watchdog, fallback, polling (C6)
  - [x] 8.1 Implement the OPC UA session and subscription setup
    - Session build reusing the `opcua_write` security mapping
      (`_opcua_security_from_params` + set_user/set_password/set_security_string);
      `create_subscription(sampling_interval_ms, handler)` + `subscribe_data_change`;
      `datachange_notification` → Trigger_Context
      `{endpoint, node_id, value, source_timestamp}`
    - _Requirements: 6.6, 6.8_

  - [x] 8.2 Implement the Liveness_Watchdog, auto-fallback, and poll mode
    - Watchdog thread: keepalive read of the server-status node every 5 s;
      `TimeoutError`/`BrokenPipeError`/`ConnectionError`/`CancelledError` (or any
      keepalive failure) → full session teardown → reconnect path; setup failure of
      subscription/monitored item → Polling_Fallback at `poll_interval_ms` (health
      mechanism `poll` + autoFallback, transition logged); explicit `mode=poll` reads
      `node_id` every `poll_interval_ms` and fires on value change; reconnect rebuilds
      always attempt true subscription first
    - _Requirements: 6.7, 8.4, 8.5, 8.6_

  - [x] 8.3 Write property test for security mapping parity
    - **Property 15: OPC UA security mapping parity**
    - Generate security parameter combinations; assert the subscription session's client
      configuration calls equal `_default_opcua_writer`'s for the same values
    - **Validates: Requirements 6.6**

  - [x] 8.4 Write property test for Trigger_Context fidelity
    - **Property 13: Trigger_Context fidelity**
    - Generate MQTT deliveries and OPC UA value sequences through stub transports;
      assert exact context shapes/values, subscribe-vs-poll shape identity, poll firing
      exactly on value change, and `trigger_context_json` persisted on the execution row
    - **Validates: Requirements 6.7, 6.8**

  - [x] 8.5 Write example tests for watchdog and fallback branches
    - Parametrize the four watchdog exception types → teardown + reconnect (8.4); stub
      failing `create_subscription` → poll fallback + health + log (8.5); rebuilt session
      attempts subscribe first (8.6)
    - _Requirements: 8.4, 8.5, 8.6_

- [x] 9. Trigger health in the workflow API (C4)
  - [x] 9.1 Surface `triggerHealth` on the registration detail endpoint
    - `api.py` registration detail gains an additive `triggerHealth` field populated
      from the manager via `runtime`; absent/empty for trigger-less registrations, all
      existing response fields unchanged
    - _Requirements: 9.1, 9.2_

  - [x] 9.2 Write example tests for health surfacing
    - Response with and without trigger registrations: field presence/absence,
      record contents across representative states, existing fields unchanged
    - _Requirements: 9.1, 9.2_

- [x] 10. Checkpoint — device engine suite
  - Run `PYTHONPATH=src/backend:test/backend-test python3 -m pytest
    test/backend-test/workflow_engine/` (includes the vendored-mirror test guarding
    Property 17). Ensure all tests pass, ask the user if questions arise.

- [x] 11. Packaging and deployment accessControl (C7)
  - [x] 11.1 Record subscribed topics at packaging time
    - `workflow_packaging.py`: `gather_subscribed_topics(definition)` — sorted,
      de-duplicated `topic` values of `mqtt_subscribe` nodes with effective
      `greengrass=True`; record as `subscribed_topics` on the version item and in the
      packaged `manifest.json` only when non-empty (byte-identical packaging otherwise)
    - _Requirements: 10.1, 10.4_

  - [x] 11.2 Merge the SubscribeToIoTCore policy at deployment time
    - `deployments.py` `create_deployment`: when the set contains a workflow component
      with recorded `subscribed_topics` and the LocalServer component, deep-merge the
      `dda:workflow-subscribe:<workflowId>` policy
      (`aws.greengrass#SubscribeToIoTCore`, resources = the topics) into the LocalServer
      entry's `configurationUpdate.merge`, leaving recipe-default policies untouched;
      when LocalServer is absent, append an actionable warning naming the workflow,
      topics, and remedy
    - _Requirements: 10.2, 10.3_

  - [x] 11.3 Write property test for topic recording and the merge
    - **Property 16: Subscribed-topics packaging and accessControl merge**
    - Generate definitions mixing greengrass/aws_iot/broker subscribe nodes and
      non-subscribing workflows; assert recorded topics, absence when empty with
      byte-identical packaging, and merged policy key/operations/resources with
      non-interference
    - **Validates: Requirements 10.1, 10.2, 10.4**

  - [x] 11.4 Write example tests for the deployment warning and goldens
    - Deployment set without LocalServer → warning contents (10.3); existing packaging
      goldens under `test/backend-test/deploy_reliability/goldens/` pass unchanged
    - _Requirements: 10.3, 10.4_

- [x] 12. Checkpoint — full suites across portal, device, and frontend
  - Portal catalog: `cd edge-cv-portal/backend && python3 -m pytest layers/workflow_core/tests/`
  - Portal backend: `cd edge-cv-portal/backend && python3 -m pytest tests/`
  - Device engine: `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/`
  - Frontend: `cd edge-cv-portal/frontend && npx vitest run src/pages/workflows/`
  - Both catalog-mirror byte tests must pass (**Property 17: Catalog copies stay
    byte-identical** — Validates: Requirements 3.3);
    `golden_zero_trigger_compilation.json` must pass unchanged
  - Ignore known pre-existing failures per repo steering; ensure all other tests pass,
    ask the user if questions arise.

- [ ] 13. Integration checkpoints — portal deploy and LocalServer device build (REQUIRES USER COORDINATION)
  - Portal side (catalog/validator/compiler/packaging/deployments/frontend) goes live
    via a portal deploy; device side (`trigger_runtime.py`, vendored copies, migration)
    rides a LocalServer gdk build — run either only with the user's explicit go-ahead
  - Verify after deploy/build: the designer offers both triggers with gated parameters;
    a packaged subscribing workflow's version item records `subscribed_topics`
  - _Requirements: 3.5, 6.1, 10.1_

- [x] 14. On-hardware verification on the JP6 device (GATED — requires user go-ahead)
  - Real MQTT-subscribe activation: deploy a trigger-driven workflow (greengrass path)
    with the accessControl merge; publish to the topic via IoT Core; verify the run
    activates, `trigger_context_json` is persisted, and `triggerHealth` reads
    `subscribed`; verify a denial on an unauthorized topic surfaces the actionable
    diagnostic
  - Real OPC UA subscription: local OPC UA server on the device (as in the Phase 0.2
    spike); verify data-change activation, then kill the server and verify the
    Liveness_Watchdog detects the silent death, reconnect/backoff engages, and health
    surfaces `reconnecting` → recovery (or `failed` at a small `retry_limit`)
  - _Requirements: 6.3, 6.6, 8.1, 8.4, 8.7, 9.2, 10.2_

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP;
  core implementation tasks are never optional.
- **Byte-in-sync discipline**: every `catalog/`, `validator/`, and `compiler/` edit in
  `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/` ends by copying
  the file verbatim to `src/backend/workflow_engine/vendor/workflow_core/`; the mirror
  tests on both sides enforce it (Property 17).
- **Hard invariants**: `golden_zero_trigger_compilation.json` and the packaging goldens
  pass unchanged; `digital_input`, `mqtt_publish`, and `opcua_write` descriptors stay
  byte-identical (catalog-baseline delta scoped to the two new descriptors).
- **Property tests**: Hypothesis (backend) / fast-check (frontend), ≥ 100 iterations,
  each tagged `Feature: trigger-activation-runtime, Property {n}: {property text}`.
- **Property → requirements traceability**: P1 (1.2, 2.2, 2.3, 2.5), P2 (1.6), P3 (4.1),
  P4 (4.2), P5 (4.5), P6 (3.1, 3.6), P7 (5.1, 5.2), P8 (5.3, 5.4, 12.1),
  P9 (7.2–7.4), P10 (7.5), P11 (7.1, 7.7), P12 (8.1, 8.3), P13 (6.7, 6.8), P14 (6.3),
  P15 (6.6), P16 (10.1, 10.2, 10.4), P17 (3.3).
- Trigger_Transform (custom code between trigger and input) is out of scope
  (Sub-feature C); the activation-group extension point is reserved, AND semantics are
  not implemented.
- This spec does **not** touch `.kiro/specs/workflow-triggers-and-input-overhaul/` or
  `.kiro/specs/triggers-stage-and-unified-input/`.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.4", "2.1", "5.1"] },
    { "id": 2, "tasks": ["3.1", "5.2", "5.3"] },
    { "id": 3, "tasks": ["1.2", "1.3", "2.2", "2.3", "2.4", "2.5", "3.2", "3.3", "3.4", "5.4", "5.5", "5.6"] },
    { "id": 4, "tasks": ["6.1"] },
    { "id": 5, "tasks": ["6.2", "7.3"] },
    { "id": 6, "tasks": ["7.1", "7.2", "8.1", "9.1", "11.1"] },
    { "id": 7, "tasks": ["8.2", "11.2"] },
    { "id": 8, "tasks": ["6.3", "6.4", "6.5", "6.6", "7.4", "7.5", "7.6", "8.3", "8.4", "8.5", "9.2", "11.3", "11.4"] }
  ]
}
```
