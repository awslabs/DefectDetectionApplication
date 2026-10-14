# Requirements Document

## Introduction

The workflow manager's `mqtt_publish` output node publishes a rendered payload on every run, over one of three paths: the device's Greengrass-managed connection to AWS IoT Core (Greengrass IPC `PublishToIoTCore`), a plain MQTT broker (paho), or AWS IoT Core over mutual TLS (paho). Every publish is sent with the MQTT retain flag off, so a consumer that connects after a run (an HMI page load, a PLC gateway reconnecting, a dashboard) sees nothing until the next run. Users who want "the last status is always available on the topic" have no way to ask for it.

This feature adds a **Retain message** checkbox to the `mqtt_publish` node in the workflow manager. When checked, the edge executor publishes the message with the MQTT retain flag set on whichever path the node uses, so the broker (or AWS IoT Core) keeps the last message per topic and delivers it to new subscribers. Unchecked (the default, and the value every existing workflow compiles to) leaves every publish byte-identical to today.

The change touches the shared node catalog (portal layer, vendored to the edge), the LocalServer executor, the workflow manager UI, and the IoT policy guidance: AWS IoT Core requires the `iot:RetainPublish` permission for retained publishes, which no DDA-provided policy grants today. Because the executor change runs on device, it requires a LocalServer component build and real-hardware verification before it is committed, per the repo's edge rule.

## Glossary

- **MQTT_Publish_Node**: the `mqtt_publish` node type in the shared workflow_core catalog (`NodeTypeDescriptor` `MQTT_PUBLISH`, category output).
- **Retain_Flag**: the new boolean `retain` parameter of the MQTT_Publish_Node (default `False`). Maps one-to-one to the MQTT RETAIN bit of the published message.
- **Retained_Message**: an MQTT message published with the RETAIN bit set; the broker stores the last one per topic and delivers it to every new subscriber of that topic.
- **Greengrass_Path**: the publish path taken when the node's `greengrass` parameter is true: Greengrass IPC `PublishToIoTCore` through the on-device nucleus (`_default_greengrass_publisher`).
- **Broker_Path**: the publish path taken when neither `greengrass` nor `aws_iot` is true: `paho.mqtt.publish.single` to `broker_host:broker_port` (`_default_mqtt_publisher`).
- **AWS_IoT_Path**: the publish path taken when `aws_iot` is true: paho over mutual TLS to the IoT Core endpoint with the `iot_*` certificate parameters.
- **Node_Catalog**: the shared workflow_core catalog whose source of truth is `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`; the LocalServer consumes a generated copy.
- **Vendored_Mirror**: `src/backend/workflow_engine/vendor/workflow_core/`, produced by `src/backend/workflow_engine/vendor/re_vendor.sh` and pinned sha256-identical to the Node_Catalog by `test/backend-test/workflow_engine/test_vendored_catalog_mirror.py`.
- **Catalog_Baseline**: `edge-cv-portal/backend/layers/workflow_core/tests/catalog_baseline.json`, the per-node-type golden the catalog preservation tests compare against; refreshed per entry, never dumped wholesale.
- **Compiled_Document**: the `CompiledPipelineDocument` produced by `workflow_core.compiler.compile`; its `executorBindings[*].parameters` carries every declared parameter default overlaid with the node's explicit values (`_effective_parameters`).
- **Executor**: `OutputBindingProcessor` in `src/backend/workflow_engine/output_bindings.py`, whose `_run_mqtt_publish` reads a binding's parameters and calls the injected publisher for the selected path.
- **Publisher_Call**: the positional-argument call the Executor makes to the injected `mqtt_publisher` `(host, port, topic, payload, qos[, client_id, tls])` or `greengrass_publisher` `(topic, payload, qos)`; pinned by `test_mqtt_publish_call_preservation.py` and `test_mqtt_greengrass_dispatch.py`.
- **Sent_Message_Detail**: the bounded string `_mqtt_detail` returns (`sent to topic '<t>' (qos <q>, <path>): <preview>`) recorded into the run's node status.
- **IoT_Policy**: the AWS IoT (not IAM) policy attached to a device's certificate. A retained publish to AWS IoT Core requires the `iot:RetainPublish` action on the topic in addition to `iot:Publish`.
- **Core_Device_Policy**: the IoT_Policy of the Greengrass core device's own certificate; the Greengrass_Path publishes through that connection, so its permissions govern the Greengrass_Path.
- **Trigger_Context**: the `{topic, payload, qos, timestamp}` dict the `mqtt_subscribe` trigger delivers into a run's metadata (`trigger_runtime.py`).

## Requirements

### Requirement 1: The catalog declares the Retain_Flag

**User Story:** As a workflow author, I want the MQTT Publish node to offer a retain option, so that the broker keeps my last published status for late subscribers.

