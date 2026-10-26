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
"""Property test: proxy exposure rule.

**Feature: mcp-servers, Property 12: Proxy exposure rule**

Spec: .kiro/specs/mcp-servers/ (design §6 ``endpoints/mcp_proxy.py``, §5
``proxy.py``, D14, Correctness Property 12; tasks.md 10.8).
Validates: Requirements 12.4, 12.5.

For any peer address, listener kind (TLS / plaintext), ``Origin`` (absent or
any value), allow-list (empty by default) and configuration state, the pure
``exposure_decision`` of ``endpoints/mcp_proxy.py`` answers 403
``invalid_origin`` when ``Origin`` is present and not allowed (always, with
an empty list); else 403 ``mcp_loopback_only`` for a non-loopback peer on the
plaintext listener; else 503 when the server is disabled or the child is not
ready; else forwards, and the outbound header set built by
``proxy.forwarded_headers`` never contains ``Origin``.
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from endpoints.mcp_proxy import LOOPBACK_HOSTS, exposure_decision
from mcp_edge.proxy import forwarded_headers

ipv4 = st.ip_addresses(v=4).map(str)
ipv6 = st.ip_addresses(v=6).map(str)
peers = st.one_of(st.sampled_from(sorted(LOOPBACK_HOSTS)), ipv4, ipv6,
                  st.sampled_from(["localhost", "0.0.0.0", "::ffff:127.0.0.1", ""]),
                  st.none(), st.text(max_size=40))
origins = st.one_of(st.none(), st.text(max_size=60),
                    st.sampled_from(["https://portal.example", "null", "http://127.0.0.1:5000"]))
allow_lists = st.one_of(st.just(()), st.lists(st.text(min_size=1, max_size=60), max_size=4),
                        st.just(["https://portal.example"]))


def oracle(peer, tls, origin, allowed, enabled, ready):
    if origin is not None and origin not in set(allowed):
        return 403, "invalid_origin"
    if peer not in ("127.0.0.1", "::1") and not tls:
        return 403, "mcp_loopback_only"
    if not enabled:
        return 503, "mcp_disabled"
    if not ready:
        return 503, "mcp_unavailable"
    return None


@settings(max_examples=100, deadline=None)
@given(peer=peers, tls=st.booleans(), origin=origins, allowed=allow_lists,
       enabled=st.booleans(), ready=st.booleans())
def test_exposure_decision(peer, tls, origin, allowed, enabled, ready):
    decision = exposure_decision(peer, tls, origin, allowed, enabled, ready)
    assert decision == oracle(peer, tls, origin, allowed, enabled, ready)


@settings(max_examples=100, deadline=None)
@given(peer=peers, tls=st.booleans(), origin=st.text(max_size=60),
       enabled=st.booleans(), ready=st.booleans())
def test_any_origin_is_refused_with_the_default_empty_list(peer, tls, origin, enabled, ready):
    assert exposure_decision(peer, tls, origin, (), enabled, ready) == (403, "invalid_origin")


header_names = st.one_of(
    st.sampled_from(["Origin", "origin", "ORIGIN", "Authorization", "Content-Type",
                     "Accept", "Mcp-Session-Id", "Mcp-Protocol-Version", "Cookie",
                     "Connection", "Host", "X-Forwarded-For", "Mcp-Param-Region",
                     "Last-Event-ID"]),
    st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126,
                                   blacklist_characters=":"), min_size=1, max_size=20))


@settings(max_examples=100, deadline=None)
@given(headers=st.dictionaries(header_names, st.text(max_size=40), max_size=10),
       request_id=st.one_of(st.none(), st.text(min_size=1, max_size=32)))
def test_forwarded_headers_never_carry_origin(headers, request_id):
    outbound = forwarded_headers(headers, request_id)
    assert "origin" not in {name.lower() for name in outbound}
    assert "cookie" not in outbound and "host" not in outbound
    assert outbound["x-request-id"] == (request_id or outbound["x-request-id"])
    assert outbound["x-request-id"]


def test_fixed_cases():
    assert exposure_decision("127.0.0.1", False, None, (), True, True) is None
    assert exposure_decision("::1", False, None, (), True, True) is None
    assert exposure_decision("192.0.2.10", True, None, (), True, True) is None
    assert exposure_decision("192.0.2.10", False, None, (), True, True) == (403, "mcp_loopback_only")
    assert exposure_decision("127.0.0.1", False, "https://portal.example", (), True, True) == \
        (403, "invalid_origin")
    assert exposure_decision("127.0.0.1", False, "https://portal.example",
                             ["https://portal.example"], True, True) is None
    assert exposure_decision("127.0.0.1", True, None, (), False, True) == (503, "mcp_disabled")
    assert exposure_decision("127.0.0.1", True, None, (), True, False) == (503, "mcp_unavailable")
