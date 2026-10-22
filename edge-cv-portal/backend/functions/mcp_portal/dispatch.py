"""Dispatch of a portal tool call to its backing handler Lambda (design §2
`dispatch.py` row, Data Models "Synthesized backend event", D30, D32).

* `build_backend_event(spec, args, principal, mcp_ctx)` synthesizes the REST
  proxy v1 event the backing handler already understands. `claims` is the
  verified access-token payload object itself, never merged with anything;
  path parameters are percent-encoded (`quote(value, safe='')`); a
  `SERVER_CONSUMED_ARGS` member travels only when `param_map` names it.
* `invoke(spec, event)` returns a `BackendResponse` or a `ToolFailure`. It
  first applies the remaining-time guard (`MIN_DISPATCH_BUDGET_MS`), then
  makes exactly one `lambda:Invoke` attempt (`total_max_attempts=1`, so
  botocore never re-sends a mutating call after a read timeout). The one
  error that proves nothing ran, `ResourceNotFoundException` (the function
  was replaced), refreshes the dispatch map once and invokes once more.
* `backend_functions()` reads the backend-key -> function-ARN map from the
  SSM parameter `MCP_BACKEND_FUNCTIONS_PARAMETER` once per container,
  lazily on the first tool call.
"""
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Union
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ReadTimeoutError

from mcp_portal.catalog import SERVER_CONSUMED_ARGS
from mcp_portal.errors import BUDGET_MESSAGE, ToolFailure, effect_unknown_failure

logger = logging.getLogger(__name__)

MIN_DISPATCH_BUDGET_MS = 24_000
CONNECT_TIMEOUT_S = 2
READ_TIMEOUT_S = 20

LAMBDA = boto3.client('lambda', config=Config(
    connect_timeout=CONNECT_TIMEOUT_S, read_timeout=READ_TIMEOUT_S,
    retries={'total_max_attempts': 1}))
SSM = boto3.client('ssm', config=Config(
    connect_timeout=2, read_timeout=5, retries={'total_max_attempts': 3}))

# Set by handler() for each invocation.
LAMBDA_CONTEXT = None

_MAP: Optional[Dict[str, str]] = None
_MAP_LOCK = threading.Lock()


class DispatchMapUnavailable(Exception):
    pass


@dataclass(frozen=True)
class McpContext:
    """The `event['mcp']` block and the request identity of one call."""
    request_id: str
    tool: str
    server: str
    client: str
    source_ip: Optional[str] = None
    user_agent: Optional[str] = None
    principal_email: Optional[str] = None
    identity_source: Optional[str] = None


@dataclass(frozen=True)
class BackendResponse:
    status: int
    body_json: object
    headers: dict = field(default_factory=dict)
    raw_body: Optional[str] = None


# ------------------------------------------------------------ dispatch map

def backend_functions(refresh: bool = False) -> Dict[str, str]:
    global _MAP
    with _MAP_LOCK:
        if _MAP is not None and not refresh:
            return _MAP
        name = os.environ.get('MCP_BACKEND_FUNCTIONS_PARAMETER')
        try:
            value = SSM.get_parameter(Name=name)['Parameter']['Value']
            loaded = json.loads(value)
        except Exception as error:  # noqa: BLE001 - mapped to backend_failure
            logger.error('Dispatch map parameter %s unavailable: %s', name,
                         type(error).__name__)
            raise DispatchMapUnavailable() from error
        if not isinstance(loaded, dict):
            logger.error('Dispatch map parameter %s is not a JSON object', name)
            raise DispatchMapUnavailable()
        _MAP = {str(k): str(v) for k, v in loaded.items()}
        return _MAP


def reset_backend_functions() -> None:
    global _MAP
    with _MAP_LOCK:
        _MAP = None


# ------------------------------------------------------------------ event

def _query_value(value) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, separators=(',', ':'))


def forwarded(spec, args: dict) -> Dict[str, Dict[str, object]]:
    """`{'path': {...}, 'query': {...}, 'body': {...}}` of a call's arguments."""
    out = {'path': {}, 'query': {}, 'body': {}}
    for argument, value in args.items():
        if argument in SERVER_CONSUMED_ARGS and argument not in spec.param_map:
            continue
        target = spec.param_map.get(argument, argument)
        out[spec.location_of(argument)][target] = value
    return out


