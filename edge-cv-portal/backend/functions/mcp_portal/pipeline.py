"""The transport-independent tool pipeline of the Portal_MCP_Server
(portal-ai-assistant design §1; mcp-servers design §4 steps 3-9).

`call_tool` runs one already-looked-up `ToolSpec` for an authenticated
`Principal`: validate the arguments and the destructive `confirm` rule, apply
the mutating gate, authorize (the denial / unavailability entry is written by
`authz.authorize`), apply the self-protection rule of a spec with
`self_protect_arg` (step 5b: a principal cannot disable or delete its own
account; mcp-user-operations design A4), dispatch (or answer
`portal_admin_whoami` locally), shape, write the audit envelope and emit the
telemetry line. It returns a plain `ToolOutcome`; the caller renders it for
its transport - `server.py` as an MCP `CallToolResult`, the Portal assistant
as a Bedrock `toolResult`. Both run step 5b, because both call `call_tool`.

This module imports no `mcp` SDK symbol, so a Lambda without the MCP layer
can use it. Its behaviour is the MCP server's own: `server.on_call_tool`
is "authenticate, look up, `call_tool`, render", and
`tests/mcp/test_pipeline_equivalence.py` pins that the two agree.
"""
import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import Optional

import jsonschema

from mcp_portal import audit, authz, catalog, dispatch, shaping, telemetry
from mcp_portal.dispatch import BackendResponse, McpContext
from mcp_portal.errors import BUDGET_MESSAGE, ToolFailure
from mcp_portal.principal import EmailLookup, refresh_email

logger = logging.getLogger(__name__)

# Step 5b, the Self_Protection_Rule (mcp-user-operations design A4).
SELF_PROTECTION_MESSAGE = 'a principal cannot disable or delete its own account'
EMAIL_UNRESOLVED_MESSAGE = ("the caller's account email could not be resolved, so the "
                            'self-protection rule cannot be evaluated; retry')


@dataclass(frozen=True)
class ToolOutcome:
    """How one tool call ended.

    `failure` is None on success, in which case `shaped` carries the result.
    `dispatched` is False for every pre-dispatch outcome (validation,
    confirmation, gate, authorization). `envelope_written` tells whether the
    pipeline wrote an `mcp_tool_call` audit envelope for this completion.
    """
    failure: Optional[ToolFailure]
    shaped: Optional[shaping.Shaped]
    status: Optional[int]
    dispatched: bool
    identity_source: Optional[str]
    duration_ms: int
    envelope_written: bool = False

    @property
    def is_error(self) -> bool:
        return self.failure is not None

    @property
    def category(self) -> Optional[str]:
        return self.failure.category if self.failure is not None else None

    def structured(self, request_id: Optional[str]) -> dict:
        """The structured content of the result (the MCP `structured_content`)."""
        if self.failure is not None:
            return self.failure.structured(request_id)
        assert self.shaped is not None
        return self.shaped.structured


# ------------------------------------------------------------------ lookup

def find_spec(name: str, specs) -> Optional[tuple]:
    """(spec, called_by_alias) for a tool name, or None."""
    for spec in specs:
        if spec.name == name:
            return spec, False
    for spec in specs:
        if spec.deprecated_alias and spec.deprecated_alias == name:
            return spec, True
    return None


# -------------------------------------------------------------- validation

def validation_failure(spec, args) -> Optional[ToolFailure]:
    """`invalid_input` naming the first failing property, or None. The
    message names the property and the failed keyword, never the value."""
    validator = jsonschema.Draft202012Validator(spec.input_schema)
    errors = sorted(validator.iter_errors(args), key=lambda e: (list(e.path), e.validator))
    if not errors:
        return None
    error = errors[0]
    if error.path:
        name = str(error.path[0])
    elif error.validator == 'required':
        missing = [r for r in error.validator_value if r not in (args or {})]
        name = missing[0] if missing else 'arguments'
    elif error.validator == 'additionalProperties':
        extra = [k for k in (args or {}) if k not in spec.input_schema.get('properties', {})]
        name = extra[0] if extra else 'arguments'
    else:
        name = 'arguments'
    if error.validator == 'required':
        return ToolFailure('invalid_input', f'{name} is required')
    if error.validator == 'additionalProperties':
        return ToolFailure('invalid_input', f'{name} is not an argument of {spec.name}')
    return ToolFailure('invalid_input', f'{name} fails {error.validator}')


