"""Property 2: Catalog well-formedness (portal clauses that need no AST walk).

Feature: mcp-servers, Property 2: Catalog well-formedness

Spec: .kiro/specs/mcp-servers/ (design.md "Correctness Properties",
Property 2; §2 `catalog.py` row; Tool-catalogs conventions; tasks.md 5.7).
Validates: Requirements 1.2, 1.3, 3.1, 3.9, 5.1, 5.2, 5.6, 14.1, 14.2, 14.4, 17.1

The AST clauses (classification, arguments, permissions, audit flags, route
dispatch) are the honesty suites of tasks 5.6 and 5.8-5.11. Hypothesis
draws specs from the authored `CATALOG`, and draws single-field mutations
of them that each clause must reject.
"""
import dataclasses
import json
import re

import jsonschema
from hypothesis import given, settings
from hypothesis import strategies as st

import _handler_ast as h
from mcp_portal import catalog as cat
from mcp_portal.catalog import (CLAIM_NAMED_ARGS, LIMIT_DEFAULT, LIMIT_MAX,
                                PATH_PARAM_PATTERN, STATUS_SYNC_NOTE, TOOL_NAME_RE,
                                classification_text, render_description)

CATALOG = cat.CATALOG
BY_NAME = {s.name: s for s in CATALOG}
SEMVER_RE = re.compile(r'^\d+\.\d+\.\d+$')
DOTTED_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$')
CURSORS = ('next_token', 'page')
LOCAL = 'portal_admin_whoami'


def _routes():
    with open(cat.ROUTES_FILE, encoding='utf-8') as handle:
        return {(r['method'], r['resource']) for r in json.load(handle)}


ROUTES = _routes()


def _branches(schema):
    return [b for g in ('oneOf', 'anyOf') for b in schema.get(g, ())]


def _array_base(target: str):
    match = re.fullmatch(r'([a-z_][a-z0-9_]*)\[0\]', target)
    return match.group(1) if match else None


