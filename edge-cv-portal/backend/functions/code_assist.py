"""
Code_Assist_Generator API module (Custom Node Code Assist)

Bedrock-backed code assistance for custom Python node modules
(custom-node-code-assist, Requirements 1.4, 2.1, 2.6, 2.8, 2.10, 6.1-6.4).

Handles POST /code-assist, dispatched from workflow_generator.handler
(this module lives in the same Lambda bundle; the handler gains one
``resource == '/code-assist'`` branch). Stateless: no chat sessions, no
DynamoDB or S3 writes - each request carries the current editor code, and
authorization is evaluated fresh on every request (Requirement 6.4).

Request body:
    {
        "usecase_id": "...",          required
        "surface": "...",             required; workflow-builder | node-designer
        "contract": "...",            required; process_frame |
                                      process_frame_or_handle | frame_hook
        "prompt": "...",              required; 1..4000 chars, at least one
                                      non-whitespace character
        "current_code": "...",        optional string; embedded in the
                                      modify-this-module block iff it contains
                                      a non-whitespace character (2.6, 2.10)
        "context": {                  optional object (frame_hook prompts)
            "node_type": "...",
            "parameters": [{"name", "param_type", "description"?}]
        }
    }

Error envelope: {"error": {"code", "message", "details"}} - identical shape
to every Workflow Manager endpoint. RBAC denial returns the uniform 403
FORBIDDEN envelope and writes an ``unauthorized_access`` audit entry before
any Bedrock client is constructed (Requirement 6.3).
"""
import ast
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

# Shared Bedrock_Configuration resolution and client construction - same
# Lambda bundle (backend/functions is one code asset), same semantics as
# workflow generation (Requirement 4).
from bedrock_common import (
    build_inference_config,
    get_bedrock_client,
    get_bedrock_configuration,
)

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, log_audit_event,
    get_usecase, rbac_manager, Permission
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Server-side twin of the frontend prompt constraint (Requirements 1.4, 2.8).
MAX_PROMPT_CHARS = 4000

VALID_SURFACES = frozenset({'workflow-builder', 'node-designer'})

TOOL_NAME = 'provide_code'


# --------------------------------------------------------------------------
# Node_Contract table and runtime environment descriptions (Requirement 2.1)
# --------------------------------------------------------------------------

# The Python_Bridge custom node runner (src/backend/workflow_engine/
# python_bridge.py) executes the Workflow_Builder contracts; the
# environment description mirrors it faithfully.
PYTHON_BRIDGE_ENVIRONMENT = (
    'RUNTIME ENVIRONMENT (Python_Bridge custom node runner):\n'
    '- process_frame(frame, metadata): `frame` is a NumPy uint8 array '
    '(H x W x C, or H x W for GRAY8; frame formats are RGB, BGR, RGBA, or '
    'GRAY8). Return None to pass the frame through unchanged, or an array '
    'of IDENTICAL shape and dtype (the runtime rejects anything else). '
    'Attach analysis results by mutating `metadata` in place.\n'
    '- handle(frame_bytes, metadata): receives the raw frame bytes; must '
    'return the tuple (frame_bytes, metadata).\n'
    '- cv2, np, and numpy are pre-bound on the handler module - no import '
    'is needed, but an explicit import is harmless.\n'
    '- `import dda_frames` provides to_array(frame_bytes, width, height, '
    "format), to_bytes(array), frame_info() -> {'width', 'height', "
    "'format'}, and load_image(path_or_s3_uri) returning a BGR uint8 array "
    '(local path or s3:// URI).\n'
    '- metadata["frame"] carries {width, height, format} on every '
    'invocation.\n'
    '- Never write to stdout - it belongs to the framed frame protocol; '
    'use sys.stderr for diagnostics.\n'
    '- Extra pip packages may be imported freely; the portal derives the '
    "node's pip requirements from the module's import statements, so emit "
    'a normal import statement for any library the user asks for.'
)