def _target_value(args: dict, target: str):
    if target.endswith('[0]'):
        value = args.get(target[:-3])
        return value[0] if isinstance(value, list) and value else None
    return args.get(target)


def expected_confirm(spec, args: dict) -> Optional[str]:
    """The value `confirm` must equal (D9; review-3 N2, review-9 N7)."""
    target = spec.confirm_target
    if isinstance(target, tuple):
        for branch, branch_target in zip(spec.input_schema.get('oneOf', ()), target):
            if all(name in args for name in branch.get('required', ())):
                return _target_value(args, branch_target)
        return None
    if target:
        return _target_value(args, target)
    return spec.confirm_literal


def confirmation_failure(spec, args: dict) -> Optional[ToolFailure]:
    if not spec.destructive:
        return None
    expected = expected_confirm(spec, args)
    confirm = args.get('confirm')
    if expected is None or confirm is None or str(confirm) != str(expected):
        return ToolFailure('confirmation_required',
                           f'{spec.name} is destructive; pass confirm={catalog.confirm_text(spec)}')
    return None


def self_protection_failure(spec, args, principal,
                            lookup: EmailLookup) -> Optional[ToolFailure]:
    """Step 5b: the refusal of a call whose target names the caller's own
    account, or None. Pure; never raises; makes no call.

    None for a spec without `self_protect_arg` and for an absent or
    non-string target. Otherwise, in this order: the target equal to
    `principal.username` or `principal.sub` is `conflict` (409); a failed
    lookup is `backend_failure` (retryable: the failure was not cached); the
    target equal to the address the lookup returned is `conflict`; else
    None, which includes an account without an email attribute (`'unknown'`
    with `failed` False). Every comparison is exact; `principal.email`, the
    cached step-1 value, is not consulted.
    """
    if not spec.self_protect_arg:
        return None
    target = (args or {}).get(spec.self_protect_arg)
    if not isinstance(target, str):
        return None
    if target == principal.username or target == principal.sub:
        return ToolFailure('conflict', SELF_PROTECTION_MESSAGE, status=409)
    if lookup.failed:
        return ToolFailure('backend_failure', EMAIL_UNRESOLVED_MESSAGE)
    if lookup.email != 'unknown' and target == lookup.email:
        return ToolFailure('conflict', SELF_PROTECTION_MESSAGE, status=409)
    return None


# ------------------------------------------------------------- local spec

def whoami(principal):
    """`portal_admin_whoami`: the caller as the real resolver sees it.

    A registry read failure under `PORTAL_REGISTRY_ENFORCED` returns the
    `authorization_unavailable` tool failure (Error Handling,
    `RegistryUnavailable` row), so the call still gets its marked audit
    entry and its `mcp_tool` line.
    """
    import shared_utils  # the SharedLayer; imported where it is needed

    user = principal.user
    try:
        role = shared_utils.rbac_manager.get_user_role(principal.sub, 'global',
                                                       user_info=user)
        usecases = shared_utils.rbac_manager.get_accessible_usecases(principal.sub, user)
    except shared_utils.RegistryUnavailable as error:
        logger.error('Portal_Identity registry unavailable for MCP tool '
                     'portal_admin_whoami: %s', error, exc_info=True)
        return ToolFailure('authorization_unavailable',
                           'the Portal identity registry is unavailable; retry')
    body = {'user_id': principal.sub, 'username': principal.username,
            'email': principal.email, 'global_role': role.value if role else None,
            'usecases': sorted(usecases or [])}
    return BackendResponse(200, body, {}, None)


# ---------------------------------------------------------------- pipeline

