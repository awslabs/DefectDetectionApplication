"""
Workflow Generator API Lambda function (Workflow Manager)

Prompt-based Workflow_Definition generation via Amazon Bedrock
(Workflow_Generator, Requirements 10.2, 10.3, 10.5, 10.7).

Routes (API Gateway REST):
    POST /workflows/generate    Submit a natural-language prompt (optionally
                                within an existing chat session) and receive a
                                generated Workflow_Definition plus the complete
                                Workflow_Validator findings list.

Design (design.md section 9):
- Chat sessions live in the WorkflowChatSessions DynamoDB table (TTL'd),
  holding the message history and the current canvas Workflow_Definition
  snapshot (stored in portal S3, referenced by ``current_definition_key``).
- Invocation uses the Bedrock Converse API with a ``create_workflow`` tool
  whose input schema IS the Workflow_Definition JSON Schema; the system
  prompt embeds the serialized node catalog (10.2).
- Follow-up prompts include the current canvas definition and instruct
  modification rather than regeneration (10.5).
- Tool output is parsed by the Workflow_Serializer and then run through the
  Workflow_Validator; the definition plus findings are returned for canvas
  rendering and review - never auto-saved or deployed (10.3).
- Bedrock_Configuration (model id, region, inference params, timeout <= 240 s)
  is read from the existing portal settings storage; the Lambda invokes with
  a client-side timeout equal to the configured value, and invocation
  failures/timeouts are returned as descriptive errors (10.6, 10.7).

Request body:
    {
        "usecase_id": "...",              required
        "prompt": "...",                  required
        "session_id": "...",              optional; omitted on the first prompt
        "current_definition": {...},      optional; canvas snapshot from the client
        "temperature": 0.1                optional; number in [0, 1] overriding the
                                          configured temperature for this invocation
                                          (top_p is then suppressed); outside the
                                          range -> 400 INVALID_TEMPERATURE
    }

Error envelope (design): {"error": {"code", "message", "details"}} with
403 RBAC denial (11.4) and 404s that avoid cross-tenant existence leaks.
"""
import copy
import importlib
import json
import os
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from decimal import Decimal
import boto3
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    get_usecase, rbac_manager, Permission
)
from workflow_core.serializer import (
    SCHEMA_VERSION,
    WORKFLOW_DEFINITION_SCHEMA,
    parse as parse_definition,
    serialize as serialize_graph,
)
from workflow_core.validator import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    validate as run_validator,
)
from workflow_core.catalog import NODE_CATALOG

# Node catalog wire serialization is shared with the node-catalog endpoint
# (workflow_validation.py lives in the same Lambda bundle).
from workflow_validation import descriptor_to_wire

# Generation_Gate (portal-build-fleet-and-workflow-gates Requirement 8):
# pure classification/decision logic in the same Lambda bundle. Every
# generated definition passes the gate before it is returned or persisted;
# session persistence happens only on accept paths.
from generation_gate import (
    ACTION_ACCEPT,
    ACTION_REJECT,
    ACTION_REPAIR,
    build_repair_message,
    classify as gate_classify,
    user_readable_errors,
)

# Shared Bedrock_Configuration resolution, client cache, and inference
# config (custom-node-code-assist Requirement 4.1): bedrock_common.py lives
# in the same Lambda bundle, so this is a same-directory import exactly like
# `from workflow_validation import …` above. The module is reloaded so it
# rebinds its environment (SETTINGS_TABLE) and boto3 resource whenever this
# module is (re-)imported — preserving the previous behavior where these
# bindings lived here and were refreshed on every import of
# workflow_generator (the test suites re-import this module per fixture
# after repointing SETTINGS_TABLE). A single extra exec at Lambda cold
# start; a no-op behaviorally in production.
import bedrock_common
bedrock_common = importlib.reload(bedrock_common)
from bedrock_common import (
    BEDROCK_CONFIG_SETTING_KEY,
    DEFAULT_BEDROCK_CONFIG,
    MAX_TIMEOUT_SECONDS,
    build_inference_config,
    get_bedrock_client,
    get_bedrock_configuration,
)

