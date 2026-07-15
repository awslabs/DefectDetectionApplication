"""Property test for reference-counted Custom_Node_Type removal (task 9.5).

**Feature: custom-node-designer, Property 19: Reference-counted removal**

For all sets of saved WorkflowVersions items — random subsets referencing
the Custom_Node_Type via the ``custom_node_types`` map attribute recorded
at save, via a legacy list attribute, or via the stored definition
document fallback, with the others not referencing it (absent id in the
attribute, definition without the node, unloadable definition, or no
recorded key at all) — the removal decision permits removal if and only
if the referencing subset is empty, and every rejection lists exactly the
referencing workflow versions.

**Validates: Requirements 14.4, 14.5**

The logic under test (`item_references_node_type`,
`definition_references_node_type`, `evaluate_removal` in
functions/custom_node_types.py) is pure over plain dicts, so it is
exercised directly with no AWS involvement. The module is imported
through the shared moto-backed session fixture only so the real
`shared_utils` layer (not a test fake) backs the import.
"""

from __future__ import annotations

import copy

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.fixture(scope="session")
def node_types(aws_stack):
    """The real custom_node_types module, imported via the session stack."""
    return aws_stack.custom_node_types


# ---------------------------------------------------------------------------
# Generators: WorkflowVersions items over random reference shapes
# ---------------------------------------------------------------------------

#: The Custom_Node_Type whose removal is being decided.
NODE_TYPE_ID = "custom.uc-p19.frame-annotator"

#: Other type ids that may appear in reference attributes and definition
#: documents without ever counting as a reference to NODE_TYPE_ID.
_other_type_ids = st.sampled_from((
    "custom.uc-p19.other-node",
    "custom.uc-other.blur",
    "source.rtsp",
    "ai.inference",
    "",
))

_versions = st.integers(min_value=1, max_value=9)


def _node(type_id, index):
    return {"id": f"node-{index}", "type": type_id, "parameters": {}}


@st.composite
def _definition(draw, referencing):
    """A stored Workflow_Definition document; places NODE_TYPE_ID on the
    canvas at a random position exactly when `referencing`."""
    nodes = [_node(t, i)
             for i, t in enumerate(draw(st.lists(_other_type_ids, max_size=3)))]
    # Structural noise that must never count as a reference.
    if draw(st.booleans()):
        nodes.append("not-a-node-dict")
    if referencing:
        position = draw(st.integers(min_value=0, max_value=len(nodes)))
        nodes.insert(position, _node(NODE_TYPE_ID, "target"))
    return {"nodes": nodes, "connections": []}


#: Reference shapes an item may take. The first three reference
#: NODE_TYPE_ID; the rest must be treated as non-referencing.
KINDS = (
    "map_ref",         # custom_node_types map attribute contains the id
    "list_ref",        # legacy list attribute contains the id
    "definition_ref",  # no attribute; stored definition places the node
    "map_noref",       # map attribute without the id (attribute preferred)
    "list_noref",      # list attribute without the id (attribute preferred)
    "definition_noref",       # definition without the node
    "definition_unloadable",  # recorded key, but the document is unreadable
    "bare",                   # no attribute and no recorded definition key
)
REFERENCING_KINDS = frozenset({"map_ref", "list_ref", "definition_ref"})


@st.composite
def _workflow_world(draw):
    """A random set of WorkflowVersions items plus the stored definition
    documents behind their s3 keys and the expected referencing subset."""
    kinds = draw(st.lists(st.sampled_from(KINDS), max_size=8))
    items = []
    definitions = {}
    expected_references = []

    for index, kind in enumerate(kinds):
        item = {
            "workflow_id": f"wf-{index}",
            "version": draw(_versions),
        }

        if kind in ("map_ref", "map_noref", "list_ref", "list_noref"):
            other = draw(st.lists(_other_type_ids, unique=True, max_size=3))
            if kind.startswith("map"):
                references = {t: draw(_versions) for t in other}
                if kind == "map_ref":
                    references[NODE_TYPE_ID] = draw(_versions)
            else:
                references = list(other)
                if kind == "list_ref":
                    references.insert(
                        draw(st.integers(min_value=0, max_value=len(other))),
                        NODE_TYPE_ID)
            item["custom_node_types"] = references
            # The save-time attribute is preferred over the stored
            # definition document: a definition that contradicts the
            # attribute must never change the decision.
            if draw(st.booleans()):
                key = f"workflows/defs/{index}.json"
                item["s3_definition_key"] = key
                definitions[key] = draw(
                    _definition(referencing=draw(st.booleans())))

        elif kind in ("definition_ref", "definition_noref"):
            key = f"workflows/defs/{index}.json"
            item["s3_definition_key"] = key
            definitions[key] = draw(
                _definition(referencing=(kind == "definition_ref")))

        elif kind == "definition_unloadable":
            # The loader yields None for this key (unreadable document);
            # treated as non-referencing.
            item["s3_definition_key"] = f"workflows/defs/missing-{index}.json"

        # "bare": neither a reference attribute nor a definition key.

        if kind in REFERENCING_KINDS:
            expected_references.append({
                "workflow_id": item["workflow_id"],
                "version": item["version"],
            })
        items.append((item, kind in REFERENCING_KINDS))

    return items, definitions, expected_references


# ---------------------------------------------------------------------------
# Property 19
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(world=_workflow_world())
def test_reference_counted_removal(node_types, world):
    """**Feature: custom-node-designer, Property 19: Reference-counted removal**

    For all sets of WorkflowVersions items with random subsets
    referencing the Custom_Node_Type (map attribute, list attribute, or
    definition-document fallback) and the others not referencing it,
    reference detection classifies every item exactly, the removal
    decision permits removal if and only if the referencing subset is
    empty, and every rejection lists exactly the referencing workflow
    versions.

    **Validates: Requirements 14.4, 14.5**
    """
    items, definitions, expected_references = world
    loader = definitions.get  # missing/unloadable keys yield None

    # Reference detection: each item classified exactly as constructed,
    # without mutation.
    detected_references = []
    for item, expected_referencing in items:
        before = copy.deepcopy(item)
        referencing = node_types.item_references_node_type(
            item, NODE_TYPE_ID, loader)
        assert item == before
        assert referencing == expected_referencing
        if referencing:
            detected_references.append({
                "workflow_id": item["workflow_id"],
                "version": item["version"],
            })

    assert detected_references == expected_references

    # Removal decision (14.4, 14.5): permitted iff no references exist;
    # rejections list exactly the referencing workflow versions.
    decision = node_types.evaluate_removal(NODE_TYPE_ID, detected_references)

    if not expected_references:
        assert decision is None
    else:
        assert decision["code"] == "CUSTOM_NODE_TYPE_IN_USE"
        assert str(len(expected_references)) in decision["message"]
        assert NODE_TYPE_ID in decision["message"]
        assert decision["details"]["node_type_id"] == NODE_TYPE_ID
        assert decision["details"]["referencing_workflows"] == expected_references
