# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
The Layout Check descriptor as the Portal serves and compiles it
(car-assembly-check task 7.4; design §2).

``get_node_type("layout_check")`` carries the four parameters with their
defaults and bounds, one executor-binding mapping per architecture and no
GStreamer element, and closes the post-processing palette section. A
workflow ``source -> model_inference -> layout_check`` (with a capture on
the detector's frame path, and optionally a conditional reading the
verdict) parses and compiles with the existing compiler: the node becomes
one generic ``executorBindings`` entry carrying ``parameters``,
``upstreamNodeIds`` and ``downstreamNodeIds``, and no stream element.

Requirements: 5.1
"""
import json

import pytest

from workflow_core.catalog import (
    ARCHITECTURES,
    CATEGORY_POST_PROCESSING,
    DEVICE_ARCHITECTURES,
    get_node_type,
    nodes_by_category,
)
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import parse

_REGIONS = json.dumps({
    "rear_fender": [[0.12, 0.44], [0.47, 0.37], [0.47, 0.86], [0.12, 0.80]],
    "front_fender": [[0.47, 0.37], [0.88, 0.47], [0.86, 0.78], [0.47, 0.80]],
    "roof": [[0.30, 0.36], [0.40, 0.16], [0.60, 0.16], [0.72, 0.36]],
})


def _params(descriptor):
    return {param.name: param for param in descriptor.parameters}


class TestDescriptor:
    def test_four_parameters_with_defaults_and_bounds(self):
        descriptor = get_node_type("layout_check")
        assert descriptor is not None
        params = _params(descriptor)
        assert list(params) == [
            "expected_regions", "min_iou", "min_confidence", "min_outline_overlap"]
        regions = params["expected_regions"]
        assert (regions.param_type, regions.required, regions.default) == ("string", True, None)
        assert regions.constraints == {"min_length": 2}
        assert regions.multiline is True
        assert "layout.<nodeId>.pass == false" in regions.description
        expected = {
            "min_iou": (0.7, {"min": 0.01, "max": 1.0}),
            "min_confidence": (0.5, {"min": 0.0, "max": 1.0}),
            "min_outline_overlap": (0.1, {"min": 0.0, "max": 0.5}),
        }
        for name, (default, constraints) in expected.items():
            param = params[name]
            assert (param.param_type, param.required) == ("float", False), name
            assert param.default == default, name
            assert param.constraints == constraints, name

    def test_one_executor_binding_per_architecture_and_no_element(self):
        descriptor = get_node_type("layout_check")
        assert [mapping.arch for mapping in descriptor.mappings] == list(ARCHITECTURES)
        for mapping in descriptor.mappings:
            assert mapping.executor_binding == "layout_check", mapping.arch
            assert mapping.element_chain == [], mapping.arch
        assert descriptor.hardware_dependent is False

    def test_it_closes_the_post_processing_palette_section(self):
        section = nodes_by_category()[CATEGORY_POST_PROCESSING]
        assert section[-1].type_id == "layout_check"
        assert [d.type_id for d in section].count("layout_check") == 1


def _definition(with_conditional=False):
    nodes = [
        ("src", "folder_source", {"location": "/aws_dda/batch-input/"}),
        ("inf", "model_inference", {"modelName": "car-assembly-gray"}),
        ("cap", "capture", {"output_path": "/aws_dda/captures"}),
        ("layout", "layout_check", {"expected_regions": _REGIONS}),
    ]
    connections = [("src", "inf"), ("inf", "cap"), ("inf", "layout")]
    if with_conditional:
        nodes.append(("verdict", "conditional", {"condition": "layout.layout.pass == false"}))
        connections.append(("layout", "verdict"))
    return {
        "schemaVersion": 1,
        "nodes": [{"id": node_id, "type": node_type, "position": {"x": index * 100, "y": 0},
                   "parameters": parameters}
                  for index, (node_id, node_type, parameters) in enumerate(nodes)],
        "connections": [{"id": "c{0}".format(index),
                         "from": {"node": source, "port": "out"},
                         "to": {"node": target, "port": "in"}}
                        for index, (source, target) in enumerate(connections)],
    }


def _compile(definition, arch, simulation=False):
    parsed = parse(json.dumps(definition))
    assert parsed.ok, parsed.error
    document = compile(parsed.graph, arch, simulation=simulation)
    assert isinstance(document, CompiledPipelineDocument), document
    return document


def _layout_binding(document):
    bindings = [entry for entry in document.executor_bindings if entry["nodeId"] == "layout"]
    assert len(bindings) == 1
    return bindings[0]


@pytest.mark.parametrize("arch", list(DEVICE_ARCHITECTURES))
def test_compiles_to_one_generic_executor_binding(arch):
    document = _compile(_definition(), arch)
    binding = _layout_binding(document)
    assert binding == {
        "nodeId": "layout",
        "binding": "layout_check",
        "parameters": {
            "expected_regions": _REGIONS,
            "min_iou": 0.7,
            "min_confidence": 0.5,
            "min_outline_overlap": 0.1,
        },
        "upstreamNodeIds": ["inf"],
        "downstreamNodeIds": [],
    }
    # Collapsed out of the frame stream, like the scene analytics nodes.
    assert all(element.get("nodeId") != "layout"
               for segment in document.segments for element in segment["elements"])
    assert document.to_dict()["executorBindings"] == document.executor_bindings


def test_the_simulation_compile_uses_the_same_binding():
    binding = _layout_binding(_compile(_definition(), "x86_64", simulation=True))
    assert binding["binding"] == "layout_check"


def test_a_downstream_conditional_is_listed_and_explicit_parameters_win():
    definition = _definition(with_conditional=True)
    definition["nodes"][3]["parameters"].update(min_iou=0.62, min_outline_overlap=0.0)
    document = _compile(definition, "arm64_jp5")
    binding = _layout_binding(document)
    assert binding["downstreamNodeIds"] == ["verdict"]
    assert binding["parameters"]["min_iou"] == 0.62
    assert binding["parameters"]["min_outline_overlap"] == 0.0
    verdict = [entry for entry in document.executor_bindings if entry["nodeId"] == "verdict"]
    assert verdict and verdict[0]["upstreamNodeIds"] == ["layout"]
