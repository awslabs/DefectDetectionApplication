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
"""Confirmation and mutating-switch guardrails of the Edge MCP child.

Spec: .kiro/specs/mcp-servers/ (design.md §7 step 2, D9, D13; Testing
Strategy row "16.5 confirmation/switch"; tasks.md 11.11). Requirements 10.7,
16.5.

Every ``M!`` tool refuses a missing or mismatching ``confirm`` before any
Backend_API call and runs with the matching one; with the switch off every
``M`` tool is absent from ``tools/list`` and refused. All phases are
delivered here so the Phase-2/3 rows are reachable.
"""
import pytest

import _child_harness as h
from mcp_edge.child import dispatch
from mcp_edge.child.catalog import CATALOG

DESTRUCTIVE = [s for s in CATALOG if s.destructive]
MUTATING = [s for s in CATALOG if not s.read_only]
ON = h.options(delivered_phases=h.ALL_PHASES, mutating_enabled=True)
OFF = h.options(delivered_phases=h.ALL_PHASES, mutating_enabled=False)


@pytest.fixture
def backend():
    fake = h.FakeBackend(routes={'*': h.json_answer({'ok': True})}).install()
    yield fake
    dispatch.set_client(None)


def call(name, args, opts):
    return h.in_memory(lambda client: client.call_tool(name, args), opts)


@pytest.mark.parametrize('spec', DESTRUCTIVE, ids=lambda s: s.name)
def test_destructive_tool_requires_the_matching_confirm(backend, spec):
    args = h.minimal_args(spec)
    expected = h.confirm_for(spec, args)
    assert expected, spec.name
    missing = call(spec.name, args, ON)
    assert missing.structured_content['category'] == 'confirmation_required'
    wrong = call(spec.name, dict(args, confirm=f'{expected}-other'), ON)
    assert wrong.structured_content['category'] == 'confirmation_required'
    assert backend.requests == []
    allowed = call(spec.name, dict(args, confirm=expected), ON)
    assert not allowed.is_error, allowed.structured_content
    assert len(backend.requests) == 1


def test_switch_off_lists_no_mutating_tool(backend):
    listed = {t.name for t in h.in_memory(lambda c: c.list_tools(), OFF).tools}
    assert not listed & {s.name for s in MUTATING}
    assert {s.name for s in CATALOG if s.read_only and s.feature is None} <= listed
    on = {t.name for t in h.in_memory(lambda c: c.list_tools(), ON).tools}
    assert {s.name for s in MUTATING if s.feature is None} <= on


@pytest.mark.parametrize('spec', MUTATING, ids=lambda s: s.name)
def test_switch_off_refuses_every_mutating_tool(backend, spec):
    args = h.minimal_args(spec)
    if spec.destructive:
        args['confirm'] = h.confirm_for(spec, args)
    result = call(spec.name, args, OFF)
    assert result.structured_content['category'] == 'mutating_tools_disabled'
    assert backend.requests == []


@pytest.mark.parametrize('principal', [h.VIEWER, h.ANONYMOUS,
                                       {'username': 'svc', 'role': 'PortalAdmin',
                                        'source': 'existing_token_auth'}])
def test_only_a_local_operator_may_mutate(backend, principal):
    backend.principal = principal
    spec = next(s for s in MUTATING if not s.destructive and s.feature is None)
    result = call(spec.name, h.minimal_args(spec), ON)
    assert result.structured_content['category'] == 'not_authorized'
    assert backend.requests == []