def build_backend_event(spec, args: dict, principal, mcp_ctx: McpContext) -> dict:
    parts = forwarded(spec, args)
    path = spec.resource
    for name, value in parts['path'].items():
        encoded = quote(str(value), safe='')
        path = path.replace('{' + name + '}', encoded).replace('{' + name + '+}', encoded)
    has_body = (spec.method or '').upper() in ('POST', 'PUT', 'PATCH') or bool(parts['body'])
    headers = {'Content-Type': 'application/json'}
    if mcp_ctx.user_agent:
        headers['User-Agent'] = mcp_ctx.user_agent
    mcp_block = {
        'channel': 'mcp', 'tool': mcp_ctx.tool, 'server': mcp_ctx.server,
        'client': mcp_ctx.client, 'request_id': mcp_ctx.request_id,
        'principal_email': mcp_ctx.principal_email,
    }
    if mcp_ctx.identity_source is not None:
        mcp_block['identity_source'] = mcp_ctx.identity_source
    return {
        'httpMethod': spec.method,
        'resource': spec.resource,
        'path': path,
        'pathParameters': {k: str(v) for k, v in parts['path'].items()} or None,
        'queryStringParameters': {k: _query_value(v) for k, v in parts['query'].items()} or None,
        'headers': headers,
        'body': json.dumps(parts['body']) if has_body else None,
        'isBase64Encoded': False,
        'requestContext': {
            'requestId': mcp_ctx.request_id,
            'authorizer': {'claims': principal.claims},
            'identity': {'sourceIp': mcp_ctx.source_ip, 'userAgent': mcp_ctx.user_agent},
        },
        'mcp': mcp_block,
    }


# ----------------------------------------------------------------- invoke

def _remaining_ms() -> Optional[int]:
    context = LAMBDA_CONTEXT
    return context.get_remaining_time_in_millis() if context is not None else None


def _parse(payload: bytes) -> BackendResponse:
    response = json.loads(payload)
    if not isinstance(response, dict) or 'statusCode' not in response:
        raise ValueError('not a proxy response')
    raw = response.get('body')
    if raw is None or raw == '':
        body = None
    elif isinstance(raw, str):
        try:
            body = json.loads(raw)
        except ValueError:
            body = {'text': raw}
    else:
        body = raw
    headers = response.get('headers') if isinstance(response.get('headers'), dict) else {}
    return BackendResponse(int(response['statusCode']), body, headers,
                           raw if isinstance(raw, str) else None)


def _invoke_once(arn: str, event: dict):
    return LAMBDA.invoke(FunctionName=arn, InvocationType='RequestResponse',
                         Payload=json.dumps(event).encode('utf-8'))


def invoke(spec, event: dict) -> Union[BackendResponse, ToolFailure]:
    remaining = _remaining_ms()
    if remaining is not None and remaining < MIN_DISPATCH_BUDGET_MS:
        logger.warning('MCP tool %s not dispatched: %d ms remaining', spec.name, remaining)
        return ToolFailure('timeout', BUDGET_MESSAGE)
    try:
        functions = backend_functions()
    except DispatchMapUnavailable:
        return ToolFailure('backend_failure', 'dispatch map unavailable')
    arn = functions.get(spec.backend)
    if arn is None:
        logger.error('No backing function for backend key %s', spec.backend)
        return ToolFailure('backend_failure', f'no backing function for {spec.backend}')
    request_id = event.get('requestContext', {}).get('requestId')
    refreshed = False
    while True:
        try:
            response = _invoke_once(arn, event)
            break
        except ClientError as error:
            code = error.response.get('Error', {}).get('Code')
            if code == 'ResourceNotFoundException' and not refreshed:
                # The one error that proves nothing ran: the function was
                # replaced. Re-read the map once and invoke once more.
                refreshed = True
                try:
                    arn = backend_functions(refresh=True).get(spec.backend)
                except DispatchMapUnavailable:
                    return ToolFailure('backend_failure', 'dispatch map unavailable')
                if arn is None:
                    return ToolFailure('backend_failure', f'no backing function for {spec.backend}')
                continue
            logger.error('Invoke of %s failed (%s), request %s', arn, code, request_id)
            if code == 'ResourceNotFoundException':
                return ToolFailure('backend_failure', 'backing function not found')
            if code == 'TooManyRequestsException':
                return ToolFailure('throttled', 'the backing function is throttled; retry', 429)
            return effect_unknown_failure(spec, 'backend_failure', 'backing call failed')
        except ReadTimeoutError:
            logger.error('Invoke of %s timed out after %d s, request %s', arn,
                         READ_TIMEOUT_S, request_id)
            return effect_unknown_failure(spec, 'timeout', 'the backing operation timed out')
        except BotoCoreError as error:
            logger.error('Invoke of %s failed (%s), request %s', arn,
                         type(error).__name__, request_id)
            return effect_unknown_failure(spec, 'backend_failure', 'backing call failed')
    if response.get('FunctionError'):
        logger.error('Backing function %s returned FunctionError %s, request %s', arn,
                     response.get('FunctionError'), request_id)
        return effect_unknown_failure(spec, 'backend_failure', 'the backing operation failed')
    try:
        return _parse(response['Payload'].read())
    except Exception:  # noqa: BLE001 - malformed payload
        logger.error('Backing function %s returned a malformed payload, request %s',
                     arn, request_id)
        return effect_unknown_failure(spec, 'backend_failure', 'malformed backend response')
