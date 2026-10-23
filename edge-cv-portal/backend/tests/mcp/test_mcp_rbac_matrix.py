"""RBAC matrix of the Portal_MCP_Server: every catalog tool x six roles x
`PORTAL_REGISTRY_ENFORCED` on/off, through the real `on_call_tool`.

Spec: .kiro/specs/mcp-servers/ (design.md §2 `authz.py` row, §4 step 5,
D5, D29; Testing Strategy row "16.3 authorization", review-3 M2; tasks.md
7.4). Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 16.3.

Registry seeding (review-3 M2), one principal per role and mode, the way
the portal provisions it:

* enforcement on: a `global` row `{role: R, status: 'enabled'}` for every
  role (the User Manager's provisioning record) plus, for UseCaseAdmin,
  DataScientist, Operator and Viewer, a Use_Case row `{role: R}` for the
  test Use_Case;
* enforcement off: a `global` PortalAdmin row for PortalAdmin, a Use_Case
  row for UseCaseAdmin, DataScientist, Operator and Viewer, and a `global`
  DataLabeler row for DataLabeler (which the legacy order ignores).

The expected decision per (tool, role, mode) comes from the oracle
Property 4 uses, `rbac_manager.has_permission` over the seeded moto table,
and is cross-checked against the role mapping written in the catalog
(each row's permissions, through `rbac_manager.role_permissions`, under the
seeding above), so a seeding or catalog mistake surfaces as a disagreement
rather than a silently adjusted expectation. An allowed call reaches the
fake Lambda once (whoami answers locally); a denied call is
`not_authorized` with exactly one marked `unauthorized_access` entry and
nothing dispatched. Also: registry outage -> `authorization_unavailable`
under enforcement on, whoami for every role, the
`documented-limitation-D29` denials, and the usability guard. All phases
are delivered and the mutating switch is on, so every row is reachable.
"""
import sys
import uuid

import pytest
from botocore.exceptions import ClientError

import _portal_harness as h
from _schema_strategy import minimal_args
from mcp_portal import authz, server
from mcp_portal.catalog import CATALOG
from mcp_portal.principal import principal_from_access_token

ROLES = ('PortalAdmin', 'UseCaseAdmin', 'DataScientist', 'Operator', 'Viewer',
         'DataLabeler')
USECASE_ROLES = ('UseCaseAdmin', 'DataScientist', 'Operator', 'Viewer')
MODES = (True, False)
USECASE = 'uc-rbac-matrix'
WHOAMI = 'portal_admin_whoami'
PORTAL_ADMIN_ONLY = ('manage_users', 'system_admin', 'create_usecases')
BUILD_PERMISSIONS = {'builds:read', 'builds:submit', 'builds:cancel'}
# The Claimed_Role of every MCP principal: an access token carries no
# `custom:role` (principal.py; D29 option (A)).
CLAIMED_ROLE = 'Viewer'

_SUBS = {}


def mode_id(enforced):
    return 'enforced' if enforced else 'legacy'


# --------------------------------------------------------------- seeding

def seeded_sub(stack, role, enforced):
    """The principal of (role, mode), its registry rows written once."""
    key = (role, enforced)
    if key in _SUBS:
        return _SUBS[key]
    sub = f'rbac-{role}-{mode_id(enforced)}-{uuid.uuid4()}'
    table = stack.tables.user_roles
    if enforced:
        table.put_item(Item={'user_id': sub, 'usecase_id': 'global', 'role': role,
                             'status': 'enabled'})
        if role in USECASE_ROLES:
            table.put_item(Item={'user_id': sub, 'usecase_id': USECASE, 'role': role})
    elif role in USECASE_ROLES:
        table.put_item(Item={'user_id': sub, 'usecase_id': USECASE, 'role': role})
    else:  # PortalAdmin, DataLabeler: a global row
        table.put_item(Item={'user_id': sub, 'usecase_id': 'global', 'role': role})
    _SUBS[key] = sub
    return sub


# ------------------------------------------------------------- arguments

def usecase_for(spec):
    """The Use_Case argument of the call: the test Use_Case for a
    Use_Case-scoped tool, none for a global or usecase_or_global one (so the
    latter is decided at `'global'`, where D29 applies)."""
    if spec.scope == 'usecase' and 'usecase_id' in spec.input_schema.get('properties', {}):
        return USECASE
    return None


def call_args(spec):
    usecase = usecase_for(spec)
    args = minimal_args(spec, **({'usecase_id': USECASE} if usecase else {}))
    if spec.destructive:
        args['confirm'] = server.expected_confirm(spec, args)
    return args


# --------------------------------------------- expectations (two sources)

def oracle_allows(shared, sub, spec, scope, user) -> bool:
    if not spec.permissions:
        return True
    return any(shared.rbac_manager.has_permission(sub, scope, shared.Permission(p),
                                                  user_info=user)
               for p in spec.permissions)