def problems(spec, catalog=CATALOG) -> list:
    by_name = {s.name: s for s in catalog}
    out = []
    schema = spec.input_schema
    props = schema.get('properties', {})
    required = set(schema.get('required', ()))

    def bad(text):
        out.append(f'{spec.name}: {text}')

    # names
    if not TOOL_NAME_RE.match(spec.name):
        bad('name pattern')
    # classification / confirmation
    if spec.read_only and spec.destructive:
        bad('read-only and destructive')
    if spec.destructive and (spec.confirm_target is None) == (spec.confirm_literal is None):
        bad('destructive needs exactly one of confirm_target / confirm_literal')
    if not spec.destructive and (spec.confirm_target or spec.confirm_literal):
        bad('confirmation on a non-destructive spec')
    if spec.confirm_literal is not None:
        bad('confirm_literal is for the edge singleton tools only')
    if isinstance(spec.confirm_target, str):
        if spec.confirm_target not in props:
            bad(f'confirm_target {spec.confirm_target!r} is not a property')
    elif isinstance(spec.confirm_target, tuple):
        branches = schema.get('oneOf', [])
        if len(branches) != len(spec.confirm_target):
            bad('per-branch confirm_target needs one entry per oneOf branch')
        for branch, target in zip(branches, spec.confirm_target):
            base = _array_base(target)
            name = base or target
            if name not in branch.get('required', ()) or name not in props:
                bad(f'confirm target {target!r} is not required by its branch')
                continue
            prop = props[name]
            if base:
                if not (prop.get('type') == 'array' and prop.get('minItems') == 1
                        and prop.get('maxItems') == 1):
                    bad(f'{name} must have minItems == maxItems == 1')
                prop = prop.get('items', {})
            pattern = prop.get('pattern') or next(
                (p.get('pattern') for p in prop.get('allOf', ()) if 'pattern' in p), None)
            if not pattern or re.search(pattern, '') is not None:
                bad(f'confirm target {target!r} needs a pattern excluding ""')
    # scope
    if spec.scope == 'usecase' and 'usecase_id' not in required:
        bad("scope 'usecase' requires usecase_id")
    if spec.scope == 'usecase_or_global' and ('usecase_id' not in props or 'usecase_id' in required):
        bad("scope 'usecase_or_global' offers an optional usecase_id")
    # permissions and routes
    if not spec.permissions and spec.name != LOCAL:
        bad('no permission')
    if spec.backend is None:
        if spec.name != LOCAL:
            bad('no backing route')
    elif (spec.method, spec.resource) not in ROUTES:
        bad('route not in catalog_routes.json')
    # schema
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        bad(f'invalid schema: {exc.message}')
    if schema.get('type') != 'object' or schema.get('additionalProperties') is not False:
        bad('schema must be an object with additionalProperties: false')
    for name in props:
        if name in CLAIM_NAMED_ARGS or name.startswith('custom:'):
            bad(f'claim-named property {name!r}')
    for branch in _branches(schema):
        if set(branch) != {'required'}:
            bad('a top-level oneOf/anyOf branch may differ in required only')
    for arg, target in spec.param_map.items():
        if target in spec.path_parameters and arg in props \
                and props[arg].get('pattern') != PATH_PARAM_PATTERN:
            bad(f'path argument {arg!r} lacks the path-parameter pattern')
    for segment in spec.path_parameters:
        if segment not in spec.param_map.values():
            bad(f'path segment {{{segment}}} is not a param_map target')
    for arg, where in spec.param_in.items():
        if where not in ('query', 'body') or spec.location_of(arg) == 'path':
            bad(f'param_in {arg!r} -> {where!r}')
    # handler
    if spec.name != LOCAL:
        uni = h.universe()
        if not uni.is_functions_module(spec.handler_module) or \
                uni.find_function(spec.handler_module, spec.handler_function) is None:
            bad('handler_module/handler_function does not exist')
    # status tool
    if spec.read_only and spec.status_tool is not None:
        bad('read-only spec with a status_tool')
    if not spec.read_only:
        observer = by_name.get(spec.status_tool)
        if observer is None or not observer.read_only:
            bad(f'status_tool {spec.status_tool!r} is not a read-only tool of the catalog')
    # description
    if not spec.description or not spec.description.endswith('.') or len(spec.description) > 300:
        bad('description must be a sentence ending in "." of <= 300 characters')
    rendered = render_description(spec)
    if f'Touches: {spec.resource_type}' not in rendered \
            or not rendered.endswith(classification_text(spec)):
        bad('rendered description')
    # aliases
    if bool(spec.deprecated_alias) != bool(spec.alias_removed_in):
        bad('deprecated_alias iff alias_removed_in')
    if spec.alias_removed_in and not SEMVER_RE.match(spec.alias_removed_in):
        bad('alias_removed_in is not a semver string')
    if spec.deprecated_alias:
        others = {s.deprecated_alias for s in catalog if s is not spec and s.deprecated_alias}
        if spec.deprecated_alias in by_name or spec.deprecated_alias in others:
            bad('alias collides')
    # D36
    if spec.bind_usecase_path is not None:
        if not (spec.read_only and spec.scope == 'usecase' and spec.result_shape == 'object'):
            bad('bind_usecase_path on a spec that is not RO / usecase / object')
        if not DOTTED_RE.match(spec.bind_usecase_path):
            bad('bind_usecase_path is not a dotted path')
    # accepted writes
    if spec.accepted_writes and (not spec.read_only
                                 or spec.accepted_writes_note != STATUS_SYNC_NOTE):
        bad('accepted_writes need an RO spec and STATUS_SYNC_NOTE')
    # limits and pagination
    if 'limit' in props:
        limit = props['limit']
        if limit.get('minimum') != 1 or limit.get('maximum') != (spec.limit_max or LIMIT_MAX) \
                or limit.get('default') != (spec.limit_default or LIMIT_DEFAULT) \
                or limit['default'] > limit['maximum']:
            bad('limit bounds')
    if ('page' in props) != ('page' in spec.param_map):
        bad('page iff mapped')
    if 'page' in props and 'offset' in props:
        bad('page and offset')
    cursor = any(c in spec.param_map for c in CURSORS)
    if cursor and 'limit' not in spec.param_map:
        bad('a mapped cursor needs a forwarded limit')
    if 'limit' in props and 'limit' not in spec.param_map:
        if cursor or 'offset' not in props:
            bad('a dispatcher-sliced limit maps no cursor and offers offset')
    if spec.result_shape == 'collection' and 'limit' in props and 'limit' not in spec.param_map \
            and 'offset' not in props:
        bad('dispatcher-sliced collection needs offset')
    # review-10 N1
    if spec.usecase_not_forwarded is not None and 'usecase_id' in spec.param_map:
        bad('usecase_not_forwarded with a forwarded usecase_id')
    return out


# ------------------------------------------------------------ properties

@settings(max_examples=100, deadline=None)
@given(st.sampled_from(CATALOG))
def test_every_spec_is_well_formed(spec):
    assert problems(spec) == []


def test_all_specs_are_well_formed():
    assert [p for spec in CATALOG for p in problems(spec)] == []


def test_names_and_aliases_are_unique():
    names = [s.name for s in CATALOG]
    aliases = [s.deprecated_alias for s in CATALOG if s.deprecated_alias]
    assert len(names) == len(set(names))
    assert len(set(names + aliases)) == len(names) + len(aliases)


