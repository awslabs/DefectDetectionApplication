"""Discovery documents of the Portal_MCP_Server (`mcp_portal/discovery.py`).

Spec: .kiro/specs/mcp-servers/ (design.md D27, "2. `mcp_portal` package"
`discovery.py` row, Data Models "Protected Resource Metadata" and
"Authorization Server Metadata facade"; tasks.md 3.2). Requirements 2.4, 7.5.
"""
import importlib
import sys

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

ORIGIN = "https://abc123defg.execute-api.us-east-1.amazonaws.com"
CANONICAL = f"{ORIGIN}/mcp"
SCOPE = f"{CANONICAL}/portal.tools"
HOSTED_UI = "https://dda-portal-example.auth.us-east-1.amazoncognito.com"
COGNITO_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Example1"
VIEWS = ("data", "models", "devices", "workflows", "admin")

ENV = {
    "MCP_AS_ISSUER": ORIGIN,
    "MCP_CANONICAL_URL": CANONICAL,
    "MCP_REQUIRED_SCOPE": SCOPE,
    "COGNITO_HOSTED_UI_ORIGIN": HOSTED_UI,
    "COGNITO_ISSUER": COGNITO_ISSUER,
}


def load(monkeypatch, **overrides):
    for key, value in {**ENV, **overrides}.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("mcp_portal.discovery", None)
    return importlib.import_module("mcp_portal.discovery")


@pytest.fixture
def discovery(monkeypatch):
    module = load(monkeypatch)
    yield module
    sys.modules.pop("mcp_portal.discovery", None)


@pytest.fixture
def client(discovery):
    return TestClient(Starlette(routes=discovery.routes()))


def expected_prm(resource):
    return {"resource": resource, "authorization_servers": [ORIGIN],
            "scopes_supported": [SCOPE], "bearer_methods_supported": ["header"]}


def test_prm_document(discovery):
    assert discovery.prm_document(CANONICAL) == expected_prm(CANONICAL)


def test_authorization_server_document(discovery):
    assert discovery.authorization_server_document() == {
        "issuer": ORIGIN,
        "authorization_endpoint": f"{HOSTED_UI}/oauth2/authorize",
        "token_endpoint": f"{HOSTED_UI}/oauth2/token",
        "revocation_endpoint": f"{HOSTED_UI}/oauth2/revoke",
        "jwks_uri": f"{COGNITO_ISSUER}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SCOPE],
    }


def test_issuer_is_the_same_string_everywhere(discovery):
    issuer = discovery.authorization_server_document()["issuer"]
    assert issuer is discovery.MCP_AS_ISSUER
    assert discovery.prm_document(CANONICAL)["authorization_servers"][0] is issuer
    assert not issuer.endswith("/")


def test_eight_get_routes(discovery):
    routes = discovery.routes()
    assert len(routes) == 8
    paths = [route.path for route in routes]
    assert paths == (
        ["/.well-known/oauth-protected-resource",
         "/.well-known/oauth-protected-resource/mcp"]
        + [f"/.well-known/oauth-protected-resource/mcp/{v}" for v in VIEWS]
        + ["/.well-known/oauth-authorization-server"])
    for route in routes:
        assert route.methods == {"GET", "HEAD"}


@pytest.mark.parametrize("view", VIEWS)
def test_path_suffixed_prm_names_its_endpoint(client, view):
    response = client.get(f"/.well-known/oauth-protected-resource/mcp/{view}")
    assert response.status_code == 200
    assert response.json() == expected_prm(f"{CANONICAL}/{view}")
    assert response.headers["cache-control"] == "public, max-age=3600"


def test_root_prm_is_the_canonical_document(client):
    root = client.get("/.well-known/oauth-protected-resource")
    canonical = client.get("/.well-known/oauth-protected-resource/mcp")
    assert root.status_code == canonical.status_code == 200
    assert root.content == canonical.content
    assert root.json() == expected_prm(CANONICAL)
    assert root.headers["cache-control"] == "public, max-age=3600"


def test_authorization_server_route(client, discovery):
    response = client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    assert response.json() == discovery.authorization_server_document()
    assert response.headers["cache-control"] == "public, max-age=3600"
    # The issuer string arrives byte for byte (no added trailing slash).
    assert f'"issuer":"{ORIGIN}"' in response.text


def test_no_openid_configuration_alias(client):
    assert client.get("/.well-known/openid-configuration").status_code == 404


def test_get_only(client):
    assert client.post("/.well-known/oauth-protected-resource").status_code == 405
    assert client.post("/.well-known/oauth-authorization-server").status_code == 405


def test_strings_end_to_end_with_a_port(monkeypatch):
    origin = "https://mcp.example.com:8443"
    module = load(monkeypatch, MCP_AS_ISSUER=origin,
                  MCP_CANONICAL_URL=f"{origin}/mcp")
    try:
        assert module.prm_document(f"{origin}/mcp")["authorization_servers"] == [origin]
        assert module.authorization_server_document()["issuer"] == origin
    finally:
        sys.modules.pop("mcp_portal.discovery", None)


@pytest.mark.parametrize("issuer", [
    "http://abc123defg.execute-api.us-east-1.amazonaws.com",
    f"{ORIGIN}/",
    f"{ORIGIN}/mcp",
    f"{ORIGIN}?x=1",
    f"{ORIGIN}#frag",
    "https://other.execute-api.us-east-1.amazonaws.com",
    "",
])
def test_import_time_validation_rejects(monkeypatch, issuer):
    with pytest.raises(AssertionError):
        load(monkeypatch, MCP_AS_ISSUER=issuer)
    sys.modules.pop("mcp_portal.discovery", None)


def test_import_time_validation_rejects_mismatched_canonical(monkeypatch):
    with pytest.raises(AssertionError):
        load(monkeypatch, MCP_CANONICAL_URL="https://elsewhere.example.com/mcp")
    sys.modules.pop("mcp_portal.discovery", None)
