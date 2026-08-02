"""
Node_Generator API Lambda function (Custom Node Designer)

Prompt-based Plugin_Scaffold generation via Amazon Bedrock
(Node_Generator, Requirements 2.1, 2.2, 2.4, 2.5, 2.6, 2.7).

Routes (API Gateway REST) - start/poll pattern. Bedrock generation takes
45-50 s, which exceeds the API Gateway REST 29 s integration cap, so the
start routes return 202 immediately and the actual generation turn runs in
an asynchronous self-invocation of this Lambda (InvocationType='Event');
the frontend polls the status route until the turn settles (mirrors the
Plugin_Simulator start/poll flow):

    POST /plugins/generate                     Start a scaffold-generation chat
                                               session: validate the request,
                                               persist the session with
                                               turn_status=pending, dispatch the
                                               generation worker, and return 202
                                               with the session id.
    GET  /plugins/generate/{session}           Poll the current generation turn:
                                               {turn_status: pending|running|
                                               completed|failed}; completed adds
                                               the generated files + assistant
                                               text (the former synchronous
                                               response shape), failed adds
                                               turn_error {code, message,
                                               details, http_status}.
    POST /plugins/generate/{session}/message   Continue a session: either a
                                               follow-up prompt modifying the
                                               current generated source (2.4,
                                               async, 202 + poll like the start
                                               route), or {"accept": true} to
                                               accept the current source into a
                                               Plugin_Record entering the standard
                                               build/simulate/lifecycle path with
                                               the generation prompt recorded as
                                               provenance (2.5, synchronous).

Design (design.md "Plugin_Scaffold and Node_Generator") - mirrors
workflow_generator.py:
- Chat sessions live in the TTL'd NodeGenSessions DynamoDB table, holding
  the message history and the declaration; the current scaffold source
  snapshot is stored in portal S3, referenced by ``current_source_key``.
- Invocation uses the Bedrock Converse API with a forced
  ``create_plugin_scaffold`` tool whose input schema is the scaffold file
  map ({files: {path: content}}) plus the declaration; the system prompt
  embeds the scaffold template conventions (the rendered reference
  scaffold for the declaration) and the Frame_Processing_Hook contract
  (2.2).
- Bedrock_Configuration handling is reused verbatim from
  workflow_generator.get_bedrock_configuration(): model id, region, and
  inference parameters from the portal settings table, invocation timeout
  clamped to at most 60 seconds, cached bedrock-runtime client with a
  client-side read timeout and retries disabled (2.7).
- Follow-up prompts include the current generated source and instruct
  modification rather than regeneration (2.4).
- Tool output failing scaffold validation (workflow_core.scaffold.
  validate_scaffold) marks the turn failed with a descriptive error in the
  poll response without touching the session's message history or source
  snapshot, so the user's prompt is preserved for retry (2.6); Bedrock
  failures/timeouts likewise surface descriptive errors through the poll
  with no history/snapshot mutation (2.7).
- Accepted source is written under plugin-sources/{usecase}/{plugin}/1/
  and a Plugin_Record (kind "generated", Lifecycle_State dev, review
  pending) is created with the generation prompt(s) recorded as
  provenance, entering the standard build/simulate/lifecycle path (2.5).

Error envelope: {"error": {"code", "message", "details"}} with 403 RBAC
denial (13.4) and 404s that avoid cross-tenant existence leaks.
"""
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
from workflow_core.scaffold import (
    HOOK_FILE,
    ScaffoldError,
    render_scaffold,
    scaffold_defects,
)

# Bedrock_Configuration and client handling are shared with the workflow
# generator (workflow_generator.py lives in the same Lambda bundle):
# settings-table configuration, timeout clamped <= 60 s, cached client
# with a client-side read timeout and no retries (Requirement 2.7).
from workflow_generator import get_bedrock_client, get_bedrock_configuration