def _mutations(spec):
    """Single-field changes each clause must reject."""
    schema = spec.input_schema
    out = [('name', dataclasses.replace(spec, name='Portal-Bad'))]
    if spec.read_only:
        out.append(('ro_destructive', dataclasses.replace(
            spec, destructive=True, confirm_literal='yes')))
        out.append(('ro_status_tool', dataclasses.replace(spec, status_tool=spec.name)))
    else:
        out.append(('no_status_tool', dataclasses.replace(spec, status_tool=None)))
    if spec.destructive:
        out.append(('no_confirm', dataclasses.replace(spec, confirm_target=None)))
    if spec.name != LOCAL:
        out.append(('no_permission', dataclasses.replace(spec, permissions=())))
        out.append(('unrouted', dataclasses.replace(spec, resource='/mcp-unregistered')))
    out.append(('claim', dataclasses.replace(spec, input_schema=dict(
        schema, properties=dict(schema.get('properties', {}), email={'type': 'string'})))))
    out.append(('open_schema', dataclasses.replace(
        spec, input_schema=dict(schema, additionalProperties=True))))
    out.append(('description', dataclasses.replace(spec, description='no full stop')))
    out.append(('alias', dataclasses.replace(spec, deprecated_alias=spec.name + '_old')))
    if 'limit' in schema.get('properties', {}):
        # ToolSpec regenerates the canonical `limit` schema, so the variant
        # is written past the constructor.
        props = dict(schema['properties'], limit=dict(schema['properties']['limit'], maximum=999))
        variant = dataclasses.replace(spec)
        object.__setattr__(variant, 'input_schema', dict(schema, properties=props))
        out.append(('limit', variant))
    if 'usecase_id' in schema.get('required', ()) and spec.scope == 'usecase':
        req = [r for r in schema['required'] if r != 'usecase_id']
        out.append(('scope', dataclasses.replace(spec, input_schema=dict(schema, required=req))))
    return out


@settings(max_examples=100, deadline=None)
@given(st.sampled_from(CATALOG).flatmap(lambda s: st.sampled_from(_mutations(s))))
def test_each_clause_rejects_its_violation(mutation):
    label, variant = mutation
    assert problems(variant), label


# ------------------------------------------------------ fixed cases

def test_per_branch_confirm_target():
    spec = BY_NAME['portal_devices_deployment_create']
    assert spec.confirm_target == ('target_thing_group', 'target_devices[0]')
    devices = spec.input_schema['properties']['target_devices']
    assert devices['minItems'] == devices['maxItems'] == 1
    wide = dict(devices, maxItems=2)
    props = dict(spec.input_schema['properties'], target_devices=wide)
    variant = dataclasses.replace(spec, input_schema=dict(spec.input_schema, properties=props))
    assert any('minItems' in p for p in problems(variant))
    group = dict(spec.input_schema['properties']['target_thing_group'])
    group.pop('pattern', None)
    group.pop('allOf', None)
    props = dict(spec.input_schema['properties'], target_thing_group=group)
    variant = dataclasses.replace(spec, input_schema=dict(spec.input_schema, properties=props))
    assert any('excluding ""' in p for p in problems(variant))


def test_no_portal_spec_uses_confirm_literal():
    assert all(s.confirm_literal is None for s in CATALOG)


def test_bind_usecase_path_fixed_cases():
    assert BY_NAME['portal_data_labeling_job_get'].bind_usecase_path == 'job.usecase_id'
    assert 'portal_data_labeling_manifest_get' not in BY_NAME
    assert [s.name for s in CATALOG if s.bind_usecase_path] == ['portal_data_labeling_job_get']
    variant = dataclasses.replace(BY_NAME['portal_data_labeling_job_get'],
                                  bind_usecase_path='job[].usecase_id')
    assert any('dotted path' in p for p in problems(variant))


def test_pagination_forwarding_fixed_cases():
    for name in ('portal_admin_audit_logs_query', 'portal_workflows_builds_list',
                 'portal_workflows_build_logs_get', 'portal_data_captures_list'):
        assert BY_NAME[name].param_map.get('limit') == 'limit', name
    preview = BY_NAME['portal_data_datasets_preview_get']
    assert preview.param_map.get('limit') == 'limit'
    assert preview.param_map.get('offset') == 'offset'
    for name in ('portal_admin_usecases_list', 'portal_devices_list',
                 'portal_devices_deployments_list'):
        assert 'offset' in BY_NAME[name].input_schema['properties'], name
    logs = BY_NAME['portal_workflows_build_logs_get']
    variant = dataclasses.replace(logs, param_map={k: v for k, v in logs.param_map.items()
                                                   if k != 'limit'})
    assert any('cursor' in p for p in problems(variant))


def test_usecase_not_forwarded_fixed_cases():
    assert BY_NAME['portal_admin_audit_logs_query'].usecase_not_forwarded == 'dead_filter'
    assert BY_NAME['portal_data_synthetic_session_retrain'].usecase_not_forwarded == \
        'stored_record'
    assert sum(1 for s in CATALOG if s.usecase_not_forwarded == 'injected') == 10
    spec = BY_NAME['portal_devices_list']
    variant = dataclasses.replace(spec, usecase_not_forwarded='injected')
    assert any('usecase_not_forwarded' in p for p in problems(variant))


def test_status_tool_rule():
    mutating = [s for s in CATALOG if not s.read_only]
    assert mutating and all(BY_NAME[s.status_tool].read_only for s in mutating)