def modelled_role(role, enforced, scope) -> str:
    """The Effective_Role the seeding above should give."""
    if enforced:
        return role
    if role == 'PortalAdmin':
        return 'PortalAdmin'
    if role in USECASE_ROLES and scope == USECASE:
        return role
    return CLAIMED_ROLE


def role_permissions(shared, role) -> set:
    return {p.value for p in shared.rbac_manager.role_permissions[shared.Role(role)]}


def modelled_allows(shared, spec, role, enforced) -> bool:
    if not spec.permissions:
        return True
    scope = authz.scope_for(spec, usecase_for(spec))
    return bool(set(spec.permissions) & role_permissions(
        shared, modelled_role(role, enforced, scope)))


# ---------------------------------------------------------------- harness

class AuditRecorder:
    """The boto3 resource of `shared_utils` with the audit table's `put_item`
    recorded (the Item reaches the moto table unchanged)."""

    def __init__(self, real, audit_table):
        self.real, self.audit_table, self.items = real, audit_table, []

    def Table(self, name):  # noqa: N802 - boto3 resource API
        table = self.real.Table(name)
        if name != self.audit_table:
            return table
        recorder = self

        class _Table:
            def put_item(self, **kwargs):
                recorder.items.append(kwargs['Item'])
                return table.put_item(**kwargs)

            def __getattr__(self, attr):
                return getattr(table, attr)

        return _Table()


@pytest.fixture
def matrix(aws_stack, monkeypatch):
    shared = sys.modules['shared_utils']
    fake = h.install(monkeypatch, phases=h.ALL_PHASES, mutating=True)
    recorder = AuditRecorder(shared.dynamodb, shared.AUDIT_LOG_TABLE)
    monkeypatch.setattr(shared, 'dynamodb', recorder)
    monkeypatch.delenv('_X_AMZN_TRACE_ID', raising=False)
    yield shared, aws_stack, fake, recorder, monkeypatch
    # The audit table is shared by the whole session and later suites scan
    # it per example: take this suite's ~2,400 entries out again.
    table = recorder.real.Table(recorder.audit_table)
    for item in recorder.items:
        table.delete_item(Key={'event_id': item['event_id'], 'timestamp': item['timestamp']})


def run(spec, sub, args):
    return h.in_memory(spec.domain, lambda client: client.call_tool(spec.name, args),
                       h.access_token(sub))


def marked(items, action, tool):
    return [i for i in items if i.get('action') == action and i.get('mcp_tool') == tool]


# ----------------------------------------------------------------- matrix

@pytest.mark.parametrize('enforced', MODES, ids=mode_id)
@pytest.mark.parametrize('role', ROLES)
@pytest.mark.parametrize('spec', CATALOG, ids=lambda s: s.name)
def test_rbac_matrix(matrix, spec, role, enforced):
    shared, stack, fake, recorder, monkeypatch = matrix
    monkeypatch.setenv('PORTAL_REGISTRY_ENFORCED', 'true' if enforced else 'false')
    sub = seeded_sub(stack, role, enforced)
    token = h.access_token(sub)
    user = principal_from_access_token(token).user
    scope = authz.scope_for(spec, usecase_for(spec))

    expected = oracle_allows(shared, sub, spec, scope, user)
    assert expected is modelled_allows(shared, spec, role, enforced), (
        f'resolver and catalog role mapping disagree for {spec.name} / {role} / '
        f'{mode_id(enforced)} at scope {scope}: a seeding or catalog mistake')

    result = run(spec, sub, call_args(spec))
    content = result.structured_content or {}
    denials = marked(recorder.items, authz.UNAUTHORIZED_ACTION, spec.name)
    if expected:
        assert content.get('category') != 'not_authorized', content
        assert denials == []
        assert len(fake.calls) == (0 if spec.backend is None else 1), spec.name
        if spec.backend is not None:
            assert fake.events[0]['mcp']['tool'] == spec.name
    else:
        assert result.is_error is True
        assert content['category'] == 'not_authorized' and content['status'] == 403
        assert fake.calls == []
        assert len(denials) == 1, recorder.items
        [entry] = denials
        assert entry['user_id'] == sub
        assert entry['result'] == 'denied'
        assert entry['channel'] == 'mcp'
        assert entry['details']['mcp_tool'] == spec.name
        assert entry['details']['required_permissions'] == list(spec.permissions)
        assert entry['details']['usecase_id'] == scope
        # Only the denial: no envelope, no other entry for this call.
        assert len(recorder.items) == 1


# ---------------------------------------------------------------- whoami

@pytest.mark.parametrize('enforced', MODES, ids=mode_id)
@pytest.mark.parametrize('role', ROLES)
def test_whoami_for_every_role(matrix, role, enforced):
    shared, stack, fake, _, monkeypatch = matrix
    monkeypatch.setenv('PORTAL_REGISTRY_ENFORCED', 'true' if enforced else 'false')
    sub = seeded_sub(stack, role, enforced)
    spec = next(s for s in CATALOG if s.name == WHOAMI)
    result = run(spec, sub, {})
    assert result.is_error is False, result.structured_content
    body = result.structured_content['result']
    assert body['user_id'] == sub
    assert body['email'] == h.EMAIL
    assert body['global_role'] == modelled_role(role, enforced, 'global')
    assert fake.calls == []


