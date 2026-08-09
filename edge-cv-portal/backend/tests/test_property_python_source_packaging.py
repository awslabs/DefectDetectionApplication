"""Property test for Custom Python source-node gathering, artifact
writing, and manifest membership (custom-python-source task 3.2).

**Feature: custom-python-source, Property 19: Packaging gathers source nodes exactly and preserves their code**

For any validated workflow embedding nodes of the three Custom Python
types (``custom_python``, ``custom_python_preprocess``,
``custom_python_source``) and other node types with arbitrary node ids,
code strings, and requirements strings:

- ``gather_custom_python_nodes`` gathers exactly those nodes with
  ``code`` and ``requirements`` preserved verbatim;
- ``build_arch_zip`` writes ``python/{nodeId}/handler.py`` and
  ``python/{nodeId}/requirements.txt`` for each gathered node into
  every architecture zip, and nothing under ``python/`` for any other
  node;
- ``build_manifest``'s ``customPythonNodeIds`` equals exactly those
  nodes' ids.

**Validates: Requirements 9.1, 9.2**

Generated graphs carry at most one ``custom_python_source`` node: the
validator's frame-feed coexistence rule rejects workflows with more
than one, so validated workflows reaching the packager never carry
two. Both functions under test are pure over the parsed graph / plain
values and the zip assembly with no curated plugins makes no AWS
calls; the module is imported through the shared moto-backed session
fixture only so its module-level boto3 clients are intercepted,
mirroring test_property_custom_python_gathering.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
import zipfile

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.serializer.models import Node, Position, WorkflowGraph


@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Import workflow_packaging inside the moto mock so its module-level
    boto3 clients (DynamoDB / S3 / KMS) are intercepted."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


# ---------------------------------------------------------------------------
# Reference expectations, restated from Requirements 9.1 / 9.2 (not
# imported from the implementation, so the test cannot silently agree
# with a wrong type set).
# ---------------------------------------------------------------------------

CUSTOM_PYTHON_TYPES = ("custom_python", "custom_python_preprocess",
                       "custom_python_source")

OTHER_TYPE_EXAMPLES = (
    "folder_source", "icam_source", "aravis_camera_source", "dewarp",
    "format_convert", "model_inference", "capture", "overlay",
)

_id_alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
node_ids = st.text(alphabet=_id_alphabet, min_size=1, max_size=24)

other_types = st.one_of(
    st.sampled_from(OTHER_TYPE_EXAMPLES),
    st.text(min_size=1, max_size=30).filter(
        lambda t: t not in CUSTOM_PYTHON_TYPES),
)

code_values = st.text(max_size=200)
requirements_values = st.text(max_size=100)


@st.composite
def graphs(draw):
    """A WorkflowGraph mixing all three Custom Python node types (at most
    one custom_python_source — validated workflows carry at most one under
    the frame-feed coexistence rule) and other types, with unique random
    ids and random code/requirements values. Returns (graph, expected)
    where expected is the ordered list of {node_id, code, requirements}
    entries for the Custom Python nodes."""
    ids = draw(st.lists(node_ids, min_size=0, max_size=12, unique=True))
    nodes, expected = [], []
    source_used = False
    for node_id in ids:
        kinds = ["custom_python", "custom_python_preprocess", "other"]
        if not source_used:
            kinds.append("custom_python_source")
        kind = draw(st.sampled_from(kinds))
        if kind == "other":
            nodes.append(Node(id=node_id, type=draw(other_types),
                              position=Position(0.0, 0.0),
                              parameters={"code": draw(code_values)}))
            continue
        if kind == "custom_python_source":
            source_used = True
        parameters = {"code": draw(code_values)}
        requirements = draw(requirements_values)
        include_requirements = draw(st.booleans())
        if include_requirements:
            parameters["requirements"] = requirements
        if kind == "custom_python_source" and draw(st.booleans()):
            parameters["allowed_uri_prefixes"] = "s3://plant-images/"
        nodes.append(Node(id=node_id, type=kind,
                          position=Position(0.0, 0.0),
                          parameters=parameters))
        expected.append({
            "node_id": node_id,
            "code": str(parameters["code"] or ""),
            "requirements": str(requirements or "") if include_requirements else "",
        })
    return WorkflowGraph(nodes=nodes, connections=[]), expected


ARCHS = ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6")


@settings(deadline=None)
@given(scenario=graphs())
def test_source_node_gathering_zip_files_and_manifest_membership(
        packaging, scenario):
    """**Feature: custom-python-source, Property 19: Packaging gathers source nodes exactly and preserves their code**

    For any validated workflow mixing all three Custom Python node types
    and other node types with arbitrary ids/code/requirements, the
    packager gathers exactly those nodes with code and requirements
    preserved verbatim, writes python/{nodeId}/handler.py and
    python/{nodeId}/requirements.txt for each into every architecture
    zip, and the manifest's customPythonNodeIds equals exactly those
    nodes' ids.

    **Validates: Requirements 9.1, 9.2**
    """
    graph, expected = scenario

    gathered = packaging.gather_custom_python_nodes(graph)

    # Exactly the Custom Python nodes of all three types, in graph
    # order, with code and requirements preserved verbatim (9.1).
    assert gathered == expected
    assert [n["node_id"] for n in gathered] == \
        [node.id for node in graph.nodes if node.type in CUSTOM_PYTHON_TYPES]

    expected_python_entries = set()
    for entry in expected:
        expected_python_entries.add(
            "python/{}/handler.py".format(entry["node_id"]))
        expected_python_entries.add(
            "python/{}/requirements.txt".format(entry["node_id"]))

    with tempfile.TemporaryDirectory() as work_dir:
        for arch in ARCHS:
            # The manifest lists exactly those nodes' ids, all three
            # types together (9.2).
            manifest = packaging.build_manifest(
                "wf-1", 1, arch,
                gst_plugins=[], python_packages=[],
                custom_python_nodes=gathered, user={"user_id": "user-1"})
            assert manifest["customPythonNodeIds"] == \
                [n["node_id"] for n in expected]

            # Both files per gathered node land in this architecture's
            # zip with verbatim content, and no other python/ entry
            # exists (9.1).
            zip_path = os.path.join(work_dir, "workflow-{}.zip".format(arch))
            packaging.build_arch_zip(
                zip_path, arch, manifest, definition_json="{}",
                compiled_json="{}", gst_plugins=[],
                custom_python_nodes=gathered)
            with zipfile.ZipFile(zip_path) as zf:
                python_entries = {name for name in zf.namelist()
                                  if name.startswith("python/")}
                assert python_entries == expected_python_entries
                for entry in expected:
                    handler = zf.read(
                        "python/{}/handler.py".format(entry["node_id"]))
                    requirements = zf.read(
                        "python/{}/requirements.txt".format(entry["node_id"]))
                    assert handler.decode("utf-8") == entry["code"]
                    assert requirements.decode("utf-8") == entry["requirements"]
