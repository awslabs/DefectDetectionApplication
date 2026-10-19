"""bindingPoints slots under the feed-first compile order
(jpeg-folder-source-decode-ubuntu2204, design C1b and C5; tasks 1 and 2).

On x86_64, x86_64_nvidia and arm64_cpu the compiler emits a mixed
document's frame-feed segment first (C1b), inside ``compile()`` and before
the packager derives ``bindingPoints`` from the compiled dict. The slots
address segments by index (``slot["segment"]``), so a slot-bearing node
that moves down a segment must still address its own element.

The graph is ``icam_source -> capture`` plus ``aravis_camera_source ->
capture``, icam first in graph order, compiled for x86_64 (constrained)
and arm64_jp5 (unconstrained) and run through the packager's binding-point
pipeline exactly as the handler drives it, with no AWS:

- task 2, preservation (passes before and after the fix): the icam point's
  slot addresses the ``v4l2src`` ``device`` argument whatever the order;
- task 1, bug condition (fails on the unfixed tree): the slot sits at
  segment 1 on x86_64, after the reorder, and at segment 0 on arm64_jp5.

The module is imported through the shared moto-backed session fixture
only so its module-level boto3 clients bind to the mock (the
test_property_aravis_binding_points.py pattern).
"""
import json
import sys

import pytest

from workflow_core.catalog.custom import resolve_catalog
from workflow_core.compiler import CompileContext
from workflow_core.compiler import compile as compile_workflow
from workflow_core.serializer import parse

ICAM_DEVICE = "/dev/video1"


@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Import workflow_packaging inside the moto mock so its module-level
    boto3 clients (DynamoDB / S3 / KMS) are intercepted."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


def icam_aravis_definition():
    """icam_source and aravis_camera_source, icam first in graph order,
    each feeding its own capture."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "cam", "type": "icam_source", "position": {"x": 0, "y": 0},
             "parameters": {"device": ICAM_DEVICE}},
            {"id": "arv", "type": "aravis_camera_source",
             "position": {"x": 0, "y": 100},
             "parameters": {"camera_id": "Aravis-Fake-GV01"}},
            {"id": "cap1", "type": "capture", "position": {"x": 200, "y": 0},
             "parameters": {"output_path": "/out1"}},
            {"id": "cap2", "type": "capture", "position": {"x": 200, "y": 100},
             "parameters": {"output_path": "/out2"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "cam", "port": "out"},
             "to": {"node": "cap1", "port": "in"}},
            {"id": "c2", "from": {"node": "arv", "port": "out"},
             "to": {"node": "cap2", "port": "in"}},
        ],
    }


def packaged_icam_slot(packaging, arch):
    """(compiled dict, the icam point's single slot) for ``arch``, through
    gather_camera_input_nodes -> build_binding_points over compiler output."""
    definition = icam_aravis_definition()
    parse_result = parse(json.dumps(definition))
    assert parse_result.ok, parse_result.error
    graph = parse_result.graph

    camera_nodes = packaging.gather_camera_input_nodes(graph, set())
    hints = packaging.binding_hints_from_definition(definition)
    catalog = resolve_catalog([])
    descriptors_by_id = {d.type_id: d for d in catalog}
    compiled = compile_workflow(
        graph, arch, CompileContext(workflow_id="wf-feed-first", workflow_version="1"),
        simulation=False, catalog=catalog)
    assert not isinstance(compiled, list), compiled
    compiled_dict = compiled.to_dict()
    points = packaging.build_binding_points(
        camera_nodes, compiled_dict, arch, hints, descriptors_by_id)
    (icam_point,) = [point for point in points if point["nodeId"] == "cam"]
    (slot,) = icam_point["slots"]
    return compiled_dict, slot


@pytest.mark.parametrize("arch", ["x86_64", "arm64_jp5"])
def test_icam_slot_addresses_the_v4l2src_device_argument(packaging, arch):
    """Task 2 (preservation, order-agnostic): the slot names the node's
    rendered device, on the node's own v4l2src element."""
    compiled, slot = packaged_icam_slot(packaging, arch)

    assert slot["param"] == "device"
    assert slot["arg"] == "device"
    assert packaging._rendered_slot_value(compiled, slot) == ICAM_DEVICE
    element = compiled["segments"][slot["segment"]]["elements"][slot["element"]]
    assert element["factory"] == "v4l2src"
    assert element["nodeId"] == "cam"


@pytest.mark.parametrize("arch, expected_segment", [("x86_64", 1), ("arm64_jp5", 0)])
def test_icam_slot_segment_follows_the_feed_first_order(packaging, arch,
                                                         expected_segment):
    """Task 1 (bug condition): on x86_64 the Aravis feed segment is emitted
    first, so the icam slot is derived at segment 1; arm64_jp5 keeps graph
    order, segment 0."""
    _, slot = packaged_icam_slot(packaging, arch)

    assert slot["segment"] == expected_segment
