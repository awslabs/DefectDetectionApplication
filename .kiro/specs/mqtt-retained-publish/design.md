# Design Document

## Overview

**Guiding rule: one new catalog parameter, threaded to the three publish calls as an opt-in keyword; every existing publish stays byte-identical.**

```
Workflow manager (portal)                     Compiled document              LocalServer (device)
-------------------------                     -----------------              --------------------
mqtt_publish node config panel                executorBindings[i]            OutputBindingProcessor
  [x] Retain message   <-- generic bool  -->    parameters: {                  _run_mqtt_publish
      (PARAMETER_DISPLAY_LABELS)                  topic, qos, ...,               retain = bool(params.get("retain", False))
                                                  retain: true|false           |
Node_Catalog (portal layer nodes.py)              }                            +-- greengrass  -> _default_greengrass_publisher(topic, payload, qos[, retain=True])
  ParameterDescriptor("retain","bool",...)                                    |                    PublishToIoTCoreRequest.retain = True
  |  re_vendor.sh                             _effective_parameters           +-- plain       -> _default_mqtt_publisher(host, port, topic, payload, qos[, retain=True])
  v                                           materialises the default        |                    paho publish.single(..., retain=True)
Vendored_Mirror (edge nodes.py)               (no compiler change)            +-- aws_iot     -> _default_mqtt_publisher(..., client_id, tls[, retain=True])
                                                                              Sent_Message_Detail: "(qos 1, greengrass, retained)"
```

The retain bit is a property of the MQTT PUBLISH packet, identical on every path, so the design adds exactly one boolean and threads it. The subtle parts are (a) preservation: dozens of tests pin the exact positional Publisher_Call shape and the detail string, so the flag is passed as a keyword argument only when true; (b) authorization: AWS IoT Core evaluates `iot:RetainPublish` separately from `iot:Publish`, and on the Greengrass path a denial can affect the nucleus's shared connection, so the policy guidance and the error text carry the permission name; (c) the catalog lives in the portal layer and is mirrored into the edge tree by a script and a sha256 guard, so the catalog change is made once and vendored, never hand-edited on the edge side.

## Architecture

### `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py` (source of truth) and `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py` (generated)

Insert one descriptor into `MQTT_PUBLISH.parameters` directly after `qos`:

```python
        # MQTT retain bit. Off by default so every existing workflow
        # compiles to retain=False and publishes byte-identically. Not
        # gated on greengrass/aws_iot: the bit means the same thing on
        # every path. AWS IoT Core (greengrass and aws_iot paths)
        # additionally requires iot:RetainPublish in the device's IoT
        # policy; a retained topic must not double as a trigger topic
        # (the broker replays it on every subscribe/reconnect).
        ParameterDescriptor("retain", "bool", required=False, default=False,
                            constraints={},
                            description="Publish with the MQTT retain flag "
                                        "so the broker (or AWS IoT Core) "
                                        "keeps the last message on the topic "
                                        "and delivers it to new subscribers. "
                                        "For AWS IoT Core (Greengrass and "
                                        "AWS IoT paths) the device's IoT "
                                        "policy must also allow "
                                        "iot:RetainPublish on the topic. Do "
                                        "not retain on a topic that is also "
                                        "an MQTT trigger of a workflow: the "
                                        "retained message re-fires the "
                                        "trigger on every reconnect.",
                            examples=[True]),
```

Rationale for the position: the UI renders parameters in declaration order, so `retain` sits with `qos` under the delivery settings rather than after the certificate paths. Rationale for no `depends_on`: the bit applies on all three paths.

