"""Unit tests for the ``wall_clock_limit_s`` parameter of the bridged
Custom Python node types.

``custom_python`` gains an optional ``wall_clock_limit_s`` (int, 1-120,
default 10 = the executor's built-in ``DEFAULT_WALL_CLOCK_LIMIT_SEC``)
carried on its ``emlpython`` element as ``wall-clock-limit-sec``.
``custom_python_preprocess`` carries the same parameter and element
because custom-python-frames Requirement 1.4 pins its element chain
identical to ``custom_python``'s. ``custom_python_source`` (a
Frame_Producer with its own environment-configured limit) is unchanged.
"""

import dataclasses
import json
import os

from workflow_core.catalog import (
    ARCHITECTURES,
    NODE_CATALOG,
    get_node_type,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BRIDGED = ("custom_python", "custom_python_preprocess")


def _params_by_name(type_id):
    return {p.name: p for p in get_node_type(type_id).parameters}


class TestWallClockParameter:
    def test_shape_on_both_bridged_node_types(self):
        for type_id in _BRIDGED:
            param = _params_by_name(type_id)["wall_clock_limit_s"]
            assert param.param_type == "int", type_id
            assert param.required is False, type_id
            assert param.default == 10, type_id
            assert param.constraints == {"min": 1, "max": 120}, type_id
            assert param.depends_on is None, type_id
            assert "default 10" in param.description, type_id

    def test_appended_after_the_pre_existing_parameters(self):
        assert [p.name for p in get_node_type("custom_python").parameters] == [
            "code", "requirements", "input_port_type", "output_port_type",
            "wall_clock_limit_s",
        ]
        assert [p.name for p in
                get_node_type("custom_python_preprocess").parameters] == [
            "code", "requirements", "wall_clock_limit_s",
        ]


class TestEmlpythonElement:
    def test_element_carries_handler_path_and_wall_clock_placeholder(self):
        for type_id in _BRIDGED:
            descriptor = get_node_type(type_id)
            assert {m.arch for m in descriptor.mappings} == set(ARCHITECTURES)
            for mapping in descriptor.mappings:
                assert len(mapping.element_chain) == 1, (type_id, mapping.arch)
                element = mapping.element_chain[0]
                assert element["factory"] == "emlpython"
                assert element["args_template"] == {
                    "handler-path": "{python_handler_path}",
                    "wall-clock-limit-sec": "{wall_clock_limit_s}",
                }
                assert mapping.plugin_dependencies == []
                assert mapping.executor_binding is None

    def test_both_bridged_node_types_share_one_element_chain(self):
        # custom-python-frames Requirement 1.4.
        post = get_node_type("custom_python")
        pre = get_node_type("custom_python_preprocess")
        for arch in ARCHITECTURES:
            assert pre.mapping_for(arch).element_chain == \
                post.mapping_for(arch).element_chain, arch


class TestOtherCustomNodeTypesUnchanged:
    def test_custom_python_source_has_no_wall_clock_parameter(self):
        names = [p.name for p in get_node_type("custom_python_source").parameters]
        assert "wall_clock_limit_s" not in names

    def test_custom_python_source_matches_the_recorded_baseline(self):
        with open(os.path.join(_HERE, "catalog_baseline.json"),
                  encoding="utf-8") as fh:
            baseline = json.load(fh)
        assert baseline["custom_python_source"] == \
            dataclasses.asdict(get_node_type("custom_python_source"))

    def test_baseline_entries_of_the_bridged_types_equal_the_live_descriptors(self):
        with open(os.path.join(_HERE, "catalog_baseline.json"),
                  encoding="utf-8") as fh:
            baseline = json.load(fh)
        assert set(baseline) == {d.type_id for d in NODE_CATALOG}
        for type_id in _BRIDGED:
            assert baseline[type_id] == \
                dataclasses.asdict(get_node_type(type_id)), type_id