# Plugin_Record construction is shared with the records API
# (plugin_records.py lives in the same Lambda bundle) so accepted source
# enters the identical dev/pending record shape (Requirement 2.5).
from plugin_records import (
    new_version_item,
    plugin_table,
    source_s3_prefix,
    version_detail,
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')
lambda_client = boto3.client('lambda')

# Environment variables
NODE_GEN_SESSIONS_TABLE = os.environ.get('NODE_GEN_SESSIONS_TABLE')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
PLUGIN_SOURCES_PREFIX = os.environ.get('PLUGIN_SOURCES_PREFIX', 'plugin-sources')

# Chat sessions expire after 24 hours (DynamoDB TTL attribute 'ttl');
# the TTL is refreshed on every message (mirrors WorkflowChatSessions).
SESSION_TTL_SECONDS = 24 * 60 * 60

# Cap of prior conversation turns replayed to the model per invocation.
# History carries the raw prompts and assistant commentary only; the
# current scaffold source is embedded once, in the new user turn (2.4).
MAX_HISTORY_MESSAGES = 20

TOOL_NAME = 'create_plugin_scaffold'

# Generation-turn states carried on the session record (start/poll flow).
TURN_PENDING = 'pending'
TURN_RUNNING = 'running'
TURN_COMPLETED = 'completed'
TURN_FAILED = 'failed'
TURN_IN_PROGRESS = (TURN_PENDING, TURN_RUNNING)


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
    """Build the error envelope: {error: {code, message, details}}"""
    return create_response(status_code, {
        'error': {
            'code': code,
            'message': message,
            'details': details or {}
        }
    })


def now_ms() -> int:
    return int(datetime.utcnow().timestamp() * 1000)


def parse_body(event: Dict) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Parse the request body; returns (body, None) or (None, error_response)"""
    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        return None, error_response(400, 'INVALID_JSON', 'Request body is not valid JSON')
    if not isinstance(body, dict):
        return None, error_response(400, 'INVALID_JSON', 'Request body must be a JSON object')
    return body, None


def can_generate(user: Dict, usecase_id: str) -> bool:
    """node-designer:generate - UseCaseAdmin within the Use_Case or
    PortalAdmin (Requirement 13.1)"""
    return rbac_manager.has_permission(
        user['user_id'], usecase_id,
        Permission.NODE_DESIGNER_GENERATE, user_info=user)


def forbidden_response(user: Dict, event: Dict, usecase_id: str) -> Dict:
    """Standard authorization error envelope with a denied-access audit
    entry (13.4), matching the node-designer feature area's shape"""
    log_audit_event(
        user_id=user['user_id'],
        action='unauthorized_access',
        resource_type='plugin_scaffold_generation',
        resource_id=event.get('resource', 'unknown'),
        result='denied',
        details={
            'required_permissions': [Permission.NODE_DESIGNER_GENERATE.value],
            'usecase_id': usecase_id,
            'method': event.get('httpMethod'),
            'path': event.get('path')
        }
    )
    return error_response(403, 'FORBIDDEN', 'Insufficient permissions', {
        'required_permissions': [Permission.NODE_DESIGNER_GENERATE.value],
        'usecase_id': usecase_id
    })


# --------------------------------------------------------------------------
# Prompt and tool assembly (Requirements 2.2, 2.4)
# --------------------------------------------------------------------------

def build_tool_config(declaration: Dict, reference_files: Dict) -> Dict:
    """
    Converse toolConfig forcing structured output through the
    create_plugin_scaffold tool. The input schema is the scaffold file map
    ({files: {path: content}}) plus the declaration: every file of the
    reference scaffold rendered for the declaration is a required string
    property, so the model must return a complete buildable scaffold
    (design "Plugin_Scaffold and Node_Generator").
    """
    required_paths = sorted(reference_files)
    schema = {
        'type': 'object',
        'properties': {
            'files': {
                'type': 'object',
                'description': (
                    'Complete Plugin_Scaffold source as a file map: relative '
                    'path -> full file content. Must contain every file of '
                    'the scaffold layout: ' + ', '.join(required_paths)
                ),
                'properties': {
                    path: {'type': 'string'} for path in required_paths
                },
                'required': required_paths,
                'additionalProperties': {'type': 'string'},
            },
            'declaration': {
                'type': 'object',
                'description': (
                    'The Custom_Node_Type declaration this scaffold '
                    'implements (echo the CUSTOM NODE TYPE DECLARATION '
                    'from the system prompt unchanged).'
                ),
            },
        },
        'required': ['files'],
    }
    return {
        'tools': [{
            'toolSpec': {
                'name': TOOL_NAME,
                'description': (
                    'Return the complete Plugin_Scaffold source that '
                    'fulfils the user request. Always call this tool with '
                    'the full file map (every scaffold file with its '
                    'complete content).'
                ),
                'inputSchema': {'json': schema}
            }
        }],
        'toolChoice': {'tool': {'name': TOOL_NAME}}
    }


def build_system_prompt(declaration: Dict, reference_files: Dict) -> str:
    """
    System prompt embedding the scaffold template conventions (the
    reference scaffold rendered for the declaration) and the
    Frame_Processing_Hook contract (Requirement 2.2).
    """
    return (
        'You are the custom node generation assistant of the DDA edge '
        'computer vision portal. Users describe the per-frame processing '
        'behavior of a workflow node in natural language; you produce the '
        'complete Plugin_Scaffold source of a GStreamer plugin '
        'implementing that behavior.\n'
        '\n'
        'FRAME_PROCESSING_HOOK CONTRACT:\n'
        f'- The file {HOOK_FILE} MUST define '
        'process_frame(frame, params) -> frame.\n'
        '- "frame" is the raw frame payload (bytes) arriving at the '
        'node\'s input port; the returned bytes are emitted on the '
        'node\'s output port.\n'
        '- "params" is a dict carrying the current value of every '
        'parameter declared on the node type, keyed by the declared '
        'parameter name. Read parameters from it; never hard-code values '
        'the user may want to tune.\n'
        '- Implement the user-described behavior INSIDE process_frame '
        '(plus any helpers in the same file). Use only the Python '
        'standard library.\n'
        '\n'
        'SCAFFOLD TEMPLATE CONVENTIONS:\n'
        '- The scaffold is a file map {path: content} with exactly the '
        'layout of the reference scaffold below: the '
        'Frame_Processing_Hook file, the C skeleton element source, one '
        'meson.build build configuration per selected Target_Architecture, '
        'and the README.\n'
        '- The C skeleton element and the build configurations are '
        'generated boilerplate: reproduce them from the reference '
        'scaffold, changing them only when the requested behavior '
        'requires it. The scaffold must remain buildable.\n'
        '- Always respond by calling the create_plugin_scaffold tool with '
        'the complete file map. Do not answer with prose only.\n'
        '- When CURRENT PLUGIN SCAFFOLD SOURCE is provided, apply the '
        'requested modification to it and return the complete modified '
        'file map; do not regenerate an unrelated scaffold from scratch.\n'
        '\n'
        'CUSTOM NODE TYPE DECLARATION (JSON):\n'
        f'{json.dumps(declaration, sort_keys=True)}\n'
        '\n'
        'REFERENCE SCAFFOLD FOR THIS DECLARATION (JSON file map):\n'
        f'{json.dumps(reference_files, sort_keys=True)}'
    )


def build_user_message(prompt: str, current_source_json: Optional[str]) -> str:
    """
    The user turn sent to the model. Follow-up prompts include the current
    generated source and instruct modification rather than regeneration
    (Requirement 2.4).
    """
    if not current_source_json:
        return prompt
    return (
        f'{prompt}\n'
        '\n'
        'CURRENT PLUGIN SCAFFOLD SOURCE (JSON file map):\n'
        f'{current_source_json}\n'
        '\n'
        'Apply the requested change to this current scaffold source rather '
        'than generating new source from scratch, and return the complete '
        'modified file map via the create_plugin_scaffold tool.'
    )


# --------------------------------------------------------------------------
# Chat sessions (NodeGenSessions, TTL'd)
# --------------------------------------------------------------------------

def sessions_table():
    return dynamodb.Table(NODE_GEN_SESSIONS_TABLE)


def source_snapshot_s3_key(usecase_id: str, session_id: str) -> str:
    """S3 key of a session's current scaffold source snapshot"""
    return (f"{PLUGIN_SOURCES_PREFIX}/{usecase_id}/gen-sessions/"
            f"{session_id}/current_source.json")


def get_session(session_id: str) -> Optional[Dict]:
    """Fetch a chat session item, or None (expired items may still be absent)"""
    response = sessions_table().get_item(Key={'session_id': session_id})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def save_session(session: Dict) -> None:
    """Persist a chat session with a refreshed TTL"""
    session['ttl'] = int(time.time()) + SESSION_TTL_SECONDS
    session['updated_at'] = now_ms()
    sessions_table().put_item(Item=session)


def load_source_snapshot(s3_key: str) -> Optional[Dict]:
    """Load a stored scaffold source snapshot ({path: content}), or None"""
    try:
        response = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=s3_key)
        return json.loads(response['Body'].read().decode('utf-8'))
    except ClientError as e:
        logger.warning(f"Could not load session source snapshot {s3_key}: {str(e)}")
        return None


