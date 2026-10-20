"""Internal-invocation marker for direct portal-to-portal Lambda invokes.

Shared-layer home of the marker a portal Lambda sets on an event it invokes
directly (``lambda:InvokeFunction``) and of the test that recognises it
(spec ``portal-authorization-and-iam-scoping``, Requirement 4.1, design I2).
Handlers import the three names from ``shared_utils``, which re-exports them;
``detector_conversion.py`` imports them from here, because it is a
standard-library-only module that the host-lane preservation tests load
without the rest of the layer.

Design constraints
------------------
* **Standard library only** - no ``boto3`` and no ``shared_utils`` import, so
  the module loads bare in the host lane and in the operator scripts.
* **Python 3.9 compatible** - ``typing`` generics, no ``X | Y`` unions, no
  ``match``: the host lane and the bucket audit script run on Python 3.9.

Why the source and not the body: API Gateway populates ``requestContext``
(``requestId``, ``apiId``, the authorizer claims) and exposes only method,
path, headers, query string and body to the client, so a client of the REST
API can neither set a top-level event key nor remove the gateway's request
ids. An event that carries the marker and no gateway request context can only
come from a direct invoke by a principal holding ``lambda:InvokeFunction``.
"""
from typing import Any, Dict

INTERNAL_MARKER_KEY = 'dda_internal'


def internal_marker(source: str, purpose: str, user_id: str = 'system') -> Dict[str, str]:
    """The top-level marker a portal Lambda sets on an event it invokes directly.

    ``source`` names the calling module and ``purpose`` the step it starts
    (both fixed strings per caller, so the audit's ``internal_source`` is
    stable); ``user_id`` is the actor the invoked handler records.
    """
    return {'source': source, 'purpose': purpose, 'user_id': user_id}


def is_internal_invocation(event: Dict[str, Any]) -> bool:
    """True only for a direct portal-to-portal invoke: the marker is present and
    the event carries no API Gateway request context. Clients of the REST API
    cannot set top-level keys or the gateway's request ids, so a gateway event
    is never classified internal."""
    rc = event.get('requestContext') or {}
    return (isinstance(event.get(INTERNAL_MARKER_KEY), dict)
            and not rc.get('requestId') and not rc.get('apiId'))
