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
"""Property 8: Backend status mapping and scrubbing (edge clause).

**Feature: mcp-servers, Property 8: Backend status mapping and scrubbing**

Spec: .kiro/specs/mcp-servers/ (design.md Correctness Properties, Property 8;
§5 ``child/dispatch.py``, Error Handling "Edge"; tasks.md 11.8).
**Validates: Requirements 9.1, 9.7, 11.3, 8.6**

For any Backend_API status and body (JSON or text, possibly carrying
traceback markers, ``Bearer`` strings, JWTs or a ``token=`` query string),
the child's error result has the category of the shared mapping table and a
message with neither ``Traceback``, ``File "`` nor the credential.
"""
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_edge.child import dispatch, shaping
from mcp_edge.child.catalog import CATALOG

TABLE = {400: 'invalid_input', 422: 'invalid_input', 401: 'not_authenticated',
         403: 'not_authorized', 404: 'not_found', 409: 'conflict', 429: 'throttled'}
CATEGORIES = set(TABLE.values()) | {'backend_failure'}
TRACEBACK = 'Traceback (most recent call last):\n  File "/app/endpoints/x.py", line 3'

jwts = st.from_regex(r'eyJ[A-Za-z0-9_-]{4,40}\.eyJ[A-Za-z0-9_-]{4,40}\.[A-Za-z0-9_-]{8,40}',
                     fullmatch=True)
opaque = st.from_regex(r'[A-Za-z0-9._~+/=-]{16,48}', fullmatch=True)


@st.composite
def messages(draw):
    jwt, token = draw(jwts), draw(opaque)
    fragments = draw(st.lists(st.one_of(
        st.text(max_size=20),
        st.just(TRACEBACK),
        st.just(f'Bearer {token}'),
        st.just(f'Authorization: Bearer {jwt}'),
        st.just(jwt),
        st.just(f'GET /images/x?token={token}&w=1'),
        st.just(f' token={token} ')), max_size=4))
    return ''.join(fragments), (jwt, token)


@st.composite
def responses(draw):
    message, secrets = draw(messages())
    status = draw(st.one_of(st.sampled_from(sorted(TABLE)), st.integers(500, 599),
                            st.integers(400, 499)))
    shape = draw(st.sampled_from(('detail', 'error', 'message', 'nested', 'pydantic',
                                  'text', 'plain', 'list')))
    if shape == 'nested':
        body = json.dumps({'detail': {'message': message}}).encode()
    elif shape == 'pydantic':
        body = json.dumps({'detail': [{'loc': ['body'], 'msg': message}]}).encode()
    elif shape == 'text':
        body = json.dumps(message).encode()
    elif shape == 'plain':
        body = message.encode('utf-8', errors='replace')
    elif shape == 'list':
        body = json.dumps([message]).encode()
    else:
        body = json.dumps({shape: message}).encode()
    content_type = 'text/plain' if shape == 'plain' else 'application/json'
    return dispatch.BackendResponse(status, content_type, body), status, secrets


def _check(failure, status, secrets):
    if status in TABLE:
        assert failure.category == TABLE[status]
    elif status >= 500:
        assert failure.category == 'backend_failure'
    else:
        assert failure.category in CATEGORIES
    assert failure.status == status
    rendered = json.dumps(failure.to_result('req').model_dump(mode='json'))
    for text in (failure.message, failure.text, rendered):
        assert 'Traceback' not in text and 'File \\"' not in text and 'File "' not in text
        for secret in secrets:
            assert secret not in text


@settings(max_examples=100, deadline=None)
@given(response=responses())
def test_status_mapping_and_scrubbing(response):
    backend_response, status, secrets = response
    _check(dispatch.failure_for(backend_response), status, secrets)


@settings(max_examples=100, deadline=None)
@given(response=responses(), spec=st.sampled_from(CATALOG))
def test_shaping_maps_every_non_2xx_alike(response, spec):
    backend_response, status, secrets = response
    shaped = shaping.shape(spec, {}, backend_response)
    assert isinstance(shaped, dispatch.ToolFailure)
    _check(shaped, status, secrets)
