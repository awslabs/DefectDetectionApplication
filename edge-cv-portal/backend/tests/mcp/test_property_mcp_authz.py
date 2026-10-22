"""Property 4: Portal authorization decision.

**Feature: mcp-servers, Property 4: Portal authorization decision**

Spec: .kiro/specs/mcp-servers/ (design.md Correctness Properties, Property 4;
§2 `authz.py` row, D5, D29; Testing Strategy row "16.3 authorization";
tasks.md 6.2). **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7,
4.7, 16.3**

The moto session stack (`conftest.aws_stack`) holds the Portal_Identity
registry; the real `shared_utils` resolver under the configured
`PORTAL_REGISTRY_ENFORCED` mode is the oracle, and the web path is the real
`rbac_middleware.rbac_check` decorator over the same registry state and the
same token claims.
"""
import importlib
import sys
import uuid
from types import SimpleNamespace

import pytest
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mcp_portal import authz
from mcp_portal import catalog as cat
from mcp_portal.principal import Principal

SETTINGS = settings(max_examples=100, deadline=None,
                    suppress_health_check=[HealthCheck.function_scoped_fixture,
                                           HealthCheck.too_slow])

WHOAMI = {s.name for s in cat.CATALOG if s.backend is None}
SPECS = [s for s in cat.CATALOG if s.name not in WHOAMI]
ROLES = ('PortalAdmin', 'UseCaseAdmin', 'DataScientist', 'Operator', 'Viewer',
         'DataLabeler')
USECASE = 'uc-prop'

# A registry row: absent, or a role (possibly not a Role) with a status
# (absent = enabled for rows written before the registry existed).
rows = st.one_of(
    st.none(),
    st.fixed_dictionaries({
        'role': st.sampled_from(ROLES + ('NotARole',)),
        'status': st.sampled_from((None, 'enabled', 'Enabled ', 'disabled', 'paused')),
    }))


@pytest.fixture
def shared(aws_stack, monkeypatch):
    module = sys.modules['shared_utils']
    monkeypatch.setattr(module, '_REQUEST_AUDIT_CONTEXT', None)
    monkeypatch.delenv('PORTAL_REGISTRY_ENFORCED', raising=False)
    monkeypatch.delenv('_X_AMZN_TRACE_ID', raising=False)
    return SimpleNamespace(module=module, stack=aws_stack, monkeypatch=monkeypatch)


@pytest.fixture
def rbac_middleware(shared):
    import rbac_middleware as module
    if module.rbac_manager is not shared.module.rbac_manager:
        module = importlib.reload(module)
    return module


def _claims(sub):
    return {'sub': sub, 'iss': 'https://issuer.example', 'client_id': 'mcp-client',
            'token_use': 'access', 'scope': 'res/portal.tools', 'exp': 4102444800,
            'iat': 1, 'jti': 'j', 'username': f'user-{sub[:8]}'}


def _principal(sub):
    claims = _claims(sub)
    return Principal(sub=sub, username=claims['username'],
                     email='person@example.com', claims=claims)


def _mcp_event(spec, claims, usecase_id):
    return {
        'httpMethod': spec.method, 'resource': spec.resource, 'path': spec.resource,
        'queryStringParameters': {'usecase_id': usecase_id} if usecase_id else None,
        'requestContext': {'requestId': 'req-p4', 'authorizer': {'claims': claims},
                           'identity': {'sourceIp': '203.0.113.7', 'userAgent': 'kiro/1'}},
        'mcp': {'channel': 'mcp', 'tool': spec.name,
                'server': f'dda-portal-mcp/{spec.domain}@1.0.0', 'client': 'kiro/1',
                'request_id': 'req-p4', 'principal_email': 'person@example.com'},
    }


def _seed(stack, sub, scope, row):
    if row is None:
        return
    item = {'user_id': sub, 'usecase_id': scope, 'role': row['role']}
    if row['status'] is not None:
        item['status'] = row['status']
    stack.tables.user_roles.put_item(Item=item)