# Code_Assist_Generator (custom-node-code-assist Requirement 2.1):
# code_assist.py lives in the same Lambda bundle and serves POST
# /code-assist, dispatched from handler() below. Reloaded AFTER the
# bedrock_common reload above so its own `from bedrock_common import ...`
# bindings always reference the freshly reloaded module (the test suites
# re-import this module per fixture after repointing SETTINGS_TABLE).
import code_assist
code_assist = importlib.reload(code_assist)

# Merged Node_Type_Catalog resolution (custom-node-designer task 9.2):
# generation embeds the Use_Case's merged palette catalog in the system
# prompt so generated workflows may use registered Custom_Node_Types
# (same palette rules as GET /workflows/node-catalog: test/prod only,
# deprecated excluded).
from node_catalog_resolution import palette_catalog_for_usecase

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

# Environment variables (SETTINGS_TABLE moved to bedrock_common)
WORKFLOW_CHAT_SESSIONS_TABLE = os.environ.get('WORKFLOW_CHAT_SESSIONS_TABLE')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
WORKFLOWS_S3_PREFIX = os.environ.get('WORKFLOWS_S3_PREFIX', 'workflows')

# BEDROCK_CONFIG_SETTING_KEY, MAX_TIMEOUT_SECONDS, and
# DEFAULT_BEDROCK_CONFIG now live in bedrock_common (re-exported above,
# values unchanged - custom-node-code-assist Requirement 4.1).

# Chat sessions expire after 24 hours (DynamoDB TTL attribute 'ttl');
# the TTL is refreshed on every message.
SESSION_TTL_SECONDS = 24 * 60 * 60

# Cap of prior conversation turns replayed to the model per invocation.
MAX_HISTORY_MESSAGES = 20

TOOL_NAME = 'create_workflow'

# Serialized node catalog is static; built once per container.
_catalog_json_cache: Optional[str] = None


def decimal_to_native(obj):
    """Convert Decimal objects from DynamoDB to native Python types"""
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    elif isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [decimal_to_native(i) for i in obj]
    return obj


def error_response(status_code: int, code: str, message: str, details: Optional[Dict] = None) -> Dict:
    """Build the Workflow Manager error envelope: {error: {code, message, details}}"""
    return create_response(status_code, {
        'error': {
            'code': code,
            'message': message,
            'details': details or {}
        }
    })


def now_ms() -> int:
    return int(datetime.utcnow().timestamp() * 1000)


def has_workflow_permission(user: Dict, usecase_id: str, permission: Permission) -> bool:
    """Check a workflow permission for the acting user on a Use_Case"""
    return rbac_manager.has_permission(user['user_id'], usecase_id, permission, user_info=user)


def forbidden_response(user: Dict, event: Dict, usecase_id: str, permissions: List[Permission]) -> Dict:
    """Uniform 403 authorization error with a denied-access audit entry (11.4)"""
    log_audit_event(
        user_id=user['user_id'],
        action='unauthorized_access',
        resource_type='workflow',
        resource_id=event.get('resource', 'unknown'),
        result='denied',
        details={
            'required_permissions': [p.value for p in permissions],
            'usecase_id': usecase_id,
            'method': event.get('httpMethod'),
            'path': event.get('path')
        }
    )
    return error_response(403, 'FORBIDDEN', 'Insufficient permissions', {
        'required_permissions': [p.value for p in permissions],
        'usecase_id': usecase_id
    })


