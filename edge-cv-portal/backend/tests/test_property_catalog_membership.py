"""Property test for merged Node_Type_Catalog membership (task 9.3).

**Feature: custom-node-designer, Property 11: Resolved catalog membership is exact**

For all sets of registered Custom_Node_Types with random lifecycle
states, deprecation flags, versions, and plugin pins, the palette
catalog resolved for a Use_Case equals the built-in NODE_CATALOG plus
exactly those non-deprecated custom types whose backing Plugin_Record
version is in test or prod state — test-state entries carrying the
test marker — while resolution for loading/validating/packaging
existing workflows additionally includes deprecated (and dev-state)
types.

**Validates: Requirements 8.2, 9.2, 9.6, 14.3**

Exercises the pure merge/marker/exclusion logic in
functions/node_catalog_resolution.py (resolve_palette_catalog,
resolve_resolution_catalog) directly over plain dicts with no AWS
involvement. Declarations reuse the valid wire shape from
test_custom_node_types.make_declaration so descriptor conversion is
the real one used by registration and the catalog endpoint.
"""

from __future__ import annotations

import copy

from hypothesis import given, settings
from hypothesis import strategies as st

from test_custom_node_types import make_declaration  # conftest sets sys.path

from node_catalog_resolution import (
    BUILTIN_TYPE_IDS,
    PALETTE_LIFECYCLE_STATES,
    resolve_palette_catalog,
    resolve_resolution_catalog,
)
from workflow_core.catalog.custom import descriptor_from_declaration
from workflow_core.catalog.nodes import NODE_CATALOG

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_TYPE_ID_POOL = st.sampled_from(
    ["custom.p11_a", "custom.p11_b", "custom.p11_c",
     "custom.p11_d", "custom.p11_e", "custom.p11_f"])

_PLUGIN_ID_POOL = st.sampled_from(["plg-1", "plg-2", "plg-3"])

#: dev / test / prod backing states, or None meaning the backing
#: Plugin_Record version is unknown (absent from the lifecycle map —
#: the palette must fail closed).
_STATE_OR_UNKNOWN = st.sampled_from(["dev", "test", "prod", None])


@st.composite
def catalog_and_lifecycles(draw):
    """A random stored catalog plus a random lifecycle-state map.

    Returns ``(items, lifecycle_states)``: CustomNodeTypes version items
    in random order — random versions, deprecated flags, and plugin pins
    (occasionally missing, i.e. an unpinned/corrupt item) — including,
    sometimes, a registration colliding with a built-in type id; and the
    Lifecycle_State of each pinned backing Plugin_Record version, with
    some pins deliberately absent from the map (unknown state).
    """
    type_ids = draw(st.lists(_TYPE_ID_POOL, unique=True,
                             min_size=1, max_size=6))
    if draw(st.booleans()):
        # A custom registration colliding with a built-in type id:
        # built-ins always win the merge.
        type_ids.append(draw(st.sampled_from(sorted(BUILTIN_TYPE_IDS))))

    items = []
    pinned_keys = set()
    for type_id in type_ids:
        versions = draw(st.sets(st.integers(min_value=1, max_value=9),
                                min_size=1, max_size=4))
        for version in sorted(versions):
            item = {
                "node_type_id": type_id,
                "version": version,
                "usecase_id": "uc-p11",
                "usecase_ids": ["uc-p11"],
                "declaration": make_declaration(type_id),
                "deprecated": draw(st.booleans()),
            }
            # Mostly pinned; occasionally the plugin pin is missing
            # entirely (fails closed like an unknown state).
            if draw(st.integers(min_value=0, max_value=9)) >= 1:
                plugin_id = draw(_PLUGIN_ID_POOL)
                plugin_version = draw(st.integers(min_value=1, max_value=4))
                item["plugin_id"] = plugin_id
                item["plugin_version"] = plugin_version
                pinned_keys.add((plugin_id, plugin_version))
            items.append(item)
    items = list(draw(st.permutations(items))) if len(items) > 1 else items

    lifecycle_states = {}
    for key in sorted(pinned_keys):
        state = draw(_STATE_OR_UNKNOWN)
        if state is not None:
            lifecycle_states[key] = state
    return items, lifecycle_states


# ---------------------------------------------------------------------------
# Reference oracle
# ---------------------------------------------------------------------------

