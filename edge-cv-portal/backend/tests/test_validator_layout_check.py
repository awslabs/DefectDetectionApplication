# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Validator rules V14 and W4 for the Layout Check node
(car-assembly-check task 7.2; design §2).

- V14_LAYOUT_CONFIG_INVALID: one error finding per offending parameter.
  ``expected_regions`` goes through the shared
  ``analytics.layout.parse_expected_regions`` and its finding carries every
  problem sentence (the cyclic duplicate rule included); a blank or
  missing value is V4's concern. ``min_iou`` (0.01 to 1),
  ``min_confidence`` (0 to 1) and ``min_outline_overlap`` (0 to 0.5) are
  range-checked when numeric.
- W4_LAYOUT_NO_DETECTOR: one warning per ``layout_check`` node with no
  ``model_inference`` node transitively upstream.
- Neither rule fires on a graph without a ``layout_check`` node: every
  existing validator fixture (the stream-free corpus pinned by the layer's
  ``test_validator_stream_and_analytics.py`` and the generated stream and
  analytics graphs of the Workflow_Builder parity corpus) gets none, and
  ``layout_check`` is kept out of ``SCENE_ANALYTICS_TYPES``.

Requirements: 5.6, 12.1
"""
import importlib
import importlib.util
import json
import math
import os
import sys

import pytest

import frontend_parity_fixtures
from workflow_core.serializer import parse
from workflow_core.validator import (
    CODE_V13_ANALYTICS_CONFIG_INVALID,
    CODE_V14_LAYOUT_CONFIG_INVALID,
    CODE_W3_ANALYTICS_NO_DETECTOR,
    CODE_W4_LAYOUT_NO_DETECTOR,
    SCENE_ANALYTICS_TYPES,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    validate,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_LAYER_TESTS = os.path.join(os.path.dirname(_HERE), "layers", "workflow_core", "tests")

_TRIANGLE = [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]
_UNSET = object()


def _graph(regions=_UNSET, upstream=("model_inference",), **parameters):
    """folder_source -> <upstream chain> -> layout_check (node 'layout')."""
    if regions is not _UNSET:
        parameters["expected_regions"] = regions
    chain = [("src", "folder_source", {"location": "/data"})]
    chain += [("up{0}".format(index), node_type, {})
              for index, node_type in enumerate(upstream)]
    chain.append(("layout", "layout_check", parameters))
    nodes = [{"id": node_id, "type": node_type, "position": {"x": index * 10, "y": 0},
              "parameters": node_parameters}
             for index, (node_id, node_type, node_parameters) in enumerate(chain)]
    connections = [{"id": "c{0}".format(index),
                    "from": {"node": chain[index][0], "port": "out"},
                    "to": {"node": chain[index + 1][0], "port": "in"}}
                   for index in range(len(chain) - 1)]
    return _parsed({"schemaVersion": 1, "nodes": nodes, "connections": connections})


def _parsed(definition):
    result = parse(json.dumps(definition))
    assert result.ok, result.error
    return result.graph


def _findings(graph, code):
    return [(finding.severity, finding.message, finding.node_id)
            for finding in validate(graph) if finding.code == code]


def _v14(regions=_UNSET, **parameters):
    return _findings(_graph(regions, **parameters), CODE_V14_LAYOUT_CONFIG_INVALID)


def _regions_finding(*problems):
    return (SEVERITY_ERROR,
            "Node 'layout': parameter 'expected_regions': " + " ".join(problems),
            "layout")


# ---------------------------------------------------------------------------
# V14: expected_regions, one finding per parameter with every sentence
# ---------------------------------------------------------------------------

_ELEVEN = json.dumps({"c{0}".format(index): _TRIANGLE for index in range(11)})
_SIXTY_FIVE = json.dumps({"roof": [
    [0.5 + 0.4 * math.cos(2 * math.pi * index / 65),
     0.5 + 0.4 * math.sin(2 * math.pi * index / 65)] for index in range(65)]})


@pytest.mark.parametrize("regions, problems", [
    ("not json", ["Expected regions are not valid JSON."]),
    ("[[0, 0], [1, 0], [0, 1]]", [
        "Expected regions must be a JSON object that maps each class label to a "
        "polygon of [x, y] points."]),
    ('"text"', [
        "Expected regions must be a JSON object that maps each class label to a "
        "polygon of [x, y] points."]),
    ("{}", ["Expected regions must name between 1 and 10 classes; this value names 0."]),
    (_ELEVEN, ["Expected regions must name between 1 and 10 classes; this value names 11."]),
    (json.dumps({"": _TRIANGLE}), ["Region 1: the class label is empty."]),
    (json.dumps({"roof": _TRIANGLE, "  ": _TRIANGLE}), ["Region 2: the class label is empty."]),
    (json.dumps({"x" * 65: _TRIANGLE}),
     ["Region 1: the class label is longer than 64 characters."]),
    (json.dumps({"roof": _TRIANGLE, " roof ": _TRIANGLE}),
     ["Region 2: the class label 'roof' is already used by an earlier region."]),
    ('{"roof": "x"}', ["Region 'roof': the polygon must be a list of [x, y] points."]),
    ('{"roof": {"points": []}}', ["Region 'roof': the polygon must be a list of [x, y] points."]),
    ('{"roof": [[0, 0], [1, 0], [0.5]]}',
     ["Region 'roof': point 3 must be a pair of numbers [x, y]."]),
    ('{"roof": [[0, 0], [1, 0], [0.5, true]]}',
     ["Region 'roof': point 3 must be a pair of numbers [x, y]."]),
    ('{"roof": [[0, 0], [1, 0], [NaN, 1]]}',
     ["Region 'roof': point 3 must be a pair of numbers [x, y]."]),
    ('{"roof": [[0, 0], [1, 0], [0.5, Infinity]]}',
     ["Region 'roof': point 3 must be a pair of numbers [x, y]."]),
    ('{"roof": [[0, 0], [1.5, 0], [0.5, 1]]}',
     ["Region 'roof': point 2 x coordinate must be between 0 and 1."]),
    ('{"roof": [[0, 0], [1, 0], [0.5, -0.1]]}',
     ["Region 'roof': point 3 y coordinate must be between 0 and 1."]),
    ('{"roof": [[0, 0], [1, 1]]}', [
        "Region 'roof': the polygon must have between 3 and 64 distinct points; "
        "this one has 2."]),
    # The cyclic duplicate rule: the repeated first vertex is dropped and
    # the count is of what remains.
    ('{"roof": [[0, 0], [1, 0], [0, 0]]}', [
        "Region 'roof': the polygon must have between 3 and 64 distinct points; "
        "this one has 2."]),
    ('{"roof": [[0, 0], [0, 0], [1, 0], [1, 0]]}', [
        "Region 'roof': the polygon must have between 3 and 64 distinct points; "
        "this one has 2."]),
    (_SIXTY_FIVE, [
        "Region 'roof': the polygon must have between 3 and 64 distinct points; "
        "this one has 65."]),
    ('{"roof": [[0, 0], [0.05, 0], [0, 0.05]]}',
     ["Region 'roof': the polygon must cover at least 0.5% of the frame."]),
    ('{"roof": [[0.1, 0.1], [0.9, 0.9], [0.9, 0.1], [0.1, 0.5]]}',
     ["Region 'roof': the polygon crosses itself; no two of its edges may intersect."]),
    ('{"roof": [[0.1, 0.1], [0.9, 0.9], [0.9, 0.1], [0.1, 0.9]]}', [
        "Region 'roof': the polygon must cover at least 0.5% of the frame.",
        "Region 'roof': the polygon crosses itself; no two of its edges may intersect."]),
    # Every problem of every region lands in the one finding, in key order.
    ('{"roof": [[0, 0], [2, 0], [0.5]], "": [[0, 0], [1, 0], [0, 1]], '
     '"wheel": [[0, 0], [1, 1]]}', [
        "Region 'roof': point 2 x coordinate must be between 0 and 1.",
        "Region 'roof': point 3 must be a pair of numbers [x, y].",
        "Region 2: the class label is empty.",
        "Region 'wheel': the polygon must have between 3 and 64 distinct points; "
        "this one has 2."]),
])
def test_v14_reports_every_rule_in_one_finding(regions, problems):
    assert _v14(regions) == [_regions_finding(*problems)]


@pytest.mark.parametrize("regions", [
    json.dumps({"roof": _TRIANGLE}),
    # A GeoJSON-style ring repeating its first vertex last.
    json.dumps({"roof": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8], [0.2, 0.2]]}),
    # Three distinct vertices plus the repeated first: accepted as three.
    json.dumps({"roof": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9], [0.1, 0.1]]}),
    json.dumps({"c{0}".format(index): _TRIANGLE for index in range(10)}),
    json.dumps({"x" * 64: _TRIANGLE}),
    # The descriptor's own example.
    '{"roof": [[0.3, 0.2], [0.7, 0.2], [0.7, 0.45], [0.3, 0.45]]}',
])
def test_v14_accepts_well_formed_regions(regions):
    assert _v14(regions) == []


@pytest.mark.parametrize("regions", [_UNSET, None, "", "   \n"])
def test_a_blank_or_missing_value_is_left_to_v4(regions):
    assert _v14(regions) == []


# ---------------------------------------------------------------------------
# V14: the three range rules
# ---------------------------------------------------------------------------

_VALID = json.dumps({"roof": _TRIANGLE})


@pytest.mark.parametrize("name, value, message", [
    ("min_iou", 0.0, "The value must be between 0.01 and 1."),
    ("min_iou", 0.005, "The value must be between 0.01 and 1."),
    ("min_iou", 1.5, "The value must be between 0.01 and 1."),
    ("min_iou", "1.5", "The value must be between 0.01 and 1."),
    ("min_confidence", -0.1, "The value must be between 0 and 1."),
    ("min_confidence", 1.2, "The value must be between 0 and 1."),
    ("min_outline_overlap", -0.1, "The value must be between 0 and 0.5."),
    ("min_outline_overlap", 0.51, "The value must be between 0 and 0.5."),
    ("min_outline_overlap", " 0.75 ", "The value must be between 0 and 0.5."),
])
def test_v14_reports_an_out_of_range_threshold(name, value, message):
    assert _v14(_VALID, **{name: value}) == [(
        SEVERITY_ERROR, "Node 'layout': parameter '{0}': {1}".format(name, message), "layout")]


@pytest.mark.parametrize("name, value", [
    ("min_iou", 0.01), ("min_iou", 1), ("min_iou", "0.7"),
    ("min_confidence", 0), ("min_confidence", 1.0),
    ("min_outline_overlap", 0), ("min_outline_overlap", 0.5),
    # Not numbers: V4's type check reports these, V14 does not.
    ("min_iou", "abc"), ("min_iou", True), ("min_iou", None), ("min_iou", [1.5]),
    ("min_outline_overlap", "1e999"), ("min_outline_overlap", "0x1"),
])
def test_v14_accepts_in_range_and_leaves_non_numbers_to_v4(name, value):
    assert _v14(_VALID, **{name: value}) == []


def test_v14_reports_one_finding_per_offending_parameter_in_order():
    findings = _v14("not json", min_iou=2, min_confidence=0.5, min_outline_overlap=0.9)
    assert [message.split("'")[3] for _severity, message, _node in findings] == \
        ["expected_regions", "min_iou", "min_outline_overlap"]


def test_v14_judges_defaults_and_fires_only_on_layout_check_nodes():
    # The descriptor defaults are in range, so only the regions are judged.
    assert _v14(_VALID) == []
    graph = _parsed({"schemaVersion": 1, "nodes": [
        {"id": "dc", "type": "detection_counter", "position": {"x": 0, "y": 0},
         "parameters": {"expected_regions": "not json", "min_iou": 5}}],
        "connections": []})
    assert _findings(graph, CODE_V14_LAYOUT_CONFIG_INVALID) == []


# ---------------------------------------------------------------------------
# W4: a layout_check node needs a detector upstream
# ---------------------------------------------------------------------------

_W4_MESSAGE = ("Node 'layout': no 'model_inference' node is upstream of this "
               "'layout_check' node, so no instance masks will exist for it to check "
               "at runtime")


def test_w4_warns_without_a_detector_upstream():
    graph = _graph(_VALID, upstream=())
    assert _findings(graph, CODE_W4_LAYOUT_NO_DETECTOR) == \
        [(SEVERITY_WARNING, _W4_MESSAGE, "layout")]
    # W3 keeps its own consumer types: the layout node is not reported there.
    assert _findings(graph, CODE_W3_ANALYTICS_NO_DETECTOR) == []


@pytest.mark.parametrize("upstream", [
    ("model_inference",),
    ("model_inference", "detection_counter"),
    ("model_inference", "inference_filter", "event_gate"),
])
def test_w4_is_silent_with_a_detector_directly_or_transitively_upstream(upstream):
    assert _findings(_graph(_VALID, upstream=upstream), CODE_W4_LAYOUT_NO_DETECTOR) == []


def test_w4_needs_the_detector_upstream_not_just_present():
    graph = _parsed({"schemaVersion": 1, "nodes": [
        {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
         "parameters": {"location": "/data"}},
        {"id": "inf", "type": "model_inference", "position": {"x": 10, "y": 0},
         "parameters": {}},
        {"id": "layout", "type": "layout_check", "position": {"x": 20, "y": 0},
         "parameters": {"expected_regions": _VALID}},
        {"id": "layout2", "type": "layout_check", "position": {"x": 30, "y": 0},
         "parameters": {"expected_regions": _VALID}}],
        "connections": [
            {"id": "c0", "from": {"node": "src", "port": "out"},
             "to": {"node": "inf", "port": "in"}},
            {"id": "c1", "from": {"node": "layout2", "port": "out"},
             "to": {"node": "layout", "port": "in"}}]})
    assert [node for _severity, _message, node in
            _findings(graph, CODE_W4_LAYOUT_NO_DETECTOR)] == ["layout", "layout2"]


# ---------------------------------------------------------------------------
# Graphs without a layout_check node are untouched
# ---------------------------------------------------------------------------

def _layer_test_module(name):
    """A module of the workflow_core layer's test package, imported under
    an alias package so it never collides with this suite's modules."""
    package = "_workflow_core_layer_tests"
    if package not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            package, os.path.join(_LAYER_TESTS, "__init__.py"),
            submodule_search_locations=[_LAYER_TESTS])
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
    return importlib.import_module("{0}.{1}".format(package, name))


