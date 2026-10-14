"""Property test for Custom_Node_Type version retention and pinning (task 9.4).

**Feature: custom-node-designer, Property 18: Version retention and pinned resolution**

For all random sequences of Custom_Node_Type declaration updates,
every prior version item remains retrievable and unchanged with
version numbers strictly increasing (Requirement 14.1); and for all
random catalogs and pin maps, resolution returns exactly the pinned
version of a type when that version exists and the latest registered
version otherwise, with deprecated types remaining resolvable
(Requirement 14.2).

**Validates: Requirements 14.1, 14.2**

Two layers are exercised:

- Retention (14.1): the versioning helpers `next_node_type_version` and
  `new_node_type_item` from functions/custom_node_types.py, driven the
  way the update handler drives them (query latest -> next version ->
  put new item), against the real moto-backed CustomNodeTypes table
  from conftest.py, so retrieval via `query_node_type_versions` proves
  the prior version items actually survive each update byte-for-byte.

- Pinned resolution (14.2): the pure `resolution_items` merge in
  functions/node_catalog_resolution.py, exercised directly over plain
  dicts with no AWS involvement.
"""

from __future__ import annotations

import copy
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.fixture(scope="session")
def node_types(aws_stack):
    """The real custom_node_types module, imported via the session stack."""
    return aws_stack.custom_node_types


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_SAFE_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=0, max_size=20)

#: One declaration update: the payload the update handler would store as
#: the new version's declaration (contents are opaque to the versioning
#: helpers), plus the backing Plugin_Record version pinned at that update.
_update = st.fixed_dictionaries({
    "displayName": _SAFE_TEXT,
    "category": st.sampled_from(["Sources", "Processing", "Sinks", "AI"]),
    "param": st.integers(min_value=-1000, max_value=1000),
    "plugin_version": st.integers(min_value=1, max_value=9),
})

_TYPE_ID_POOL = st.sampled_from(
    ["custom.p18_a", "custom.p18_b", "custom.p18_c",
     "custom.p18_d", "custom.p18_e", "custom.p18_f"])


@st.composite
def catalog_and_pins(draw):
    """A random stored catalog plus a random pin map.

    Returns (items, pins, versions_by_type) where `items` are
    CustomNodeTypes version items in random order (random deprecated
    flags), `pins` maps a subset of type ids to an existing or a
    vanished version number, and `versions_by_type` records the
    registered versions of each type.
    """
    type_ids = draw(st.lists(_TYPE_ID_POOL, unique=True,
                             min_size=1, max_size=6))
    versions_by_type = {}
    items = []
    for type_id in type_ids:
        versions = sorted(draw(st.sets(st.integers(min_value=1, max_value=12),
                                       min_size=1, max_size=5)))
        versions_by_type[type_id] = versions
        for version in versions:
            items.append({
                "node_type_id": type_id,
                "version": version,
                "usecase_id": "uc-p18",
                "usecase_ids": ["uc-p18"],
                "plugin_id": f"plugin-{type_id}",
                "plugin_version": version,
                "declaration": {"typeId": type_id, "marker": version},
                "deprecated": draw(st.booleans()),
            })
    items = list(draw(st.permutations(items))) if len(items) > 1 else items

    pins = {}
    for type_id in type_ids:
        pin_kind = draw(st.sampled_from(["none", "existing", "vanished"]))
        if pin_kind == "existing":
            pins[type_id] = draw(st.sampled_from(versions_by_type[type_id]))
        elif pin_kind == "vanished":
            # Requirement 14.2 fallback: a pin to a version that no
            # longer exists resolves to the latest version of the type.
            pins[type_id] = draw(st.integers(min_value=13, max_value=99))
    return items, pins, versions_by_type