def put_source_snapshot(s3_key: str, files: Dict) -> None:
    """Store the current scaffold source snapshot in portal S3"""
    s3.put_object(
        Bucket=PORTAL_ARTIFACTS_BUCKET,
        Key=s3_key,
        Body=json.dumps(files, sort_keys=True).encode('utf-8'),
        ContentType='application/json'
    )


def session_prompts(session: Dict) -> List[str]:
    """Every user prompt of the session, in order (provenance, 2.5)"""
    return [m['text'] for m in (session.get('messages') or [])
            if m.get('role') == 'user']


def set_turn_state(session: Dict, status: str,
                   error: Optional[Dict] = None) -> None:
    """Persist the session's generation-turn state (start/poll flow).

    Only turn_status/turn_error change: a failed turn never mutates the
    message history or the source snapshot, so the user's prompt stays
    retryable (2.6, 2.7).
    """
    session['turn_status'] = status
    session['turn_error'] = error
    save_session(session)


def dispatch_generation_worker(session_id: str, prompt: str,
                               user: Dict) -> None:
    """
    Asynchronously self-invoke this Lambda (InvocationType='Event') with
    the worker payload: the generation turn runs outside the API Gateway
    29 s integration cap while the start route returns 202 immediately.
    """
    function_name = (os.environ.get('NODE_GENERATOR_FUNCTION_NAME')
                     or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'))
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType='Event',
        Payload=json.dumps({
            'node_gen_worker': True,
            'session_id': session_id,
            'prompt': prompt,
            'user': user,
        }).encode('utf-8'),
    )


