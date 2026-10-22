"""Catalog arguments <-> route parameters (`test_mcp_catalog_arguments.py`).

Spec: .kiro/specs/mcp-servers/ (design.md §2 `catalog.py` row, "Argument <->
route rule" and `SERVER_CONSUMED_ARGS`; Testing Strategy row "Catalog
arguments <-> route parameters"; tasks.md 5.10). Requirements 1.3, 1.6, 5.6.

The read collector of `_handler_ast.py` follows the classification walk
(plus `getattr(<module>, '<literal>')` delegation) and collects the keys a
route reads from `queryStringParameters` and the JSON body, folding string
constants (module-level, same-module or imported; `Name` or `module.CONST`)
and the elements of tuple/list/set constants iterated in loops and
comprehensions (review-9 N3). Path-mapped targets are checked against the
`{name}` segments of the spec's `resource` instead. Required keys are those
guarded by a `400` branch on `not <var>` for a read that is not reassigned
in between (review-8 N1), or listed in `validate_required_fields`.
"""
import dataclasses

import pytest

import _handler_ast as h
from mcp_portal import catalog as cat
from mcp_portal.catalog import SERVER_CONSUMED_ARGS

ROUTED = [s for s in cat.CATALOG if s.backend is not None]
BY_NAME = {s.name: s for s in cat.CATALOG}
CURSORS = ('next_token', 'page')


@dataclasses.dataclass
class Route:
    reads: set           # {(kind, key)}
    required: set        # {(kind, key)}
    at_least_one: list   # [frozenset((kind, key), ...)]
    graph: list
    decorator_reads_usecase: bool
    decorator_global: bool

    @property
    def keys(self) -> set:
        return {key for kind, key in self.reads if kind in (h.QUERY, h.BODY)}

    def reads_key(self, key: str) -> bool:
        return key in self.keys


def route(module: str, function: str) -> Route:
    uni = h.universe()
    start = uni.find_function(module, function)
    assert start is not None, (module, function)
    reads, required, groups, graph = h.ReadCollector(uni).collect(start)
    dreads, dglobal = h.decorator_usecase(uni.node(start))
    return Route({(r.kind, r.key) for r in reads}, set(required), groups, graph,
                 dreads, dglobal)


def spec_route(spec) -> Route:
    return route(spec.handler_module, spec.handler_function)


def _required_in_schema(spec, args) -> bool:
    schema = spec.input_schema
    required = set(schema.get('required', ()))
    branches = [set(b.get('required', ())) for g in ('oneOf', 'anyOf')
                for b in schema.get(g, ())]
    return any(a in required for a in args) or bool(branches) and all(
        any(a in b for a in args) for b in branches)


def argument_problems(spec, r: Route = None) -> list:
    r = r or spec_route(spec)
    problems = []
    props = spec.input_schema.get('properties', {})
    # 1. every forwarded argument names a parameter the route reads
    for arg in props:
        if arg in SERVER_CONSUMED_ARGS and arg not in spec.param_map:
            continue
        target = spec.param_map.get(arg, arg)
        if spec.location_of(arg) == 'path':
            if target not in spec.path_parameters:
                problems.append(f'{spec.name}: {arg} -> path {target!r} is not a segment '
                                f'of {spec.resource}')
        elif not r.reads_key(target):
            problems.append(f'{spec.name}: {arg} -> {target!r} is not read by the route')
    # 2. every required key is required by the schema
    for kind, key in sorted(r.required):
        if kind == h.PATH:
            continue
        args = [a for a in props if spec.param_map.get(a, a) == key]
        if not args:
            problems.append(f'{spec.name}: the route requires {kind} {key!r}, not offered')
        elif not _required_in_schema(spec, args):
            problems.append(f'{spec.name}: the route requires {kind} {key!r}, '
                            f'optional in the schema')
    for group in r.at_least_one:
        args = [a for a in props for _k, key in group if spec.param_map.get(a, a) == key]
        if not _required_in_schema(spec, args):
            problems.append(f'{spec.name}: the route requires one of {sorted(group)}, '
                            'not a schema group')
    # 3. the converse `usecase_id` clause (review-9 M1; review-10 N1)
    reads_usecase = r.decorator_reads_usecase or r.reads_key('usecase_id')
    if reads_usecase and 'usecase_id' not in spec.param_map \
            and not spec.usecase_not_forwarded \
            and not (spec.scope == 'global' and r.decorator_global):
        problems.append(f'{spec.name}: the route reads usecase_id; no param_map entry '
                        'and no usecase_not_forwarded')
    if spec.usecase_not_forwarded and (not reads_usecase or 'usecase_id' in spec.param_map):
        problems.append(f'{spec.name}: usecase_not_forwarded={spec.usecase_not_forwarded!r} '
                        'is stale')
    # 4. pagination (review-9 M2/N4)
    reads_limit, reads_offset = r.reads_key('limit'), r.reads_key('offset')
    cursor = any(a in spec.param_map for a in CURSORS)
    if (reads_limit or cursor) and 'limit' not in spec.param_map:
        problems.append(f'{spec.name}: the route paginates (limit read: {reads_limit}, '
                        f'cursor mapped: {cursor}); limit has no param_map entry')
    if reads_offset and 'offset' not in spec.param_map:
        problems.append(f'{spec.name}: the route reads offset; no param_map entry')
    if 'limit' in props and 'limit' not in spec.param_map:
        if cursor:
            problems.append(f'{spec.name}: a dispatcher-sliced limit maps a cursor')
        if 'offset' not in props:
            problems.append(f'{spec.name}: a dispatcher-sliced limit offers no offset')
    if 'limit' in props:
        cap = h.limit_cap(r.graph) if 'limit' in spec.param_map else None
        expected = min(200, cap or 200, spec.limit_max or 200)
        if props['limit'].get('maximum') != expected:
            problems.append(f"{spec.name}: limit maximum {props['limit'].get('maximum')}, "
                            f'expected {expected} (route cap {cap}, limit_max {spec.limit_max})')
    return problems