def parse_body(event: Dict) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Parse the request body; returns (body, None) or (None, error_response)"""
    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        return None, error_response(400, 'INVALID_JSON', 'Request body is not valid JSON')
    if not isinstance(body, dict):
        return None, error_response(400, 'INVALID_JSON', 'Request body must be a JSON object')
    return body, None


# --------------------------------------------------------------------------
# Bedrock_Configuration (Requirements 10.6, 10.7)
#
# get_bedrock_configuration and get_bedrock_client now live in
# bedrock_common (imported above with unchanged semantics); node_generator
# keeps importing them from this module.
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Prompt and tool assembly (Requirements 10.2, 10.5)
# --------------------------------------------------------------------------

def serialized_catalog_json(catalog=NODE_CATALOG) -> str:
    """A node type catalog in its camelCase wire form, as a JSON string.

    Defaults to the static built-in catalog (cached per container); a
    merged catalog carrying a Use_Case's Custom_Node_Types is serialized
    per invocation (task 9.2).
    """
    global _catalog_json_cache
    if catalog is NODE_CATALOG:
        if _catalog_json_cache is None:
            _catalog_json_cache = json.dumps(
                [descriptor_to_wire(d) for d in NODE_CATALOG],
                sort_keys=True
            )
        return _catalog_json_cache
    return json.dumps([descriptor_to_wire(d) for d in catalog],
                      sort_keys=True)


def create_workflow_tool_schema() -> Dict:
    """
    The create_workflow tool input schema IS the Workflow_Definition JSON
    Schema (design section 9), minus the metadata keywords the Converse API
    does not need.
    """
    schema = copy.deepcopy(WORKFLOW_DEFINITION_SCHEMA)
    schema.pop('$schema', None)
    schema.pop('$id', None)
    return schema


def build_tool_config() -> Dict:
    """Converse toolConfig forcing structured output through create_workflow"""
    return {
        'tools': [{
            'toolSpec': {
                'name': TOOL_NAME,
                'description': (
                    'Return the complete Workflow_Definition graph document '
                    'that fulfils the user request. Always call this tool '
                    'with the full definition (all nodes, parameters, '
                    'positions, and connections).'
                ),
                'inputSchema': {'json': create_workflow_tool_schema()}
            }
        }],
        'toolChoice': {'tool': {'name': TOOL_NAME}}
    }


def build_system_prompt(catalog=NODE_CATALOG) -> str:
    """System prompt embedding the serialized node catalog (Requirement
    10.2). ``catalog`` is the effective (possibly merged) catalog for the
    requesting Use_Case (custom-node-designer task 9.2)."""
    return (
        'You are the workflow generation assistant of the DDA edge computer '
        'vision portal. Users describe video analytics pipelines in natural '
        'language; you compose them as Workflow_Definition graph documents.\n'
        '\n'
        'Rules:\n'
        '- Use ONLY node types from the NODE TYPE CATALOG below; the "typeId" '
        'field is the value for a node\'s "type".\n'
        '- Every workflow needs at least one input-category node and at least '
        'one output-category node.\n'
        '- Set every required parameter to a value satisfying its declared '
        'type and constraints; keep catalog defaults unless the user asks '
        'otherwise.\n'
        '- Connections join an output port of one node ("from") to an input '
        'port of another node ("to"); the two port types must be compatible. '
        'Port names and types are declared in the catalog.\n'
        '- The graph must be acyclic and every node must be reachable from an '
        'input node.\n'
        '- Give nodes short unique ids (n1, n2, ...) and connections unique '
        'ids (c1, c2, ...). Lay node positions out left to right in '
        'processing order (roughly 250 px horizontal spacing).\n'
        f'- "schemaVersion" is always {SCHEMA_VERSION}.\n'
        '- Always respond by calling the create_workflow tool with the '
        'complete definition. Do not answer with prose only.\n'
        '- When a CURRENT CANVAS WORKFLOW DEFINITION is provided, apply the '
        'requested modification to it and return the complete modified '
        'definition; do not regenerate an unrelated workflow from scratch.\n'
        '\n'
        f'NODE TYPE CATALOG (JSON):\n{serialized_catalog_json(catalog)}'
    )


def build_user_message(prompt: str, current_definition_json: Optional[str]) -> str:
    """
    The user turn sent to the model. Follow-up prompts (and first prompts
    over a non-empty canvas) include the current canvas definition and
    instruct modification rather than regeneration (Requirement 10.5).
    """
    if not current_definition_json:
        return prompt
    return (
        f'{prompt}\n'
        '\n'
        'CURRENT CANVAS WORKFLOW DEFINITION (JSON):\n'
        f'{current_definition_json}\n'
        '\n'
        'Apply the requested change to this current definition rather than '
        'generating a new workflow from scratch, and return the complete '
        'modified Workflow_Definition via the create_workflow tool.'
    )


# --------------------------------------------------------------------------
# Chat sessions (WorkflowChatSessions, TTL'd)
# --------------------------------------------------------------------------

def snapshot_s3_key(usecase_id: str, session_id: str) -> str:
    """S3 key of a session's current canvas definition snapshot"""
    return f"{WORKFLOWS_S3_PREFIX}/{usecase_id}/chat-sessions/{session_id}/current_definition.json"