def run_generation_worker(event: Dict) -> Dict:
    """
    Worker entry (async self-invocation): run one generation turn for the
    dispatched session and persist the outcome on the session record.
    Success is persisted by run_generation_turn (turn_status=completed with
    the source snapshot as the result pointer); any failure marks the turn
    failed with the error envelope contents in turn_error, leaving the
    message history and snapshot untouched (2.6, 2.7).
    """
    session_id = event.get('session_id')
    prompt = event.get('prompt')
    user = event.get('user') or {}
    session = get_session(str(session_id)) if session_id else None
    if not session:
        logger.error(f"Generation worker: session {session_id} not found/expired")
        return {'ok': False}
    try:
        set_turn_state(session, TURN_RUNNING)
        current_source = None
        if session.get('current_source_key'):
            # A follow-up turn modifies the current source snapshot (2.4).
            current_source = load_source_snapshot(session['current_source_key'])
            if current_source is None:
                set_turn_state(session, TURN_FAILED, {
                    'code': 'NO_CURRENT_SOURCE',
                    'message': 'The generated source snapshot is no longer '
                               'available; please retry the prompt.',
                    'details': {},
                    'http_status': 409,
                })
                return {'ok': False}
        result = run_generation_turn(session, prompt, current_source, user)
        if result.get('statusCode') == 200:
            return {'ok': True}
        body = json.loads(result.get('body') or '{}')
        error = body.get('error') or {}
        set_turn_state(session, TURN_FAILED, {
            'code': error.get('code', 'GENERATION_FAILED'),
            'message': error.get('message', 'Scaffold generation failed. '
                                 'Your prompt was not lost - please retry.'),
            'details': error.get('details') or {},
            'http_status': result.get('statusCode'),
        })
        return {'ok': False}
    except Exception as exc:
        logger.error(f"Generation worker failed unexpectedly: {str(exc)}",
                     exc_info=True)
        set_turn_state(session, TURN_FAILED, {
            'code': 'INTERNAL_ERROR',
            'message': 'Scaffold generation failed unexpectedly. '
                       'Your prompt was not lost - please retry.',
            'details': {},
            'http_status': 500,
        })
        return {'ok': False}


# --------------------------------------------------------------------------
# Bedrock invocation (Requirements 2.2, 2.7)
# --------------------------------------------------------------------------

def converse_messages(history: List[Dict], user_text: str) -> List[Dict]:
    """Converse-format message list: replayed history plus the new user turn"""
    messages = [
        {'role': m['role'], 'content': [{'text': m['text']}]}
        for m in history[-MAX_HISTORY_MESSAGES:]
    ]
    messages.append({'role': 'user', 'content': [{'text': user_text}]})
    return messages