@pytest.mark.parametrize('spec', ROUTED, ids=lambda s: s.name)
def test_arguments_match_route_parameters(spec):
    assert argument_problems(spec) == []


# ------------------------------------------------- fixed regression cases

def test_captures_list():
    r = route('captures', 'list_captures')
    assert {(h.QUERY, 'usecase_id'), (h.QUERY, 'prefix')} <= r.required
    assert h.limit_cap(r.graph) == 100
    spec = BY_NAME['portal_data_captures_list']
    assert {'usecase_id', 'prefix'} <= set(spec.input_schema['required'])
    assert spec.input_schema['properties']['limit']['maximum'] == 30


def test_datasets_prefix_and_preview_cap():
    assert (h.BODY, 'prefix') in route('datasets', 'count_images').required \
        or (h.QUERY, 'prefix') in route('datasets', 'count_images').required
    preview = route('datasets', 'get_image_preview')
    assert (h.QUERY, 'prefix') in preview.required
    assert h.limit_cap(preview.graph) == 50
    assert not any(key == 'prefix' for _k, key in route('datasets', 'list_datasets').required)


def test_analyze_device_logs_reads_exactly_usecase_id_and_hours():
    r = route('device_logs_analyzer', 'analyze_device_logs')
    assert {(k, key) for k, key in r.reads if k != h.PATH} == {
        (h.QUERY, 'usecase_id'), (h.QUERY, 'hours')}
    props = BY_NAME['portal_devices_logs_analyze'].input_schema['properties']
    assert 'hours' in props and 'component' not in props and 'question' not in props


def test_validate_workflow_reads_version_only():
    r = route('workflow_validation', 'validate_workflow')
    assert {(k, key) for k, key in r.reads if k == h.BODY} == {(h.BODY, 'version')}
    props = BY_NAME['portal_workflows_validate'].input_schema['properties']
    assert 'version' in props and 'definition' not in props


@pytest.mark.parametrize('module,function,keys', [
    ('training', 'transform_manifest', {'source_manifest_uri'}),
    ('greengrass_publish', 'publish_component', {'component_name', 'component_version'}),
    ('data_management', 'create_folder', {'folder_path'}),
    ('synthetic_data', 'get_prompt_template', {'object_type', 'defect_type'}),
    ('build_jobs', 'list_build_branches', {'repository'}),
    ('component_configuration', 'get_configuration_schema', {'component_name'}),
    ('workflows', 'create_workflow', {'name', 'definition'}),
    ('deployments', 'get_deployment', {'usecase_id'}),
    ('deployments', 'cancel_deployment', {'usecase_id'}),
])
def test_required_keys(module, function, keys):
    assert keys <= {key for _k, key in route(module, function).required}