def _latest_by_type(items):
    """Independent latest-version-per-type computation."""
    latest = {}
    for item in items:
        type_id = item["node_type_id"]
        current = latest.get(type_id)
        if current is None or item["version"] > current["version"]:
            latest[type_id] = item
    return latest


def _expected_palette_states(items, lifecycle_states):
    """{type_id: backing state} of exactly the palette-eligible types:
    latest version, not deprecated, backing state test or prod (dev and
    unknown excluded — fail closed)."""
    eligible = {}
    for type_id, item in _latest_by_type(items).items():
        if item.get("deprecated"):
            continue
        key = (item.get("plugin_id"), item.get("plugin_version"))
        if key[0] is None or key[1] is None:
            continue
        state = lifecycle_states.get(key)
        if state in PALETTE_LIFECYCLE_STATES:
            eligible[type_id] = state
    return eligible


# ---------------------------------------------------------------------------
# Property 11a: palette membership and markers are exact
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(data=catalog_and_lifecycles())
def test_palette_catalog_membership_is_exact(data):
    """**Feature: custom-node-designer, Property 11: Resolved catalog membership is exact**

    For all random catalogs and lifecycle-state maps, the resolved
    palette catalog is exactly the built-in NODE_CATALOG (unchanged,
    first, winning every type-id collision) plus one descriptor per
    eligible custom type — the latest version, non-deprecated, backing
    Plugin_Record in test or prod (dev and unknown excluded) — with
    ``test`` markers on exactly the merged test-state entries and
    nothing else in the catalog or the marker map.

    **Validates: Requirements 8.2, 9.2, 9.6, 14.3**
    """
    items, lifecycle_states = data
    items_before = copy.deepcopy(items)

    merged, markers = resolve_palette_catalog(items, lifecycle_states)

    latest = _latest_by_type(items)
    eligible_states = _expected_palette_states(items, lifecycle_states)
    # Built-ins win on collision: a colliding custom type never merges.
    expected_custom_ids = set(eligible_states) - BUILTIN_TYPE_IDS

    # The built-in catalog comes first, unchanged (8.2; built-ins win).
    assert merged[:len(NODE_CATALOG)] == NODE_CATALOG

    # Membership is exact: one entry per eligible custom type, in
    # deterministic type-id order, and nothing else (8.2, 9.2, 14.3).
    tail = merged[len(NODE_CATALOG):]
    assert [d.type_id for d in tail] == sorted(expected_custom_ids)

    # Each merged custom entry is the descriptor of the latest
    # version's stored declaration, byte-for-byte (8.2: same
    # declaration structure as built-in types).
    for descriptor in tail:
        expected = descriptor_from_declaration(
            latest[descriptor.type_id]["declaration"])
        assert descriptor == expected

    # Markers are exact: 'test' on precisely the merged test-state
    # entries; prod entries and built-ins never carry one (9.6).
    assert markers == {type_id: "test"
                       for type_id, state in eligible_states.items()
                       if state == "test" and type_id not in BUILTIN_TYPE_IDS}

    # Resolution is a read: the stored items are never mutated.
    assert items == items_before


# ---------------------------------------------------------------------------
# Property 11b: resolution membership additionally keeps deprecated types
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(data=catalog_and_lifecycles())
def test_resolution_catalog_additionally_includes_deprecated_types(data):
    """**Feature: custom-node-designer, Property 11: Resolved catalog membership is exact**

    For all random catalogs, the resolution catalog existing workflows
    load/validate/package against contains the built-in NODE_CATALOG
    plus exactly one descriptor per registered custom type regardless
    of deprecation or lifecycle state (deprecated and dev-state types
    stay resolvable), so its membership is always a superset of the
    palette's.

    **Validates: Requirements 8.2, 9.2, 9.6, 14.3**
    """
    items, lifecycle_states = data

    resolution = resolve_resolution_catalog(items)

    expected_ids = set(_latest_by_type(items)) - BUILTIN_TYPE_IDS

    assert resolution[:len(NODE_CATALOG)] == NODE_CATALOG
    assert [d.type_id for d in resolution[len(NODE_CATALOG):]] == \
        sorted(expected_ids)

    # The palette never offers something existing workflows could not
    # resolve (14.3: exclusion from new placement, not from loading).
    palette, _ = resolve_palette_catalog(items, lifecycle_states)
    assert {d.type_id for d in palette}.issubset(
        {d.type_id for d in resolution})