def get_session(session_id: str) -> Optional[Dict]:
    """Fetch a chat session item, or None (expired items may still be absent)"""
    table = dynamodb.Table(WORKFLOW_CHAT_SESSIONS_TABLE)
    response = table.get_item(Key={'session_id': session_id})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def save_session(session: Dict) -> None:
    """Persist a chat session with a refreshed TTL"""
    session['ttl'] = int(time.time()) + SESSION_TTL_SECONDS
    session['updated_at'] = now_ms()
    dynamodb.Table(WORKFLOW_CHAT_SESSIONS_TABLE).put_item(Item=session)


def load_snapshot(s3_key: str) -> Optional[Dict]:
    """Load a stored canvas definition snapshot from portal S3, or None"""
    try:
        response = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=s3_key)
        return json.loads(response['Body'].read().decode('utf-8'))
    except ClientError as e:
        logger.warning(f"Could not load session snapshot {s3_key}: {str(e)}")
        return None


def put_snapshot(s3_key: str, canonical_json: str) -> None:
    """Store the current canvas definition snapshot in portal S3"""
    s3.put_object(
        Bucket=PORTAL_ARTIFACTS_BUCKET,
        Key=s3_key,
        Body=canonical_json.encode('utf-8'),
        ContentType='application/json'
    )


# --------------------------------------------------------------------------
# Bedrock invocation (Requirements 10.2, 10.7)
# --------------------------------------------------------------------------

def converse_messages(history: List[Dict], user_text: str) -> List[Dict]:
    """Converse-format message list: replayed history plus the new user turn"""
    messages = [
        {'role': m['role'], 'content': [{'text': m['text']}]}
        for m in history[-MAX_HISTORY_MESSAGES:]
    ]
    messages.append({'role': 'user', 'content': [{'text': user_text}]})
    return messages