#### Acceptance Criteria

1. THE Node_Catalog SHALL declare on the MQTT_Publish_Node a `ParameterDescriptor` named `retain` with `param_type = "bool"`, `required = False`, `default = False`, `constraints = {}`, no `depends_on`, `examples = [True]`, positioned immediately after the `qos` parameter.
2. THE `retain` description SHALL state that the message is published with the MQTT retain flag so the broker or AWS IoT Core keeps the last message per topic for new subscribers, that publishing retained messages to AWS IoT Core (the Greengrass and AWS IoT paths) requires the device's IoT policy to allow `iot:RetainPublish` on the topic, and that a retained topic should not also be a workflow trigger topic (see Requirement 8).
3. THE Node_Catalog SHALL leave every other MQTT_Publish_Node parameter (name, order, type, default, constraints, description, `depends_on`), its ports, its mappings, its plugin dependencies and `hardware_dependent` byte-identical to the current definition.
4. THE Node_Catalog SHALL NOT add `retain` to the `mqtt_subscribe` node type; the connection parameters mirrored between `mqtt_subscribe` and `mqtt_publish` (`topic`, `qos`, `greengrass`, `aws_iot`, `iot_*`, `broker_host`, `broker_port`) SHALL remain field-for-field equal.
5. WHEN the Node_Catalog changes, THE Vendored_Mirror SHALL be regenerated with `re_vendor.sh` so that `test_vendored_catalog_mirror.py` passes, and THE Catalog_Baseline SHALL NOT be modified: its `mqtt_publish` entry is recorded bug-condition evidence whose preservation tests pin only the parameters present in the baseline, so an added parameter needs no baseline change.
6. THE catalog validator SHALL accept `retain` values `true` and `false` and SHALL report `V4_INVALID_PARAMETER_VALUE` for a non-boolean `retain` value, through the existing generic V4 type check and with no mqtt_publish-specific validator code.
7. THE V6 check (an MQTT_Publish_Node must enable `greengrass`, enable `aws_iot`, or set `broker_host`) SHALL be unchanged.

### Requirement 2: The workflow manager renders a Retain message checkbox

**User Story:** As a workflow author, I want a checkbox on the MQTT Publish node's configuration panel, so that I can turn retention on without editing JSON.

#### Acceptance Criteria

1. WHEN the node configuration panel renders an MQTT_Publish_Node, THE Portal SHALL render the `retain` parameter as a checkbox labelled `Retain message`, through the existing generic bool-parameter rendering (`ParameterControl` for `paramType === 'bool'`) with the label supplied via `PARAMETER_DISPLAY_LABELS`.
2. THE checkbox SHALL be visible regardless of the `greengrass` and `aws_iot` values (no `dependsOn`).
3. WHEN a node has no explicit `retain` value (every workflow saved before this feature), THE checkbox SHALL render unchecked.
4. WHEN the user checks the checkbox, THE Portal SHALL call `onParametersChange(nodeId, { retain: true })`; WHEN the user unchecks it, `{ retain: false }`.
5. THE catalog-served `retain` description SHALL render below the checkbox exactly as the `aws_iot` description does today.
6. THE Portal SHALL make no other change to the MQTT_Publish_Node's configuration panel; the `aws_iot` checkbox and the `iot_*` `dependsOn` gating SHALL behave byte-identically.

### Requirement 3: The Retain_Flag reaches the device through the compiled document

**User Story:** As the edge executor, I want the retain setting present in every compiled mqtt_publish binding, so that I never have to guess.

#### Acceptance Criteria

1. WHEN a workflow is compiled, THE Compiled_Document's `executorBindings` entry for each MQTT_Publish_Node SHALL carry `parameters.retain`, the node's explicit value when set, else `false`, through the existing `_effective_parameters` materialisation and with no compiler code change.
2. WHEN a workflow saved before this feature is compiled, THE Compiled_Document SHALL differ from its pre-feature compilation only by the added `"retain": false` key in each mqtt_publish binding's `parameters`.
3. THE workflow packaging, component recipe generation (including the LocalServer `VersionRequirement` floor and the `aws.greengrass.ipc.mqttproxy` `accessControl` merge for publish topics) and deployment flows SHALL be unchanged by the presence of `retain`.
4. THE simulation (test harness) recording binding for `mqtt_publish` SHALL record `retain` alongside the other parameters and SHALL NOT contact a broker.

### Requirement 4: The Executor publishes retained messages on every path

**User Story:** As a workflow author, I want the retain option to work the same whether I publish through Greengrass, a plain broker, or AWS IoT over mutual TLS, so that I do not have to know which path sets which bit.

#### Acceptance Criteria

