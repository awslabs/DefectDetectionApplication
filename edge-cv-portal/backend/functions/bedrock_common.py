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

This module lives in the same Lambda bundle as its consumers
(backend/functions is one code asset), so they import it directly.
"""
import logging
import os
from decimal import Decimal
from typing import Any, Dict, Tuple

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
    # Cross-region inference profile: current Anthropic models on Bedrock
    # are invokable only through inference profiles (the bare
    # foundation-model ids are not directly invokable, and older direct
    # ids like claude-3-5-sonnet have reached end of life).
    'model_id': 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
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