# ---------------------------------------------------------------------------
# Property 18a: version retention across declaration updates (14.1)
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(updates=st.lists(_update, min_size=1, max_size=6))
def test_updates_retain_every_prior_version(node_types, aws_stack, updates):
    """**Feature: custom-node-designer, Property 18: Version retention and pinned resolution**

    For all random sequences of declaration updates on a
    Custom_Node_Type, each update creates a new version item with a
    strictly increasing version number, and after every update all
    prior version items remain retrievable and unchanged, each still
    pinning the backing Plugin_Record version recorded at its creation.

    **Validates: Requirements 14.1, 14.2**
    """
    mod = node_types
    table = aws_stack.tables.custom_node_types

    # A fresh type id per example isolates examples on the shared
    # session table without truncation.
    type_id = f"custom.p18_{uuid.uuid4().hex[:12]}"

    snapshots = []  # deep copies taken at creation time
    latest_version = None

    for index, update in enumerate(updates):
        # Drive the helpers as the register/update handlers do:
        # query the latest version, number the next one, build and
        # store the new version item.
        version = mod.next_node_type_version(latest_version)

        # Strictly increasing numbering (14.1).
        assert version == (1 if latest_version is None
                           else latest_version + 1)

        declaration = {
            "typeId": type_id,
            "displayName": update["displayName"],
            "category": update["category"],
            "param": update["param"],
        }
        item = mod.new_node_type_item(
            node_type_id=type_id,
            version=version,
            usecase_id="uc-p18",
            usecase_ids=["uc-p18"],
            plugin_id="plugin-p18",
            plugin_version=update["plugin_version"],
            declaration=declaration,
            user_id="user-p18",
            timestamp=1_700_000_000_000 + index,
        )
        table.put_item(Item=mod.to_dynamo_json(item))
        snapshots.append(copy.deepcopy(item))
        latest_version = version

        # After this update, every prior version item (and the new one)
        # remains retrievable and unchanged (14.1).
        stored = mod.query_node_type_versions(type_id)
        assert [s["version"] for s in stored] == \
            list(range(len(snapshots), 0, -1))  # newest first, no gaps

        by_version = {s["version"]: s for s in stored}
        for snapshot in snapshots:
            assert by_version[snapshot["version"]] == snapshot

    # The retained items still pin the backing Plugin_Record version
    # recorded at their creation, regardless of later updates (14.2
    # groundwork: packaging resolves the recorded plugin version).
    stored = {s["version"]: s for s in mod.query_node_type_versions(type_id)}
    for index, update in enumerate(updates):
        assert stored[index + 1]["plugin_version"] == update["plugin_version"]


# ---------------------------------------------------------------------------
# Property 18b: pinned resolution (14.2)
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(data=catalog_and_pins())
def test_resolution_honors_pins_and_falls_back_to_latest(data):
    """**Feature: custom-node-designer, Property 18: Version retention and pinned resolution**

    For all random catalogs of Custom_Node_Type version items and all
    random pin maps, resolution returns exactly one item per type: the
    pinned version when it exists, the latest registered version
    otherwise (including when the pin names a vanished version), with
    deprecated items resolving like any other (14.3 interplay: saved
    workflows stay resolvable) and the input items never mutated.

    **Validates: Requirements 14.1, 14.2**
    """
    from node_catalog_resolution import resolution_items

    items, pins, versions_by_type = data
    items_before = copy.deepcopy(items)

    resolved = resolution_items(items, pins)

    # Exactly one resolved item per registered type, ordered by type id.
    assert [r["node_type_id"] for r in resolved] == sorted(versions_by_type)

    by_key = {(i["node_type_id"], i["version"]): i for i in items}
    for entry in resolved:
        type_id = entry["node_type_id"]
        versions = versions_by_type[type_id]
        pin = pins.get(type_id)
        expected_version = pin if pin in versions else max(versions)
        # The pinned version when it exists, the latest otherwise (14.2).
        assert entry["version"] == expected_version
        # The resolved entry is the stored version item itself: the
        # declaration (and deprecated flag) recorded for that version.
        assert entry == by_key[(type_id, expected_version)]

    # Without pins every type resolves to its latest version (14.2
    # fallback baseline).
    unpinned = resolution_items(items)
    assert {r["node_type_id"]: r["version"] for r in unpinned} == \
        {t: max(v) for t, v in versions_by_type.items()}

    # Resolution is a read: the stored items are never mutated.
    assert items == items_before
