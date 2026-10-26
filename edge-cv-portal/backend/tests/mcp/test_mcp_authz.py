"""Principal resolution and pre-dispatch authorization (`principal.py`, `authz.py`).

Spec: .kiro/specs/mcp-servers/ (design.md §2 `principal.py` and `authz.py`
rows, D5, D29 option (A), Error Handling "Pre-dispatch RBAC" and "Email
lookup"; tasks.md 6.1). Requirements 3.1, 3.2, 3.3, 3.4, 3.9, 4.7.

The moto session stack (`conftest.aws_stack`) with the real `shared_utils`
resolver; the Cognito client is a fake.
"""
import logging
import sys
import uuid
from types import SimpleNamespace

import pytest
from boto3.dynamodb.conditions import Attr

from mcp_portal import authz, principal
from mcp_portal.catalog import ToolSpec, schema, uc_schema


class FakeCognito:
    def __init__(self, email='person@example.com', fail=False, role='PortalAdmin'):
        self.calls = []
        self.email, self.fail, self.role = email, fail, role

    def admin_get_user(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError('boom')
        attributes = [{'Name': 'sub', 'Value': 'x'},
                      {'Name': 'custom:role', 'Value': self.role}]
        if self.email:
            attributes.append({'Name': 'email', 'Value': self.email})
        return {'Username': kwargs['Username'], 'UserAttributes': attributes}


@pytest.fixture
def cognito(monkeypatch):
    fake = FakeCognito()
    monkeypatch.setattr(principal, 'COGNITO', fake)
    monkeypatch.setenv('COGNITO_USER_POOL_ID', 'us-east-1_pool')
    principal.clear_email_cache()
    yield fake
    principal.clear_email_cache()


def claims_for(sub=None, username=None):
    sub = sub or str(uuid.uuid4())
    return {'sub': sub, 'iss': 'https://issuer.example', 'client_id': 'mcp-client',
            'token_use': 'access', 'scope': 'res/portal.tools', 'exp': 4102444800,
            'iat': 1, 'jti': 'j', 'username': username or f'user-{sub[:8]}'}


def token_for(claims):
    return SimpleNamespace(claims=claims, subject=claims['sub'])


def mcp_event(spec, claims, usecase_id=None):
    return {
        'httpMethod': spec.method, 'resource': spec.resource, 'path': spec.resource,
        'queryStringParameters': {'usecase_id': usecase_id} if usecase_id else None,
        'requestContext': {'requestId': 'req-1', 'authorizer': {'claims': claims},
                           'identity': {'sourceIp': '203.0.113.7', 'userAgent': 'kiro/1'}},
        'mcp': {'channel': 'mcp', 'tool': spec.name,
                'server': 'dda-portal-mcp/devices@1.0.0', 'client': 'kiro/1',
                'request_id': 'req-1', 'principal_email': 'person@example.com'},
    }


def uc_spec(**overrides):
    values = dict(
        name='portal_devices_fixture_list', domain='devices', title='Fixture',
        description='A fixture.', resource_type='device', input_schema=uc_schema(),
        backend='devices', handler_module='devices', handler_function='list_devices',
        method='GET', resource='/devices', param_map={'usecase_id': 'usecase_id'},
        permissions=('view_devices',), scope='usecase')
    values.update(overrides)
    return ToolSpec(**values)


def global_spec(**overrides):
    values = dict(
        name='portal_admin_fixture_list', domain='admin', title='Fixture',
        description='A fixture.', resource_type='user', input_schema=schema(),
        backend='userManagement', handler_module='user_management',
        handler_function='list_users', method='GET', resource='/users',
        permissions=('manage_users',), scope='global')
    values.update(overrides)
    return ToolSpec(**values)


@pytest.fixture
def stack(aws_stack, monkeypatch):
    shared = sys.modules['shared_utils']
    monkeypatch.setattr(shared, '_REQUEST_AUDIT_CONTEXT', None)
    monkeypatch.delenv('PORTAL_REGISTRY_ENFORCED', raising=False)
    return aws_stack


def grant(stack, sub, usecase_id, role):
    stack.tables.user_roles.put_item(Item={'user_id': sub, 'usecase_id': usecase_id,
                                           'role': role})


def audit_entries(stack, sub, action):
    items = stack.tables.audit_log.scan(FilterExpression=Attr('user_id').eq(sub))['Items']
    return [i for i in items if i.get('action') == action]


# ------------------------------------------------------------- principal

def test_principal_user_matches_get_user_from_event(cognito, stack):
    claims = claims_for()
    p = principal.principal_from_access_token(token_for(claims))
    shared = sys.modules['shared_utils']
    expected = shared.get_user_from_event({'requestContext': {'authorizer': {'claims': claims}}})
    assert p.user == expected
    assert p.user['role'] == 'Viewer' and p.user['email'] == claims['username']
    assert p.claims is claims and p.sub == claims['sub']
    assert p.email == 'person@example.com'


def test_email_lookup_reads_only_email_and_is_cached_per_sub(cognito):
    claims = claims_for()
    first = principal.principal_from_access_token(token_for(claims))
    second = principal.principal_from_access_token(token_for(claims))
    assert first.email == second.email == 'person@example.com'
    assert cognito.calls == [{'UserPoolId': 'us-east-1_pool', 'Username': claims['username']}]
    # custom:role from AdminGetUser is not read (D29 option (A))
    assert first.user['role'] == 'Viewer' and 'custom:role' not in first.claims


def test_email_lookup_failure_is_unknown_and_not_cached(cognito, caplog):
    cognito.fail = True
    claims = claims_for()
    with caplog.at_level(logging.WARNING, logger='mcp_portal.principal'):
        p = principal.principal_from_access_token(token_for(claims))
    assert p.email == 'unknown'
    assert any('Email lookup failed' in r.getMessage() for r in caplog.records)
    cognito.fail = False
    assert principal.principal_from_access_token(token_for(claims)).email == 'person@example.com'
    assert len(cognito.calls) == 2


def test_account_without_email_is_unknown(cognito):
    cognito.email = None
    assert principal.principal_from_access_token(token_for(claims_for())).email == 'unknown'


# ----------------------------------------------------------------- authz

def test_scope_for():
    assert authz.scope_for(uc_spec(), 'uc-1') == 'uc-1'
    assert authz.scope_for(global_spec(), 'uc-1') == 'global'
    either = uc_spec(scope='usecase_or_global',
                     input_schema=schema({'usecase_id': {'type': 'string'}}))
    assert authz.scope_for(either, 'uc-1') == 'uc-1'
    assert authz.scope_for(either, None) == 'global'


def test_usecase_role_allows_and_returns_identity_source(cognito, stack):
    claims = claims_for()
    grant(stack, claims['sub'], 'uc-allow', 'Viewer')
    p = principal.principal_from_access_token(token_for(claims))
    spec = uc_spec()
    result = authz.authorize(spec, p, 'uc-allow', mcp_event(spec, claims, 'uc-allow'))
    assert result.allowed and result.scope == 'uc-allow'
    assert result.identity_source in ('registry', 'absent')
    assert audit_entries(stack, claims['sub'], 'unauthorized_access') == []


def test_denial_writes_a_marked_unauthorized_access_entry(cognito, stack):
    claims = claims_for()
    grant(stack, claims['sub'], 'uc-deny', 'Viewer')
    p = principal.principal_from_access_token(token_for(claims))
    spec = uc_spec(name='portal_devices_fixture_delete', permissions=('manage_devices',))
    result = authz.authorize(spec, p, 'uc-deny', mcp_event(spec, claims, 'uc-deny'))
    assert not result.allowed and isinstance(result.decision, authz.Denied)
    assert result.decision.user_role == 'Viewer'
    entries = audit_entries(stack, claims['sub'], 'unauthorized_access')
    assert len(entries) == 1
    entry = entries[0]
    assert entry['result'] == 'denied'
    assert entry['channel'] == 'mcp' and entry['mcp_tool'] == spec.name
    assert entry['source_ip'] == '203.0.113.7'
    assert entry['email'] == 'person@example.com'
    details = entry['details']
    assert details['required_permissions'] == ['manage_devices']
    assert details['usecase_id'] == 'uc-deny'
    assert details['user_role'] == 'Viewer'
    assert details['claimed_role'] == 'Viewer'
    assert details['mcp_tool'] == spec.name


def test_global_tool_is_checked_at_global(cognito, stack):
    claims = claims_for()
    grant(stack, claims['sub'], 'uc-x', 'UseCaseAdmin')
    p = principal.principal_from_access_token(token_for(claims))
    spec = global_spec()
    # A Use_Case role does not satisfy a global permission.
    result = authz.authorize(spec, p, 'uc-x', mcp_event(spec, claims))
    assert not result.allowed and result.scope == 'global'
    grant(stack, claims['sub'], 'global', 'PortalAdmin')
    assert authz.authorize(spec, p, None, mcp_event(spec, claims)).allowed


def test_any_of_declared_permissions(cognito, stack):
    claims = claims_for()
    grant(stack, claims['sub'], 'uc-any', 'Operator')
    p = principal.principal_from_access_token(token_for(claims))
    spec = uc_spec(permissions=('create_training_jobs', 'manage_devices'))
    assert authz.authorize(spec, p, 'uc-any', mcp_event(spec, claims, 'uc-any')).allowed


def test_enforcement_on_unprovisioned_principal_is_denied(cognito, stack, monkeypatch):
    monkeypatch.setenv('PORTAL_REGISTRY_ENFORCED', 'true')
    claims = claims_for()
    grant(stack, claims['sub'], 'uc-enf', 'Operator')   # no global row
    p = principal.principal_from_access_token(token_for(claims))
    spec = uc_spec()
    result = authz.authorize(spec, p, 'uc-enf', mcp_event(spec, claims, 'uc-enf'))
    assert not result.allowed
    assert result.identity_source == 'absent'
    grant(stack, claims['sub'], 'global', 'Viewer')
    result = authz.authorize(spec, p, 'uc-enf', mcp_event(spec, claims, 'uc-enf'))
    assert result.allowed and result.identity_source == 'registry'


def test_registry_unavailable_is_a_marked_failure(cognito, stack, monkeypatch):
    shared = sys.modules['shared_utils']
    claims = claims_for()
    p = principal.principal_from_access_token(token_for(claims))

    def unavailable(*_a, **_k):
        raise shared.RegistryUnavailable('registry read failed')

    monkeypatch.setattr(shared.rbac_manager, 'has_permission', unavailable)
    spec = uc_spec()
    result = authz.authorize(spec, p, 'uc-down', mcp_event(spec, claims, 'uc-down'))
    assert isinstance(result.decision, authz.Unavailable)
    entries = audit_entries(stack, claims['sub'], 'authorization_unavailable')
    assert len(entries) == 1
    assert entries[0]['result'] == 'failure' and entries[0]['mcp_tool'] == spec.name
    assert entries[0]['details']['error'] == 'registry read failed'
