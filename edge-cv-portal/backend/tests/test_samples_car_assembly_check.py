# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
The car-assembly-check sample workflows (task 13.3; design §10, DD17,
DD19, DD21; Requirements 6.3, 6.5, 7.2, 9.1, 10.8, 11.4, 11.6).

`samples/car-assembly-check/workflow.json`, `workflow-batch.json` and
`workflow-color.json` are `POST /workflows` definition bodies whose
`expected_regions` and `min_iou` are `<REPLACE: ...>` placeholders until the
label run of 6.2. With fixture values substituted, each one:

* parses, and validates with zero errors and no W1 / W4 / V14 finding;
* compiles for arm64_jp5 (the Verification_Device's arch), arm64_jp6,
  arm64_jp7 and the simulation arch, to segments holding exactly one
  `emlcapture` and four executor bindings (`layout`, `verdict`, `accept`,
  `reject`), with no rotation `videoflip` (the only `videoflip` allowed is
  the `method=automatic` EXIF step of `folder_source`'s JPEG file chain);
* carries template T, which parses as JSON and renders through the
  device's `output_bindings.render_json_template` to a boolean `pass` and a
  list `flagged`.

The README's production-camera form (a `rotate` node with `rotate-180`
between `source` and `gray`) validates and compiles with one
`videoflip method=rotate-180` ahead of the bridge element.
"""
import copy
import json
import os
import sys

import pytest

from workflow_core.catalog.models import ARCH_ARM64_JP5, ARCH_ARM64_JP6, ARCH_ARM64_JP7, ARCH_SIM
from workflow_core.compiler import CompileContext
from workflow_core.compiler import compile as compile_workflow
from workflow_core.serializer import parse
from workflow_core.validator import SEVERITY_ERROR, validate

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_SAMPLES = os.path.join(_REPO, "samples", "car-assembly-check")
_DEVICE_BACKEND = os.path.join(_REPO, "src", "backend")
if _DEVICE_BACKEND not in sys.path:
    # Appended: the device tree must never shadow a Portal module.
    sys.path.append(_DEVICE_BACKEND)

from workflow_engine import output_bindings  # noqa: E402

FILES = ("workflow.json", "workflow-batch.json", "workflow-color.json")
ARCHES = ((ARCH_ARM64_JP5, False), (ARCH_ARM64_JP6, False), (ARCH_ARM64_JP7, False), (ARCH_SIM, True))
FIXTURE_REGIONS = json.dumps({
    "rear_fender": [[0.12, 0.44], [0.19, 0.38], [0.30, 0.37], [0.47, 0.37], [0.47, 0.86],
                    [0.40, 0.90], [0.31, 0.90], [0.26, 0.82], [0.12, 0.80]],
    "front_fender": [[0.47, 0.37], [0.74, 0.37], [0.88, 0.47], [0.90, 0.60], [0.86, 0.78],
                     [0.78, 0.81], [0.72, 0.88], [0.63, 0.88], [0.58, 0.80], [0.47, 0.80]],
    "roof": [[0.30, 0.36], [0.33, 0.24], [0.40, 0.16], [0.50, 0.13], [0.60, 0.16],
             [0.68, 0.24], [0.72, 0.36]]})
FIXTURE_MIN_IOU = 0.65
TEMPLATE_T = {"pass": "{layout.layout.pass}",
              "front_fender": "{layout.layout.pieces.front_fender.status}",
              "rear_fender": "{layout.layout.pieces.rear_fender.status}",
              "roof": "{layout.layout.pieces.roof.status}",
              "flagged": "{layout.layout.flagged}", "sent_at": "{now_iso}"}
BINDINGS = ["layout", "verdict", "accept", "reject"]


def load(name):
    with open(os.path.join(_SAMPLES, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def nodes_of(definition):
    return {node["id"]: node for node in definition["nodes"]}


def filled(definition):
    result = copy.deepcopy(definition)
    parameters = nodes_of(result)["layout"]["parameters"]
    parameters["expected_regions"] = FIXTURE_REGIONS
    parameters["min_iou"] = FIXTURE_MIN_IOU
    return result


def graph_of(definition):
    result = parse(json.dumps(definition))
    assert result.ok, result.error
    return result.graph


def compiled(definition, arch, simulation):
    document = compile_workflow(graph_of(definition), arch,
                                CompileContext(workflow_id="wf-car", workflow_version="1"),
                                simulation=simulation)
    assert not isinstance(document, list), document
    return document.to_dict()


def elements(document):
    return [element for segment in document["segments"] for element in segment["elements"]]


def production_camera_form():
    """The README's substitution: rotate (rotate-180) between source and gray."""
    definition = filled(load("workflow.json"))
    definition["nodes"].append({"id": "rotate", "type": "rotate", "position": {"x": 110, "y": 0},
                                "parameters": {"method": "rotate-180"}})
    for connection in definition["connections"]:
        if connection["from"]["node"] == "source":
            connection["to"] = {"node": "rotate", "port": "in"}
    definition["connections"].append({"id": "c-rotate", "from": {"node": "rotate", "port": "out"},
                                      "to": {"node": "gray", "port": "in"}})
    return definition


# ---------------------------------------------------------------------------
# The files as checked in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FILES)
def test_the_checked_in_shape(name):
    definition = load(name)
    nodes = nodes_of(definition)
    layout_parameters = nodes["layout"]["parameters"]
    assert layout_parameters["expected_regions"].startswith("<REPLACE: ")
    assert str(layout_parameters["min_iou"]).startswith("<REPLACE: ")
    assert layout_parameters["min_outline_overlap"] == 0.1
    assert layout_parameters["min_confidence"] == 0.5
    types = [node["type"] for node in definition["nodes"]]
    assert "rotate" not in types
    assert types.count("capture") == 1
    assert nodes["capture"]["parameters"] == {"output_path": "/aws_dda/captures", "interval": 0,
                                              "quality": 100}
    assert nodes["verdict"]["parameters"] == {"condition": "layout.layout.pass == true"}
    assert nodes["model"]["parameters"]["detection_sort_order"] == "left_to_right"
    for node_id, topic in (("accept", "dda/car-assembly/pass"), ("reject", "dda/car-assembly/fail")):
        parameters = nodes[node_id]["parameters"]
        assert parameters["topic"] == topic and parameters["greengrass"] is True
        assert parameters["payload_format"] == "json"
        assert json.loads(parameters["payload_template"]) == TEMPLATE_T
    assert "{execution_id}" not in json.dumps(definition) and "{capture_id}" not in json.dumps(definition)


def test_the_variants_differ_only_where_they_should():
    gray, batch, color = (load(name) for name in FILES)
    with open(os.path.join(_SAMPLES, "grayscale_handler.py"), "r", encoding="utf-8") as handle:
        assert nodes_of(gray)["gray"]["parameters"] == {"code": handle.read(), "requirements": "",
                                                        "wall_clock_limit_s": 10}
    assert nodes_of(gray)["source"]["type"] == "aravis_camera_source"
    assert nodes_of(batch)["source"] == dict(nodes_of(gray)["source"], type="folder_source", parameters={
        "location": "/aws_dda/batch-input/", "file_pattern": "*.jpg"})
    assert [n for n in batch["nodes"] if n["id"] != "source"] == [
        n for n in gray["nodes"] if n["id"] != "source"]
    assert batch["connections"] == gray["connections"]
    assert "gray" not in nodes_of(color)
    assert nodes_of(gray)["model"]["parameters"]["modelName"] == "car-assembly-seg-gray"
    assert nodes_of(color)["model"]["parameters"]["modelName"] == "car-assembly-seg-color"
    assert {"id": "c1", "from": {"node": "source", "port": "out"},
            "to": {"node": "model", "port": "in"}} in color["connections"]


# ---------------------------------------------------------------------------
# Validate and compile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FILES)
def test_validates_with_zero_errors(name):
    findings = validate(graph_of(filled(load(name))))
    assert [f for f in findings if f.severity == SEVERITY_ERROR] == []
    assert not [f.code for f in findings if f.code.startswith(("W1", "W4", "V14"))]


@pytest.mark.parametrize("arch, simulation", ARCHES)
@pytest.mark.parametrize("name", FILES)
def test_compiles(name, arch, simulation):
    document = compiled(filled(load(name)), arch, simulation)
    factories = [element["factory"] for element in elements(document)]
    assert factories.count("emlcapture") == 1
    assert sorted(b["nodeId"] for b in document["executorBindings"]) == sorted(BINDINGS)
    flips = [element for element in elements(document) if element["factory"] == "videoflip"]
    assert all(flip["nodeId"] == "source" and flip["args"].get("method") == "automatic"
               for flip in flips), flips
    if name != "workflow-batch.json":
        assert flips == []


def test_production_camera_form_adds_one_rotate_180_ahead_of_the_bridge():
    definition = production_camera_form()
    findings = validate(graph_of(definition))
    assert [f for f in findings if f.severity == SEVERITY_ERROR] == []
    document = compiled(definition, ARCH_ARM64_JP5, False)
    chain = elements(document)
    flips = [index for index, element in enumerate(chain) if element["factory"] == "videoflip"]
    assert len(flips) == 1
    assert chain[flips[0]]["args"]["method"] == "rotate-180"
    bridge = next(index for index, element in enumerate(chain) if element["nodeId"] == "gray")
    assert flips[0] < bridge


# ---------------------------------------------------------------------------
# Template T through the device renderer
# ---------------------------------------------------------------------------


def test_template_t_renders_native_types():
    template = json.loads(nodes_of(load("workflow.json"))["accept"]["parameters"]["payload_template"])
    section = {"pass": False, "flagged": ["b07d44aa"],
               "pieces": {"front_fender": {"status": "ok"}, "rear_fender": {"status": "misplaced"},
                          "roof": {"status": "missing"}}}
    payload = output_bindings.render_json_template(template, {"layout": {"layout": section}})
    assert payload["pass"] is False
    assert payload["flagged"] == ["b07d44aa"]
    assert (payload["front_fender"], payload["rear_fender"], payload["roof"]) == ("ok", "misplaced", "missing")
    assert isinstance(payload["sent_at"], str) and payload["sent_at"]
    assert json.loads(json.dumps(payload)) == payload