def invoke_generation(config: Dict, messages: List[Dict],
                      catalog=NODE_CATALOG) -> Tuple[Optional[Dict], Optional[str], Optional[Dict]]:
    """
    Invoke the configured Bedrock model via the Converse API with the
    effective (possibly merged) node type catalog in the system prompt.

    Returns (tool_input, assistant_text, None) on success or
    (None, None, error_response). Timeouts and invocation failures are
    returned as descriptive errors with the prompt preserved client-side
    (Requirement 10.7).
    """
    client = get_bedrock_client(config['region'], config['timeout_seconds'])
    # Never send temperature and top_p together: recent Anthropic models
    # (e.g. claude-sonnet-4-5) reject requests specifying both. Temperature
    # wins when set; top_p is sent only when temperature is absent/None
    # (bedrock_common.build_inference_config, Requirements 4.2, 4.3).
    inference_config = build_inference_config(config)
    try:
        response = client.converse(
            modelId=config['model_id'],
            system=[{'text': build_system_prompt(catalog)}],
            messages=messages,
            inferenceConfig=inference_config,
            toolConfig=build_tool_config()
        )
    except (ReadTimeoutError, ConnectTimeoutError):
        logger.error(f"Bedrock invocation exceeded the configured timeout "
                     f"({config['timeout_seconds']}s, model {config['model_id']})")
        return None, None, error_response(
            504, 'GENERATION_TIMEOUT',
            f"Workflow generation timed out after {config['timeout_seconds']} seconds. "
            'Your prompt was not lost - please retry.',
            {'timeout_seconds': config['timeout_seconds'], 'model_id': config['model_id']}
        )
    except EndpointConnectionError as e:
        logger.error(f"Bedrock endpoint unreachable: {str(e)}")
        return None, None, error_response(
            502, 'BEDROCK_UNREACHABLE',
            f"The Bedrock endpoint in region {config['region']} could not be reached. "
            'Check the Bedrock configuration.',
            {'region': config['region']}
        )
    except ClientError as e:
        error = e.response.get('Error', {})
        logger.error(f"Bedrock invocation failed: {error.get('Code')}: {error.get('Message')}")
        return None, None, error_response(
            502, 'BEDROCK_INVOCATION_FAILED',
            f"The Bedrock model invocation failed: {error.get('Message', 'unknown error')}",
            {'bedrock_error_code': error.get('Code'), 'model_id': config['model_id']}
        )

    content = (response.get('output', {}).get('message', {}) or {}).get('content', [])
    tool_input = None
    text_parts: List[str] = []
    for block in content:
        if 'toolUse' in block and block['toolUse'].get('name') == TOOL_NAME:
            tool_input = block['toolUse'].get('input')
        elif 'text' in block:
            text_parts.append(block['text'])
    assistant_text = '\n'.join(text_parts).strip()

    if not isinstance(tool_input, dict):
        logger.error(f"Model returned no {TOOL_NAME} tool call "
                     f"(stopReason={response.get('stopReason')})")
        return None, None, error_response(
            502, 'NO_WORKFLOW_RETURNED',
            'The model did not return a workflow definition. Please retry or rephrase the prompt.',
            {'stop_reason': response.get('stopReason'), 'model_text': assistant_text[:500]}
        )
    return tool_input, assistant_text, None


# --------------------------------------------------------------------------
# Generation_Gate response shaping (Requirements 8.3, 8.5, 8.6, 8.8)
# --------------------------------------------------------------------------

def gate_metadata(decision, repaired: bool = False,
                  corrected_errors: Optional[List[Dict]] = None) -> Dict:
    """The ``gate`` object attached to accept-path responses (design §8).

    ``repaired``/``corrected_errors`` are supplied by the Repair_Pass
    (task 5.2) when an automatic correction was applied (Req 8.6).
    """
    return {
        'passed': True,
        'repaired': repaired,
        'corrected_errors': corrected_errors or [],
        'structural_error_codes': sorted({
            e.get('code') for e in (corrected_errors or []) if e.get('code')
        }),
    }


def generation_rejected_response(structural_errors: List[Dict],
                                 definition: Dict,
                                 repair_attempted: bool) -> Dict:
    """The 422 GENERATION_REJECTED envelope (Req 8.5, 8.7, 8.8).

    Returned strictly before any session mutation, so the session canvas
    snapshot and chat message history keep their prior state (8.9) and
    the client retries with the preserved prompt.
    """
    return error_response(
        422, 'GENERATION_REJECTED',
        'The generated workflow has structural errors that prevent it from '
        'working, so it was not applied. The canvas was left unchanged - '
        'please retry or rephrase the prompt.',
        {
            'structural_errors': user_readable_errors(structural_errors,
                                                      definition),
            'repair_attempted': repair_attempted,
            'prompt_preserved': True,
        }
    )


# --------------------------------------------------------------------------
# Generate endpoint
# --------------------------------------------------------------------------