def invoke_generation(config: Dict, system_prompt: str, messages: List[Dict],
                      tool_config: Dict) -> Tuple[Optional[Dict], Optional[str], Optional[Dict]]:
    """
    Invoke the configured Bedrock model via the Converse API.

    Returns (tool_input, assistant_text, None) on success or
    (None, None, error_response). Timeouts and invocation failures are
    returned as descriptive errors; the session is never mutated on
    failure, so the user's prompt is preserved for retry (Requirement 2.7).
    """
    client = get_bedrock_client(config['region'], config['timeout_seconds'])
    # Never send temperature and top_p together (recent Anthropic models
    # reject both); temperature wins when set - same rule as
    # workflow_generator.invoke_generation.
    inference_config = {'maxTokens': int(config['max_tokens'])}
    temperature = config.get('temperature')
    top_p = config.get('top_p')
    if temperature is not None:
        inference_config['temperature'] = float(temperature)
    elif top_p is not None:
        inference_config['topP'] = float(top_p)
    try:
        response = client.converse(
            modelId=config['model_id'],
            system=[{'text': system_prompt}],
            messages=messages,
            inferenceConfig=inference_config,
            toolConfig=tool_config
        )
    except (ReadTimeoutError, ConnectTimeoutError):
        logger.error(f"Bedrock invocation exceeded the configured timeout "
                     f"({config['timeout_seconds']}s, model {config['model_id']})")
        return None, None, error_response(
            504, 'GENERATION_TIMEOUT',
            f"Scaffold generation timed out after {config['timeout_seconds']} seconds. "
            'Your prompt was not lost - please retry.',
            {'timeout_seconds': config['timeout_seconds'], 'model_id': config['model_id']}
        )
    except EndpointConnectionError as e:
        logger.error(f"Bedrock endpoint unreachable: {str(e)}")
        return None, None, error_response(
            502, 'BEDROCK_UNREACHABLE',
            f"The Bedrock endpoint in region {config['region']} could not be reached. "
            'Check the Bedrock configuration. Your prompt was not lost - please retry.',
            {'region': config['region']}
        )
    except ClientError as e:
        error = e.response.get('Error', {})
        logger.error(f"Bedrock invocation failed: {error.get('Code')}: {error.get('Message')}")
        return None, None, error_response(
            502, 'BEDROCK_INVOCATION_FAILED',
            f"The Bedrock model invocation failed: {error.get('Message', 'unknown error')}. "
            'Your prompt was not lost - please retry.',
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
            502, 'NO_SCAFFOLD_RETURNED',
            'The model did not return Plugin_Scaffold source. '
            'Your prompt was not lost - please retry or rephrase it.',
            {'stop_reason': response.get('stopReason'), 'model_text': assistant_text[:500]}
        )
    return tool_input, assistant_text, None


# --------------------------------------------------------------------------
# Shared generation turn (first prompt and follow-ups)
# --------------------------------------------------------------------------

def run_generation_turn(session: Dict, prompt: str,
                        current_source: Optional[Dict],
                        user: Dict) -> Dict:
    """
    Execute one generation turn: assemble the prompts and the forced tool,
    invoke Bedrock, validate the returned scaffold, and persist the session
    plus the source snapshot. Any failure returns the error without
    mutating the session, preserving the prompt for retry (2.6, 2.7).
    """
    declaration = json.loads(session['declaration_json'])

    # The reference scaffold doubles as declaration validation and as the
    # template conventions embedded in the system prompt (2.2).
    try:
        reference_files = render_scaffold(declaration)
    except ScaffoldError as exc:
        return error_response(400, 'INVALID_DECLARATION', str(exc),
                              {'field': exc.field})

    current_source_json = (json.dumps(current_source, sort_keys=True)
                           if current_source else None)
    user_text = build_user_message(prompt, current_source_json)
    history = session.get('messages') or []
    messages = converse_messages(history, user_text)

    config = get_bedrock_configuration()
    tool_input, assistant_text, err = invoke_generation(
        config, build_system_prompt(declaration, reference_files),
        messages, build_tool_config(declaration, reference_files))
    if err:
        return err

    files = tool_input.get('files')

    # Output that does not form a buildable Plugin_Scaffold returns a
    # descriptive error; the session stays untouched so the prompt is
    # preserved for retry (2.6).
    defects = scaffold_defects(files, declaration)
    if defects:
        logger.error(f"Generated scaffold failed validation: {defects}")
        return error_response(
            422, 'GENERATED_SCAFFOLD_INVALID',
            'The generated output does not form a buildable Plugin_Scaffold: '
            + '; '.join(defects)
            + '. Your prompt was not lost - please retry or rephrase it.',
            {'defects': defects}
        )
    # Drop non-string extras defensively (schema allows extra string files
    # only; anything else would fail the build submission later).
    files = {path: content for path, content in files.items()
             if isinstance(path, str) and isinstance(content, str)}

    # ------------------------------------------------------- persist session
    # The generated file map becomes the session's source snapshot for
    # follow-up modification prompts (2.4). History carries the raw prompt
    # (not the embedded source) to keep session items small; the follow-up
    # turn re-embeds the latest source from the snapshot.
    snapshot_key = source_snapshot_s3_key(session['usecase_id'], session['session_id'])
    put_source_snapshot(snapshot_key, files)

    timestamp = now_ms()
    session['messages'] = (history + [
        {'role': 'user', 'text': prompt, 'at': timestamp},
        {'role': 'assistant',
         'text': assistant_text or 'Produced Plugin_Scaffold source (see the current scaffold source).',
         'at': timestamp},
    ])[-MAX_HISTORY_MESSAGES * 2:]
    session['current_source_key'] = snapshot_key
    session['model_id'] = config['model_id']
    # The snapshot is the completed turn's result pointer for the poll route.
    session['turn_status'] = TURN_COMPLETED
    session['turn_error'] = None
    save_session(session)

    log_audit_event(
        user_id=user['user_id'],
        action='generate_plugin_scaffold',
        resource_type='plugin_scaffold_generation',
        resource_id=session['session_id'],
        result='success',
        details={'usecase_id': session['usecase_id'],
                 'model_id': config['model_id'],
                 'file_count': len(files)}
    )

    return create_response(200, {
        'session_id': session['session_id'],
        'usecase_id': session['usecase_id'],
        'files': files,
        'assistant_text': assistant_text,
        'model_id': config['model_id'],
    })


