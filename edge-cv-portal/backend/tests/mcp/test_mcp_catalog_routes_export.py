"""Catalog routes export and handler dispatch (`test_mcp_catalog_routes_export.py`).

Spec: .kiro/specs/mcp-servers/ (design.md §2 `catalog.py` row,
`catalog_routes()` and `--export-routes`; D32 `status_tool`; Testing
Strategy row "Catalog routes are registered Portal_API methods", the pytest
half and the handler-dispatch half; tasks.md 5.6). Requirements 1.1, 1.3, 3.1.

* `catalog_routes()` and `catalog_tools()` equal the checked-in
  `mcp_portal/catalog_routes.json` / `catalog_tools.json` byte for byte, so a
  catalog change without `python -m mcp_portal.catalog --export-routes`
  fails here (the jest half pins the same routes file to the synthesized
  API Gateway methods).
* Registration is not implementation: for every spec with a backing route,
  the AST walk of `_handler_ast.DispatchWalker` from the backend module's
  entry point (`handler`/`lambda_handler`, through a forwarding shim such as
  `camera_video_pin.handler` -> `camera_registry.handler`) reaches
  `handler_function` on a branch whose guard compares the request method
  with the spec's `method` and matches its `resource`, or through a
  `(method, resource)` route-table key; where one `(method, resource)`
  reaches several functions through a request-key discriminator, the
  spec's required arguments select its own.
"""
import dataclasses
import json
import re

import pytest

import _handler_ast as h
from mcp_portal import catalog as cat
from mcp_portal.catalog import SERVER_CONSUMED_ARGS

ROUTED = [s for s in cat.CATALOG if s.backend is not None]
BY_NAME = {s.name: s for s in cat.CATALOG}


def test_routes_file_pins_the_catalog_byte_for_byte():
    with open(cat.ROUTES_FILE, encoding='utf-8') as handle:
        text = handle.read()
    assert text == cat.dumps_export(cat.catalog_routes())
    assert json.loads(text) == cat.catalog_routes()


def test_tools_file_pins_the_catalog_byte_for_byte():
    with open(cat.TOOLS_FILE, encoding='utf-8') as handle:
        text = handle.read()
    assert text == cat.dumps_export(cat.catalog_tools())
    tools = json.loads(text)
    assert [t['name'] for t in tools] == sorted(s.name for s in cat.CATALOG)
    for entry in tools:
        spec = BY_NAME[entry['name']]
        assert entry['status_tool'] == spec.status_tool
        assert (entry['status_tool'] is None) == spec.read_only


def test_routes_are_sorted_unique_triples_of_routed_specs():
    routes = cat.catalog_routes()
    triples = [(r['method'], r['resource'], r['backend']) for r in routes]
    assert len(triples) == len(set(triples))
    assert sorted(triples, key=lambda t: (t[1], t[0], t[2])) == triples
    assert set(triples) == {(s.method, s.resource, s.backend) for s in ROUTED}
    assert all(set(r) == {'method', 'resource', 'backend'} for r in routes)


def test_local_spec_contributes_no_route():
    whoami = BY_NAME['portal_admin_whoami']
    assert whoami.backend is None
    assert not any(r['resource'] is None for r in cat.catalog_routes())


# ------------------------------------------------------ handler dispatch

def _snake(name: str) -> str:
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


def entry_module(spec) -> str:
    """The backend module whose entry point the route invokes: the spec's
    own module, or the forwarding module its backend names
    (`cameraVideoPin` -> `camera_video_pin`)."""
    uni = h.universe()
    forwarding = _snake(spec.backend)
    if forwarding != spec.handler_module and forwarding in uni.modules \
            and not uni.modules[forwarding].layer:
        return forwarding
    return spec.handler_module


_SITES = {}


def sites(module: str):
    if module not in _SITES:
        _SITES[module] = h.DispatchWalker(h.universe()).sites(module)
    return _SITES[module]