# --------------------------------------------------------- registry outage

class _FailingRolesTable:
    def get_item(self, **_kwargs):
        raise ClientError({'Error': {'Code': 'ProvisionedThroughputExceededException',
                                     'Message': 'registry read failed'}}, 'GetItem')

    def query(self, **_kwargs):
        raise ClientError({'Error': {'Code': 'ProvisionedThroughputExceededException',
                                     'Message': 'registry read failed'}}, 'Query')


@pytest.mark.parametrize('spec', CATALOG, ids=lambda s: s.name)
def test_registry_outage_is_authorization_unavailable(matrix, spec):
    shared, stack, fake, recorder, monkeypatch = matrix
    monkeypatch.setenv('PORTAL_REGISTRY_ENFORCED', 'true')
    sub = seeded_sub(stack, 'PortalAdmin', True)
    roles_table = shared.USER_ROLES_TABLE

    class _Outage(AuditRecorder):
        def Table(self, name):  # noqa: N802 - boto3 resource API
            if name == roles_table:
                return _FailingRolesTable()
            return super().Table(name)

    outage = _Outage(recorder.real, recorder.audit_table)
    outage.items = recorder.items
    monkeypatch.setattr(shared, 'dynamodb', outage)
    result = run(spec, sub, call_args(spec))
    assert result.is_error is True
    assert result.structured_content['category'] == 'authorization_unavailable'
    assert fake.calls == []
    if spec.permissions:
        [entry] = marked(outage.items, authz.AUTHORIZATION_UNAVAILABLE_ACTION, spec.name)
        assert entry['result'] == 'failure' and entry['channel'] == 'mcp'
    else:  # whoami: the envelope of the failed call (Error Handling row)
        [entry] = marked(outage.items, 'mcp_tool_call', spec.name)
        assert entry['details']['error_category'] == 'authorization_unavailable'
    assert len(outage.items) == 1


# ------------------------------------------ documented-limitation-D29

def test_documented_limitation_D29_enforcement_off(matrix):  # noqa: N802
    """documented-limitation-D29: with enforcement off, DataLabeler is
    denied the labeler tools (its global row is ignored, the Claimed_Role
    Viewer decides) and DataScientist / UseCaseAdmin are denied the global
    build tools (their Use_Case rows do not reach scope 'global')."""
    shared, stack, fake, recorder, monkeypatch = matrix
    monkeypatch.setenv('PORTAL_REGISTRY_ENFORCED', 'false')
    labeler_tools = [s for s in CATALOG if 'labeling:tasks-self' in s.permissions]
    build_tools = [s for s in CATALOG if s.scope != 'usecase'
                   and set(s.permissions) & BUILD_PERMISSIONS]
    assert labeler_tools and build_tools
    cases = [(s, 'DataLabeler') for s in labeler_tools] + \
        [(s, r) for s in build_tools for r in ('DataScientist', 'UseCaseAdmin')]
    for spec, role in cases:
        result = run(spec, seeded_sub(stack, role, False), call_args(spec))
        assert result.structured_content['category'] == 'not_authorized', (spec.name, role)
    assert fake.calls == []
    # The same principals under enforcement on hold what their rows grant.
    monkeypatch.setenv('PORTAL_REGISTRY_ENFORCED', 'true')
    for spec in labeler_tools:
        result = run(spec, seeded_sub(stack, 'DataLabeler', True), call_args(spec))
        assert result.structured_content.get('category') != 'not_authorized', spec.name


# ------------------------------------------------------- usability guard

def test_usability_guard(matrix):
    shared = matrix[0]
    phase_one = [s for s in CATALOG if s.phase == 1 and s.permissions]
    for spec in phase_one:
        if spec.scope in ('global', 'usecase_or_global'):
            for enforced in MODES:
                assert modelled_allows(shared, spec, 'PortalAdmin', enforced), (
                    spec.name, mode_id(enforced))
    others = [r for r in ROLES if r != 'PortalAdmin']
    unusable = []
    for spec in CATALOG:
        if not spec.permissions or set(spec.permissions) <= set(PORTAL_ADMIN_ONLY):
            continue
        held = any(set(spec.permissions) & role_permissions(shared, r) for r in others)
        if held and not any(modelled_allows(shared, spec, r, True) for r in others):
            unusable.append(spec.name)
    assert unusable == []
    # A tool no role can use in either mode.
    nobody = [s.name for s in CATALOG if s.permissions and not any(
        modelled_allows(shared, s, r, e) for r in ROLES for e in MODES)]
    assert nobody == []