# --------------------------------------------------------------------------
# POST /plugins/generate - first prompt of a new session
# --------------------------------------------------------------------------

def generate_scaffold(event: Dict, user: Dict) -> Dict:
    """
    POST /plugins/generate
    Body: {usecase_id, prompt, declaration}

    Starts a scaffold-generation chat session: validates the request,
    persists the session with turn_status=pending, dispatches the
    asynchronous generation worker, and returns 202 with the session id
    immediately; the caller polls GET /plugins/generate/{session} for the
    generated Plugin_Scaffold source (2.1, 2.2, 2.3). Nothing is built or
    recorded until the user accepts the source (2.5).
    """
    body, err = parse_body(event)
    if err:
        return err

    usecase_id = body.get('usecase_id')
    prompt = body.get('prompt')
    declaration = body.get('declaration')

    missing = [f for f in ('usecase_id', 'prompt', 'declaration') if not body.get(f)]
    if missing:
        return error_response(400, 'MISSING_FIELDS',
                              f"Missing required fields: {', '.join(missing)}")
    if not isinstance(prompt, str) or not prompt.strip():
        return error_response(400, 'INVALID_PROMPT', 'prompt must be a non-empty string')
    if not isinstance(declaration, dict):
        return error_response(400, 'INVALID_DECLARATION',
                              'declaration must be a JSON object (the Custom_Node_Type '
                              'declaration the scaffold implements)')

    if not can_generate(user, usecase_id):
        return forbidden_response(user, event, usecase_id)

    try:
        get_usecase(usecase_id)
    except ValueError:
        return error_response(404, 'USECASE_NOT_FOUND', 'Use case not found')

    # Validate the declaration up front so an invalid one fails fast with
    # the offending field identified, before any Bedrock invocation.
    try:
        render_scaffold(declaration)
    except ScaffoldError as exc:
        return error_response(400, 'INVALID_DECLARATION', str(exc),
                              {'field': exc.field})

    session = {
        'session_id': str(uuid.uuid4()),
        'usecase_id': usecase_id,
        'user_id': user['user_id'],
        'declaration_json': json.dumps(declaration, sort_keys=True),
        'messages': [],
        'current_source_key': None,
        'created_at': now_ms(),
        'turn_status': TURN_PENDING,
        'turn_error': None,
    }
    save_session(session)
    return start_generation_turn(session, prompt.strip(), user)


