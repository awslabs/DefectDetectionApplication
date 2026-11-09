"""
Shared Bedrock_Configuration resolution and client construction
(custom-node-code-assist, Requirements 4.1-4.7).

Extracted verbatim from workflow_generator.py so every Bedrock-backed
feature (workflow generation, custom node code assist) reads the same
`bedrock_configuration` settings item with identical semantics:

- Defaults overridden by stored values; sampling parameters
  (temperature / top_p) honor an explicitly stored null (the parameter
  stays unset and is omitted from the invocation).
- The invocation timeout is coerced to an int (junk -> 240) and clamped
  to [1, 240] seconds.
- bedrock-runtime clients are cached per (region, timeout) with a
  client-side read timeout equal to the configured invocation timeout
  and retries disabled, so total wall time cannot exceed the timeout.
- build_inference_config emits maxTokens plus at most ONE sampling
  parameter: temperature when set, else topP when set (recent Anthropic
  models reject requests specifying both).
- send_dropping_refused_options sends a Converse request and, when the
  model refuses a forced tool choice, a temperature or a topP, sends it
  again without the refused option; a model that accepts them gets the
  request unchanged.

This module lives in the same Lambda bundle as its consumers
(backend/functions is one code asset), so they import it directly.
"""
import logging
import os
import re
from decimal import Decimal
from typing import Any, Callable, Dict, List, Tuple

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
    'max_tokens': 4096,
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


def get_bedrock_configuration() -> Dict:
    """
    Load the Bedrock_Configuration from the portal settings table, falling
    back to sensible defaults for any missing value.

    Stored item shape (written by the PortalAdmin settings API):
        {setting_key: 'bedrock_configuration',
         value: {model_id, region, max_tokens, temperature, top_p,
                 timeout_seconds}}
    A flat item (attributes directly on the item) is also accepted.

    The timeout is clamped to at most 240 seconds.
    """
    config = dict(DEFAULT_BEDROCK_CONFIG)
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
        except ClientError as e:
            logger.warning(f"Could not read Bedrock configuration, using defaults: {str(e)}")

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
    timeout equal to the configured value). Retries are disabled so the
    total wall time cannot exceed the configured timeout.
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


def send_dropping_refused_options(send: Callable[..., Dict],
                                  request: Dict) -> Dict:
    """
    ``send(**request)``, where ``send`` is a bedrock-runtime client's
    ``converse`` or ``converse_stream``; when the model refuses one of the
    request's options, the request is sent again without it.

    The options are those in REFUSABLE_REQUEST_OPTIONS: a forced tool
    choice (the tools stay offered, the model is no longer required to
    call one), a temperature and a topP. The first request is always the
    caller's request unchanged, so a model that accepts these options
    gets exactly one request, as before. Bedrock answers a refusal before
    the model generates anything, and each new request drops at least
    one more option, so at most three more requests are sent. Any other
    error reaches the caller unchanged; ``request`` is never modified.
    """
    while True:
        try:
            return send(**request)
        except ClientError as error:
            refused = refused_request_options(error, request)
            if not refused:
                raise
        names = ', '.join(key for _section, key in refused)
        logger.warning(f"Model {request.get('modelId')} refused {names}; "
                       f"sending the request again without it")
        request = dict(request)
        for section, key in refused:
            request[section] = {name: value
                                for name, value in request[section].items()
                                if name != key}
