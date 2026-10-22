"""Property test: the naming and identifier convention of both servers.
**Feature: mcp-servers, Property 15: Naming and identifier convention**
Spec: .kiro/specs/mcp-servers/ (design.md D22, Requirement 14, Correctness
Property 15; tasks.md 16.1).
Validates: Requirements 14.1, 14.3.

The edge catalog (`src/backend/mcp_edge/child/catalog.py`) is loaded by path
next to the portal `CATALOG`; it has no I/O and imports only the
`mcp_edge` package constants, loaded by path too. For any pair of tools, one
per server, that share a concept argument (`workflow_id`, `registration_id`,
`execution_id`, `device_id`, `usecase_id`), the argument name is the same
and its schema type is identical. Names follow D22: `portal_<domain>_...`
and `edge_...`, lowercase `[a-z0-9_]`, at most 64 characters.
"""
import importlib.util
import os
import re
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_portal import VIEWS
from mcp_portal import catalog as portal_catalog

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
EDGE_PACKAGE = os.path.join(REPO, 'src', 'backend', 'mcp_edge')
CONCEPTS = ('workflow_id', 'registration_id', 'execution_id', 'device_id', 'usecase_id')
NAME_RE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _edge_catalog():
    if 'mcp_edge' not in sys.modules:
        sys.modules['mcp_edge'] = _load('mcp_edge', os.path.join(EDGE_PACKAGE, '__init__.py'))
    return _load('mcp_edge_child_catalog_by_path',
                 os.path.join(EDGE_PACKAGE, 'child', 'catalog.py'))


EDGE = _edge_catalog().CATALOG
PORTAL = portal_catalog.CATALOG


def _concepts(spec):
    props = spec.input_schema.get('properties', {})
    return {name: props[name] for name in CONCEPTS if name in props}


PAIRS = [(p, e, c) for p in PORTAL for e in EDGE
         for c in set(_concepts(p)) & set(_concepts(e))]


def _type(prop):
    return prop.get('type'), prop.get('items', {}).get('type')


def test_there_are_shared_concept_arguments():
    assert PAIRS
    assert {c for _p, _e, c in PAIRS} >= {'workflow_id'}


@settings(max_examples=100, deadline=None)
@given(pair=st.sampled_from(PAIRS))
def test_shared_concept_arguments_match(pair):
    portal_spec, edge_spec, concept = pair
    portal_prop = _concepts(portal_spec)[concept]
    edge_prop = _concepts(edge_spec)[concept]
    assert _type(portal_prop) == _type(edge_prop) == ('string', None), (
        portal_spec.name, edge_spec.name, concept)


@settings(max_examples=100, deadline=None)
@given(spec=st.sampled_from(list(PORTAL) + list(EDGE)))
def test_tool_names_follow_the_convention(spec):
    assert NAME_RE.fullmatch(spec.name)
    if spec in PORTAL:
        assert spec.name.startswith(f'portal_{spec.domain}_') and spec.domain in VIEWS
    else:
        assert spec.name.startswith('edge_')
    # No camelCase spelling of a concept argument on either server.
    for concept in CONCEPTS:
        camel = re.sub(r'_(.)', lambda m: m.group(1).upper(), concept)
        assert camel not in spec.input_schema.get('properties', {}), (spec.name, camel)