# The Node_Designer Frame_Processing_Hook (workflow_core/scaffold.py:
# plugin/frame_processing_hook.py) runs in the GStreamer element's
# embedded interpreter with the declared element parameters in `params`.
FRAME_HOOK_ENVIRONMENT = (
    'RUNTIME ENVIRONMENT (Frame_Processing_Hook, embedded interpreter):\n'
    '- process_frame(frame, params): `frame` is the video frame to process '
    'and `params` is a dict carrying the element\'s declared GObject '
    'parameters (parameter name -> current value). Return the processed '
    'frame.\n'
    '- The module runs inside the GStreamer element\'s embedded Python '
    'interpreter; there is no `metadata` argument and no dda_frames helper '
    'module on this surface.'
)

# Node_Contract table (design "Prompt assembly"): entry-point rule,
# human-readable signature, and per-contract environment description.
# `entry_points`/`require_exactly_one` drive validate_entry_point (task 2.2).
CONTRACTS: Dict[str, Dict[str, Any]] = {
    'process_frame': {
        'entry_points': frozenset({'process_frame'}),
        'require_exactly_one': False,
        'signature': 'process_frame(frame, metadata)',
        'environment': PYTHON_BRIDGE_ENVIRONMENT,
    },
    'process_frame_or_handle': {
        'entry_points': frozenset({'process_frame', 'handle'}),
        'require_exactly_one': True,
        'signature': 'process_frame(frame, metadata) or '
                     'handle(frame_bytes, metadata)',
        'environment': PYTHON_BRIDGE_ENVIRONMENT,
    },
    'frame_hook': {
        'entry_points': frozenset({'process_frame'}),
        'require_exactly_one': False,
        'signature': 'process_frame(frame, params)',
        'environment': FRAME_HOOK_ENVIRONMENT,
    },
}


# --------------------------------------------------------------------------
# Envelope helpers (same shape as workflow_generator / node_generator)
# --------------------------------------------------------------------------