1. THE Executor SHALL read the Retain_Flag as `bool(parameters.get("retain", False))`.
2. WHEN the Retain_Flag is true on the Greengrass_Path, THE Executor SHALL set `retain = True` on the `PublishToIoTCoreRequest` alongside `topic_name`, `payload` and `qos`, keeping the existing QoS clamp to `AT_LEAST_ONCE`/`AT_MOST_ONCE`.
3. WHEN the Retain_Flag is true on the Broker_Path, THE Executor SHALL pass `retain=True` to `paho.mqtt.publish.single` alongside the existing `topic`, `payload`, `qos`, `hostname`, `port`, `client_id` and `tls` arguments.
4. WHEN the Retain_Flag is true on the AWS_IoT_Path, THE Executor SHALL pass `retain=True` to `paho.mqtt.publish.single` with the existing mutual-TLS arguments, port switch (1883 to 8883) and QoS clamp to 1 unchanged.
5. WHEN the Retain_Flag is false or absent, THE Executor SHALL make every Publisher_Call byte-identical to the current implementation: the same positional arguments and no keyword arguments, and a `PublishToIoTCoreRequest` on which `retain` is never assigned.
6. WHEN the Retain_Flag is true, THE Executor SHALL make the Publisher_Call with the same positional arguments as today plus exactly one keyword argument `retain=True`, so an injected publisher that does not accept `retain` fails loudly rather than silently dropping the flag.
7. THE default publishers `_default_mqtt_publisher` and `_default_greengrass_publisher` SHALL accept a keyword-only `retain: bool = False` parameter and SHALL behave byte-identically to today when it is false.
8. IF the installed `awsiot.greengrasscoreipc.model.PublishToIoTCoreRequest` does not expose a `retain` attribute (older SDK), THEN THE Executor SHALL raise a `RuntimeError` naming the node's topic and stating that retained publishing requires awsiotsdk 1.13 or newer and Greengrass nucleus 2.10 or newer, and SHALL NOT publish the message non-retained.
9. THE Executor SHALL NOT change the payload rendering, the attached-metadata embedding, the `AWS_IOT_REQUIRED_PARAMETERS` check, or the containment of per-node errors into `OutputBindingError`.

### Requirement 5: The Sent_Message_Detail records retention

**User Story:** As an operator reading a run's node status, I want to see that a message was published retained, so that I can tell a sticky status from a transient one.

#### Acceptance Criteria

1. WHEN the Retain_Flag is true, THE Sent_Message_Detail SHALL be `sent to topic '<topic>' (qos <qos>, <path>, retained): <preview>` where `<path>` is `plain`, `aws_iot` or `greengrass` exactly as today.
2. WHEN the Retain_Flag is false, THE Sent_Message_Detail SHALL be byte-identical to the current implementation.
3. THE payload preview bound (`DETAIL_PREVIEW_LIMIT`) and truncation marker SHALL be unchanged.

### Requirement 6: Authorization failures name the missing permission

**User Story:** As a workflow author whose retained publish is rejected, I want the run error to tell me which permission is missing, so that I fix the IoT policy instead of the workflow.

#### Acceptance Criteria

1. WHEN the Greengrass_Path publish raises the IPC `UnauthorizedError` and the Retain_Flag is true, THE Executor SHALL re-raise the existing actionable `RuntimeError` (naming the topic and the `aws.greengrass.ipc.mqttproxy` `accessControl`) extended with a sentence stating that a retained publish additionally requires `iot:RetainPublish` on the topic in the core device's IoT policy.
2. WHEN the Retain_Flag is false, THE `UnauthorizedError` re-raise text SHALL be byte-identical to the current implementation.
3. THE `retain` parameter description (Requirement 1.2) SHALL carry the same `iot:RetainPublish` guidance so the author sees it before deploying.

### Requirement 7: DDA-provided IoT policies allow retained publishing where they allow publishing

**User Story:** As a device operator, I want the IoT policies DDA provisions or documents to permit retained publishes on the same topics they permit publishes, so that checking the box does not fail at the broker.

#### Acceptance Criteria

1. WHEN `device_provisioning.py` generates a device IoT_Policy, THE Portal SHALL add `iot:RetainPublish` to the statement that grants `iot:Publish`, on the identical `Resource` list, and SHALL change nothing else in the policy.
2. THE documented IoT policy examples for edge devices (`README_main.md` Greengrass device policy example and `station_install/README.md`) SHALL list `iot:RetainPublish` alongside `iot:Publish` on the same topic resources.
3. THE IAM policies for the token-exchange role (`station_install/edge-device-iam-policy.json`, `station_install/create-edge-device-iam-role.sh`, the portal account role) SHALL NOT be changed: `iot:RetainPublish` is an IoT data-plane permission evaluated against the certificate's IoT_Policy, not the IAM role.
4. THE feature documentation (the `retain` description and the workflow manager user guide, if one exists for output nodes) SHALL state that for an already-provisioned Greengrass core device the operator must add `iot:RetainPublish` to the Core_Device_Policy by hand, because the portal cannot edit a policy it did not create.
5. THE security audit gates (`test/backend-test/security/`) SHALL remain green: `iot:RetainPublish` SHALL appear only on topic-scoped resources (never `"Resource": "*"`), and any preservation baseline that pins a touched file SHALL be rebaselined in the same change with the reason recorded in the commit.