def _usecase_arg(spec, draw_none):
    if spec.scope == 'global':
        return None
    if spec.scope == 'usecase_or_global' and draw_none:
        return None
    return USECASE


def _oracle(shared_module, sub, scope, spec, user):
    return any(shared_module.rbac_manager.has_permission(
        sub, scope, shared_module.Permission(p), user_info=user)
        for p in spec.permissions)


def _web_allows(rbac_middleware, spec, claims, usecase_id) -> bool:
    """The REST path: `rbac_check` over a browser-shaped event with the same
    claims and no `mcp` block."""
    shared_module = sys.modules['shared_utils']

    @rbac_middleware.rbac_check([shared_module.Permission(p) for p in spec.permissions],
                                allow_global=authz.scope_for(spec, usecase_id) == 'global')
    def handler(event, context):
        return {'statusCode': 200, 'body': '{}'}

    event = {'httpMethod': spec.method, 'resource': spec.resource, 'path': spec.resource,
             'queryStringParameters': ({'usecase_id': usecase_id}
                                       if spec.scope != 'global' and usecase_id else None),
             'pathParameters': None,
             'requestContext': {'authorizer': {'claims': claims},
                                'identity': {'sourceIp': '198.51.100.4',
                                             'userAgent': 'Mozilla/5.0'}}}
    return handler(event, None)['statusCode'] == 200


def _marked(stack, sub, action):
    items = stack.tables.audit_log.scan(FilterExpression=Attr('user_id').eq(sub))['Items']
    return [i for i in items if i.get('action') == action and i.get('channel') == 'mcp']


def _deciding_role(shared_module, sub, scope, user):
    return shared_module.rbac_manager.get_user_role(sub, scope, user_info=user)


@SETTINGS
@given(spec=st.sampled_from(SPECS), global_row=rows, usecase_row=rows,
       enforced=st.booleans(), no_usecase=st.booleans())
def test_portal_authorization_decision(shared, rbac_middleware, spec, global_row,
                                       usecase_row, enforced, no_usecase):
    shared_module, stack = shared.module, shared.stack
    shared.monkeypatch.setenv('PORTAL_REGISTRY_ENFORCED', 'true' if enforced else 'false')
    sub = f'p4-{uuid.uuid4()}'
    _seed(stack, sub, 'global', global_row)
    _seed(stack, sub, USECASE, usecase_row)
    principal = _principal(sub)
    usecase_id = _usecase_arg(spec, no_usecase)
    scope = authz.scope_for(spec, usecase_id)

    result = authz.authorize(spec, principal, usecase_id,
                             _mcp_event(spec, principal.claims, usecase_id))

    # The real resolver under the configured mode is the oracle.
    expected = _oracle(shared_module, sub, scope, spec, principal.user)
    assert result.allowed is expected, (spec.name, global_row, usecase_row, enforced)
    assert result.scope == scope

    # A denial writes exactly one marked unauthorized_access entry.
    denials = _marked(stack, sub, 'unauthorized_access')
    if expected:
        assert denials == []
    else:
        assert len(denials) == 1
        entry = denials[0]
        assert entry['result'] == 'denied'
        assert entry['mcp_tool'] == spec.name and entry['details']['mcp_tool'] == spec.name
        assert entry['details']['required_permissions'] == list(spec.permissions)

    # Never more permissive than the web path for the same registry state.
    if result.allowed:
        assert _web_allows(rbac_middleware, spec, principal.claims, usecase_id), spec.name

    role = _deciding_role(shared_module, sub, scope, principal.user)
    # Viewer is denied every mutating tool.
    if role == shared_module.Role.VIEWER and not spec.read_only:
        assert not result.allowed, spec.name
    # Enforcement on, a global DataLabeler deciding: only labeling:tasks-self.
    if enforced and role == shared_module.Role.DATA_LABELER:
        assert result.allowed is ('labeling:tasks-self' in spec.permissions), spec.name
    # Enforcement off (D29 option (A), documented-limitation-D29): a
    # non-PortalAdmin is denied every global tool whose permission Viewer lacks.
    viewer = {p.value for p in shared_module.rbac_manager.role_permissions[
        shared_module.Role.VIEWER]}
    if (not enforced and spec.scope == 'global'
            and role != shared_module.Role.PORTAL_ADMIN
            and not set(spec.permissions) & viewer):
        assert not result.allowed, spec.name