def error_response(status_code: int, code: str, message: str,
                   details: Optional[Dict] = None) -> Dict:
    """Build the error envelope: {error: {code, message, details}}"""
    return create_response(status_code, {
        'error': {
            'code': code,
            'message': message,
            'details': details or {}
        }
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
# Request validation (design "400 matrix"; Requirements 1.4, 2.8)
# --------------------------------------------------------------------------

def validate_request(body: Dict) -> Optional[Dict]:
    """Validate a POST /code-assist body; None when valid, else the 400
    error_response per the design's 400 matrix."""
    missing = [f for f in ('usecase_id', 'surface', 'contract', 'prompt')
               if not body.get(f)]
    if missing:
        return error_response(400, 'MISSING_FIELDS',
                              f"Missing required fields: {', '.join(missing)}")

    if body['surface'] not in VALID_SURFACES:
        return error_response(
            400, 'INVALID_SURFACE',
            f"surface must be one of: {', '.join(sorted(VALID_SURFACES))}",
            {'surface': body['surface']})

    if body['contract'] not in CONTRACTS:
        return error_response(
            400, 'INVALID_CONTRACT',
            f"contract must be one of: {', '.join(sorted(CONTRACTS))}",
            {'contract': body['contract']})

    prompt = body['prompt']
    if not isinstance(prompt, str) or not prompt.strip():
        return error_response(
            400, 'INVALID_PROMPT',
            'prompt must be a string with at least one non-whitespace character')
    if len(prompt) > MAX_PROMPT_CHARS:
        return error_response(
            400, 'INVALID_PROMPT',
            f'prompt must be at most {MAX_PROMPT_CHARS} characters',
            {'length': len(prompt), 'max_length': MAX_PROMPT_CHARS})

    current_code = body.get('current_code')
    if current_code is not None and not isinstance(current_code, str):
        return error_response(400, 'INVALID_JSON',
                              'current_code must be a string when present')

    context = body.get('context')
    if context is not None and not isinstance(context, dict):
        return error_response(400, 'INVALID_JSON',
                              'context must be an object when present')

    return None


# --------------------------------------------------------------------------
# Authorization (Requirements 6.1-6.4)
# --------------------------------------------------------------------------

def surface_permissions(surface: str) -> List[Permission]:
    """The permissions that authorize Code_Assistant use on a surface."""
    if surface == 'workflow-builder':
        return [Permission.WORKFLOW_CREATE, Permission.WORKFLOW_EDIT]
    return [Permission.NODE_DESIGNER_GENERATE]


def is_authorized(user: Dict, usecase_id: str, surface: str) -> bool:
    """Per-surface authorization, evaluated fresh on every request (6.4):
    the workflow create or edit permission for the Workflow_Builder surface
    (6.1), or the Node_Designer generate permission - UseCaseAdmin within
    the Use_Case or PortalAdmin, the same rule as node_generator.
    can_generate - for the Node_Designer surface (6.2)."""
    return any(
        rbac_manager.has_permission(user['user_id'], usecase_id,
                                    permission, user_info=user)
        for permission in surface_permissions(surface)
    )


def forbidden_response(user: Dict, event: Dict, usecase_id: str,
                       surface: str) -> Dict:
    """Uniform 403 authorization error with a denied-access audit entry
    carrying the acting user, surface, Use_Case, and timestamp (6.3);
    written before any Bedrock client is constructed."""
    log_audit_event(
        user_id=user['user_id'],
        action='unauthorized_access',
        resource_type='code_assist',
        resource_id=event.get('resource', 'unknown'),
        result='denied',
        details={
            'required_permissions': [p.value for p in surface_permissions(surface)],
            'surface': surface,
            'usecase_id': usecase_id,
            'method': event.get('httpMethod'),
            'path': event.get('path')
        }
    )
    return error_response(403, 'FORBIDDEN', 'Insufficient permissions', {
        'surface': surface,
        'usecase_id': usecase_id
    })


# --------------------------------------------------------------------------
# Prompt assembly - pure functions (Requirements 2.1, 2.6, 2.10)
# --------------------------------------------------------------------------

def build_system_prompt(contract: str, context: Optional[Dict] = None) -> str:
    """System prompt carrying the contract's entry-point signature, its
    runtime environment description, and the generation rules. For
    frame_hook, the declared element parameters from ``context.parameters``
    are embedded so the model addresses `params` correctly."""
    spec = CONTRACTS[contract]

    parts = [
        'You are the custom node code assistant of the DDA edge computer '
        'vision portal. Users describe the Python processing code or filter '
        'they need in natural language; you write the complete Python node '
        'module that implements it.',
        '',
        f"TARGET ENTRY POINT: {spec['signature']}",
        '',
        spec['environment'],
    ]

    if contract == 'frame_hook':
        parameters = (context or {}).get('parameters') or []
        param_lines = []
        for param in parameters:
            if not isinstance(param, dict) or not param.get('name'):
                continue
            line = f"- {param['name']} ({param.get('param_type', 'unknown')})"
            if param.get('description'):
                line += f": {param['description']}"
            param_lines.append(line)
        if param_lines:
            parts += ['',
                      'DECLARED ELEMENT PARAMETERS (available in `params`):']
            parts += param_lines

    rules = [
        '',
        'Rules:',
        f'- Always respond by calling the {TOOL_NAME} tool with the COMPLETE '
        'Python module source in `code` and one short paragraph for the '
        'user in `notes`. Do not answer with prose only.',
    ]
    if spec['require_exactly_one']:
        rules.append(
            '- Define EXACTLY ONE of the entry points process_frame(frame, '
            'metadata) or handle(frame_bytes, metadata) - never both '
            '(process_frame for decoded frame processing, handle for raw '
            'bytes).')
    else:
        rules.append(
            f"- The module must define the entry point {spec['signature']} "
            'at the top level.')
    rules.append(
        '- Emit a normal `import` statement for every non-builtin library '
        'the code uses, including any library the user explicitly asks for.')
    rules.append(
        '- Keep the module complete and self-contained: when a CURRENT '
        'MODULE CODE block is provided, apply the requested change to that '
        'code and return the ENTIRE modified module - never a fragment, a '
        'diff, or code unrelated to the current module.')

    return '\n'.join(parts + rules)


def build_user_message(prompt: str, current_code: Optional[str]) -> str:
    """The user turn sent to the model: the prompt verbatim, plus the
    current editor code in a modify-not-regenerate block if and only if it
    contains at least one non-whitespace character (Requirements 2.6,
    2.10). A whitespace-only editor is treated as empty - the prompt is
    sent alone and nothing is presented as code to modify."""
    if not current_code or not current_code.strip():
        return prompt
    return (
        f'{prompt}\n'
        '\n'
        'CURRENT MODULE CODE:\n'
        f'{current_code}\n'
        '\n'
        'Apply the requested change to this current module rather than '
        'generating unrelated code from scratch, and return the complete '
        f'modified module via the {TOOL_NAME} tool.'
    )


# --------------------------------------------------------------------------
# Entry-point validation - pure function (Requirements 2.2, 2.3, 5.6)
# --------------------------------------------------------------------------

# Defect prefix distinguishing a parse failure (422 GENERATED_CODE_INVALID)
# from an entry-point defect (422 MISSING_ENTRY_POINT).
INVALID_PYTHON_PREFIX = 'generated code is not valid Python'


def validate_entry_point(code: str, contract: str) -> Optional[str]:
    """None when the generated module is valid for the contract; a defect
    description otherwise.

    - ``ast.parse`` failure -> 'generated code is not valid Python: ...'
    - The top-level FunctionDef names are intersected with the contract's
      entry points; zero matches -> 'missing entry point ...'
    - ``require_exactly_one`` contracts (custom_python) must define exactly
      one of process_frame/handle -> 'defines both entry points ...' when
      both are present (two entry points would silently shadow one another
      at runtime: the Python_Bridge prefers process_frame).
    """
    spec = CONTRACTS[contract]
    try:
        module = ast.parse(code)
    except (SyntaxError, ValueError) as e:
        return f'{INVALID_PYTHON_PREFIX}: {e}'

    top_level = {node.name for node in module.body
                 if isinstance(node, ast.FunctionDef)}
    defined = top_level & spec['entry_points']

    if not defined:
        return (f"missing entry point: the module must define "
                f"{spec['signature']} at the top level")
    if spec['require_exactly_one'] and len(defined) > 1:
        return ('defines both entry points process_frame and handle; '
                'define exactly one of them')
    return None


# --------------------------------------------------------------------------
# Bedrock failure categorization (Requirement 5.1)
# --------------------------------------------------------------------------

# botocore error code -> Requirement 5.1 failure category. Total: every
# unlisted code falls through to 'model-error' (design mapping table).
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
    """Map a botocore error code to exactly one of the four Requirement
    5.1 failure categories; anything unrecognized is 'model-error'."""
    return BEDROCK_ERROR_CATEGORIES.get(error_code, 'model-error')


# --------------------------------------------------------------------------
# Bedrock invocation (Requirements 2.1-2.3, 5.1-5.3, 5.6)
# --------------------------------------------------------------------------

def build_tool_config() -> Dict:
    """Converse toolConfig forcing structured output through provide_code:
    extraction is a field read, never markdown-fence scraping. 'No tool
    call or empty code' is the well-defined NO_CODE_RETURNED trigger."""
    return {
        'tools': [{
            'toolSpec': {
                'name': TOOL_NAME,
                'description': (
                    'Return the complete Python node module that fulfils '
                    'the user request. Always call this tool with the '
                    'entire module source in `code` and one short '
                    'paragraph for the user in `notes`.'
                ),
                'inputSchema': {'json': {
                    'type': 'object',
                    'required': ['code'],
                    'properties': {
                        'code': {
                            'type': 'string',
                            'description': 'the complete Python module'
                        },
                        'notes': {
                            'type': 'string',
                            'description': 'one short paragraph for the user'
                        }
                    }
                }}
            }
        }],
        'toolChoice': {'tool': {'name': TOOL_NAME}}
    }


# --------------------------------------------------------------------------
# Endpoint
# --------------------------------------------------------------------------

def generate_code(contract: str, system_prompt: str, user_message: str) -> Dict:
    """Bedrock Converse invocation with the forced provide_code tool,
    entry-point validation, and error mapping.

    Nothing before this point constructs a Bedrock client, so every
    400/403/404 settles without Bedrock traffic (Requirement 6.3). The
    Bedrock_Configuration is resolved fresh per invocation through the
    shared module (Requirement 4.1); the client-side read timeout equals
    the clamped configured timeout and retries are disabled, so wall time
    cannot exceed it (Requirement 4.4). Success returns
    {code, notes, model_id, contract} and persists nothing (2.7, 6.4).
    """
    config = get_bedrock_configuration()
    client = get_bedrock_client(config['region'], config['timeout_seconds'])
    try:
        response = client.converse(
            modelId=config['model_id'],
            system=[{'text': system_prompt}],
            messages=[{'role': 'user', 'content': [{'text': user_message}]}],
            inferenceConfig=build_inference_config(config),
            toolConfig=build_tool_config()
        )
    except (ReadTimeoutError, ConnectTimeoutError):
        logger.error(f"Code assist invocation exceeded the configured timeout "
                     f"({config['timeout_seconds']}s, model {config['model_id']})")
        return error_response(
            504, 'GENERATION_TIMEOUT',
            f"Code generation timed out after {config['timeout_seconds']} seconds. "
            'Your prompt was not lost - please retry.',
            {'timeout_seconds': config['timeout_seconds'],
             'model_id': config['model_id']}
        )
    except EndpointConnectionError as e:
        logger.error(f"Bedrock endpoint unreachable: {str(e)}")
        return error_response(
            502, 'BEDROCK_UNREACHABLE',
            f"The Bedrock endpoint in region {config['region']} could not be "
            'reached. Check the Bedrock configuration.',
            {'region': config['region'], 'category': 'model-access'}
        )
    except ClientError as e:
        error = e.response.get('Error', {})
        logger.error(f"Bedrock invocation failed: {error.get('Code')}: "
                     f"{error.get('Message')}")
        return error_response(
            502, 'BEDROCK_INVOCATION_FAILED',
            f"The Bedrock model invocation failed: "
            f"{error.get('Message', 'unknown error')}",
            {'category': categorize_bedrock_error(error.get('Code')),
             'bedrock_error_code': error.get('Code'),
             'model_id': config['model_id']}
        )

    content = (response.get('output', {}).get('message', {}) or {}).get('content', [])
    tool_input = None
    for block in content:
        if 'toolUse' in block and block['toolUse'].get('name') == TOOL_NAME:
            tool_input = block['toolUse'].get('input')

    code = tool_input.get('code') if isinstance(tool_input, dict) else None
    if not isinstance(code, str) or not code.strip():
        logger.error(f"Model returned no {TOOL_NAME} tool call or empty code "
                     f"(stopReason={response.get('stopReason')})")
        return error_response(
            422, 'NO_CODE_RETURNED',
            'The model did not return code. Please retry or rephrase the prompt.',
            {'stop_reason': response.get('stopReason')}
        )

    defect = validate_entry_point(code, contract)
    if defect is not None:
        logger.error(f"Generated code rejected ({contract}): {defect}")
        if defect.startswith(INVALID_PYTHON_PREFIX):
            return error_response(
                422, 'GENERATED_CODE_INVALID',
                'The generated code is not valid Python. Please retry or '
                'rephrase the prompt.',
                {'defect': defect}
            )
        return error_response(
            422, 'MISSING_ENTRY_POINT',
            'The generated code lacks the required entry point '
            f"({CONTRACTS[contract]['signature']}). Please retry or "
            'rephrase the prompt.',
            {'defect': defect, 'contract': contract}
        )

    notes = tool_input.get('notes')
    return create_response(200, {
        'code': code,
        'notes': notes if isinstance(notes, str) else '',
        'model_id': config['model_id'],
        'contract': contract,
    })


def handle_code_assist(event: Dict, user: Dict) -> Dict:
    """
    POST /code-assist
    Body: {usecase_id, surface, contract, prompt, current_code?, context?}

    Validates the request (400 matrix), authorizes per surface with an
    audit entry on denial (403 before any Bedrock traffic), resolves the
    Use_Case (404), assembles the contract-specific prompts, and delegates
    to the Bedrock invocation. Nothing is persisted anywhere (2.7, 6.4).
    """
    body, err = parse_body(event)
    if err:
        return err

    err = validate_request(body)
    if err:
        return err

    usecase_id = body['usecase_id']
    surface = body['surface']

    # Authorization first (fresh per request, 6.4); denial is audited and
    # settled before any Bedrock client exists (6.3).
    if not is_authorized(user, usecase_id, surface):
        return forbidden_response(user, event, usecase_id, surface)

    try:
        get_usecase(usecase_id)
    except ValueError:
        return error_response(404, 'USECASE_NOT_FOUND', 'Use case not found')

    contract = body['contract']
    return generate_code(
        contract=contract,
        system_prompt=build_system_prompt(contract, body.get('context')),
        user_message=build_user_message(body['prompt'], body.get('current_code')),
    )