### Requirement 8: The subscribe side is unchanged and the replay hazard is documented

**User Story:** As a workflow author, I want to understand what happens if I retain on a topic my workflow also listens to, so that I do not create a run loop.

#### Acceptance Criteria

1. THE `mqtt_subscribe` trigger transports SHALL be unchanged: the Trigger_Context SHALL remain exactly `{topic, payload, qos, timestamp}` and neither transport SHALL read or act on the incoming message's retain bit.
2. THE `retain` description and the design document SHALL state that a broker redelivers a Retained_Message to every new subscription (including every reconnect and every LocalServer restart), so a retained topic that is also an `mqtt_subscribe` trigger topic re-fires the trigger on each of those events; retaining on trigger topics is unsupported.
3. Adding retain-awareness to the trigger side (skipping retained deliveries or surfacing the bit in the Trigger_Context) is out of scope for this feature.

### Requirement 9: Compatibility with LocalServer versions that predate the feature

**User Story:** As a device operator with a mixed fleet, I want a workflow with retain enabled to deploy safely to a device whose LocalServer does not know the flag, so that the deployment does not fail.

#### Acceptance Criteria

1. WHEN a Compiled_Document carrying `retain: true` is executed by a LocalServer that predates this feature, THE Executor of that version SHALL ignore the unknown key and publish non-retained, as it does today for any unknown parameter (no deployment failure, no run failure).
2. THE feature SHALL NOT raise the workflow component's LocalServer `VersionRequirement` floor automatically for `retain`; the packaging floor mechanism (`min_local_server_version_for`) is unchanged.
3. THE design document SHALL record the first LocalServer version per architecture that honours the Retain_Flag, so operators can raise the floor deliberately via `WORKFLOW_MIN_LOCAL_SERVER_VERSIONS` if they need retention guaranteed.

### Requirement 10: Preservation gates and verification

**User Story:** As a maintainer, I want the change proven non-regressive by the existing suites and verified on real hardware, so that publishing keeps working for every existing workflow.

#### Acceptance Criteria

1. THE existing Publisher_Call preservation suites (`test_mqtt_publish_call_preservation.py`, `test_mqtt_greengrass_dispatch.py`, `TestMqttPublish*` in `test_workflow_output_bindings.py`, `TestMqttSentDetail` in `test_output_sent_message_details.py`) SHALL pass without modification.
2. THE catalog suites SHALL pass after the intended edits only: the exact parameter-name list in `test_unified_input_descriptor.py::TestOutputDescriptorsUnchanged::test_mqtt_publish_category_ports_and_parameter_names` extended with `retain` after `qos`, and the compiled-bytes golden `tests/golden_zero_trigger_compilation.json` regenerated (its `icam_source -> model_inference -> mqtt_publish` documents gain exactly one `"retain": false` default per device architecture and nothing else — verify with `diff` before committing); the Catalog_Baseline SHALL NOT be modified (Requirement 1.5) and `test_property_descriptor_mirroring.py` SHALL pass unmodified.
3. THE Vendored_Mirror guard (`test_vendored_catalog_mirror.py`) and a `diff -r` of the vendored compiler/validator against the portal layer SHALL show the catalog change only.
4. THE frontend suites (`NodeConfigPanel.test.tsx`, `parameters.test.ts`) SHALL pass with the fixture extended, and `npm run build` SHALL succeed.
5. THE security preservation suite SHALL be run before the LocalServer build per `.kiro/steering/builds.md`, including the IAM out-of-scope guard.
6. BEFORE the edge change is committed, IT SHALL be verified on a real device running a LocalServer built from the change: a workflow with an MQTT_Publish_Node on the Greengrass_Path with `retain` checked publishes a message that `aws iot-data get-retained-message --topic <topic>` returns; a second MQTT_Publish_Node with `retain` unchecked on another topic publishes a message that `get-retained-message` does not return; the Sent_Message_Detail of the first shows `retained` and of the second does not; the Greengrass nucleus's IoT Core connection stays connected across the retained publish (`greengrass.log` shows no disconnect); the backend stays healthy for the observation period. WHEN a plain broker is available, the Broker_Path SHALL additionally be verified with `mosquitto_sub -F %r` showing the retain bit.
7. THE verification SHALL cover every LocalServer architecture the change is built for (at minimum the JP7 device available today); the commit or PR SHALL state which devices were verified.
