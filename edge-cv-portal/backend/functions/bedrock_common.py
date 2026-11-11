"""
Shared Bedrock_Configuration resolution and client construction
(custom-node-code-assist, Requirements 4.1-4.7).

Extracted verbatim from workflow_generator.py so every Bedrock-backed
feature (workflow generation, custom node code assist) reads the same
`bedrock_configuration` settings item with identical semantics:

- Defaults overridden by stored values; sampling parameters
  (temperature / top_p) honor an explicitly stored null (the parameter
  stays unset and is omitted from the invocation).
- With no max_tokens stored, a request's max_tokens is
  default_max_tokens() of the model it goes to: the model's maximum
  output when MODEL_MAX_OUTPUT_TOKENS lists it, else 4096.
- The invocation timeout is coerced to an int (junk -> 240) and clamped
  to [1, 240] seconds.
- bedrock-runtime clients are cached per (region, timeout) with a
  client-side read timeout equal to the configured invocation timeout
  and retries disabled, so a request's wall time cannot exceed the
  timeout.
- build_inference_config emits maxTokens plus at most ONE sampling
  parameter: temperature when set, else topP when set (recent Anthropic
  models reject requests specifying both).
- send_dropping_refused_options sends a Converse request and, when the
  model refuses a forced tool choice, a temperature or a topP, sends it
  again without the refused option. When the model refuses a maxTokens
  above its maximum output, it sends the request again once, with the
  maximum that Bedrock's refusal states (lower_max_tokens=False leaves
  that refusal to the caller). A model that accepts the request gets it
  unchanged.

This module lives in the same Lambda bundle as its consumers
(backend/functions is one code asset), so they import it directly.
"""
import logging
import os
import re
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

logger = logging.getLogger()

# AWS clients
dynamodb = boto3.resource('dynamodb')

# Environment variables
SETTINGS_TABLE = os.environ.get('SETTINGS_TABLE')

# Bedrock_Configuration lives in the portal settings table under this key;
# the settings UI/API (workflow-manager task 10.2) is restricted to
# PortalAdmin via bedrock-config:write (workflow-manager Requirement 10.6).
BEDROCK_CONFIG_SETTING_KEY = 'bedrock_configuration'

# The invocation timeout is configurable up to 240 seconds
# (workflow-manager Requirement 10.7; code-assist Requirement 4.4).
# Raised from 60: large-output scaffold generations (e.g. node designer
# with high max_tokens models) regularly exceed 60 s.
MAX_TIMEOUT_SECONDS = 240

# Default max_tokens per model, for a request when the Bedrock
# configuration stores no max_tokens (default_max_tokens()). A listed
# model gets its documented maximum output. Any other model gets
# UNLISTED_MODEL_MAX_TOKENS, the single default every model got before
# this table, so its requests are unchanged. Keys are model ids; an
# inference profile id resolves through the model it fronts.
MODEL_MAX_OUTPUT_TOKENS: Dict[str, int] = {
    # Claude Opus 5.5, the default model: "Max output tokens: 128K" on its
    # Amazon Bedrock model card. Its adaptive thinking is always on and
    # counts toward max_tokens, so at 4096 Node Designer scaffolds were cut
    # off.
    'anthropic.claude-opus-5-5': 128000,
}
UNLISTED_MODEL_MAX_TOKENS = 4096

DEFAULT_BEDROCK_CONFIG = {
    # Global cross-region inference profile of Claude Opus 5.5: current
    # Anthropic models on Bedrock are invokable only through inference
    # profiles (the bare foundation-model ids are not directly invokable,
    # and older direct ids like claude-3-5-sonnet have reached end of
    # life). A global profile may serve a request in any commercial
    # region; a regional profile (e.g. us.) set in the Bedrock
    # configuration keeps requests in its geography.
    'model_id': 'global.anthropic.claude-opus-5-5',
    'region': os.environ.get('AWS_REGION', 'us-east-1'),
    # default_max_tokens() of the default model. With no max_tokens
    # stored, get_bedrock_configuration() resolves it for the model the
    # request goes to.
    'max_tokens': 128000,
    # Sampling parameters are unset by default: they are sent to Bedrock
    # only when explicitly configured in the settings (or overridden
    # per-request). Recent Anthropic models reject requests that set
    # temperature at all, and never accept temperature AND top_p together,
    # so build_inference_config() omits None values and sends at most one
    # of the two.
    'temperature': None,
    'top_p': None,
    'timeout_seconds': MAX_TIMEOUT_SECONDS,
}

