"""Property test: tool list determinism and filtering.

**Feature: mcp-servers, Property 3: Tool list determinism and filtering**

Spec: .kiro/specs/mcp-servers/ (design.md D8, D35, "2. `mcp_portal`
package" `catalog.py` row, Correctness Property 3; tasks.md 4.2).
Validates: Requirements 1.7, 5.4, 10.7, 14.4, 17.3.

For any view (or the aggregate endpoint), delivered-phase set and mutating
switch value, `tools_for_view` returns a list sorted by `(domain, name)` that
contains a tool iff its phase is delivered and it is read-only or the switch
is on, and contains the alias entry of a spec with `deprecated_alias` iff it
contains the spec itself: the alias sorted by its own name, carrying the
spec's schema and annotations and a description starting
`Deprecated alias of <name>; removed in <alias_removed_in>.`. Calling it
twice yields identical lists.

Run over the authored `CATALOG` and over generated catalogs (which carry
aliases and every phase/classification combination).
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_portal import VIEWS
from mcp_portal import catalog as cat

_VIEW = st.one_of(st.none(), st.sampled_from(VIEWS))
_PHASES = st.sets(st.sampled_from((1, 2, 3)), max_size=3).map(
    lambda s: tuple(sorted(s)))
_NAME = st.from_regex(r"\A[a-z][a-z0-9_]{0,20}\Z", fullmatch=True)


@st.composite
def catalogs(draw):
    names = draw(st.lists(_NAME, min_size=0, max_size=12, unique=True))
    alias_pool = [f"{name}_old" for name in names]
    specs = []
    for name, alias in zip(names, alias_pool):
        read_only = draw(st.booleans())
        destructive = (not read_only) and draw(st.booleans())
        has_alias = draw(st.booleans()) and alias not in names
        specs.append(cat.ToolSpec(
            name=name, domain=draw(st.sampled_from(VIEWS)), title=name,
            description="Does a thing.", resource_type="thing",
            input_schema=cat.schema({"usecase_id": cat.identifier()},
                                    required=["usecase_id"]),
            backend="devices", handler_module="devices",
            handler_function="list_devices", method="GET",
            resource="/api/v1/things",
            classification=cat.READ_ONLY if read_only else cat.MUTATING,
            phase=draw(st.sampled_from((1, 2, 3))),
            destructive=destructive,
            confirm_literal="yes" if destructive else None,
            deprecated_alias=alias if has_alias else None,
            alias_removed_in="2.0.0" if has_alias else None))
    return tuple(specs)


def _check(specs, view, phases, mutating):
    first = cat.tools_for_view(view, phases, mutating, catalog=specs)
    second = cat.tools_for_view(view, phases, mutating, catalog=specs)
    assert [t.model_dump() for t in first] == [t.model_dump() for t in second]

    by_name = {spec.name: spec for spec in specs}
    by_alias = {spec.deprecated_alias: spec for spec in specs
                if spec.deprecated_alias}
    listed = [tool.name for tool in first]
    assert len(listed) == len(set(listed))

    keys = []
    for tool in first:
        spec = by_name.get(tool.name) or by_alias[tool.name]
        keys.append((spec.domain, tool.name))
    assert keys == sorted(keys)

    for spec in specs:
        expected = ((view is None or spec.domain == view)
                    and spec.phase in phases
                    and (spec.read_only or mutating))
        assert (spec.name in listed) is expected
        if spec.deprecated_alias:
            assert (spec.deprecated_alias in listed) is expected

    for tool in first:
        if tool.name not in by_alias:
            continue
        spec = by_alias[tool.name]
        canonical = cat.to_mcp_tool(spec)
        assert tool.input_schema == canonical.input_schema
        assert tool.annotations == canonical.annotations
        assert tool.description.startswith(
            f"Deprecated alias of {spec.name}; removed in "
            f"{spec.alias_removed_in}.")


@settings(max_examples=100, deadline=None)
@given(view=_VIEW, phases=_PHASES, mutating=st.booleans())
def test_authored_catalog_listing(view, phases, mutating):
    _check(cat.CATALOG, view, phases, mutating)


@settings(max_examples=100, deadline=None)
@given(specs=catalogs(), view=_VIEW, phases=_PHASES, mutating=st.booleans())
def test_generated_catalog_listing(specs, view, phases, mutating):
    _check(specs, view, phases, mutating)
