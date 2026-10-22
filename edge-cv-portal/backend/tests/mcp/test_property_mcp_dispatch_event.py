"""Property 5: Dispatch event faithfulness.

**Feature: mcp-servers, Property 5: Dispatch event faithfulness**

Spec: .kiro/specs/mcp-servers/ (design.md Correctness Properties, Property 5;
§2 `dispatch.py` row, Data Models "Synthesized backend event", Requirement
3.9 interpretation; tasks.md 6.4). **Validates: Requirements 1.3, 2.7, 3.9,
4.2**

For any spec of the catalog and any schema-valid argument set, the
synthesized REST proxy v1 event places every forwarded argument where the
spec's `param_map`/`param_in` say, carries the verified claims object
unchanged as the only authorizer key, carries the `mcp` block, and never
contains the access token string.
"""
import base64
import json
from urllib.parse import quote

from hypothesis import given, settings
from hypothesis import strategies as st

from _schema_strategy import arguments
from mcp_portal import catalog as cat
from mcp_portal.catalog import SERVER_CONSUMED_ARGS
from mcp_portal.dispatch import McpContext, build_backend_event
from mcp_portal.principal import Principal

ROUTED = [s for s in cat.CATALOG if s.backend is not None]
BODY_METHODS = ('POST', 'PUT', 'PATCH')


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')


@st.composite
def calls(draw):
    spec = draw(st.sampled_from(ROUTED))
    args = draw(arguments(spec))
    sub = draw(st.uuids()).hex
    claims = {'sub': sub, 'iss': 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool',
              'client_id': 'mcp-client', 'token_use': 'access',
              'scope': 'dda-portal/portal.tools', 'exp': 4102444800, 'iat': 1,
              'jti': draw(st.uuids()).hex, 'username': f'user-{sub[:8]}',
              'auth_time': draw(st.integers(1, 2_000_000_000))}
    signature = draw(st.binary(min_size=32, max_size=64))
    token = '.'.join((_b64({'alg': 'RS256', 'kid': 'k1'}), _b64(claims),
                      base64.urlsafe_b64encode(signature).decode().rstrip('=')))
    ctx = McpContext(
        request_id=draw(st.from_regex(r'[A-Za-z0-9-]{1,36}', fullmatch=True)),
        tool=spec.name, server=f'dda-portal-mcp/{spec.domain}@1.0.0',
        client=draw(st.sampled_from(('kiro/1.2.3', 'unknown', 'other-client/0.1'))),
        source_ip=draw(st.sampled_from(('203.0.113.7', None))),
        user_agent=draw(st.sampled_from(('kiro/1.2.3', None))),
        principal_email=draw(st.sampled_from(('person@example.com', 'unknown'))),
        identity_source=draw(st.sampled_from((None, 'registry', 'absent'))))
    return spec, args, claims, token, ctx


def _location(spec, argument):
    """Where an argument travels (path, param_in, else by method)."""
    target = spec.param_map.get(argument, argument)
    if argument in spec.param_map and target in spec.path_parameters:
        return 'path'
    if argument in spec.param_in:
        return spec.param_in[argument]
    return 'body' if (spec.method or '').upper() in BODY_METHODS else 'query'


def _query_text(value) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, separators=(',', ':'))


@settings(max_examples=100, deadline=None)
@given(calls())
def test_dispatch_event_faithfulness(call):
    spec, args, claims, token, ctx = call
    principal = Principal(sub=claims['sub'], username=claims['username'],
                          email=ctx.principal_email, claims=claims)
    snapshot = json.loads(json.dumps(claims))
    event = build_backend_event(spec, args, principal, ctx)

    expected = {'path': {}, 'query': {}, 'body': {}}
    for argument, value in args.items():
        if argument in SERVER_CONSUMED_ARGS and argument not in spec.param_map:
            continue
        expected[_location(spec, argument)][spec.param_map.get(argument, argument)] = value

    assert event['httpMethod'] == spec.method
    assert event['resource'] == spec.resource
    path = spec.resource
    for name, value in expected['path'].items():
        path = path.replace('{' + name + '}', quote(str(value), safe=''))
        path = path.replace('{' + name + '+}', quote(str(value), safe=''))
    assert event['path'] == path
    assert '{' not in event['path'], (spec.name, event['path'])
    assert event['pathParameters'] == ({k: str(v) for k, v in expected['path'].items()}
                                       or None)
    assert event['queryStringParameters'] == (
        {k: _query_text(v) for k, v in expected['query'].items()} or None)
    if (spec.method or '').upper() in BODY_METHODS or expected['body']:
        assert json.loads(event['body']) == expected['body']
    else:
        assert event['body'] is None

    # The verified payload object itself, unaltered, is the only authorizer key.
    authorizer = event['requestContext']['authorizer']
    assert list(authorizer) == ['claims']
    assert authorizer['claims'] is claims and claims == snapshot

    block = event['mcp']
    assert block['channel'] == 'mcp'
    assert block['tool'] == spec.name and block['server'] == ctx.server
    assert block['client'] == ctx.client and block['request_id'] == ctx.request_id
    assert block['principal_email'] == ctx.principal_email
    assert block.get('identity_source') == ctx.identity_source
    assert event['requestContext']['requestId'] == ctx.request_id

    serialized = json.dumps(event)
    assert token not in serialized
    assert token.split('.')[2] not in serialized
    assert 'Authorization' not in event['headers']