def test_create_folder_bucket_is_defaulted():
    r = route('data_management', 'create_folder')
    assert r.reads_key('bucket')
    assert not any(key == 'bucket' for _k, key in r.required)


def test_compilation_reads_targets_and_offers_no_auto_triggered():
    assert route('compilation', 'start_compilation_job').reads_key('targets')
    props = BY_NAME['portal_models_compilation_create'].input_schema['properties']
    assert 'targets' in props and 'auto_triggered' not in props


def test_cursor_names():
    for name in ('portal_workflows_builds_list', 'portal_workflows_build_logs_get'):
        spec = BY_NAME[name]
        assert spec.param_map['next_token'] == 'nextToken'
        assert route(spec.handler_module, spec.handler_function).reads_key('nextToken')
    logs = route('training', 'get_training_logs')
    assert logs.reads_key('nextForwardToken') and not logs.reads_key('nextToken')
    assert BY_NAME['portal_models_training_logs_get'].param_map['next_token'] == 'nextForwardToken'


def test_component_schema_reads_no_usecase_id():
    r = route('component_configuration', 'get_configuration_schema')
    assert not r.reads_key('usecase_id') and not r.decorator_reads_usecase


def test_device_logs_require_query_usecase_id():
    for spec in (s for s in ROUTED if s.handler_module == 'device_logs'):
        assert (h.QUERY, 'usecase_id') in spec_route(spec).required, spec.name
        assert spec.param_map.get('usecase_id') == 'usecase_id', spec.name


def test_submit_labeler_task_reads_annotation():
    assert route('dda_labeling', 'submit_labeler_task').reads_key('annotation')


INJECTED = [s for s in cat.CATALOG if s.usecase_not_forwarded == 'injected']


def test_setdefault_scoped_specs_keep_usecase_id_rbac_only():
    assert len(INJECTED) == 10
    for spec in INJECTED:
        assert spec.handler_module in ('labeling', 'dda_labeling'), spec.name
        assert 'usecase_id' not in spec.param_map, spec.name
        assert spec_route(spec).decorator_reads_usecase, spec.name


@pytest.mark.parametrize('module,function,kind', [
    ('devices', 'list_devices', h.QUERY), ('devices', 'get_device', h.QUERY),
    ('deployments', 'list_deployments', h.QUERY),
    ('shared_components', 'list_shared_components', h.QUERY),
    ('datasets', 'list_datasets', h.QUERY), ('datasets', 'get_image_preview', h.QUERY),
    ('pre_labeled_datasets', 'list_datasets', h.QUERY),
    ('workflow_validation', 'get_node_catalog', h.QUERY),
    ('user_management', 'list_users', h.QUERY),
    ('datasets', 'count_images', h.BODY),
    ('pre_labeled_datasets', 'validate_manifest', h.BODY),
    ('workflow_generator', 'submit_generation', h.BODY),
])
def test_usecase_id_rows_read_it(module, function, kind):
    assert (kind, 'usecase_id') in route(module, function).reads
    specs = [s for s in ROUTED if (s.handler_module, s.handler_function) == (module, function)]
    assert specs
    for spec in specs:
        assert spec.param_map.get('usecase_id') == 'usecase_id', spec.name


def test_delete_usecase_takes_its_id_from_the_path():
    spec = BY_NAME['portal_admin_usecase_delete']
    assert spec.param_map['usecase_id'] == 'id' and spec.location_of('usecase_id') == 'path'


def test_entry_point_reads_the_path_argument_and_passes_it_positionally():
    # usecases.handler -> get_usecase(path_parameters['id'], user)
    spec = BY_NAME['portal_admin_usecase_get']
    assert spec.location_of('usecase_id') == 'path' and spec.param_map['usecase_id'] == 'id'
    entry = route('usecases', 'handler')
    assert (h.PATH, 'id') in entry.reads


@pytest.mark.parametrize('module,function,limit,offset', [
    ('audit_logs', 'get_audit_logs', True, False),
    ('build_jobs', 'list_builds', True, False),
    ('build_jobs', 'get_build_logs', True, False),
    ('datasets', 'get_image_preview', True, True),
    ('captures', 'list_captures', True, False),
])
def test_route_paginated_rows(module, function, limit, offset):
    r = route(module, function)
    assert r.reads_key('limit') == limit and r.reads_key('offset') == offset
    for spec in (s for s in ROUTED if (s.handler_module, s.handler_function) == (module, function)):
        assert spec.param_map.get('limit') == 'limit', spec.name
        if offset:
            assert spec.param_map.get('offset') == 'offset', spec.name


