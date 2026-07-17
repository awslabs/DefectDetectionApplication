"""Property test for Custom Python node gathering and manifest membership
(custom-python-frames task 2.2).

**Feature: custom-python-frames, Property 3: Custom Python node gathering and manifest membership**

For any workflow graph containing an arbitrary mix of ``custom_python``
nodes, ``custom_python_preprocess`` nodes, and other node types with
arbitrary node ids, code strings, and requirements strings:

- ``gather_custom_python_nodes`` returns exactly the Custom Python nodes
  of both types (no other node gathered, none missed), with each node's
  ``code`` and ``requirements`` parameter values preserved;
- ``build_manifest``'s ``customPythonNodeIds`` equals exactly those
  nodes' ids.

**Validates: Requirements 2.4, 2.5**

Both functions are pure over the parsed graph / plain values, so they
are exercised directly with no AWS calls. The module is imported through
the shared moto-backed session fixture only so its module-level boto3
clients are intercepted and the real ``shared_utils`` layer backs the
import, mirroring the other packaging property tests.
"""

from __future__ import annotations

import sys

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
# Reference expectations, restated from Requirements 2.4 / 2.5 (not imported
# from the implementation, so the test cannot silently agree with a wrong
# type set).
# ---------------------------------------------------------------------------

CUSTOM_PYTHON_TYPES = ("custom_python", "custom_python_preprocess")

# Other node types a graph may mix in: realistic catalog type ids plus
# arbitrary strings, excluding the two Custom Python types.
OTHER_TYPE_EXAMPLES = (
    "folder_source", "camera_source", "dewarp", "format_convert",
    "model_inference", "capture", "overlay",
)

_id_alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
node_ids = st.text(alphabet=_id_alphabet, min_size=1, max_size=24)

other_types = st.one_of(
    st.sampled_from(OTHER_TYPE_EXAMPLES),
    st.text(min_size=1, max_size=30).filter(
        lambda t: t not in CUSTOM_PYTHON_TYPES),
)

# Arbitrary code / requirements parameter values as the definition may
# carry them (including empty and absent — gather normalizes to str).
code_values = st.text(max_size=200)
requirements_values = st.text(max_size=100)


@st.composite
def graphs(draw):
    """A WorkflowGraph mixing both Custom Python node types and other
    types, with unique random ids and random code/requirements values.
    Returns (graph, expected) where expected is the ordered list of
    {node_id, code, requirements} entries for the Custom Python nodes."""
    ids = draw(st.lists(node_ids, min_size=0, max_size=12, unique=True))
    nodes, expected = [], []
    for node_id in ids:
        kind = draw(st.sampled_from(("custom_python",
                                     "custom_python_preprocess",
                                     "other")))
        if kind == "other":
            nodes.append(Node(id=node_id, type=draw(other_types),
                              position=Position(0.0, 0.0),
                              parameters={"code": draw(code_values)}))
            continue
        parameters = {}
        code = draw(code_values)
        requirements = draw(requirements_values)
        parameters["code"] = code
        # requirements is optional; sometimes omit it entirely.
        include_requirements = draw(st.booleans())
        if include_requirements:
            parameters["requirements"] = requirements
        nodes.append(Node(id=node_id, type=kind,
                          position=Position(0.0, 0.0),
                          parameters=parameters))
        expected.append({
            "node_id": node_id,
            "code": str(code or ""),
            "requirements": str(requirements or "") if include_requirements else "",
        })
    return WorkflowGraph(nodes=nodes, connections=[]), expected


ARCHS = ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6")


@settings(deadline=None)
@given(scenario=graphs(), arch=st.sampled_from(ARCHS))
def test_gathering_and_manifest_membership(packaging, scenario, arch):
    """**Feature: custom-python-frames, Property 3: Custom Python node gathering and manifest membership**

    For any graph mixing both Custom Python node types and other node
    types with arbitrary ids/code/requirements, gather_custom_python_nodes
    returns exactly the Custom Python nodes with code and requirements
    preserved, and build_manifest's customPythonNodeIds equals exactly
    those nodes' ids.

    **Validates: Requirements 2.4, 2.5**
    """
    graph, expected = scenario

    gathered = packaging.gather_custom_python_nodes(graph)

    # Exactly the Custom Python nodes of both types, in graph order,
    # with code and requirements preserved (2.4).
    assert gathered == expected
    assert [n["node_id"] for n in gathered] == \
        [node.id for node in graph.nodes if node.type in CUSTOM_PYTHON_TYPES]

    # The manifest lists exactly those nodes' ids, both types together (2.5).
    manifest = packaging.build_manifest(
        "wf-1", 1, arch,
        gst_plugins=["dda-emlpython"], python_packages=[],
        custom_python_nodes=gathered, user={"user_id": "user-1"})
    assert manifest["customPythonNodeIds"] == [n["node_id"] for n in expected]