def start_generation_turn(session: Dict, prompt: str, user: Dict) -> Dict:
    """
    Dispatch the asynchronous generation worker for a pending turn and
    return 202 with the session id (well under the API Gateway 29 s cap);
    the caller polls GET /plugins/generate/{session} for the outcome.
    """
    try:
        dispatch_generation_worker(session['session_id'], prompt, user)
    except Exception as exc:
        logger.error(f"Could not dispatch the generation worker: {str(exc)}",
                     exc_info=True)
        set_turn_state(session, TURN_FAILED, {
            'code': 'GENERATION_DISPATCH_FAILED',
            'message': 'The generation could not be started. '
                       'Your prompt was not lost - please retry.',
            'details': {},
            'http_status': 502,
        })
        return error_response(
            502, 'GENERATION_DISPATCH_FAILED',
            'The generation could not be started. '
            'Your prompt was not lost - please retry.')
    return create_response(202, {
        'session_id': session['session_id'],
        'usecase_id': session['usecase_id'],
        'turn_status': TURN_PENDING,
    })


# --------------------------------------------------------------------------
# GET /plugins/generate/{session} - poll the current generation turn
# --------------------------------------------------------------------------

def get_generation_status(event: Dict, user: Dict, session_id: str) -> Dict:
    """
    GET /plugins/generate/{session}

    Poll the session's current generation turn. While the turn is
    pending/running only the status is returned; a completed turn adds the
    generated files, the assistant commentary, and the model id (the former
    synchronous response shape of run_generation_turn); a failed turn adds
    turn_error {code, message, details, http_status} so the client can
    surface the error and preserve the prompt for retry (2.6, 2.7).
    """
    session, err = load_authorized_session(event, user, session_id)
    if err:
        return err

    status = session.get('turn_status') or TURN_PENDING
    payload: Dict[str, Any] = {
        'session_id': session['session_id'],
        'usecase_id': session['usecase_id'],
        'turn_status': status,
    }

    if status == TURN_FAILED:
        payload['turn_error'] = session.get('turn_error') or {
            'code': 'GENERATION_FAILED',
            'message': 'Scaffold generation failed. '
                       'Your prompt was not lost - please retry.',
            'details': {},
            'http_status': 502,
        }
    elif status == TURN_COMPLETED:
        files = None
        if session.get('current_source_key'):
            files = load_source_snapshot(session['current_source_key'])
        if files is None:
            # Result pointer missing (snapshot expired/deleted): report the
            # turn failed so the client can retry rather than hang.
            payload['turn_status'] = TURN_FAILED
            payload['turn_error'] = {
                'code': 'NO_CURRENT_SOURCE',
                'message': 'The generated source snapshot is no longer '
                           'available; please retry the prompt.',
                'details': {},
                'http_status': 409,
            }
        else:
            assistant_text = next(
                (m.get('text', '') for m in reversed(session.get('messages') or [])
                 if m.get('role') == 'assistant'), '')
            payload['files'] = files
            payload['assistant_text'] = assistant_text
            payload['model_id'] = session.get('model_id')

    return create_response(200, payload)


# --------------------------------------------------------------------------
# POST /plugins/generate/{session}/message - follow-up or acceptance
# --------------------------------------------------------------------------

def load_authorized_session(event: Dict, user: Dict,
                            session_id: str) -> Tuple[Optional[Dict], Optional[Dict]]:
    """
    Load a session for the acting user. Expired/unknown sessions and
    sessions of other users all yield the same 404, so existence is never
    leaked. Returns (session, None) or (None, error_response).
    """
    session = get_session(session_id)
    if not session or session.get('user_id') != user['user_id']:
        return None, error_response(404, 'SESSION_NOT_FOUND',
                                    'Generation session not found or expired')
    if not can_generate(user, session['usecase_id']):
        return None, forbidden_response(user, event, session['usecase_id'])
    return session, None


def session_message(event: Dict, user: Dict, session_id: str) -> Dict:
    """
    POST /plugins/generate/{session}/message
    Body: {prompt}                            follow-up modification (2.4):
                                              202 + poll, like the start route
          {accept: true, name, description?}  accept the current source (2.5)
    """
    body, err = parse_body(event)
    if err:
        return err

    session, err = load_authorized_session(event, user, session_id)
    if err:
        return err

    # A turn is already in flight: neither a second prompt nor acceptance
    # may race the worker's persistence of the current turn.
    if session.get('turn_status') in TURN_IN_PROGRESS:
        return error_response(409, 'GENERATION_IN_PROGRESS',
                              'A generation turn is already running for this '
                              'session; poll GET /plugins/generate/{session} '
                              'until it settles')

    if body.get('accept'):
        return accept_scaffold(event, user, session, body)

    prompt = body.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        return error_response(400, 'INVALID_PROMPT',
                              'prompt must be a non-empty string (or pass accept: true)')

    if not session.get('current_source_key'):
        return error_response(409, 'NO_CURRENT_SOURCE',
                              'The session has no generated source to modify; '
                              'start over with POST /plugins/generate')

    # Same async treatment as the start route: mark the turn pending and
    # dispatch the worker, which re-embeds the current source snapshot (2.4).
    set_turn_state(session, TURN_PENDING)
    return start_generation_turn(session, prompt.strip(), user)


