# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Property tests: the edge catalog's well-formedness and tool list.

**Feature: mcp-servers, Property 2: Catalog well-formedness** (edge clauses)
**Feature: mcp-servers, Property 3: Tool list determinism and filtering** (edge list)

Spec: .kiro/specs/mcp-servers/ (design.md §5 ``child/catalog.py``, D9, D32,
D34, D35, Correctness Properties 2 and 3; tasks.md 11.13).
Validates: Requirements 8.2, 8.3, 8.5, 10.1, 10.7, 14.1, 14.2, 14.4, 17.3.
"""
import ast
import os
import re

import jsonschema
from hypothesis import given, settings
from hypothesis import strategies as st

import _edge_ast as ea
from mcp_edge import DELIVERED_PHASES
from mcp_edge.child import catalog

SETTINGS = settings(max_examples=100, deadline=None)
CATALOG = catalog.CATALOG
NAMES = [s.name for s in CATALOG]
BY_NAME = {s.name: s for s in CATALOG}
CLAIM_NAMES = ('sub', 'iss', 'aud', 'scope', 'client_id', 'token_use', 'username', 'email')
DOTTED_PATH = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(\[\])?(\.[A-Za-z_][A-Za-z0-9_]*(\[\])?)*$')
SEMVER = re.compile(r'^\d+\.\d+\.\d+$')
PAGINATION_PARAMS = ('limit', 'size', 'maxResults', 'startingPoint', 'offset')


def _proxy_read_timeout() -> float:
    path = os.path.join(ea.BACKEND, 'mcp_edge', 'proxy.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'MCP_PROXY_UPSTREAM_READ_S'
                for t in node.targets):
            return node.value.value
    raise AssertionError('MCP_PROXY_UPSTREAM_READ_S not found')


UPSTREAM_READ_S = _proxy_read_timeout()


def _route_names(spec):
    """Names of the route function's parameters and Depends() model fields."""
    uni = ea.universe()
    ref = ea.FuncRef(spec.handler_module, spec.handler_function)
    node = uni.node(ref)
    names = set()
    args = node.args.args + node.args.kwonlyargs
    for arg in args:
        names.add(arg.arg)
    positional = node.args.args
    defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
    for arg, default in list(zip(positional, defaults)) + list(
            zip(node.args.kwonlyargs, node.args.kw_defaults)):
        if isinstance(default, ast.Call) and ea._call_name(default.func) == 'Depends':
            target = default.args[0] if default.args else ea._annotation_class(arg.annotation)
            resolved = uni.resolve_expr(ref, target) if target is not None else None
            if resolved and resolved[0] == 'class':
                cls = uni.modules[resolved[1][0]].classes[resolved[1][1]]
                names |= {i.target.id for i in cls.body
                          if isinstance(i, ast.AnnAssign) and isinstance(i.target, ast.Name)}
    return names


# ------------------------------------------------------------- Property 2

def test_names_and_aliases_are_unique():
    assert len(NAMES) == len(set(NAMES))
    aliases = [s.deprecated_alias for s in CATALOG if s.deprecated_alias]
    assert len(aliases) == len(set(aliases)) and not set(aliases) & set(NAMES)
    assert len([s for s in CATALOG if s.confirm_literal]) == 4


