"""Bug 2 fix-checking — topic-only Greengrass config packages as a valid
output node.

Bugfix spec: workflow-manager-integration-bugfixes (Bug 2, task 4.5).

**Property 2: Expected Behavior** — MQTT publish through Greengrass with
only a topic. Task 1.2's exploration test asserts a topic-only Greengrass
``mqtt_publish`` config *validates* (no error, `greengrass` parameter
exists, `broker_host` not required). This module adds the companion
fix-checking coverage: a topic-only Greengrass config not only validates
but **packages as a valid output node** — the compiler emits a valid
Compiled Pipeline Document in which the `mqtt_publish` node appears
exactly once as an executor binding (`binding == "mqtt_publish"`) carrying
the `greengrass`/`topic` parameters, and does NOT force a `broker_host`
or any `iot_*` certificate path.

Property 2: Bug Condition — MQTT publish through Greengrass with only a topic
  For any mqtt_publish configuration where isBugCondition2 holds (the
  user wants Greengrass-managed publishing and no topic-only config is
  currently valid), the fixed system SHALL accept a Greengrass publish
  configuration supplying only the topic — valid without `broker_host`,
  `broker_port`, or any `iot_*` certificate path — and SHALL package it
  as a valid output node.

Validates: Requirements 2.2
"""

from workflow_core.catalog import DEVICE_ARCHITECTURES
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)

_POS = Position(0.0, 0.0)

#: Certificate parameters a Greengrass config must NOT require.
_IOT_CERT_PARAMS = (
    "iot_ca_cert_path",
    "iot_client_cert_path",
    "iot_private_key_path",
)


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id, source_node, target_node, source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


def _topic_only_greengrass_graph():
    """folder_source -> model_inference -> mqtt_publish(topic + greengrass).

    A frame source feeds inference (VideoFrames -> InferenceMeta) which
    feeds the mqtt_publish input (InferenceMeta). The mqtt_publish node
    supplies only the `topic` plus the `greengrass` option — no broker
    host/port and no certificate paths.
    """
    return WorkflowGraph(
        nodes=[
            _node("src", "folder_source", location="/data/images"),
            _node("inf", "model_inference", modelName="widget-anomaly-v3"),
            _node(
                "mqtt", "mqtt_publish",
                topic="factory/line1/inspection",
                greengrass=True,
            ),
        ],
        connections=[
            _conn("c1", "src", "inf"),
            _conn("c2", "inf", "mqtt"),
        ],
    )


def _mqtt_binding(document):
    matches = [
        b for b in document.executor_bindings
        if b.get("nodeId") == "mqtt" and b.get("binding") == "mqtt_publish"
    ]
    return matches


class TestBug2GreengrassPackagesAsOutputNode:
    """Property 2: Expected Behavior — a topic-only Greengrass config
    packages as a valid output node on every device architecture."""

    def test_topic_only_greengrass_compiles_on_every_device_arch(self):
        graph = _topic_only_greengrass_graph()
        for arch in DEVICE_ARCHITECTURES:
            result = compile(graph, arch)
            assert isinstance(result, CompiledPipelineDocument), (
                "topic-only Greengrass config failed to compile on "
                "{0}: {1}".format(arch, result)
            )

    def test_mqtt_node_packaged_once_as_executor_binding(self):
        graph = _topic_only_greengrass_graph()
        for arch in DEVICE_ARCHITECTURES:
            document = compile(graph, arch)
            assert isinstance(document, CompiledPipelineDocument)
            bindings = _mqtt_binding(document)
            assert len(bindings) == 1, (
                "mqtt_publish should package as exactly one output-node "
                "executor binding on {0}; got {1}".format(arch, bindings)
            )
            # The node is referenced exactly once across the whole document
            # (Requirement 6.6) — a valid output node.
            assert document.referenced_node_ids().count("mqtt") == 1

    def test_packaged_binding_carries_greengrass_topic_only(self):
        graph = _topic_only_greengrass_graph()
        for arch in DEVICE_ARCHITECTURES:
            document = compile(graph, arch)
            assert isinstance(document, CompiledPipelineDocument)
            binding = _mqtt_binding(document)[0]
            parameters = binding.get("parameters") or {}
            assert parameters.get("greengrass") is True, (
                "packaged Greengrass binding lost greengrass=True on "
                "{0}: {1}".format(arch, parameters)
            )
            assert parameters.get("topic") == "factory/line1/inspection"
            # The topic-only Greengrass config packages without a broker
            # host and without any certificate path (they default to the
            # off/empty catalog defaults, never forced by the user).
            assert not parameters.get("broker_host"), (
                "Greengrass packaging should not require a broker_host on "
                "{0}: {1}".format(arch, parameters)
            )
            for cert_param in _IOT_CERT_PARAMS:
                assert not parameters.get(cert_param), (
                    "Greengrass packaging should not require {0} on "
                    "{1}: {2}".format(cert_param, arch, parameters)
                )
