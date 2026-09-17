"""mqtt-retained-publish — validator and compiler coverage for the
``mqtt_publish`` ``retain`` parameter.

Spec: .kiro/specs/mqtt-retained-publish (task 1.4).

The ``retain`` parameter is a plain optional bool (default ``False``) on
the ``mqtt_publish`` descriptor. This module pins the two behaviours the
rest of the feature depends on:

* Validator (Requirements 1.6, 1.7): a non-bool value is rejected with
  exactly one ``V4_INVALID_PARAMETER_VALUE`` finding naming ``retain``;
  explicit ``True``/``False`` validate cleanly on both the Greengrass
  and the plain-broker paths.
* Compiler (Requirements 3.1, 3.2): the compiled executor binding carries
  ``parameters["retain"] is True`` when the node sets it, and
  ``parameters["retain"] is False`` (the catalog default) when the node
  omits it. Setting ``retain`` changes ONLY that key — every other
  binding key is identical to the same graph compiled without the
  parameter, so existing workflows compile to the same document plus a
  ``retain: False`` default.

Validates: Requirements 1.6, 1.7, 3.1, 3.2, 10.2
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
from workflow_core.validator import (
    CODE_V4_INVALID_PARAMETER_VALUE,
    SEVERITY_ERROR,
    validate,
)

_POS = Position(0.0, 0.0)
_TOPIC = "factory/line1/inspection"


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id, source_node, target_node, source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


def _graph(**mqtt_parameters):
    """folder_source -> model_inference -> mqtt_publish(**mqtt_parameters).

    Same shape as the Bug 2 Greengrass fixtures so the retain coverage
    exercises the exact graph every other mqtt_publish test compiles.
    """
    return WorkflowGraph(
        nodes=[
            _node("src", "folder_source", location="/data/images"),
            _node("inf", "model_inference", modelName="widget-anomaly-v3"),
            _node("mqtt", "mqtt_publish", topic=_TOPIC, **mqtt_parameters),
        ],
        connections=[
            _conn("c1", "src", "inf"),
            _conn("c2", "inf", "mqtt"),
        ],
    )


def _greengrass_graph(**extra):
    return _graph(greengrass=True, **extra)


def _broker_graph(**extra):
    return _graph(broker_host="broker.local", broker_port=1883, **extra)


def _errors_for_node(findings, node_id):
    return [
        f for f in findings
        if f.severity == SEVERITY_ERROR and f.node_id == node_id
    ]


def _retain_findings(findings):
    return [
        f for f in findings
        if f.code == CODE_V4_INVALID_PARAMETER_VALUE
        and f.node_id == "mqtt"
        and "'retain'" in f.message
    ]


def _mqtt_binding(document):
    matches = [
        b for b in document.executor_bindings
        if b.get("nodeId") == "mqtt" and b.get("binding") == "mqtt_publish"
    ]
    assert len(matches) == 1, matches
    return matches[0]


# --------------------------------------------------------------------------
# Validator (Requirements 1.6, 1.7)
# --------------------------------------------------------------------------

class TestRetainValidation:

    def test_non_bool_retain_is_exactly_one_invalid_value_finding(self):
        findings = validate(_greengrass_graph(retain="yes"))
        retain_findings = _retain_findings(findings)
        assert len(retain_findings) == 1, (
            "expected exactly one V4_INVALID_PARAMETER_VALUE for retain; "
            f"got {[(f.code, f.message) for f in findings]}"
        )
        assert "bool" in retain_findings[0].message
        # The string value is the ONLY problem with this node.
        assert _errors_for_node(findings, "mqtt") == retain_findings

    def test_int_retain_is_rejected(self):
        # bool is an int subclass in Python; the reverse must not hold.
        findings = validate(_greengrass_graph(retain=1))
        assert len(_retain_findings(findings)) == 1

    def test_explicit_true_validates_on_greengrass_path(self):
        findings = validate(_greengrass_graph(retain=True))
        assert _errors_for_node(findings, "mqtt") == []

    def test_explicit_false_validates_on_greengrass_path(self):
        findings = validate(_greengrass_graph(retain=False))
        assert _errors_for_node(findings, "mqtt") == []

    def test_explicit_true_validates_on_broker_path(self):
        findings = validate(_broker_graph(retain=True))
        assert _errors_for_node(findings, "mqtt") == []

    def test_absent_retain_validates(self):
        # Every pre-existing workflow omits the key; the default satisfies V4.
        findings = validate(_greengrass_graph())
        assert _errors_for_node(findings, "mqtt") == []


# --------------------------------------------------------------------------
# Compiler (Requirements 3.1, 3.2)
# --------------------------------------------------------------------------

class TestRetainCompilation:

    def test_explicit_true_compiles_to_retain_true_on_every_arch(self):
        graph = _greengrass_graph(retain=True)
        for arch in DEVICE_ARCHITECTURES:
            document = compile(graph, arch)
            assert isinstance(document, CompiledPipelineDocument), (arch, document)
            parameters = _mqtt_binding(document)["parameters"]
            assert parameters["retain"] is True, (arch, parameters)

    def test_absent_retain_compiles_to_catalog_default_false(self):
        graph = _greengrass_graph()
        for arch in DEVICE_ARCHITECTURES:
            document = compile(graph, arch)
            assert isinstance(document, CompiledPipelineDocument), (arch, document)
            parameters = _mqtt_binding(document)["parameters"]
            assert "retain" in parameters, (arch, parameters)
            assert parameters["retain"] is False, (arch, parameters)

    def test_explicit_false_compiles_to_retain_false(self):
        graph = _broker_graph(retain=False)
        for arch in DEVICE_ARCHITECTURES:
            document = compile(graph, arch)
            assert isinstance(document, CompiledPipelineDocument), (arch, document)
            assert _mqtt_binding(document)["parameters"]["retain"] is False

    def test_retain_changes_only_the_retain_key(self):
        # Requirement 3.2 / 10.2: the same graph with and without
        # retain=True compiles to bindings that differ ONLY in
        # parameters["retain"]. Nothing else about the document moves.
        for arch in DEVICE_ARCHITECTURES:
            plain = compile(_greengrass_graph(), arch)
            retained = compile(_greengrass_graph(retain=True), arch)
            assert isinstance(plain, CompiledPipelineDocument), (arch, plain)
            assert isinstance(retained, CompiledPipelineDocument), (arch, retained)

            plain_binding = _mqtt_binding(plain)
            retained_binding = _mqtt_binding(retained)
            assert set(plain_binding) == set(retained_binding)
            for key in plain_binding:
                if key == "parameters":
                    continue
                assert plain_binding[key] == retained_binding[key], (arch, key)

            plain_params = dict(plain_binding["parameters"])
            retained_params = dict(retained_binding["parameters"])
            assert plain_params.pop("retain") is False
            assert retained_params.pop("retain") is True
            assert plain_params == retained_params, (arch, plain_params, retained_params)

            # The rest of the document (segments, plugin deps, other
            # bindings) is untouched by the output-node flag.
            assert plain.segments == retained.segments, arch
            assert plain.plugin_dependencies == retained.plugin_dependencies, arch
            other_plain = [b for b in plain.executor_bindings if b["nodeId"] != "mqtt"]
            other_retained = [b for b in retained.executor_bindings if b["nodeId"] != "mqtt"]
            assert other_plain == other_retained, arch