def _existing_fixture_graphs():
    corpus = _layer_test_module("test_validator_stream_and_analytics")
    graphs = sorted(corpus._fixtures().items())
    assert graphs, "the layer's validator fixture corpus is empty"
    for entry in frontend_parity_fixtures.inline_parity_corpus()["entries"]:
        if any(node["type"] == "layout_check" for node in entry["graph"]["nodes"]):
            continue
        graphs.append((entry["name"], _parsed(entry["graph"])))
    return graphs


def test_no_existing_fixture_gets_a_v14_or_w4_finding():
    graphs = _existing_fixture_graphs()
    codes = set()
    for name, graph in graphs:
        findings = validate(graph)
        codes.update(finding.code for finding in findings)
        fired = {finding.code for finding in findings} & {
            CODE_V14_LAYOUT_CONFIG_INVALID, CODE_W4_LAYOUT_NO_DETECTOR}
        assert fired == set(), (name, sorted(fired))
    # The corpus does exercise the neighbouring analytics rules.
    assert {CODE_V13_ANALYTICS_CONFIG_INVALID, CODE_W3_ANALYTICS_NO_DETECTOR} <= codes
    assert len(graphs) > 250


def test_layout_check_is_not_a_scene_analytics_type():
    # SCENE_ANALYTICS_TYPES feeds the packager's stream feature floor and
    # the V13/W3 scope; both stay as they were.
    assert SCENE_ANALYTICS_TYPES == frozenset(
        {"detection_counter", "object_association", "event_gate"})
