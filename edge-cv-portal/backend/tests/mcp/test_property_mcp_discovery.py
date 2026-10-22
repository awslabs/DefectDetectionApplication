"""Property test: discovery document consistency.

**Feature: mcp-servers, Property 17: Discovery document consistency**

Spec: .kiro/specs/mcp-servers/ (design.md D27, "2. `mcp_portal` package"
`discovery.py` row, Data Models "Protected Resource Metadata" and
"Authorization Server Metadata facade", Correctness Property 17;
tasks.md 3.3). Validates: Requirements 2.4, 7.5.

* For any syntactically valid `https://` origin (host, optional port, no
  path) used as `MCP_AS_ISSUER`, canonical URL `<origin>/mcp`, view name and
  scope string: `prm_document(url)["resource"] == url`,
  `prm_document(url)["authorization_servers"] == [origin]`,
  `authorization_server_document()["issuer"] == origin`, neither string ends
  with `/`, the facade's three Cognito endpoints start with
  `COGNITO_HOSTED_UI_ORIGIN` and `jwks_uri` with `COGNITO_ISSUER`, and
  `code_challenge_methods_supported == ["S256"]`.
* For any origin carrying a path, query, fragment or trailing slash,
  importing `discovery` with that value raises.
"""
import importlib
import os
import sys
from unittest import mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

VIEWS = ("data", "models", "devices", "workflows", "admin")
MODULE = "mcp_portal.discovery"

_LABEL = st.from_regex(r"\A[a-z0-9]([a-z0-9-]{0,14}[a-z0-9])?\Z", fullmatch=True)
_HOST = st.lists(_LABEL, min_size=1, max_size=4).map(".".join)
_PORT = st.one_of(st.none(), st.integers(min_value=1, max_value=65535))


@st.composite
def origins(draw):
    host = draw(_HOST)
    port = draw(_PORT)
    return f"https://{host}" if port is None else f"https://{host}:{port}"


_SCOPE = st.text(alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7e),
                 min_size=1, max_size=60)
_VIEW = st.one_of(st.none(), st.sampled_from(VIEWS))
_DECORATION = st.one_of(
    st.just("/"),
    st.from_regex(r"\A/[A-Za-z0-9._~-]{1,20}/?\Z", fullmatch=True),
    st.from_regex(r"\A\?[A-Za-z0-9=&]{0,20}\Z", fullmatch=True),
    st.from_regex(r"\A#[A-Za-z0-9]{0,20}\Z", fullmatch=True),
)


def _load(env):
    sys.modules.pop(MODULE, None)
    with mock.patch.dict(os.environ, env):
        try:
            return importlib.import_module(MODULE)
        finally:
            sys.modules.pop(MODULE, None)


@settings(max_examples=100, deadline=None)
@given(origin=origins(), view=_VIEW, scope=_SCOPE,
       hosted_ui=origins(), cognito=origins())
def test_documents_are_consistent_for_any_valid_origin(origin, view, scope,
                                                       hosted_ui, cognito):
    canonical = f"{origin}/mcp"
    cognito_issuer = f"{cognito}/us-east-1_Example1"
    discovery = _load({
        "MCP_AS_ISSUER": origin,
        "MCP_CANONICAL_URL": canonical,
        "MCP_REQUIRED_SCOPE": scope,
        "COGNITO_HOSTED_UI_ORIGIN": hosted_ui,
        "COGNITO_ISSUER": cognito_issuer,
    })
    url = canonical if view is None else f"{canonical}/{view}"

    prm = discovery.prm_document(url)
    facade = discovery.authorization_server_document()

    assert prm["resource"] == url
    assert prm["authorization_servers"] == [origin]
    assert facade["issuer"] == origin
    assert not prm["resource"].endswith("/")
    assert not facade["issuer"].endswith("/")
    for key in ("authorization_endpoint", "token_endpoint", "revocation_endpoint"):
        assert facade[key].startswith(hosted_ui + "/")
    assert facade["jwks_uri"].startswith(cognito_issuer + "/")
    assert facade["code_challenge_methods_supported"] == ["S256"]
    assert prm["scopes_supported"] == [scope] == facade["scopes_supported"]


@settings(max_examples=100, deadline=None)
@given(origin=origins(), decoration=_DECORATION)
def test_import_rejects_an_origin_with_path_query_fragment_or_slash(origin, decoration):
    issuer = origin + decoration
    with pytest.raises(AssertionError):
        _load({
            "MCP_AS_ISSUER": issuer,
            "MCP_CANONICAL_URL": f"{origin}/mcp",
            "MCP_REQUIRED_SCOPE": f"{origin}/mcp/portal.tools",
            "COGNITO_HOSTED_UI_ORIGIN": "https://hosted.example.com",
            "COGNITO_ISSUER": "https://issuer.example.com/pool",
        })