@SETTINGS
@given(spec=st.sampled_from(CATALOG))
def test_catalog_well_formedness(spec):
    assert catalog.TOOL_NAME_RE.fullmatch(spec.name)
    schema = spec.input_schema
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema.get('additionalProperties') is False
    props = schema.get('properties', {})
    assert not [p for p in props if p in CLAIM_NAMES or p.startswith('custom:')]
    for keyword in ('oneOf', 'anyOf'):
        for branch in schema.get(keyword, ()):
            assert set(branch) == {'required'}, (spec.name, keyword)
    # Classification, confirmation and status tool (D9, D32).
    if spec.read_only:
        assert not spec.destructive and spec.status_tool is None
    else:
        assert BY_NAME[spec.status_tool].read_only, spec.name
    if spec.destructive:
        assert (spec.confirm_target is None) != (spec.confirm_literal is None)
        assert 'confirm' in props and 'confirm' not in schema.get('required', ())
    else:
        assert spec.confirm_target is None and spec.confirm_literal is None
    if isinstance(spec.confirm_target, str):
        assert spec.confirm_target in props
    elif isinstance(spec.confirm_target, tuple):
        branches = schema.get('oneOf', ())
        assert len(branches) == len(spec.confirm_target)
        for branch, target in zip(branches, spec.confirm_target):
            name = target[:-3] if target.endswith('[0]') else target
            assert name in branch['required']
            prop = props[name]
            if target.endswith('[0]'):
                assert prop.get('minItems') == prop.get('maxItems') == 1
                prop = prop['items']
            assert prop.get('pattern') and not re.fullmatch(prop['pattern'], '')
    # Descriptions (D35).
    assert spec.description and spec.description.endswith('.') and len(spec.description) <= 300
    rendered = catalog.render_description(spec)
    assert f'Touches: {spec.resource_type} on this device.' in rendered
    assert rendered.endswith(catalog.classification_text(spec))
    if spec.read_only:
        assert rendered.endswith('Read-only.')
    elif not spec.destructive:
        assert rendered.endswith('MUTATING.')
    else:
        assert rendered.endswith(
            f'MUTATING, DESTRUCTIVE — requires confirm={catalog.confirm_text(spec)}.')
    assert bool(spec.deprecated_alias) == bool(spec.alias_removed_in)
    if spec.alias_removed_in:
        assert SEMVER.fullmatch(spec.alias_removed_in)
    # Handler (an existing route function, never an entry point).
    assert ea.universe().node(ea.FuncRef(spec.handler_module, spec.handler_function)) is not None
    # Shaping fields (D34).
    assert spec.scrub_paths is True
    if spec.image_field:
        assert spec.result_shape == 'object' and DOTTED_PATH.fullmatch(spec.image_field)
    for path in spec.drop_fields:
        assert DOTTED_PATH.fullmatch(path), (spec.name, path)
    if spec.result_constant is not None:
        assert spec.image_field
    if spec.accepted_effects:
        assert spec.read_only and spec.accepted_effects_note == catalog.ACCEPTED_EFFECT_NOTE
    # Pagination (review-6 M1, review-9 M2/N4).
    if 'limit' in props:
        limit = props['limit']
        assert limit['minimum'] == 1
        assert limit['maximum'] == (spec.limit_max or catalog.LIMIT_MAX)
        assert limit['default'] == (spec.limit_default or catalog.LIMIT_DEFAULT)
        assert limit['default'] <= limit['maximum']
    assert ('page' in props) == ('page' in spec.param_map)
    assert not ('page' in props and 'offset' in props)
    if 'limit' in props:
        reads = _route_names(spec) & set(PAGINATION_PARAMS)
        if reads:
            assert 'limit' in spec.param_map, (spec.name, reads)
            if reads & {'offset', 'startingPoint'}:
                assert 'offset' in spec.param_map
        else:
            assert 'offset' in props and not set(spec.param_map) & {'next_token', 'page'}
    # Text generation needs the feature; the proxy budget bounds the timeout.
    if spec.handler_module == 'endpoints.text_generation':
        assert spec.feature == 'vllm'
    assert spec.timeout_s <= UPSTREAM_READ_S - 10


def test_fixed_cases():
    images = BY_NAME['edge_workflow_images_list'].input_schema['properties']['limit']
    assert images['maximum'] == 2 and images['default'] == 2
    assert 'limit' in BY_NAME['edge_registration_executions_list'].param_map
    assert 'image_source_configuration' not in \
        BY_NAME['edge_image_source_preview'].input_schema['properties']
    counts = {c: len([s for s in CATALOG if s.classification == c])
              for c in (catalog.READ_ONLY, catalog.MUTATING)}
    assert len(CATALOG) == 73 and counts[catalog.READ_ONLY] == 47
    # 47 / 9 / 17 by phase (revision-12 recount); every Phase-1 tool is RO.
    assert [len([s for s in CATALOG if s.phase == p]) for p in (1, 2, 3)] == [47, 9, 17]
    assert all(s.read_only for s in CATALOG if s.phase == 1)


# ------------------------------------------------------------- Property 3

PHASE_SETS = st.sets(st.sampled_from([1, 2, 3]))
FEATURE_SETS = st.lists(st.sampled_from(['vllm', 'other']), unique=True)


@SETTINGS
@given(phases=PHASE_SETS, mutating=st.booleans(), features=FEATURE_SETS)
def test_tool_list_determinism_and_filtering(phases, mutating, features):
    phases = tuple(sorted(phases))
    first = catalog.tools_for(delivered_phases=phases, mutating_enabled=mutating,
                              features=features)
    second = catalog.tools_for(delivered_phases=phases, mutating_enabled=mutating,
                               features=features)
    assert [t.model_dump() for t in first] == [t.model_dump() for t in second]
    names = [t.name for t in first]
    assert names == sorted(names)
    listed = {s.name for s in CATALOG
              if s.phase in phases and (s.feature is None or s.feature in features)
              and (s.read_only or mutating)}
    by_tool = {t.name: t for t in first}
    for spec in CATALOG:
        assert (spec.name in by_tool) == (spec.name in listed), spec.name
        if spec.deprecated_alias:
            assert (spec.deprecated_alias in by_tool) == (spec.name in listed)
            if spec.name in listed:
                alias = by_tool[spec.deprecated_alias]
                assert alias.input_schema == by_tool[spec.name].input_schema
                assert alias.annotations == by_tool[spec.name].annotations
                assert alias.description.startswith(
                    f'Deprecated alias of {spec.name}; removed in {spec.alias_removed_in}.')
    for spec in CATALOG:
        accepted = catalog.listed(spec, phases, mutating, features)
        assert accepted == (spec.name in listed)


def test_delivered_list_is_phase_one_read_only():
    tools = catalog.tools_for(delivered_phases=DELIVERED_PHASES, features=('vllm',))
    assert DELIVERED_PHASES == (1,)
    assert len([t for t in tools if t.name in BY_NAME]) == 47
    assert all(BY_NAME[t.name].read_only for t in tools if t.name in BY_NAME)
    without = catalog.tools_for(delivered_phases=DELIVERED_PHASES)
    assert not [t for t in without if t.name in BY_NAME and BY_NAME[t.name].feature]