# Cached per (region, timeout) so warm invocations reuse connections.
_bedrock_clients: Dict[Tuple[str, int], Any] = {}


def _decimal_to_native(obj):
    """Convert Decimal objects from DynamoDB to native Python types"""
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    elif isinstance(obj, dict):
        return {k: _decimal_to_native(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_decimal_to_native(i) for i in obj]
    return obj


def default_max_tokens(model_id: Any) -> int:
    """
    The max_tokens of a request to ``model_id`` when the Bedrock
    configuration stores none: the model's maximum output when
    MODEL_MAX_OUTPUT_TOKENS lists it, else UNLISTED_MODEL_MAX_TOKENS.

    An inference profile id (``global.``, ``us.``, ``eu.`` ... followed by
    a model id) resolves through the model it fronts, the id after the
    first '.', as the settings model listing derives it. Every key starts
    with its provider, so a model id's own tail never matches. Total: any
    other value, including a non-string, gives UNLISTED_MODEL_MAX_TOKENS.
    """
    if not isinstance(model_id, str):
        return UNLISTED_MODEL_MAX_TOKENS
    model_id = model_id.strip()
    fronted = model_id.split('.', 1)[1] if '.' in model_id else None
    for candidate in (model_id, fronted):
        if candidate in MODEL_MAX_OUTPUT_TOKENS:
            return MODEL_MAX_OUTPUT_TOKENS[candidate]
    return UNLISTED_MODEL_MAX_TOKENS


def get_bedrock_configuration(request_model_id: Optional[str] = None) -> Dict:
    """
    Load the Bedrock_Configuration from the portal settings table, falling
    back to sensible defaults for any missing value.

    Stored item shape (written by the PortalAdmin settings API):
        {setting_key: 'bedrock_configuration',
         value: {model_id, region, max_tokens, temperature, top_p,
                 timeout_seconds}}
    A flat item (attributes directly on the item) is also accepted.

    With no max_tokens stored (no item, no value or null), max_tokens is
    default_max_tokens() of the model the request goes to:
    ``request_model_id`` when given (the auto-labeler's ``bedrock:``
    family, which sends the labeling job's model), else the resolved
    model_id. A stored max_tokens is returned as stored.

    The timeout is clamped to at most 240 seconds.
    """
    config = dict(DEFAULT_BEDROCK_CONFIG)
    max_tokens_stored = False
    if SETTINGS_TABLE:
        try:
            response = dynamodb.Table(SETTINGS_TABLE).get_item(
                Key={'setting_key': BEDROCK_CONFIG_SETTING_KEY}
            )
            item = response.get('Item')
            if item:
                stored = item.get('value') if isinstance(item.get('value'), dict) else item
                stored = _decimal_to_native(stored)
                for key in DEFAULT_BEDROCK_CONFIG:
                    if key in ('temperature', 'top_p'):
                        # An explicitly stored null unsets a sampling
                        # parameter (recent Anthropic models reject
                        # temperature and top_p together; nulling
                        # temperature lets top_p be sent instead).
                        if key in stored:
                            config[key] = stored[key]
                    elif stored.get(key) is not None:
                        config[key] = stored[key]
                max_tokens_stored = stored.get('max_tokens') is not None
        except ClientError as e:
            logger.warning(f"Could not read Bedrock configuration, using defaults: {str(e)}")

    if not max_tokens_stored:
        config['max_tokens'] = default_max_tokens(
            config['model_id'] if request_model_id is None
            else request_model_id)

    try:
        timeout = int(config['timeout_seconds'])
    except (TypeError, ValueError):
        timeout = MAX_TIMEOUT_SECONDS
    config['timeout_seconds'] = max(1, min(timeout, MAX_TIMEOUT_SECONDS))
    return config


def get_bedrock_client(region: str, timeout_seconds: int):
    """
    bedrock-runtime client with a client-side read timeout equal to the
    configured invocation timeout (the Lambda invokes with a client-side
    timeout equal to the configured value). Retries are disabled so a
    request's wall time cannot exceed the configured timeout.
    """
    cache_key = (region, timeout_seconds)
    client = _bedrock_clients.get(cache_key)
    if client is None:
        client = boto3.client(
            'bedrock-runtime',
            region_name=region,
            config=BotoConfig(
                connect_timeout=min(timeout_seconds, 10),
                read_timeout=timeout_seconds,
                retries={'max_attempts': 0}
            )
        )
        _bedrock_clients[cache_key] = client
    return client


# botocore error code -> failure category (custom-node-code-assist
# Requirement 5.1; shared with the Portal assistant). Total: every unlisted
# code falls through to 'model-error'.
BEDROCK_ERROR_CATEGORIES: Dict[str, str] = {
    'ThrottlingException': 'throttling',
    'TooManyRequestsException': 'throttling',
    'ServiceQuotaExceededException': 'throttling',
    'AccessDeniedException': 'authorization',
    'UnrecognizedClientException': 'authorization',
    'ExpiredTokenException': 'authorization',
    'ResourceNotFoundException': 'model-access',
    'ModelNotReadyException': 'model-access',
    'ValidationException': 'model-access',
    'ModelErrorException': 'model-error',
    'ModelTimeoutException': 'model-error',
    'ServiceUnavailableException': 'model-error',
    'InternalServerException': 'model-error',
}


def categorize_bedrock_error(error_code: Any) -> str:
    """Map a botocore error code to exactly one of the four failure
    categories; anything unrecognized is 'model-error'."""
    return BEDROCK_ERROR_CATEGORIES.get(error_code, 'model-error')


def build_inference_config(config: Dict) -> Dict:
    """
    Converse inferenceConfig from a resolved Bedrock_Configuration.

    Never send temperature and top_p together: recent Anthropic models
    (e.g. claude-sonnet-4-5) reject requests specifying both. Temperature
    wins when set; top_p is sent only when temperature is absent/None
    (Requirements 4.2, 4.3).
    """
    inference_config = {'maxTokens': int(config['max_tokens'])}
    temperature = config.get('temperature')
    top_p = config.get('top_p')
    if temperature is not None:
        inference_config['temperature'] = float(temperature)
    elif top_p is not None:
        inference_config['topP'] = float(top_p)
    return inference_config


# Request options a model can refuse while it accepts the rest of the
# request: (request section, key, how Bedrock's ValidationException names
# the option). Claude Opus 5.5, for example, answers a forced tool with
# 'tool_choice: type "tool" and "any" are not supported for this model.'
# and a temperature with '`temperature` is deprecated for this model.'
# (likewise `top_p`).
REFUSABLE_REQUEST_OPTIONS: Tuple[Tuple[str, str, Any], ...] = (
    ('toolConfig', 'toolChoice', re.compile(r'\btool[_ ]?choice\b')),
    ('inferenceConfig', 'temperature', re.compile(r'\btemperature\b')),
    ('inferenceConfig', 'topP', re.compile(r'\btop[_ ]?p\b')),
)


def refused_request_options(error: Exception,
                            request: Dict) -> List[Tuple[str, str]]:
    """The (section, key) options of ``request`` that ``error``, a
    ValidationException, names as refused. Empty for any other error and
    for options the request does not carry."""
    if not isinstance(error, ClientError):
        return []
    details = error.response.get('Error', {})
    if details.get('Code') != 'ValidationException':
        return []
    message = str(details.get('Message') or '').lower()
    return [(section, key)
            for section, key, pattern in REFUSABLE_REQUEST_OPTIONS
            if key in (request.get(section) or {}) and pattern.search(message)]


# Bedrock refuses a maxTokens above the model's maximum output with a
# ValidationException that states the maximum. Amazon Nova Pro, for
# example, answers 128000 with 'The maximum tokens you requested exceeds
# the model limit of 10000. Try again with a maximum tokens value that is
# lower than 10000.' The wording was the same for every model family
# tried (Amazon, Anthropic, Meta, Mistral, DeepSeek, OpenAI and others).
# A number written with a separator ('10,000') does not match, so a
# limit is never misread.
MAX_TOKENS_LIMIT_PATTERN = re.compile(
    r'\bmaximum tokens you requested exceeds the model limit of (\d+)\b(?![.,]\d)',
    re.IGNORECASE)


def refused_max_tokens_limit(error: Exception,
                             request: Dict) -> Optional[int]:
    """The model's maximum output that ``error``, a ValidationException
    refusing ``request``'s maxTokens, states. None for any other error,
    for a request without an integer maxTokens, and for a stated maximum
    that is not a positive number below the request's maxTokens."""
    if not isinstance(error, ClientError):
        return None
    details = error.response.get('Error', {})
    if details.get('Code') != 'ValidationException':
        return None
    match = MAX_TOKENS_LIMIT_PATTERN.search(str(details.get('Message') or ''))
    max_tokens = (request.get('inferenceConfig') or {}).get('maxTokens')
    if match is None or isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
        return None
    limit = int(match.group(1))
    return limit if 1 <= limit < max_tokens else None


def send_dropping_refused_options(send: Callable[..., Dict],
                                  request: Dict, *,
                                  lower_max_tokens: bool = True) -> Dict:
    """
    ``send(**request)``, where ``send`` is a bedrock-runtime client's
    ``converse`` or ``converse_stream``; when the model refuses part of
    the request, the request is sent again without that part, or with
    the model's maximum.

    Two kinds of refusal are handled:

    - One of the options in REFUSABLE_REQUEST_OPTIONS: a forced tool
      choice (the tools stay offered, the model is no longer required to
      call one), a temperature or a topP. The request is sent again
      without the refused option.
    - A maxTokens above the model's maximum output, which the refusal
      states (refused_max_tokens_limit). The request is sent again with
      maxTokens set to that maximum, at most once per call. With
      ``lower_max_tokens=False`` the refusal reaches the caller instead:
      the ``llm:`` auto-labeling family fails the image with the stated
      limit (llm-model-token-and-image-sizing Requirements 9.4, 9.7).

    The first request is always the caller's request unchanged, so a
    request the model accepts, including one whose maxTokens is within
    the model's maximum, is sent exactly once, as before. Bedrock answers
    a refusal before the model generates anything. Each new request
    drops at least one more option or is the one lowering, so at most
    four more requests are sent. A second maxTokens refusal (for example
    from a model whose stated maximum also counts the prompt) and any
    other error reach the caller unchanged; ``request`` is never
    modified.
    """
    lowered = not lower_max_tokens
    while True:
        try:
            return send(**request)
        except ClientError as error:
            refused = refused_request_options(error, request)
            limit = None if lowered else refused_max_tokens_limit(error, request)
            if not refused and limit is None:
                raise
        request = dict(request)
        if limit is not None:
            logger.warning(f"Model {request.get('modelId')} refused maxTokens "
                           f"{request['inferenceConfig']['maxTokens']} above its "
                           f"limit of {limit}; sending the request again with "
                           f"maxTokens {limit}")
            request['inferenceConfig'] = dict(request['inferenceConfig'],
                                              maxTokens=limit)
            lowered = True
        if refused:
            names = ', '.join(key for _section, key in refused)
            logger.warning(f"Model {request.get('modelId')} refused {names}; "
                           f"sending the request again without it")
            for section, key in refused:
                request[section] = {name: value
                                    for name, value in request[section].items()
                                    if name != key}