def test_build_logs_with_a_dispatcher_sliced_limit_fails():
    # The route's default page is 200 events, the catalog default 50.
    spec = BY_NAME['portal_workflows_build_logs_get']
    param_map = {k: v for k, v in spec.param_map.items() if k != 'limit'}
    variant = dataclasses.replace(spec, param_map=param_map)
    problems = argument_problems(variant)
    assert any('limit has no param_map entry' in p for p in problems)


@pytest.mark.parametrize('module,function,keys', [
    # body.get(deployment_preflight.ACKNOWLEDGEMENT_FIELD)
    ('deployments', 'create_deployment', {'acknowledged_retained_components'}),
    # tuning_settings.SAMPLE_EXPORT_FIELD / SAMPLE_RETENTION_FIELD, iterated pairs
    ('usecases', 'create_usecase', {'tuning_sample_export', 'tuning_sample_retention_days'}),
    # the local `updatable_fields` list
    ('usecases', 'update_usecase', {'name', 'description', 'region', 'owner',
                                    'default_device_group', 'data_s3_bucket',
                                    'tuning_sample_export', 'tuning_sample_retention_days'}),
    # PATCHABLE_FIELDS
    ('synthetic_data', 'patch_session', {'object_type', 'defect_type', 'source_images'}),
])
def test_constant_reads_are_folded(module, function, keys):
    assert keys <= route(module, function).keys


def test_constant_names_resolve():
    uni = h.universe()
    assert uni.module('deployment_preflight').constants['ACKNOWLEDGEMENT_FIELD'] == \
        'acknowledged_retained_components'
    assert uni.module('tuning_settings').constants['SAMPLE_EXPORT_FIELD'] == 'tuning_sample_export'


def test_labeling_job_create_follows_the_dda_delegation():
    r = route('labeling', 'create_labeling_job')
    assert h.FuncRef('dda_labeling', 'create_dda_job') in r.graph
    # read only on the DDA branch
    dda = route('dda_labeling', 'create_dda_job')
    assert dda.keys - route('labeling', 'create_labeling_job').keys == set()


@pytest.mark.parametrize('name', ['portal_admin_usecases_list', 'portal_devices_list',
                                  'portal_devices_deployments_list'])
def test_dispatcher_sliced_rows_offer_offset(name):
    spec = BY_NAME[name]
    assert 'offset' in spec.input_schema['properties'] and 'offset' not in spec.param_map
    assert 'limit' not in spec.param_map


def test_usecase_not_forwarded_cases():
    audit = BY_NAME['portal_admin_audit_logs_query']
    assert audit.usecase_not_forwarded == 'dead_filter'
    assert 'usecase_id' not in audit.input_schema['properties']
    assert route('audit_logs', 'get_audit_logs').reads_key('usecase_id')
    retrain = BY_NAME['portal_data_synthetic_session_retrain']
    assert retrain.usecase_not_forwarded == 'stored_record'
    r = route('synthetic_data', 'retrain_session')
    assert h.FuncRef('training', 'create_training_job') in r.graph
    assert r.reads_key('usecase_id')
    # the injected body keys do not count as required of the caller
    assert not any(key in ('usecase_id', 'dataset_manifest_s3') for _k, key in r.required)
    assert {'model_name', 'model_type', 'model_version'} <= {k for _x, k in r.required}


def test_stale_usecase_not_forwarded_fails():
    spec = BY_NAME['portal_devices_list']
    variant = dataclasses.replace(spec, usecase_not_forwarded='injected')
    assert any('stale' in p for p in argument_problems(variant))


def test_unread_argument_fails():
    spec = BY_NAME['portal_devices_logs_analyze']
    props = dict(spec.input_schema['properties'], question={'type': 'string'})
    variant = dataclasses.replace(spec, input_schema=dict(spec.input_schema, properties=props))
    assert any("'question' is not read" in p for p in argument_problems(variant))


def test_at_least_one_flags_group_is_an_any_of():
    # devices.update_device_flags: at least one of the flag fields.
    spec = BY_NAME['portal_devices_flags_update']
    assert spec.input_schema.get('anyOf')
