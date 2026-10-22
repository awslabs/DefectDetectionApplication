"""The `mcp_tool_call` audit envelope (design §2 `audit.py` row, D6, N8, D36).

`write_envelope` writes one marked `mcp_tool_call` entry through
`shared_utils.log_audit_event` when `envelope_required` says the completion
needs it:

* every **pre-dispatch** outcome (validation, confirmation, the mutating
  switch, the time budget, the dispatch map), except a denial or an
  authorization-unavailable outcome, which `authz` already recorded with its
  own marked entry;
* after dispatch, a call that produced no backend status (invoke failure,
  timeout, throttle, malformed payload);
* `status < 300` when the spec's handler writes no audit entry of its own
  (`not spec.handler_audits`);
* `status >= 300`, except a 403 from a `handler_audits_denials` route (the
  decorated handler already wrote a marked `unauthorized_access`);
* always when the D36 binding check refused a 2xx body: `result='denied'`,
  `details.reason='usecase_binding'`, `details.status` = the backend's 2xx
  status, even when `handler_audits` is True, because the handler's own entry
  records a read that MCP did not deliver (revision 10, review-9 N5;
  revision 11, review-10 N4).

Count contract (review-3 N4): a handler writing `k` entries on success leaves
exactly `max(k, 1)` marked entries for the completion; a D36 refusal leaves
`k + 1`. The specs pinned `handler_audits=False` in the honesty test's
`PINNED_AUDIT_FLAGS` audit on some branches only, so their envelope is always
written and a branch that also audits leaves `k + 1`.

A failed write is swallowed by `log_audit_event` (audit loss never fails a
tool call; strictness is the handler's job, Requirement 4.4); an exception
escaping it is logged as `audit_envelope_failed`.
"""
import logging
from dataclasses import dataclass, field
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

ACTION = 'mcp_tool_call'
COLLECTION = 'collection'
RESULT_SUCCESS = 'success'
RESULT_FAILURE = 'failure'
RESULT_DENIED = 'denied'
BINDING_REASON = 'usecase_binding'
# Categories whose pre-dispatch entry `authz.authorize` already wrote.
AUTHZ_CATEGORIES = ('not_authorized', 'authorization_unavailable')


@dataclass(frozen=True)
class Outcome:
    """How one tool call completed, as far as the audit envelope needs it.

    `dispatched` is False for every pre-dispatch outcome. `category` is the
    error category of the tool result (None on success). `binding_refused`
    marks the D36 refusal of a 2xx body.
    """
    dispatched: bool
    category: Optional[str] = None
    binding_refused: bool = False
    args: Mapping = field(default_factory=dict)
    usecase_id: Optional[str] = None


def envelope_required(spec, outcome: Outcome, backend_status: Optional[int]) -> bool:
    """The D6/N8 rule: does this completion need the MCP envelope?"""
    if not outcome.dispatched:
        return outcome.category not in AUTHZ_CATEGORIES
    if outcome.binding_refused:
        return True
    if backend_status is None:
        return True
    if backend_status < 300:
        return not spec.handler_audits
    return not (backend_status == 403 and spec.handler_audits_denials)


def _result(outcome: Outcome) -> str:
    if outcome.binding_refused:
        return RESULT_DENIED
    return RESULT_FAILURE if outcome.category else RESULT_SUCCESS


def _resource_id(spec, outcome: Outcome, resource_id: Optional[str]) -> str:
    if resource_id:
        return str(resource_id)
    for name in spec.input_schema.get('properties', {}):
        if name.endswith('_id') and name != 'usecase_id' and outcome.args.get(name):
            return str(outcome.args[name])
    return COLLECTION


def marker_event(principal, mcp_ctx) -> dict:
    """A minimal event carrying the claims, the request identity and the `mcp`
    block, for completions that ended before a backend event was built."""
    block = {'channel': 'mcp', 'tool': mcp_ctx.tool, 'server': mcp_ctx.server,
             'client': mcp_ctx.client, 'request_id': mcp_ctx.request_id,
             'principal_email': mcp_ctx.principal_email}
    if mcp_ctx.identity_source is not None:
        block['identity_source'] = mcp_ctx.identity_source
    return {'requestContext': {'requestId': mcp_ctx.request_id,
                               'authorizer': {'claims': principal.claims},
                               'identity': {'sourceIp': mcp_ctx.source_ip,
                                            'userAgent': mcp_ctx.user_agent}},
            'mcp': block}


def details_for(spec, outcome: Outcome, duration_ms: int,
                backend_status: Optional[int]) -> dict:
    details = {'tool': spec.name, 'status': backend_status,
               'duration_ms': int(duration_ms)}
    if outcome.usecase_id is not None:
        details['usecase_id'] = outcome.usecase_id
    if outcome.category:
        details['error_category'] = outcome.category
    if outcome.binding_refused:
        details['reason'] = BINDING_REASON
    for name in spec.audit_args:
        if name in outcome.args:
            details[name] = outcome.args[name]
    return details


def write_envelope(spec, principal, mcp_ctx, outcome: Outcome, duration_ms: int,
                   backend_status: Optional[int] = None,
                   resource_id: Optional[str] = None,
                   mcp_event: Optional[dict] = None) -> bool:
    """Write the envelope when `envelope_required`; True when one was written.

    `mcp_event` is the synthesized backend event when the call got that far;
    otherwise the attribution is taken from `marker_event`.
    """
    if not envelope_required(spec, outcome, backend_status):
        return False
    import shared_utils  # the SharedLayer; imported where it is needed

    event = mcp_event if mcp_event is not None else marker_event(principal, mcp_ctx)
    user = principal.user
    try:
        shared_utils.log_audit_event(
            user_id=user['user_id'], action=ACTION, resource_type=spec.resource_type,
            resource_id=_resource_id(spec, outcome, resource_id),
            result=_result(outcome),
            details=shared_utils.sanitize_audit_details(
                details_for(spec, outcome, duration_ms, backend_status)),
            identity=shared_utils.attribution_from(event, user,
                                                   usecase_id=outcome.usecase_id))
    except Exception as error:  # noqa: BLE001 - audit loss never fails a call
        logger.error('audit_envelope_failed tool=%s request=%s error=%s', spec.name,
                     mcp_ctx.request_id, type(error).__name__)
    return True