def accept_scaffold(event: Dict, user: Dict, session: Dict, body: Dict) -> Dict:
    """
    Accept the session's current generated source: the source is validated
    once more, written under the standard plugin-sources prefix, and a
    Plugin_Record (kind "generated", Lifecycle_State dev, review pending)
    is created with the generation prompt recorded as provenance - from
    here the source follows the same build, simulation, lifecycle, and
    security review path as user-written scaffold source (2.5).
    """
    declaration = json.loads(session['declaration_json'])

    files = None
    if session.get('current_source_key'):
        files = load_source_snapshot(session['current_source_key'])
    if files is None:
        return error_response(409, 'NO_CURRENT_SOURCE',
                              'The session has no generated source to accept; '
                              'submit a prompt first')

    defects = scaffold_defects(files, declaration)
    if defects:
        return error_response(
            422, 'GENERATED_SCAFFOLD_INVALID',
            'The current generated source does not form a buildable '
            'Plugin_Scaffold: ' + '; '.join(defects),
            {'defects': defects}
        )

    name = body.get('name') or declaration.get('displayName')
    if not isinstance(name, str) or not name.strip():
        return error_response(400, 'INVALID_NAME',
                              'name must be a non-empty string')

    usecase_id = session['usecase_id']
    plugin_id = str(uuid.uuid4())
    timestamp = now_ms()
    prompts = session_prompts(session)

    # Provenance records the generation prompt(s) (2.5) alongside the
    # declaration and the generating user/timestamp/model.
    provenance = {
        'prompt': '\n\n'.join(prompts),
        'prompts': prompts,
        'scaffoldDeclaration': session['declaration_json'],
        'generatedBy': user['user_id'],
        'generatedAt': timestamp,
        'modelId': session.get('model_id'),
        'generationSessionId': session['session_id'],
    }

    item = new_version_item(
        plugin_id=plugin_id, version=1, usecase_id=usecase_id,
        name=name.strip(), kind='generated', user_id=user['user_id'],
        timestamp=timestamp,
        description=body.get('description',
                             declaration.get('description') or ''),
        deepstream=body.get('deepstream', False),
        provenance=provenance,
    )

    # Source lands under the standard plugin-sources layout used by the
    # create wizard and the Plugin_Build_Service.
    prefix = source_s3_prefix(usecase_id, plugin_id, 1)
    for path, content in sorted(files.items()):
        s3.put_object(
            Bucket=PORTAL_ARTIFACTS_BUCKET,
            Key=prefix + path,
            Body=content.encode('utf-8'),
            ContentType='text/plain; charset=utf-8'
        )

    plugin_table().put_item(
        Item=item,
        ConditionExpression='attribute_not_exists(plugin_id)'
    )

    log_audit_event(
        user_id=user['user_id'],
        action='accept_generated_scaffold',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='success',
        details={'usecase_id': usecase_id, 'name': name.strip(),
                 'kind': 'generated', 'version': 1,
                 'generation_session_id': session['session_id']}
    )

    return create_response(201, {'plugin': version_detail(item)})


# --------------------------------------------------------------------------
# Lambda handler
# --------------------------------------------------------------------------

def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler - routes to the appropriate operation"""
    # Asynchronous worker invocation (self Event-invoke from the start
    # routes): not an API Gateway event, so it is dispatched before any
    # HTTP routing or user extraction.
    if event.get('node_gen_worker'):
        return run_generation_worker(event)

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
        path_params = event.get('pathParameters') or {}

        if resource == '/plugins/generate' and http_method == 'POST':
            return generate_scaffold(event, user)

        if resource == '/plugins/generate/{session}' and http_method == 'GET':
            session_id = path_params.get('session')
            if not session_id:
                return error_response(400, 'MISSING_SESSION', 'Session id is required')
            return get_generation_status(event, user, str(session_id))

        if resource == '/plugins/generate/{session}/message' and http_method == 'POST':
            session_id = path_params.get('session')
            if not session_id:
                return error_response(400, 'MISSING_SESSION', 'Session id is required')
            return session_message(event, user, str(session_id))

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
