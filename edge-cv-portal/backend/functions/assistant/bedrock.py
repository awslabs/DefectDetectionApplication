"""Bedrock Converse for the assistant (design §4 `bedrock.py`, §8.1, D12;
Requirements 7.1, 7.4, 7.6, 7.7, 11.1).

`stream` drives `converse_stream`, calls `on_text` with every text delta and
returns a `ModelResponse` with the assistant message content (text and
toolUse blocks), the stop reason and the usage. A configured temperature or
top_p that the model refuses is dropped and the request sent again
(`bedrock_common.send_dropping_refused_options`). Errors are classified into
the assistant's failure codes; two `ValidationException` messages are
recognized as capability signals (tool use, cache points unsupported).
"""
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from botocore.exceptions import BotoCoreError, ClientError, ReadTimeoutError

import bedrock_common

CACHE_PREFIXES_ENV = 'ASSISTANT_CACHE_MODEL_PREFIXES'
# Model families that take `cachePoint` blocks: every Anthropic Claude id
# (generations before 3.5 Haiku reject them, and the rejection path below
# strips the points and retries once per container, so listing the family
# costs one failed call on such a model rather than missing the cache on every
# newer one) and Amazon Nova. Validate-first (ii) on 2026-11-03 found the
# deployed model outside the earlier 3.7/4.x enumeration.
DEFAULT_CACHE_PREFIXES = ('anthropic.claude-', 'amazon.nova')

FAILURE_CODES = {
    'throttling': 'bedrock_throttled',
    'authorization': 'bedrock_authorization',
    'model-access': 'bedrock_model_access',
    'model-error': 'bedrock_model_error',
}
RETRYABLE = ('bedrock_throttled',)


class ModelError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message
        self.retryable = retryable


class ToolUseUnsupported(Exception):
    """The model rejected `toolConfig` (design D12)."""


class CachePointUnsupported(Exception):
    """The model rejected `cachePoint` blocks."""


