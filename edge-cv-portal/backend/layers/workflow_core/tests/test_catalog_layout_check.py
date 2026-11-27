"""Unit tests for the ``layout_check`` catalog descriptor
(car-assembly-check task 7.1; design §2).

Asserts the descriptor's identity, ports (one InferenceMeta input, one
InferenceMeta output), its four parameters in order (``expected_regions``
required and multi-line, whose description carries the verdict paths and
the ``layout.<nodeId>.pass == false`` condition example; the ``min_iou``,
``min_confidence`` and ``min_outline_overlap`` defaults and bounds), the
mappings (executor binding ``layout_check`` with no GStreamer element on
every architecture), ``hardware_dependent``, that the shared condition
grammar is untouched (Requirement 5.5), the appended catalog position, and
the baseline delta scope (the recorded ``catalog_baseline.json`` covers
exactly the live catalog and its ``layout_check`` entry equals the live
descriptor).
"""

import dataclasses
import json
import os

from workflow_core.analytics import layout
from workflow_core.catalog import (
    ARCHITECTURES,
    CATEGORY_POST_PROCESSING,
    CONDITION_EXAMPLES,
    CONDITION_LANGUAGE_DESCRIPTION,
    NODE_CATALOG,
    PORT_TYPE_INFERENCE_META,
    get_node_type,
)

_HERE = os.path.dirname(os.path.abspath(__file__))


def _descriptor():
    descriptor = get_node_type("layout_check")
    assert descriptor is not None
    return descriptor


def _params_by_name(descriptor):
    return {param.name: param for param in descriptor.parameters}


class TestLayoutCheckIdentityAndPorts:
    def test_identity_and_category(self):
        descriptor = _descriptor()
        assert descriptor.type_id == "layout_check"
        assert descriptor.category == CATEGORY_POST_PROCESSING
        assert descriptor.display_name == "Layout Check"

    def test_one_inference_meta_input_and_output(self):
        descriptor = _descriptor()
        assert [(p.name, p.port_type) for p in descriptor.inputs] == \
            [("in", PORT_TYPE_INFERENCE_META)]
        assert [(p.name, p.port_type) for p in descriptor.outputs] == \
            [("out", PORT_TYPE_INFERENCE_META)]


class TestLayoutCheckParameters:
    def test_parameter_names_in_order(self):
        # The design's "three parameters" predate owner decision O19, which
        # added min_outline_overlap last.
        assert [p.name for p in _descriptor().parameters] == [
            "expected_regions", "min_iou", "min_confidence", "min_outline_overlap",
        ]

    def test_expected_regions(self):
        param = _params_by_name(_descriptor())["expected_regions"]
        assert param.param_type == "string"
        assert param.required is True
        assert param.default is None
        assert param.constraints == {"min_length": 2}
        assert param.multiline is True
        for token in ("layout.<nodeId>.pass",
                      "layout.<nodeId>.pieces.<label>.status",
                      "layout.<nodeId>.pass == false",
                      "3 to 64 [x, y] points", "(1 to 10)", "min_iou"):
            assert token in param.description, token
        assert param.examples == [
            '{"roof": [[0.3, 0.2], [0.7, 0.2], [0.7, 0.45], [0.3, 0.45]]}']

    def test_the_expected_regions_example_parses(self):
        example = _params_by_name(_descriptor())["expected_regions"].examples[0]
        regions, problems = layout.parse_expected_regions(example)
        assert problems == []
        assert list(regions) == ["roof"]

    def test_min_iou(self):
        param = _params_by_name(_descriptor())["min_iou"]
        assert param.param_type == "float"
        assert param.required is False
        assert param.default == 0.7 == layout.DEFAULT_MIN_IOU
        assert param.constraints == {"min": 0.01, "max": 1.0}
        assert param.examples == [0.7, 0.6]
        assert param.multiline is False

    def test_min_confidence(self):
        param = _params_by_name(_descriptor())["min_confidence"]
        assert param.param_type == "float"
        assert param.required is False
        assert param.default == 0.5 == layout.DEFAULT_MIN_CONFIDENCE
        assert param.constraints == {"min": 0.0, "max": 1.0}
        assert param.examples == [0.5]
        assert param.multiline is False

    def test_min_outline_overlap(self):
        param = _params_by_name(_descriptor())["min_outline_overlap"]
        assert param.param_type == "float"
        assert param.required is False
        assert param.default == 0.1 == layout.DEFAULT_MIN_OUTLINE_OVERLAP
        assert param.constraints == {"min": 0.0, "max": layout.MAX_MIN_OUTLINE_OVERLAP}
        assert param.examples == [0.1, 0.0]
        assert param.multiline is False
        for token in ("union of the expected regions", "layout.<nodeId>.ignored",
                      "0 counts every detection"):
            assert token in param.description, token

    def test_no_parameter_is_gated(self):
        for param in _descriptor().parameters:
            assert param.depends_on is None, param.name


class TestLayoutCheckMappings:
    def test_one_executor_binding_per_architecture(self):
        descriptor = _descriptor()
        assert [m.arch for m in descriptor.mappings] == list(ARCHITECTURES)
        for mapping in descriptor.mappings:
            assert mapping.executor_binding == "layout_check", mapping.arch
            assert mapping.element_chain == [], mapping.arch
            assert mapping.plugin_dependencies == [], mapping.arch

    def test_not_hardware_dependent(self):
        assert _descriptor().hardware_dependent is False


class TestSharedConditionGrammarUntouched:
    def test_no_layout_path_in_the_shared_condition_examples(self):
        # Requirement 5.5: the layout example lives in the descriptor's own
        # description; the shared tuple and text stay as they were.
        assert not any("layout." in example for example in CONDITION_EXAMPLES)
        assert "layout." not in CONDITION_LANGUAGE_DESCRIPTION


class TestLayoutCheckCatalogPositionAndBaseline:
    def test_appended_after_s3_upload(self):
        order = [d.type_id for d in NODE_CATALOG]
        assert order.index("layout_check") == 32
        assert order.count("layout_check") == 1
        assert order[31] == "s3_upload"

    def test_baseline_delta_scoped_to_the_new_descriptor(self):
        with open(os.path.join(_HERE, "catalog_baseline.json"),
                  encoding="utf-8") as fh:
            baseline = json.load(fh)
        assert set(baseline) == {d.type_id for d in NODE_CATALOG}
        assert baseline["layout_check"] == \
            dataclasses.asdict(get_node_type("layout_check"))
