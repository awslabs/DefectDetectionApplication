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
"""Property 16: Edge credential forwarding.

**Feature: mcp-servers, Property 16: Edge credential forwarding**

Spec: .kiro/specs/mcp-servers/ (design.md Correctness Properties, Property
16; §5 ``child/dispatch.py``, §7 step 3, D12; tasks.md 11.8).
**Validates: Requirements 9.1, 9.7, 11.3, 8.6**

For any edge spec, schema-valid arguments and credential (or none),
``call_backend`` sends the credential as ``Authorization: Bearer`` and never
in the URL when ``credential_in_query`` is False, as the ``token`` query
parameter and never in a header when it is True, neither with no credential,
and always forwards ``X-Request-ID``. The Backend_API is an
``httpx2.MockTransport`` recording the request.
"""
import base64
import json
from urllib.parse import quote

import httpx2
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from _child_schema import arguments
from mcp_edge.child import dispatch
from mcp_edge.child.catalog import CATALOG

PIN = 'edge_static_image_pin'

credentials = st.one_of(
    st.none(),
    st.from_regex(r'[A-Za-z0-9_-]{8,40}\.[A-Za-z0-9_-]{8,40}\.[A-Za-z0-9_-]{8,40}', fullmatch=True),
    st.from_regex(r'[A-Za-z0-9._~+/=-]{16,64}', fullmatch=True))


class Recorder:
    def __init__(self):
        self.requests = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(200, json={'ok': True})


@pytest.fixture
def recorder():
    handler = Recorder()
    dispatch.set_client(httpx2.Client(base_url='https://127.0.0.1:5443',
                                      transport=httpx2.MockTransport(handler)))
    yield handler
    dispatch.set_client(None)


@st.composite
def calls(draw):
    spec = draw(st.sampled_from(CATALOG))
    args = draw(arguments(spec))
    if spec.name == PIN:
        args['image_base64'] = base64.b64encode(draw(st.binary(min_size=1, max_size=64))).decode()
    request_id = draw(st.from_regex(r'[A-Za-z0-9-]{1,36}', fullmatch=True))
    credential = draw(credentials)
    # A credential equal to an argument value would appear in the URL as the
    # argument, which says nothing about where the credential went.
    assume(credential is None or not any(credential in json.dumps(value)
                                         for value in args.values()))
    assume(credential is None or credential not in request_id)
    return spec, args, credential, request_id


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(call=calls())
def test_edge_credential_forwarding(recorder, call):
    spec, args, credential, request_id = call
    recorder.requests.clear()
    result = dispatch.call_backend(spec, args, credential, request_id)
    assert isinstance(result, dispatch.BackendResponse), (spec.name, result)
    [request] = recorder.requests
    assert request.headers['x-request-id'] == request_id
    url = str(request.url)
    header_values = [value for name, value in request.headers.items() if name != 'x-request-id']
    if credential is None:
        assert 'authorization' not in request.headers
        assert 'token' not in request.url.params
        return
    if spec.credential_in_query:
        assert request.url.params['token'] == credential
        assert 'authorization' not in request.headers
        assert not any(credential in value for value in header_values)
    else:
        assert request.headers['authorization'] == f'Bearer {credential}'
        assert 'token' not in request.url.params
        assert credential not in url and quote(credential, safe='') not in url


def test_the_five_image_routes_take_the_credential_in_the_query():
    in_query = sorted(s.name for s in CATALOG if s.credential_in_query)
    assert len(in_query) == 5
    assert all(s.handler_module.endswith('download_file') for s in CATALOG
               if s.credential_in_query)


def test_whoami_forwards_the_header_and_request_id(recorder):
    from mcp_edge.child import principal

    recorder.requests.clear()
    principal.resolve_principal('Bearer abc.def.ghi', 'req-9')
    [request] = recorder.requests
    assert request.headers['authorization'] == 'Bearer abc.def.ghi'
    assert request.headers['x-request-id'] == 'req-9'
    assert 'abc.def.ghi' not in str(request.url)
