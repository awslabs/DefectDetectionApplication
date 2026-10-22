"""OAuth discovery documents of the Portal_MCP_Server (design D27).

Served unauthenticated by the MCP Lambda:

* `GET /.well-known/oauth-protected-resource` (root): the Protected Resource
  Metadata (RFC 9728) of the canonical `/mcp` endpoint;
* `GET /.well-known/oauth-protected-resource/mcp[/<view>]`: one document per
  endpoint URL (six), each naming that URL as its `resource`;
* `GET /.well-known/oauth-authorization-server`: a static RFC 8414 document
  whose endpoints are the Cognito hosted UI's and whose `jwks_uri` is the
  user pool's key set. It issues nothing; Cognito mints and verifies codes
  and tokens.

Every document is rendered from environment strings, end to end. No
`AnyHttpUrl` is constructed, because pydantic would add a trailing slash to
a path-less origin and the client's exact issuer match would then fail.
`MCP_AS_ISSUER` is validated when the module is imported, so a surprising
value fails the Lambda's cold start (an AssertionError, raised explicitly
so it holds under `python -O`) instead of serving a mismatched issuer.
"""
import os
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_portal import VIEWS

PRM_PATH = '/.well-known/oauth-protected-resource'
AS_METADATA_PATH = '/.well-known/oauth-authorization-server'
CACHE_HEADERS = {'Cache-Control': 'public, max-age=3600'}

MCP_AS_ISSUER = os.environ.get('MCP_AS_ISSUER', '')
MCP_CANONICAL_URL = os.environ.get('MCP_CANONICAL_URL', '')
MCP_REQUIRED_SCOPE = os.environ.get('MCP_REQUIRED_SCOPE', '')
COGNITO_HOSTED_UI_ORIGIN = os.environ.get('COGNITO_HOSTED_UI_ORIGIN', '')
COGNITO_ISSUER = os.environ.get('COGNITO_ISSUER', '')


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}'


def validate_issuer(issuer: str, canonical_url: str) -> None:
    """Raise AssertionError unless `issuer` is a bare https origin (no path,
    query, fragment or trailing slash) equal to the origin of
    `canonical_url`."""
    parts = urlsplit(issuer)
    if parts.scheme != 'https' or not parts.netloc:
        raise AssertionError(f'MCP_AS_ISSUER must be an https:// origin: {issuer!r}')
    if parts.path or parts.query or parts.fragment or issuer.endswith('/') \
            or '?' in issuer or '#' in issuer:
        raise AssertionError('MCP_AS_ISSUER must have no path, query, fragment '
                         f'or trailing slash: {issuer!r}')
    if _origin(canonical_url) != issuer:
        raise AssertionError('MCP_AS_ISSUER must equal the origin of '
                         f'MCP_CANONICAL_URL: {issuer!r} vs {canonical_url!r}')


validate_issuer(MCP_AS_ISSUER, MCP_CANONICAL_URL)


def endpoint_urls() -> list:
    """The six endpoint URLs: the aggregate `/mcp` and one per view."""
    return [MCP_CANONICAL_URL] + [f'{MCP_CANONICAL_URL}/{view}' for view in VIEWS]


def prm_document(resource_url: str) -> dict:
    """Protected Resource Metadata for one endpoint URL."""
    return {
        'resource': resource_url,
        'authorization_servers': [MCP_AS_ISSUER],
        'scopes_supported': [MCP_REQUIRED_SCOPE],
        'bearer_methods_supported': ['header'],
    }


def authorization_server_document() -> dict:
    """The RFC 8414 authorization-server metadata facade."""
    return {
        'issuer': MCP_AS_ISSUER,
        'authorization_endpoint': f'{COGNITO_HOSTED_UI_ORIGIN}/oauth2/authorize',
        'token_endpoint': f'{COGNITO_HOSTED_UI_ORIGIN}/oauth2/token',
        'revocation_endpoint': f'{COGNITO_HOSTED_UI_ORIGIN}/oauth2/revoke',
        'jwks_uri': f'{COGNITO_ISSUER}/.well-known/jwks.json',
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code', 'refresh_token'],
        'code_challenge_methods_supported': ['S256'],
        'token_endpoint_auth_methods_supported': ['none'],
        'scopes_supported': [MCP_REQUIRED_SCOPE],
    }


def _document_endpoint(document: dict):
    async def endpoint(_request):
        return JSONResponse(document, headers=CACHE_HEADERS)
    return endpoint


def routes() -> list:
    """The eight `GET` routes: the root PRM, six path-suffixed PRMs and the
    authorization-server facade."""
    result = [Route(PRM_PATH, _document_endpoint(prm_document(MCP_CANONICAL_URL)),
                    methods=['GET'])]
    for url in endpoint_urls():
        suffix = urlsplit(url).path
        result.append(Route(f'{PRM_PATH}{suffix}',
                            _document_endpoint(prm_document(url)),
                            methods=['GET']))
    result.append(Route(AS_METADATA_PATH,
                        _document_endpoint(authorization_server_document()),
                        methods=['GET']))
    return result