@dataclass
class ModelResponse:
    content: List[Dict[str, Any]] = field(default_factory=list)   # assistant message blocks
    text: str = ''
    tool_uses: List[Dict[str, Any]] = field(default_factory=list)  # {toolUseId, name, input}
    stop_reason: Optional[str] = None
    usage: Dict[str, int] = field(default_factory=lambda: {
        'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0})
    latency_ms: Optional[int] = None


def supports_cache_points(model_id: str) -> bool:
    raw = os.environ.get(CACHE_PREFIXES_ENV)
    prefixes = tuple(p.strip() for p in raw.split(',') if p.strip()) if raw else DEFAULT_CACHE_PREFIXES
    bare = model_id or ''
    candidates = {bare}
    # An inference profile id carries a region or scope prefix before the
    # provider: `us.`, `eu.`, `apac.`, `global.`. Every prefix in the list
    # starts with the provider name, so the stripped form can only match a
    # model id, never a bare `claude-…` tail.
    head, sep, tail = bare.partition('.')
    if sep and head.isalpha():
        candidates.add(tail)
    return any(candidate.startswith(p) for candidate in candidates for p in prefixes)


def client(config: Dict[str, Any]):
    timeout = int(config.get('timeout_seconds') or bedrock_common.MAX_TIMEOUT_SECONDS)
    timeout = max(1, min(timeout, bedrock_common.MAX_TIMEOUT_SECONDS))
    return bedrock_common.get_bedrock_client(config['region'], timeout)


def build_request(*, model_id: str, system: Sequence[Dict[str, Any]],
                  messages: Sequence[Dict[str, Any]], tool_config: Optional[Dict[str, Any]],
                  inference_config: Dict[str, Any],
                  guardrail: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    request: Dict[str, Any] = {
        'modelId': model_id,
        'system': list(system),
        'messages': list(messages),
        'inferenceConfig': dict(inference_config),
    }
    if tool_config:
        request['toolConfig'] = tool_config
    if guardrail and guardrail.get('guardrail_id'):
        request['guardrailConfig'] = {
            'guardrailIdentifier': guardrail['guardrail_id'],
            'guardrailVersion': guardrail.get('guardrail_version') or 'DRAFT',
        }
    return request


def strip_cache_points(request: Dict[str, Any]) -> Dict[str, Any]:
    stripped = dict(request)
    stripped['system'] = [b for b in request.get('system', []) if 'cachePoint' not in b]
    if 'toolConfig' in request:
        tools = [t for t in request['toolConfig'].get('tools', []) if 'cachePoint' not in t]
        stripped['toolConfig'] = {**request['toolConfig'], 'tools': tools}
    return stripped


def classify(error: Exception) -> ModelError:
    if isinstance(error, ReadTimeoutError):
        return ModelError('bedrock_timeout', 'the model did not answer in time')
    if isinstance(error, ClientError):
        code = error.response.get('Error', {}).get('Code')
        message = str(error.response.get('Error', {}).get('Message') or '')
        category = bedrock_common.categorize_bedrock_error(code)
        failure = FAILURE_CODES.get(category, 'bedrock_model_error')
        return ModelError(failure, f'{code or "error"}: {message[:200]}', failure in RETRYABLE)
    if isinstance(error, BotoCoreError):
        return ModelError('bedrock_model_error', type(error).__name__)
    return ModelError('bedrock_model_error', type(error).__name__)


def _capability_signal(error: Exception) -> Optional[Exception]:
    if not isinstance(error, ClientError):
        return None
    if error.response.get('Error', {}).get('Code') != 'ValidationException':
        return None
    message = str(error.response.get('Error', {}).get('Message') or '').lower()
    if 'cachepoint' in message or 'cache point' in message:
        return CachePointUnsupported(message)
    if 'toolconfig' in message or 'tool use' in message or 'tools' in message \
            or 'tool_use' in message:
        return ToolUseUnsupported(message)
    return None


def stream(bedrock_client, request: Dict[str, Any],
           on_text: Optional[Callable[[str], None]] = None) -> ModelResponse:
    """Run `converse_stream`; raise `ToolUseUnsupported`, `CachePointUnsupported`
    or `ModelError`."""
    response = ModelResponse()
    try:
        output = bedrock_common.send_dropping_refused_options(
            bedrock_client.converse_stream, request)
        _consume(output['stream'], response, on_text)
    except (ClientError, BotoCoreError) as error:
        signal = _capability_signal(error)
        if signal is not None:
            raise signal from error
        raise classify(error) from error
    return response


def _consume(events, response: ModelResponse, on_text: Optional[Callable[[str], None]]) -> None:
    blocks: Dict[int, Dict[str, Any]] = {}
    order: List[int] = []
    for event in events:
        if 'contentBlockStart' in event:
            start = event['contentBlockStart']
            index = start.get('contentBlockIndex', len(order))
            tool_use = (start.get('start') or {}).get('toolUse')
            blocks[index] = ({'kind': 'toolUse', 'toolUseId': tool_use.get('toolUseId'),
                              'name': tool_use.get('name'), 'input': ''}
                             if tool_use else {'kind': 'text', 'text': ''})
            order.append(index)
        elif 'contentBlockDelta' in event:
            delta = event['contentBlockDelta']
            index = delta.get('contentBlockIndex', order[-1] if order else 0)
            if index not in blocks:
                blocks[index] = {'kind': 'text', 'text': ''}
                order.append(index)
            block = blocks[index]
            part = delta.get('delta') or {}
            if 'text' in part:
                block['kind'] = 'text'
                block['text'] = block.get('text', '') + part['text']
                if on_text:
                    on_text(part['text'])
            elif 'toolUse' in part:
                block['input'] = block.get('input', '') + (part['toolUse'].get('input') or '')
        elif 'messageStop' in event:
            response.stop_reason = event['messageStop'].get('stopReason')
        elif 'metadata' in event:
            usage = event['metadata'].get('usage') or {}
            response.usage = {
                'input': int(usage.get('inputTokens', 0)),
                'output': int(usage.get('outputTokens', 0)),
                'cache_read': int(usage.get('cacheReadInputTokens', 0)),
                'cache_write': int(usage.get('cacheWriteInputTokens', 0)),
            }
            metrics = event['metadata'].get('metrics') or {}
            if 'latencyMs' in metrics:
                response.latency_ms = int(metrics['latencyMs'])
    texts = []
    for index in order:
        block = blocks[index]
        if block['kind'] == 'text':
            if block['text']:
                response.content.append({'text': block['text']})
                texts.append(block['text'])
        else:
            raw = block.get('input') or '{}'
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except ValueError:
                parsed = {'_unparsed_input': raw[:2000]}
            if not isinstance(parsed, dict):
                parsed = {'_value': parsed}
            use = {'toolUseId': block['toolUseId'], 'name': block['name'], 'input': parsed}
            response.content.append({'toolUse': use})
            response.tool_uses.append(use)
    response.text = ''.join(texts)
