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
"""A handshake-era client against the Edge MCP child.

Spec: .kiro/specs/mcp-servers/ (design.md Testing Strategy row "16.9 legacy
revision"; tasks.md 11.11). Requirements 16.9, 15.3.

``Client(streamable_http_client(...), mode="legacy")`` over
``httpx2.ASGITransport`` against the child's real app (stateless, JSON
responses): ``initialize``, ``tools/list`` and one ``tools/call``.
"""
import pytest

import _child_harness as h
from mcp_edge.child import dispatch


@pytest.fixture
def backend():
    fake = h.FakeBackend(routes={('GET', '/system/station'): h.json_answer({'name': 's'})})
    fake.install()
    yield fake
    dispatch.set_client(None)


def test_legacy_initialize_list_and_call(backend):
    async def go(client):
        info = client.server_info
        tools = await client.list_tools()
        result = await client.call_tool('edge_system_station_get', {})
        return client.protocol_version, info, tools, result

    version, info, tools, result = h.over_http(
        go, mode='legacy', headers={'Authorization': 'Bearer session-token'})
    assert info.name == 'dda-edge-mcp' and info.version == '1.0.0'
    assert version < '2026-07-28'
    assert any(t.name == 'edge_system_station_get' for t in tools.tools)
    assert not result.is_error and result.structured_content['result'] == {'name': 's'}
    assert backend.whoami_requests[-1].headers['authorization'] == 'Bearer session-token'


def test_legacy_client_records_its_client_info(backend):
    records = []
    h.over_http(lambda c: c.call_tool('edge_system_station_get', {}), mode='legacy',
                records=records)
    [record] = records
    assert record['mcp_client'] != '' and len(record['mcp_client']) <= 128