class _FailingRolesTable:
    def get_item(self, **_kwargs):
        raise ClientError({'Error': {'Code': 'ProvisionedThroughputExceededException',
                                     'Message': 'registry read failed'}}, 'GetItem')


@SETTINGS
@given(spec=st.sampled_from(SPECS), enforced=st.booleans(), no_usecase=st.booleans())
def test_registry_read_failure(shared, spec, enforced, no_usecase):
    """Enforcement on: `authorization_unavailable` and one marked entry.
    Enforcement off: the legacy resolver's swallow-to-Viewer, never a crash."""
    shared_module, stack = shared.module, shared.stack
    sub = f'p4u-{uuid.uuid4()}'
    principal = _principal(sub)
    usecase_id = _usecase_arg(spec, no_usecase)
    real = shared_module.dynamodb
    roles_table = shared_module.USER_ROLES_TABLE

    class _Resource:
        def Table(self, name):  # noqa: N802 - boto3 resource API
            return _FailingRolesTable() if name == roles_table else real.Table(name)

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('PORTAL_REGISTRY_ENFORCED', 'true' if enforced else 'false')
        patch.setattr(shared_module, 'dynamodb', _Resource())
        result = authz.authorize(spec, principal, usecase_id,
                                 _mcp_event(spec, principal.claims, usecase_id))

    unavailable = _marked(stack, sub, 'authorization_unavailable')
    denials = _marked(stack, sub, 'unauthorized_access')
    if enforced:
        assert isinstance(result.decision, authz.Unavailable)
        assert len(unavailable) == 1 and denials == []
        assert unavailable[0]['result'] == 'failure'
        assert unavailable[0]['mcp_tool'] == spec.name
    else:
        viewer = {p.value for p in shared_module.rbac_manager.role_permissions[
            shared_module.Role.VIEWER]}
        assert unavailable == []
        assert result.allowed is bool(set(spec.permissions) & viewer), spec.name
        assert len(denials) == (0 if result.allowed else 1)


def test_documented_limitation_d29_enforcement_off(shared):
    """documented-limitation-D29: under enforcement off a global DataLabeler
    row grants nothing (the legacy order ignores it) and DataScientist /
    UseCaseAdmin Use_Case rows do not reach the global build tools."""
    stack = shared.stack
    labeler_tools = [s for s in SPECS if 'labeling:tasks-self' in s.permissions]
    build_tools = [s for s in SPECS if s.scope == 'global'
                   and set(s.permissions) & {'builds:read', 'builds:submit', 'builds:cancel'}]
    assert labeler_tools and build_tools
    shared.monkeypatch.setenv('PORTAL_REGISTRY_ENFORCED', 'false')
    labeler = f'd29l-{uuid.uuid4()}'
    _seed(stack, labeler, 'global', {'role': 'DataLabeler', 'status': 'enabled'})
    for spec in labeler_tools:
        usecase_id = _usecase_arg(spec, False)
        assert not authz.authorize(spec, _principal(labeler), usecase_id,
                                   _mcp_event(spec, _claims(labeler), usecase_id)).allowed
    for role in ('DataScientist', 'UseCaseAdmin'):
        sub = f'd29b-{uuid.uuid4()}'
        _seed(stack, sub, USECASE, {'role': role, 'status': None})
        for spec in build_tools:
            assert not authz.authorize(spec, _principal(sub), None,
                                       _mcp_event(spec, _claims(sub), None)).allowed