Procedure (from `src/backend/workflow_engine/vendor/README.md`):
1. Edit the portal-layer file only.
2. Run `src/backend/workflow_engine/vendor/re_vendor.sh` to regenerate the Vendored_Mirror; `test_vendored_catalog_mirror.py` must pass.
3. Do not touch `catalog_baseline.json`. The `mqtt_publish` entry deliberately records pre-Bug-2 values; `TestMqttPublishPreservation::test_unchanged_parameters_are_byte_identical_to_baseline` iterates the baseline's parameters (so a parameter added only to the live catalog is never compared) and `TestCatalogPreservation` excludes `mqtt_publish` from exact equality. Refreshing the entry would break `test_broker_host_changed_only_in_required_flag`.
4. Update the exact parameter-name list in `test_unified_input_descriptor.py::test_mqtt_publish_category_ports_and_parameter_names` (insert `"retain"` after `"qos"`). `test_property_descriptor_mirroring.py::_MQTT_CONNECTION_PARAMS` is NOT extended (publish-only parameter).
5. Regenerate `tests/golden_zero_trigger_compilation.json`. It pins the compiled bytes of `icam_source -> model_inference -> mqtt_publish` on every device architecture, and `_effective_parameters` now materialises `"retain": false` into each of those six mqtt_publish bindings — the exact "same document plus a `retain: False` default" delta Requirement 3.2 describes. Regenerate with the module-documented `_write_golden()` (append `python/` to `sys.path`; do not prepend, the layer's vendored manylinux `rpds` shadows the host one) and confirm with `diff` that the only change is six added `"retain": false` lines before committing. Both `test_property_zero_trigger_preservation.py` and `test_property_no_new_trigger_preservation.py` read this golden.

These two test-side edits (4, 5) are the only ones the catalog change legitimately requires. No validator or compiler change: V4 type-checks the declared bool generically; `_effective_parameters` materialises the default into every compiled mqtt_publish binding.

### `edge-cv-portal/frontend/src/pages/workflows/NodeConfigPanel.tsx`

The generic renderer already produces a `Checkbox` for `paramType === 'bool'` and shows the catalog description beneath it. The only change is a display label:

```ts
const PARAMETER_DISPLAY_LABELS: Record<string, string> = {
  // ...
  aws_iot: 'AWS IoT support',
  retain: 'Retain message',
  // ...
};
```

Test fixture (`NodeConfigPanel.test.tsx` `MQTT_PUBLISH`): add the `retain` descriptor AFTER `aws_iot` and the `iot_*` entries, so the existing `findCheckbox()` (first checkbox) assertions in the `mqtt_publish AWS IoT support` describe block keep resolving to `aws_iot`. New tests select the retain checkbox by its label. No change to `parameters.ts` (bool predicate already exact).

### `src/backend/workflow_engine/output_bindings.py`

Four edits, all additive.

1. Publishers accept a keyword-only `retain`:

```python
def _default_mqtt_publisher(host, port, topic, payload, qos,
                            client_id=None, tls=None, *, retain=False):
    import paho.mqtt.publish as mqtt_publish
    retain_kwargs = {"retain": True} if retain else {}
    mqtt_publish.single(
        topic, payload=payload, qos=int(qos), hostname=host, port=int(port),
        client_id=client_id or "", tls=dict(tls) if tls else None,
        **retain_kwargs,              # forwarded only when true
    )


def _default_greengrass_publisher(topic, payload, qos, *, retain=False):
    ...
    request = model.PublishToIoTCoreRequest()
    request.topic_name = topic
    request.payload = payload.encode("utf-8")
    request.qos = qos_value
    if retain:
        if not hasattr(request, "retain"):
            raise RuntimeError(
                "Retained publishing to topic '{0}' requires awsiotsdk >= 1.13 "
                "(PublishToIoTCoreRequest.retain) and Greengrass nucleus >= 2.10; "
                "the installed SDK has no retain field".format(topic))
        request.retain = True
    ...
    except model.UnauthorizedError as error:
        message = <existing accessControl text>
        if retain:
            message += (" A retained publish additionally requires "
                        "'iot:RetainPublish' on the topic in the core device's "
                        "IoT policy (the Greengrass core certificate's policy), "
                        "not only 'iot:Publish'.")
        raise RuntimeError(message) from error
```

`retain` is never assigned on the request when false, so the IPC request for existing workflows is unchanged (Requirement 4.5). The `hasattr` guard implements Requirement 4.8 without importing SDK version metadata.

The paho side applies the same only-when-true rule rather than always passing `retain=bool(retain)`: `test_workflow_output_bindings.py::test_default_publisher_forwards_client_id_and_tls_to_paho` and the `mqtt_boundary` fixture in `test_workflow_engine_integration.py` run the real `_default_mqtt_publisher` against a fake `publish.single(topic, payload, qos, hostname, port, client_id, tls)` that has no `retain` parameter, so an always-present keyword would `TypeError` in suites Requirement 10.1 says must pass unmodified. paho's own default is `retain=False`, so the wire behaviour is identical either way.

2. `_run_mqtt_publish` reads the flag once and passes it as a keyword only when true, preserving every pinned call shape:

```python
        topic = str(parameters["topic"])
        qos = int(parameters.get("qos", 0))
        retain = bool(parameters.get("retain", False))
        retain_kwargs = {"retain": True} if retain else {}

        if parameters.get("greengrass"):
            self._greengrass_publisher(topic, payload_text, qos, **retain_kwargs)
            return self._mqtt_detail(topic, qos, "greengrass", payload_text, retain)
        ...
        if not parameters.get("aws_iot"):
            self._mqtt_publisher(host, port, topic, payload_text, qos, **retain_kwargs)
            return self._mqtt_detail(topic, qos, "plain", payload_text, retain)
        ...
        self._mqtt_publisher(host, port, topic, payload_text, qos,
                             str(parameters["iot_thing_name"]), tls, **retain_kwargs)
        return self._mqtt_detail(topic, qos, "aws_iot", payload_text, retain)
```

Decision: keyword-only-when-true rather than a new positional argument. The preservation suites assert `calls == [((h, p, t, payload, q), {})]` for the injected recorders; a positional `retain=False` would change every existing call and force a wholesale rebaseline, while a keyword present only when true keeps them untouched and makes a publisher that ignores the flag fail with `TypeError` instead of silently publishing non-retained.

3. Detail string:

```python
    @staticmethod
    def _mqtt_detail(topic, qos, path, payload_text, retain=False):
        flags = "{0}, retained".format(path) if retain else path
        return "sent to topic '{0}' (qos {1}, {2}): {3}".format(
            topic, qos, flags, _preview(payload_text))
```

4. Docstrings of `_run_mqtt_publish` and both publishers gain a sentence on `retain`.

Nothing else in the module changes: payload rendering, `_embed_attached_metadata`, `AWS_IOT_REQUIRED_PARAMETERS`, QoS clamps, `OutputBindingError` aggregation.

### Trigger side (`src/backend/workflow_engine/trigger_runtime.py`)

No change. Both transports deliver `{topic, payload, qos, timestamp}` and ignore the retain bit; `test_property_trigger_context.py` and `test_mqtt_transports.py` pin that shape. The replay hazard is documented in the catalog description and here: a retained message is redelivered by the broker on every new subscription, which for `mqtt_subscribe` means every LocalServer restart, every reconnect after a network blip, and the first subscribe after deploy. A workflow that retains on its own trigger topic would therefore re-run itself on each of those events. Making the trigger retain-aware (skip retained deliveries, or add `retain` to the Trigger_Context) is a separate feature because it changes the pinned Trigger_Context shape.

### IoT policy: `edge-cv-portal/backend/functions/device_provisioning.py` and documentation

`device_provisioning.py` builds the IoT policy for portal-provisioned devices with a statement `{"Action": ["iot:Publish"], "Resource": ["arn:aws:iot:*:*:topicfilter/$aws/things/${iot:Connection.Thing.ThingName}/*"]}`. Change the action list to `["iot:Publish", "iot:RetainPublish"]` on the same resources; nothing else moves. (Note: that resource pattern only covers the thing's own `$aws/things/...` topics; workflow topics such as `quality/...` are governed by whatever policy the operator attached to the Greengrass core certificate, which the portal does not manage.)

Documentation: `station_install/README.md` gains a troubleshooting entry ("Retained MQTT Publish Denied") whose `create-policy-version` snippet lists `iot:RetainPublish` with `iot:Publish` in a NEW statement on topic-scoped resources, plus one sentence: "Required only if a workflow's MQTT Publish node has Retain message enabled; add it to an existing core device's IoT policy by hand." The installer's existing `iot:Publish` statement in that snippet is on `"Resource": "*"`, so the action is deliberately not appended there (Requirement 7.5). `README_main.md` carries no IoT policy — its `dda-greengrass-policy` is the token-exchange IAM role policy (attached to `dda-greengrass-role`) — so it gets a Troubleshooting "Retained MQTT publish denied" note pointing at the station_install entry and saying the IAM policy is unchanged, not an edit to that JSON. The IAM files (`edge-device-iam-policy.json`, `create-edge-device-iam-role.sh`, `setup_station.sh`) are not touched: `iot:RetainPublish` is a device data-plane action, so putting it in the token-exchange IAM role would be wrong and would trip the IAM audit's scoping rules.

Failure mode to document: with MQTT 3.1.1 (the nucleus default), AWS IoT Core drops the connection of a client that publishes without authorization. On the Greengrass_Path that connection is the nucleus's, so an unauthorized retained publish can briefly disconnect every cloud message on the device until the nucleus reconnects. This is why the description and the error text both name the permission, and why the on-device verification checks `greengrass.log` for disconnects.

### Compatibility and versions

- Older LocalServers ignore `retain` (they read only known keys from the binding parameters), so a `retain: true` workflow deploys and runs, publishing non-retained. No automatic `VersionRequirement` floor bump (Requirement 9). The first LocalServer versions that honour the flag are recorded here on release: `aws.edgeml.dda.LocalServer.arm64JP7` >= 1.0.32 (first build from this change; adjust to the actual published version), JP6/JP5 the first build containing this change. Operators who need retention guaranteed set `WORKFLOW_MIN_LOCAL_SERVER_VERSIONS` accordingly.
- awsiotsdk pinned at 1.31.0 in `src/backend/requirements.txt` carries `PublishToIoTCoreRequest.retain`; the Greengrass nucleus must be >= 2.10.0 to honour it (the DLAP runs 2.12.0). VERIFIED 2026-09-13 inside the running LocalServer JP7 1.0.31 backend container on adlink-dlap-701 (python3.11, awsiotsdk 1.31.0): `inspect.signature(PublishToIoTCoreRequest.__init__)` includes `retain`, and a bare `PublishToIoTCoreRequest()` has `retain is None`, so the `hasattr` guard passes there. paho-mqtt's `publish.single` also accepts `retain`. The wave-2 edge suites (23 retain tests + the four unmodified preservation suites + the three end-to-end anchors, 108 tests) were run in that same container against the modified `output_bindings.py` and all passed.

## Data models

Catalog (wire form served by `GET /workflows/node-catalog`, produced by `parameter_to_wire`):

```json
{"name": "retain", "paramType": "bool", "required": false, "default": false,
 "constraints": {}, "dependsOn": null,
 "description": "Publish with the MQTT retain flag ...", "examples": [true]}
```

Compiled binding (unchanged shape, one added key):

```json
{"nodeId": "mqtt_publish_4", "binding": "mqtt_publish",
 "parameters": {"broker_host": null, "broker_port": 1883, "topic": "quality/checker1/status",
                "payload_template": "{{\"status\":\"completed\"}}", "qos": 0,
                "retain": true, "greengrass": true, "aws_iot": false,
                "iot_thing_name": null, "iot_ca_cert_path": null,
                "iot_client_cert_path": null, "iot_private_key_path": null},
 "upstreamNodeIds": ["..."], "downstreamNodeIds": []}
```

## Correctness properties

- **P1 (preservation, off):** for every parameter combination with `retain` absent or false, across the three paths, the Publisher_Call `(args, kwargs)` recorded by an injected publisher and the returned Sent_Message_Detail equal those of the pre-feature executor byte-for-byte (`kwargs == {}`).
- **P2 (retain on):** for every parameter combination with `retain: true`, the Publisher_Call has the same positional `args` as P1's for that combination and `kwargs == {"retain": True}`, and the detail equals the P1 detail with `<path>` replaced by `<path>, retained`.
- **P3 (catalog mirroring):** for the connection parameter names shared by `mqtt_subscribe` and `mqtt_publish`, the descriptors remain field-for-field equal, and `retain` is present on `mqtt_publish` only.
- **P4 (compile delta):** for any valid pre-feature workflow document, the compiled document equals the pre-feature compilation after inserting `"retain": false` into each mqtt_publish binding's `parameters`, and nothing else.
- **P5 (V4):** `retain` in `{true, false}` validates; any non-boolean value yields exactly one `V4_INVALID_PARAMETER_VALUE` naming `retain`.

## Testing strategy

Backend edge (`test/backend-test/workflow_engine/`):
- Run the existing preservation suites unmodified first (Requirement 10.1).
- New `test_mqtt_publish_retain.py`: unit cases for each path with `retain: true` (kwargs `{"retain": True}`, detail suffix), `retain: false`/absent (kwargs `{}`), a Hypothesis property over `(greengrass, aws_iot, qos, broker_port, retain)` implementing P1/P2 against a recorded pre-feature reference, the `hasattr` guard (fake `model` module whose request class lacks `retain` -> `RuntimeError` naming the topic, no publish), and the extended `UnauthorizedError` text (present only when retain).
- Fake-IPC boundary test in `test/backend-test/output_bindings_fixes/` style: a fake `awsiot.greengrasscoreipc` whose request records attribute assignments; assert `retain` is assigned only when requested.

Portal layer (`edge-cv-portal/backend/layers/workflow_core/tests/`):
- Existing catalog suites with the two intended edits (baseline entry, name list).
- New assertions in `test_catalog_content.py`: `retain` descriptor fields (Requirement 1.1) and description content (1.2, 6.3, 8.2).
- Compile test: a graph with `retain: true` yields `parameters["retain"] is True`; a graph without it yields `False` (P4).
- Validator test: `retain: "yes"` -> `V4_INVALID_PARAMETER_VALUE` (P5).
- `device_provisioning` test: generated policy's publish statement lists both actions on the same resources and the rest of the policy is unchanged (compare against a pre-feature snapshot).

Frontend (`edge-cv-portal/frontend`, vitest): fixture extended; 'renders the "Retain message" checkbox unchecked by default'; 'propagates checking as retain: true and unchecking as retain: false'; existing `aws_iot` tests unchanged and green.

Security: run `test/backend-test/security/` and the preservation suite; `iot:RetainPublish` appears only on scoped topic resources.

On device (after a LocalServer build from the branch): the Requirement 10.6 procedure on the JP7 DLAP. Suggested topics: `dda/test/retain-on` and `dda/test/retain-off` (covered by the core policy's `dda/*` grants where present); `aws iot-data get-retained-message --topic dda/test/retain-on` returns the payload, the `retain-off` topic returns `ResourceNotFoundException`; `journalctl`/`greengrass.log` shows no MQTT disconnect during the run.

## Decisions

- **D1 keyword-only-when-true.** Keeps every pinned Publisher_Call and detail byte-identical for existing workflows, and turns a publisher that does not understand `retain` into a loud `TypeError` rather than a silent non-retained publish.
- **D2 default false, no `depends_on`.** Existing workflows must compile to the same behaviour; the bit is path-independent.
- **D3 no automatic floor bump.** A `retain: true` workflow on an old LocalServer degrades to non-retained rather than failing to deploy; the floor mechanism stays an operator decision (Requirement 9). Revisit if a hard guarantee is needed.
- **D4 policy guidance, not enforcement.** The portal cannot inspect or edit the IoT policy on the core device's certificate; it adds the action to the policies it does generate and names the permission in the UI description and in the denial error.
- **D5 trigger side untouched.** Retain-aware subscription changes the pinned Trigger_Context and deserves its own spec; the hazard is documented instead.
- **D6 `hasattr` guard over version parsing.** The failure mode is "the SDK model has no field"; checking the attribute is exact and needs no version table.
