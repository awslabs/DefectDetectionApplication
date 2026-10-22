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
"""Property 10: Edge role-gate decision.

**Feature: mcp-servers, Property 10: Edge role-gate decision**

Spec: .kiro/specs/mcp-servers/ (design.md Correctness Properties, Property 10;
§5 ``child/authz.py``, D13; tasks.md 11.6). **Validates: Requirements 10.2,
10.3, 10.4, 10.5, 10.6, 9.1, 16.3**

For any principal (source, role or none), any catalog spec, ``confirm``
absent / matching / mismatching, and the mutating switch, ``authz.decide``
allows read-only tools for everyone, allows a mutating tool iff the switch
is on, the source is ``local_session``, the role is Operator or above and
(for a destructive tool) ``confirm`` matches, and otherwise names the
specific reason.
"""
import re

from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_edge.child import authz
from mcp_edge.child.catalog import CATALOG
from mcp_edge.child.principal import Principal

SOURCES = ('local_session', 'existing_token_auth', 'anonymous')
ROLES = ('PortalAdmin', 'UseCaseAdmin', 'DataScientist', 'Operator', 'Viewer',
         'DataLabeler', None)
MUTATING_ROLES = {'Operator', 'UseCaseAdmin', 'PortalAdmin'}
_INDEXED = re.compile(r'^([a-z_][a-z0-9_]*)\[(\d+)\]$')


@st.composite
def calls(draw):
    spec = draw(st.sampled_from(CATALOG))
    args, expected = {}, None
    if spec.destructive:
        if spec.confirm_literal is not None:
            expected = spec.confirm_literal
        else:
            targets = spec.confirm_target
            target = draw(st.sampled_from(targets)) if isinstance(targets, tuple) else targets
            value = draw(st.from_regex(r'[A-Za-z0-9_-]{1,24}', fullmatch=True))
            match = _INDEXED.match(target)
            if match:
                args[match.group(1)] = [value]
            else:
                args[target] = value
            expected = value
    confirm = draw(st.sampled_from(('absent', 'match', 'mismatch')))
    if confirm == 'match' and expected is not None:
        args['confirm'] = expected
    elif confirm == 'mismatch':
        args['confirm'] = (expected or '') + '-other'
    principal = Principal(username=draw(st.sampled_from(('op1', 'anonymous', 'svc'))),
                          role=draw(st.sampled_from(ROLES)),
                          source=draw(st.sampled_from(SOURCES)))
    return spec, args, principal, draw(st.booleans()), expected


@settings(max_examples=100, deadline=None)
@given(calls())
def test_edge_role_gate_decision(call):
    spec, args, principal, mutating_enabled, expected = call
    decision = authz.decide(spec, principal, mutating_enabled, args)
    if spec.read_only:
        assert decision is authz.ALLOWED
        return
    if not mutating_enabled:
        assert decision.category == 'mutating_tools_disabled', spec.name
        return
    if principal.source != 'local_session' or principal.role not in MUTATING_ROLES:
        assert decision.category == 'not_authorized', (spec.name, principal)
        assert 'Operator role or higher' in decision.message
        return
    if spec.destructive and args.get('confirm') != expected:
        assert decision.category == 'confirmation_required', spec.name
        return
    assert decision is authz.ALLOWED, spec.name


def test_fixed_cases():
    by_name = {s.name: s for s in CATALOG}
    operator = Principal('op1', 'Operator', 'local_session')
    snapshot = by_name['edge_system_snapshot_create']
    assert authz.decide(snapshot, operator, True, {'confirm': 'snapshot'}) is authz.ALLOWED
    assert authz.decide(snapshot, operator, True, {}).category == 'confirmation_required'
    assert authz.decide(snapshot, operator, False, {'confirm': 'snapshot'}).category == \
        'mutating_tools_disabled'
    anonymous = Principal('anonymous', None, 'anonymous')
    assert authz.decide(snapshot, anonymous, True, {'confirm': 'snapshot'}).category == \
        'not_authorized'
    # A token carrying a role outside the local session grants nothing.
    cloud = Principal('svc', 'PortalAdmin', 'existing_token_auth')
    assert authz.decide(snapshot, cloud, True, {'confirm': 'snapshot'}).category == \
        'not_authorized'
    delete = by_name['edge_workflow_delete']
    assert authz.decide(delete, operator, True, {'workflow_id': 'w1', 'confirm': 'w1'}) \
        is authz.ALLOWED
    assert authz.decide(delete, operator, True, {'workflow_id': 'w1', 'confirm': 'w2'}) \
        .category == 'confirmation_required'
    viewer = Principal('v', 'Viewer', 'local_session')
    for spec in CATALOG:
        if spec.read_only:
            assert authz.decide(spec, anonymous, False, {}) is authz.ALLOWED
        else:
            assert authz.decide(spec, viewer, True, {}).category == 'not_authorized'