def generate_workflow(event: Dict, user: Dict) -> Dict:
    """
    POST /workflows/generate
    Body: {usecase_id, prompt, session_id?, current_definition?}

    Invokes the configured Bedrock model with the prompt and the node type
    catalog and returns a Workflow_Definition (10.2) together with the
    complete Workflow_Validator findings list for user review on the canvas.
    The result is never auto-saved or deployed (10.3). Follow-up prompts in
    the same session modify the current canvas definition (10.5). Parse and
    invocation failures return descriptive errors and persist no session
    changes, so the canvas stays untouched and the prompt can be retried
    (10.4, 10.7).

    Every generated definition passes the Generation_Gate before it is
    returned or persisted (Req 8.1): structural rejections return 422
    GENERATION_REJECTED, validator failures return 422
    GENERATION_VALIDATION_INCOMPLETE, and both leave the chat session and
    canvas snapshot untouched (8.5, 8.9, 8.11). Session persistence
    (put_snapshot + save_session) executes only on accept paths.
    """
    body, err = parse_body(event)
    if err:
        return err

    usecase_id = body.get('usecase_id')
    prompt = body.get('prompt')
    missing = [f for f in ('usecase_id', 'prompt') if not body.get(f)]
    if missing:
        return error_response(400, 'MISSING_FIELDS',
                              f"Missing required fields: {', '.join(missing)}")
    if not isinstance(prompt, str) or not prompt.strip():
        return error_response(400, 'INVALID_PROMPT', 'prompt must be a non-empty string')

    # Optional per-invocation sampling override: a number in [0, 1]. When
    # present it replaces the configured/settings temperature for this
    # invocation only (and, per the existing rule, suppresses top_p -
    # temperature and top_p are never sent together).
    temperature_override = body.get('temperature')
    if temperature_override is not None:
        if isinstance(temperature_override, bool) \
                or not isinstance(temperature_override, (int, float)) \
                or not (0 <= temperature_override <= 1):
            return error_response(
                400, 'INVALID_TEMPERATURE',
                'temperature must be a number between 0 and 1',
                {'temperature': temperature_override})

    # Generation drafts workflow content, so it requires the create/edit
    # permission (DataScientist or UseCaseAdmin per the design RBAC matrix).
    if not (has_workflow_permission(user, usecase_id, Permission.WORKFLOW_CREATE)
            or has_workflow_permission(user, usecase_id, Permission.WORKFLOW_EDIT)):
        return forbidden_response(user, event, usecase_id,
                                  [Permission.WORKFLOW_CREATE, Permission.WORKFLOW_EDIT])

    try:
        get_usecase(usecase_id)
    except ValueError:
        return error_response(404, 'USECASE_NOT_FOUND', 'Use case not found')

    # ---------------------------------------------------------------- session
    session_id = body.get('session_id')
    session: Optional[Dict] = None
    if session_id:
        session = get_session(str(session_id))
        # Expired/unknown sessions and sessions of other users or Use_Cases
        # all yield the same 404, so existence is never leaked.
        if not session or session.get('user_id') != user['user_id'] \
                or session.get('usecase_id') != usecase_id:
            return error_response(404, 'SESSION_NOT_FOUND',
                                  'Chat session not found or expired')
    else:
        session_id = str(uuid.uuid4())
        session = {
            'session_id': session_id,
            'usecase_id': usecase_id,
            'user_id': user['user_id'],
            'messages': [],
            'current_definition_key': None,
            'created_at': now_ms(),
        }

    # ------------------------------------------- current canvas definition
    # The client-provided snapshot is authoritative (the user may have edited
    # the canvas since the last turn); otherwise fall back to the session's
    # stored snapshot (10.5).
    current_definition_json: Optional[str] = None
    provided = body.get('current_definition')
    if provided is not None:
        try:
            raw = provided if isinstance(provided, str) else json.dumps(provided)
        except (TypeError, ValueError) as exc:
            return error_response(400, 'INVALID_CURRENT_DEFINITION',
                                  f'current_definition is not JSON-serializable: {exc}')
        result = parse_definition(raw)
        if not result.ok:
            return error_response(400, 'INVALID_CURRENT_DEFINITION',
                                  f'current_definition is not a valid Workflow_Definition: '
                                  f'{result.error.message}',
                                  {'code': result.error.code, 'path': result.error.path})
        current_definition_json = serialize_graph(result.graph)
    elif session.get('current_definition_key'):
        snapshot = load_snapshot(session['current_definition_key'])
        if snapshot is not None:
            current_definition_json = json.dumps(snapshot, sort_keys=True)

    # ------------------------------------------------------------ invocation
    config = get_bedrock_configuration()
    if temperature_override is not None:
        # Request-scoped override of the configured temperature; setting a
        # non-null temperature makes invoke_generation send temperature
        # only, suppressing top_p (Anthropic models reject both together).
        config['temperature'] = float(temperature_override)
    user_text = build_user_message(prompt.strip(), current_definition_json)
    history = session.get('messages') or []
    messages = converse_messages(history, user_text)

    # The Use_Case's merged palette catalog (built-ins + registered
    # Custom_Node_Types in test/prod, deprecated excluded) drives both the
    # system prompt and the validation of the generated output (task 9.2).
    catalog, _markers = palette_catalog_for_usecase(usecase_id)

    tool_input, assistant_text, err = invoke_generation(config, messages,
                                                        catalog=catalog)
    if err:
        # No session mutation on failure: the canvas stays untouched and the
        # client retries with the preserved prompt (10.4, 10.7).
        return err

    # ------------------------------------------------- parse + validate (10.3)
    # The unparseable-output rejection precedes any session mutation:
    # persistence only happens on the gate's accept path below (8.10).
    result = parse_definition(json.dumps(tool_input))
    if not result.ok:
        logger.error(f"Generated definition failed to parse: "
                     f"{result.error.code} at {result.error.path}: {result.error.message}")
        return error_response(
            422, 'GENERATED_DEFINITION_INVALID',
            'The generated output could not be parsed into a valid '
            f'Workflow_Definition: {result.error.message}. The canvas was left '
            'unchanged - please retry or rephrase the prompt.',
            {'code': result.error.code, 'path': result.error.path}
        )

    canonical_json = serialize_graph(result.graph)
    definition = json.loads(canonical_json)

    # ------------------------------------------------ Generation_Gate (Req 8)
    # Full Workflow_Validator run against the same merged catalog, wrapped
    # fail-closed: a validator that cannot complete rejects the generation
    # with the session untouched (8.11). The complete findings list
    # accompanies any accepted definition so the user reviews it on the
    # canvas before any save (10.3, 8.3).
    try:
        findings = run_validator(result.graph, catalog=catalog)
        decision = gate_classify(findings, catalog)
    except Exception as exc:  # fail closed (8.11)
        logger.error(f"Workflow_Validator failed to complete on the generated "
                     f"definition: {exc}", exc_info=True)
        return error_response(
            422, 'GENERATION_VALIDATION_INCOMPLETE',
            'The generated workflow could not be validated, so it was not '
            'applied. The canvas was left unchanged - please retry the prompt.',
            {'prompt_preserved': True}
        )

    gate_repaired = False
    gate_corrected_errors: Optional[List[Dict]] = None

    if decision.action == ACTION_REPAIR:
        # ------------------------------------------- Repair_Pass (Req 8.4)
        # Exactly one automatic re-invocation carrying the failed
        # definition and per-error correction instructions, appended as
        # one additional user turn to the same Converse message list.
        # Never more than one pass per generation request; every failure
        # mode rejects fail-closed with the session untouched (8.7, 8.9).
        original_errors = decision.structural_errors
        original_definition = definition

        repair_messages = messages + [
            {'role': 'user', 'content': [{
                'text': build_repair_message(canonical_json, original_errors)
            }]},
        ]
        repaired_input, repaired_text, repair_err = invoke_generation(
            config, repair_messages, catalog=catalog)
        if repair_err:
            # Repair invocation failed: reject with the ORIGINAL
            # Structural_Errors (the Repair_Pass did not complete, 8.7).
            return generation_rejected_response(
                original_errors, original_definition, repair_attempted=True)

        repaired_result = parse_definition(json.dumps(repaired_input))
        if not repaired_result.ok:
            # Repair output unparseable: the Repair_Pass did not complete,
            # so reject with the ORIGINAL Structural_Errors (8.7, 8.10).
            logger.error(
                f"Repair_Pass output failed to parse: "
                f"{repaired_result.error.code} at {repaired_result.error.path}: "
                f"{repaired_result.error.message}")
            return generation_rejected_response(
                original_errors, original_definition, repair_attempted=True)

        repaired_canonical_json = serialize_graph(repaired_result.graph)
        repaired_definition = json.loads(repaired_canonical_json)

        try:
            repaired_findings = run_validator(repaired_result.graph,
                                              catalog=catalog)
            repaired_decision = gate_classify(repaired_findings, catalog)
        except Exception as exc:  # fail closed (8.11)
            # Validation of the repair result could not complete: the
            # Repair_Pass did not complete, so reject with the ORIGINAL
            # Structural_Errors (8.7).
            logger.error(f"Workflow_Validator failed to complete on the "
                         f"Repair_Pass result: {exc}", exc_info=True)
            return generation_rejected_response(
                original_errors, original_definition, repair_attempted=True)

        if repaired_decision.action != ACTION_ACCEPT:
            # Result still structurally broken: reject with the REMAINING
            # Structural_Errors — no second Repair_Pass, ever (8.4, 8.7).
            return generation_rejected_response(
                repaired_decision.structural_errors, repaired_definition,
                repair_attempted=True)

        # Clean repaired result: continue on the accept path below with
        # the repaired definition, its complete findings, and the repair
        # indication (8.6). Only the original user turn and the final
        # assistant text are persisted — repair-internal turns are not.
        gate_repaired = True
        gate_corrected_errors = original_errors
        decision = repaired_decision
        canonical_json = repaired_canonical_json
        definition = repaired_definition
        assistant_text = repaired_text

    elif decision.action == ACTION_REJECT:
        # Unrepairable Structural_Errors: reject before any session
        # mutation — snapshot and message history keep their prior state
        # and the client retries with the preserved prompt (8.5, 8.9).
        return generation_rejected_response(
            decision.structural_errors, definition, repair_attempted=False)

    # ------------------------------------------------------ accept path (8.3)
    wire_findings = decision.all_findings
    error_count = sum(1 for f in wire_findings
                      if f.get('severity') == SEVERITY_ERROR)
    warning_count = sum(1 for f in wire_findings
                        if f.get('severity') == SEVERITY_WARNING)

    # ------------------------------------------------------- persist session
    # Persistence executes only on the gate's accept path (8.1, 8.9): the
    # accepted definition becomes the session's canvas snapshot for
    # follow-up modification prompts (10.5).
    snapshot_key = snapshot_s3_key(usecase_id, session_id)
    put_snapshot(snapshot_key, canonical_json)

    timestamp = now_ms()
    session['messages'] = (history + [
        {'role': 'user', 'text': user_text, 'at': timestamp},
        {'role': 'assistant',
         'text': assistant_text or 'Produced a workflow definition (see the current canvas definition).',
         'at': timestamp},
    ])[-MAX_HISTORY_MESSAGES * 2:]
    session['current_definition_key'] = snapshot_key
    save_session(session)

    return create_response(200, {
        'session_id': session_id,
        'usecase_id': usecase_id,
        'definition': definition,
        'findings': wire_findings,
        'error_count': error_count,
        'warning_count': warning_count,
        'validation_passed': error_count == 0,
        'assistant_text': assistant_text,
        'model_id': config['model_id'],
        'gate': gate_metadata(decision, repaired=gate_repaired,
                              corrected_errors=gate_corrected_errors),
    })


def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler - routes to the appropriate operation"""
    try:
        http_method = event.get('httpMethod')

        # Handle CORS preflight requests
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }

        user = get_user_from_event(event)
        resource = event.get('resource', '')

        if resource == '/workflows/generate' and http_method == 'POST':
            return generate_workflow(event, user)

        # Code assistance for custom Python node modules
        # (custom-node-code-assist Requirement 2.1). Unexpected exceptions
        # fall through to the 500 INTERNAL_ERROR guard below.
        if resource == '/code-assist' and http_method == 'POST':
            return code_assist.handle_code_assist(event, user)

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