def dispatch_problem(spec):
    uni = h.universe()
    props = spec.input_schema.get('properties', {})
    sent = {a: spec.param_map.get(a, a) for a in props
            if not (a in SERVER_CONSUMED_ARGS and a not in spec.param_map)}
    required = set(spec.input_schema.get('required', ()))
    branches = [set(b.get('required', ())) for g in ('oneOf', 'anyOf')
                for b in spec.input_schema.get(g, ())]

    def requires(keys):
        args = [a for a, key in sent.items() if key in keys]
        return any(a in required for a in args) or bool(branches) and all(
            any(a in b for a in args) for b in branches)

    target = uni.find_function(spec.handler_module, spec.handler_function)
    if target is None:
        return f'{spec.name}: {spec.handler_module}.{spec.handler_function} does not exist'
    module = entry_module(spec)
    if h.DispatchWalker(uni).entry(module) is None:
        return f'{spec.name}: {module} has no entry point'
    problem = h.dispatch_problem(uni, spec.method, spec.resource, target, sites(module),
                                 set(sent.values()), requires)
    return f'{spec.name}: {problem}' if problem else None


@pytest.mark.parametrize('spec', ROUTED, ids=lambda s: s.name)
def test_entry_point_reaches_handler_function_on_its_route(spec):
    assert dispatch_problem(spec) is None


def test_handler_function_is_never_a_dispatching_entry_point():
    for spec in ROUTED:
        if spec.handler_function in h.ENTRY_POINTS:
            # 'handler' only when the route is an inline branch of it
            assert spec.handler_module == entry_module(spec), spec.name


def test_wrong_method_or_resource_fails():
    spec = BY_NAME['portal_devices_get']
    assert dispatch_problem(dataclasses.replace(spec, method='DELETE'))
    assert dispatch_problem(dataclasses.replace(spec, resource='/devices'))


def test_static_video_specs_go_through_the_forwarding_shim():
    video = [s for s in ROUTED if s.backend == 'cameraVideoPin']
    assert video
    for spec in video:
        assert spec.handler_module == 'camera_registry'
        assert entry_module(spec) == 'camera_video_pin'
        assert dispatch_problem(spec) is None
    reached = {s.target for s in sites('camera_video_pin')}
    assert h.FuncRef('camera_registry', 'handler') in reached


def test_deployments_discriminator_selects_each_spec():
    target = BY_NAME['portal_devices_deployment_for_target_get']
    listing = BY_NAME['portal_devices_deployments_list']
    assert (target.method, target.resource) == (listing.method, listing.resource)
    assert dispatch_problem(target) is None and dispatch_problem(listing) is None
    # without a required target argument the discriminated branch is not selected
    loose = dict(target.input_schema)
    loose.pop('oneOf', None)
    loose.pop('anyOf', None)
    loose['required'] = [r for r in loose.get('required', ())
                         if r not in ('target_device', 'target_thing_group')]
    problem = dispatch_problem(dataclasses.replace(target, input_schema=loose))
    assert problem and 'does not require' in problem
    # a listing that sends a target key would select the other branch
    props = dict(listing.input_schema['properties'],
                 target_device={'type': 'string'})
    variant = dataclasses.replace(listing, input_schema=dict(listing.input_schema,
                                                             properties=props))
    assert dispatch_problem(variant)


def test_component_create_and_discover_are_not_implemented_and_absent():
    # Registered POST /components and /components/discover; the handler
    # compares the method only with GET and DELETE (Backlog 21).
    uni = h.universe()
    methods = set()
    for site in sites('components'):
        methods |= h.guard_facts(uni, site.guards)[0]
    assert methods and methods <= {'GET', 'DELETE', 'OPTIONS'}
    routes = {(r['method'], r['resource']) for r in cat.catalog_routes()}
    assert ('POST', '/components') not in routes
    assert ('POST', '/components/discover') not in routes


def test_account_lifecycle_routes_are_absent():
    # Not registered until Backlog 2.
    routes = {(r['method'], r['resource']) for r in cat.catalog_routes()}
    for absent in (('POST', '/admin/users'), ('POST', '/admin/users/{username}/disable'),
                   ('POST', '/admin/users/{username}/enable'),
                   ('DELETE', '/admin/users/{username}')):
        assert absent not in routes
