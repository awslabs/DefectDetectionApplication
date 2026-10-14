"""
Git_Connection helpers shared by git_sync.py and plugin_importer.py
(private-repo-plugin-import task 1; custom-node-source-lifecycle 2.3,
2.4, 9.5).

Both Lambda modules need to look up a Git_Connection, hand its token to a
CodeBuild project as a SECRETS_MANAGER-typed environment variable, and
classify / redact the runner's stderr. They must not import each other
(plugin_importer already duplicates a helper locally to avoid a cycle with
plugin_builds), so the pure and read-only pieces live here in the shared
layer. git_sync.py re-exports them under their original names so its
callers and tests are unchanged.

Nothing here ever reads a secret value: the Lambda roles have no
secretsmanager:GetSecretValue, and the token is resolved inside CodeBuild.
"""
import os
import re
from decimal import Decimal
from typing import Any, Dict, Optional

import boto3


def decimal_to_native(obj):
    """Convert DynamoDB Decimals to native numbers (same rule as
    plugin_records.decimal_to_native; duplicated because a layer module
    cannot import a function module)."""
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    if isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decimal_to_native(i) for i in obj]
    return obj


GIT_CONNECTIONS_TABLE = os.environ.get('GIT_CONNECTIONS_TABLE')
GIT_SECRET_PREFIX = os.environ.get('GIT_SECRET_PREFIX', 'dda-portal/git-connections')

PROVIDERS = ('github', 'gitlab')
#: Username git sends alongside a PAT for each provider.
PROVIDER_USERNAMES = {'github': 'x-access-token', 'gitlab': 'oauth2'}

CONNECTION_VERIFYING = 'verifying'
CONNECTION_VERIFIED = 'verified'
CONNECTION_FAILED = 'failed'

FAILURE_CATEGORIES = ('authentication', 'not_found', 'unreachable', 'diverged',
                      'push_rejected', 'invalid_source', 'internal')

# Runner stderr markers -> Failure_Category (mirror of runner.sh classify).
_CLASSIFY_RULES = (
    ('authentication', ('authentication failed', 'could not read username',
                        'could not read password', ' 401', 'http 401', ' 403',
                        'http 403', 'invalid username or password',
                        'permission denied')),
    ('not_found', ('repository not found', 'not found', ' 404', 'http 404',
                   "couldn't find remote ref", 'could not find remote branch',
                   'pathspec', 'does not appear to be a git repository')),
    ('unreachable', ('could not resolve host', 'connection timed out',
                     'unable to access', 'failed to connect', 'connection refused',
                     'network is unreachable', 'operation timed out')),
)

# Credential material patterns removed from stored excerpts (9.5).
_REDACTIONS = (
    re.compile(r'https?://[^/@\s]+@'),                             # user:token@host
    re.compile(r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{8,}\b'),   # GitHub classic
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{8,}\b'),               # GitHub fine-grained
    re.compile(r'\bglpat-[A-Za-z0-9_\-]{8,}\b'),                  # GitLab PAT
)

_dynamodb = None


def _table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource('dynamodb')
    return _dynamodb.Table(GIT_CONNECTIONS_TABLE)


# ------------------------------------------------------------ pure helpers

def classify_failure(text: Any) -> str:
    """Failure_Category of runner stderr (Property 11): exactly one of
    authentication / not_found / unreachable / internal."""
    lowered = str(text or '').lower()
    for category, markers in _CLASSIFY_RULES:
        if any(marker in lowered for marker in markers):
            return category
    return 'internal'


def redact(text: Any) -> str:
    """Remove credential material from a log excerpt (9.5, Property 11)."""
    result = str(text or '')
    result = _REDACTIONS[0].sub('https://***@', result)
    for pattern in _REDACTIONS[1:]:
        result = pattern.sub('***', result)
    return result


def token_env_override(connection: Dict) -> Dict:
    """
    The one SECRETS_MANAGER-typed StartBuild environment variable that
    carries a Git_Connection's token into CodeBuild (2.3, 2.4, Property 10).
    The value names the secret and its JSON key; CodeBuild resolves it, the
    Lambda never does.
    """
    return {'name': 'GIT_TOKEN', 'value': f"{connection['secret_arn']}:token",
            'type': 'SECRETS_MANAGER'}


def provider_username(connection: Dict) -> str:
    return PROVIDER_USERNAMES.get(connection.get('provider'), 'x-access-token')


# --------------------------------------------------------------- read-only

def get_connection(connection_id: str) -> Optional[Dict]:
    """The stored Git_Connection item (including secret_arn), or None."""
    if not connection_id:
        return None
    response = _table().get_item(Key={'connection_id': str(connection_id)})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def resolve_connection(connection_id: Any, usecase_id: str
                       ) -> 'tuple[Optional[Dict], Optional[str]]':
    """
    Look a Git_Connection up for use by a Use_Case-scoped operation
    (private-repo-plugin-import 1.3, 1.4, 7.2). Returns (connection, None)
    when it exists, belongs to `usecase_id`, and is `verified`; otherwise
    (connection-or-None, reason) with reason one of:

      'not_found'     - no such connection, or it belongs to another
                        Use_Case (indistinguishable to the caller, so a
                        connection id never leaks across Use_Cases);
                        the connection is None
      'not_verified'  - exists in this Use_Case but its status is not
                        `verified`; the connection is returned so the
                        caller can report its status without a second read
    """
    if not isinstance(connection_id, str) or not connection_id.strip():
        return None, 'not_found'
    connection = get_connection(connection_id.strip())
    if not connection or connection.get('usecase_id') != usecase_id:
        return None, 'not_found'
    if connection.get('status') != CONNECTION_VERIFIED:
        return connection, 'not_verified'
    return connection, None
