"""Pre-dispatch authorization of a portal tool call (design §2 `authz.py` row, D5).

`authorize(spec, principal, usecase_id)` asks the real resolver,
`shared_utils.rbac_manager.has_permission(user_id, scope, permission,
user_info=user)`, for each declared permission (any-of, like `rbac_check`).
The scope is the `usecase_id` argument for a `'usecase'` tool, `'global'`
for a `'global'` tool, and `usecase_id or 'global'` for
`'usecase_or_global'`. Because the resolver is the oracle, the
enforcement-mode behaviour of D29 is inherited, not re-implemented.

It also returns the Identity_Source that `dispatch` places in
`event['mcp']`, and writes the marked `unauthorized_access` (denied) /
`authorization_unavailable` (failure) entries with `rbac_check`'s details
plus `mcp_tool`, attributed through `attribution_from` on the synthesized
event so the source IP, user agent, Identity_Source and the marker are all
recorded.
"""
import logging
from dataclasses import dataclass
from typing import Optional, Tuple, Union

logger = logging.getLogger(__name__)

UNAUTHORIZED_ACTION = 'unauthorized_access'
# rbac_middleware.AUTHORIZATION_UNAVAILABLE_ACTION
AUTHORIZATION_UNAVAILABLE_ACTION = 'authorization_unavailable'
RESOURCE_TYPE = 'api_endpoint'


@dataclass(frozen=True)
class Denied:
    required_permissions: Tuple[str, ...]
    scope: str
    user_role: str
    claimed_role: str


@dataclass(frozen=True)
class Unavailable:
    error: str


@dataclass(frozen=True)
class Authorization:
    decision: Union[None, Denied, Unavailable]
    scope: str
    identity_source: Optional[str]

    @property
    def allowed(self) -> bool:
        return self.decision is None


def scope_for(spec, usecase_id: Optional[str]) -> str:
    """The RBAC scope of a call (review-3 N3)."""
    if spec.scope == 'global':
        return 'global'
    if spec.scope == 'usecase_or_global':
        return usecase_id or 'global'
    return usecase_id


def _details(spec, permissions, scope, event, claimed_role, **extra) -> dict:
    details = {
        'required_permissions': list(permissions),
        'usecase_id': scope,
        'method': (event or {}).get('httpMethod'),
        'path': (event or {}).get('path'),
        'claimed_role': claimed_role,
        'mcp_tool': spec.name,
    }
    details.update(extra)
    return details


def authorize(spec, principal, usecase_id: Optional[str],
              mcp_event: Optional[dict] = None) -> Authorization:
    """Decide the call; write the marked denial / unavailability entry.

    `mcp_event` is the synthesized backend event (without an
    `identity_source` in its `mcp` block); it attributes the audit entries.
    """
    import shared_utils  # the SharedLayer; imported where it is needed

    user = principal.user
    user_id = user['user_id']
    claimed_role = user.get('role', 'unknown')
    scope = scope_for(spec, usecase_id)
    permissions = tuple(spec.permissions)
    identity = shared_utils.attribution_from(mcp_event, user, usecase_id=scope)
    identity_source = identity.get('identity_source')
    resource_id = spec.resource or spec.name
    try:
        allowed = not permissions or any(
            shared_utils.rbac_manager.has_permission(
                user_id, scope, shared_utils.Permission(p), user_info=user)
            for p in permissions)
        if allowed:
            return Authorization(None, scope, identity_source)
        role = shared_utils.rbac_manager.get_user_role(user_id, scope, user_info=user)
        decision = Denied(permissions, scope, role.value if role else 'none', claimed_role)
        shared_utils.log_audit_event(
            user_id=user_id, action=UNAUTHORIZED_ACTION, resource_type=RESOURCE_TYPE,
            resource_id=resource_id, result='denied',
            details=_details(spec, permissions, scope, mcp_event, claimed_role,
                             user_role=decision.user_role),
            identity=identity)
        logger.info('MCP tool %s denied for sub %s at scope %s', spec.name, user_id, scope)
        return Authorization(decision, scope, identity_source)
    except shared_utils.RegistryUnavailable as error:
        logger.error('Portal_Identity registry unavailable for MCP tool %s: %s',
                     spec.name, error, exc_info=True)
        shared_utils.log_audit_event(
            user_id=user_id, action=AUTHORIZATION_UNAVAILABLE_ACTION,
            resource_type=RESOURCE_TYPE, resource_id=resource_id, result='failure',
            details=_details(spec, permissions, scope, mcp_event, claimed_role,
                             error=str(error)),
            identity=identity)
        return Authorization(Unavailable(str(error)), scope, identity_source)