def call_tool(spec, args: dict, principal, ctx: McpContext, *,
              mutating_enabled: bool, view: Optional[str] = None,
              server_version: str = '', event_name: str = telemetry.EVENT) -> ToolOutcome:
    """Steps 3-9 of the pipeline for one spec (design §4 of mcp-servers), with
    step 5b between authorization and dispatch (mcp-user-operations).

    `ctx` carries the request identity and the marker block (`channel`,
    `server`, `client`, `request_id`); its `identity_source` is filled here
    from the authorization decision. Step 5b does not rebind it, so its
    `principal_email` stays the value the caller set. `mutating_enabled` is
    the caller's switch (the MCP deployment flag, or the assistant's
    Actions_Switch). `view`, `server_version` and `event_name` only shape
    the telemetry line.
    """
    started = time.monotonic()
    args = dict(args or {})
    usecase_id = args.get('usecase_id') if isinstance(args.get('usecase_id'), str) else None

    def elapsed_ms() -> float:
        return (time.monotonic() - started) * 1000

    def log(category: Optional[str], status: Optional[int]) -> None:
        telemetry.log_tool_completion(
            principal=principal, tool=spec.name, view=view, category=category,
            status=status, duration_ms=elapsed_ms(), request_id=ctx.request_id,
            client=ctx.client, server_version=server_version, event=event_name)

    def finish(failure: Optional[ToolFailure], dispatched: bool, status=None,
               shaped=None, event=None, identity_source=None) -> ToolOutcome:
        outcome = audit.Outcome(
            dispatched=dispatched, category=failure.category if failure else None,
            binding_refused=bool(shaped is not None and shaped.binding_refused),
            args=args, usecase_id=usecase_id)
        ctx_for_audit = dataclasses.replace(ctx, identity_source=identity_source)
        written = audit.write_envelope(spec, principal, ctx_for_audit, outcome, elapsed_ms(),
                                       status, shaped.resource_id if shaped is not None else None,
                                       mcp_event=event)
        log(failure.category if failure else None, status)
        return ToolOutcome(failure=failure, shaped=shaped, status=status, dispatched=dispatched,
                           identity_source=identity_source, duration_ms=int(elapsed_ms()),
                           envelope_written=written)

    # 3. validate
    failure = validation_failure(spec, args) or confirmation_failure(spec, args)
    if failure is not None:
        logger.info('%s for %s: %s', failure.category, spec.name, failure.message)
        return finish(failure, dispatched=False)
    # 4. gate
    if not spec.read_only and not mutating_enabled:
        return finish(ToolFailure('mutating_tools_disabled',
                                  f'{spec.name} is mutating and mutating tools are '
                                  'disabled on this deployment'), dispatched=False)
    # 5. authorize (the denial / unavailability entry is written by authz)
    event = dispatch.build_backend_event(spec, args, principal, ctx) if spec.backend else None
    decision = authz.authorize(spec, principal, usecase_id, mcp_event=event)
    if isinstance(decision.decision, authz.Denied):
        log('not_authorized', None)
        return ToolOutcome(
            failure=ToolFailure('not_authorized',
                                f'{spec.name} requires one of {list(spec.permissions)} '
                                f'at scope {decision.scope}', status=403),
            shaped=None, status=None, dispatched=False, identity_source=None,
            duration_ms=int(elapsed_ms()))
    if isinstance(decision.decision, authz.Unavailable):
        log('authorization_unavailable', None)
        return ToolOutcome(
            failure=ToolFailure('authorization_unavailable',
                                'the Portal identity registry is unavailable; retry'),
            shaped=None, status=None, dispatched=False, identity_source=None,
            duration_ms=int(elapsed_ms()))
    identity_source = decision.identity_source
    # 5b. self-protection (disable and delete): first the dispatcher's own
    # time-budget rule (no read without the time a dispatch needs), then the
    # caller's address read from the User_Pool on this call, then the decision
    if spec.self_protect_arg:
        remaining = dispatch._remaining_ms()
        if remaining is not None and remaining < dispatch.MIN_DISPATCH_BUDGET_MS:
            return finish(ToolFailure('timeout', BUDGET_MESSAGE), dispatched=False,
                          identity_source=identity_source)
        lookup = refresh_email(principal)
        failure = self_protection_failure(spec, args, principal, lookup)
        if failure is not None:
            logger.info('%s for %s: %s', failure.category, spec.name, failure.message)
            return finish(failure, dispatched=False, identity_source=identity_source)
    # 6. dispatch
    if spec.backend is None:
        response = whoami(principal)
        event = None
    else:
        event = dispatch.build_backend_event(
            spec, args, principal, dataclasses.replace(ctx, identity_source=identity_source))
        response = dispatch.invoke(spec, event)
    if isinstance(response, ToolFailure):
        return finish(response, dispatched=True, status=None, event=event,
                      identity_source=identity_source)
    # 7-9. binding check and shaping, audit, log
    shaped = shaping.shape(spec, args, response)
    return finish(shaped.failure, dispatched=True, status=response.status, shaped=shaped,
                  event=event, identity_source=identity_source)
