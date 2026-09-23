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
                                      process_frame_or_handle | frame_hook |
                                      produce_frame
        "prompt": "...",              required; 1..4000 chars, at least one
                                      non-whitespace character
        "current_code": "...",        optional string; embedded in the
                                      modify-this-module block iff it contains
                                      a non-whitespace character (2.6, 2.10)
        "context": {                  optional object
            "node_type": "...",
            "parameters": [{"name", "param_type", "description"?}],
            "active_file": "...",      Source_Editor: path of the edited file
            "files": {path: content},  other Source_Tree text files (<= 256 KiB)
            "file_paths": [...],       every Source_Tree path
            "kind": "scaffold" | "generated" | "imported"
        },
        "diagnostics": {              optional (custom-node-source-lifecycle 5)
            "kind": "build" | "simulation" | "user",
            "architecture": "...",     build kind: the failing Target_Architecture
            "text": "..."              <= 16 KiB of error output
        }
    }

Response: {code, notes, model_id, contract, target_file} — `target_file`
names the Source_Tree file the code applies to (the active file unless
the model redirected the fix to another provided path, 5.7/5.8).

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
# Build platform table and scaffold layout constants (workflow_core layer):
# the Node_Designer prompts describe the per-architecture toolchain and
# the Plugin_Scaffold file roles (custom-node-source-lifecycle 5.10).
from workflow_core.catalog import DEVICE_ARCHITECTURES, describe_build_platforms
from workflow_core.scaffold import HOOK_FILE

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

# Diagnostic_Context bounds (custom-node-source-lifecycle 5.4): the panel
# keeps the last 16 KiB before submission; the server enforces the cap.
MAX_DIAGNOSTICS_CHARS = 16 * 1024
DIAGNOSTIC_KINDS = frozenset({'build', 'simulation', 'user'})

# Multi-file context bounds (5.6): the other Source_Tree text files travel
# with the request up to this total, largest files omitted first by the
# client (omitted paths are still listed in file_paths).
MAX_CONTEXT_FILES_BYTES = 256 * 1024
MAX_CONTEXT_FILES = 64

# Node_Designer contracts: the ones whose prompts carry the scaffold layout
# and build platforms and whose responses may redirect to a Target_File.
NODE_DESIGNER_CONTRACTS = frozenset({'frame_hook', 'plugin_source'})


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

# Non-hook Source_Tree files of a Plugin_Scaffold (custom-node-source-
# lifecycle 5.9): the C skeleton element, the per-architecture meson build
# configurations, and the README. The model returns the complete
# replacement content of one file; there is no Python entry point.
PLUGIN_SOURCE_ENVIRONMENT = (
    'TARGET FILE: a non-Python file of the Plugin_Scaffold (C skeleton '
    'element source, a builds/<arch>/meson.build configuration, or the '
    'README). Return the COMPLETE corrected content of the target file in '
    "`code` - never a fragment or a diff. Preserve the file's language "
    'and its existing structure unless the fix requires otherwise.'
)

# Plugin_Scaffold layout every Node_Designer prompt describes (5.10), so
# the model knows which file each kind of failure belongs to.
SCAFFOLD_LAYOUT = (
    'PLUGIN_SCAFFOLD LAYOUT (rendered by the portal):\n'
    f'- {HOOK_FILE}: the Python Frame_Processing_Hook exposing '
    'process_frame(frame, params); user processing logic lives here.\n'
    '- plugin/gst<element>.c: the C skeleton GStreamer element that embeds '
    'the Python interpreter, bridges frames through an appsink/appsrc '
    'pair into the hook, and exposes the declared parameters as GObject '
    'properties plumbed into the hook\'s `params` dict.\n'
    '- builds/<arch>/meson.build: one meson build configuration per '
    'Target_Architecture (dependencies, compile flags, install rules); '
    'compiler and linker errors for one architecture usually originate '
    'here or in the C source.\n'
    '- README.md: usage notes.\n'
    'A build failure for one architecture is compiled with that '
    'architecture\'s toolchain listed under BUILD PLATFORMS.'
)

# The Python_Bridge frame producer (custom-python-source, Requirements
# 9.4, 9.5): produce_frame(context) runs exactly once per workflow run in
# the same handler subprocess isolation as the per-frame contracts. The
# environment description mirrors the runner's produce operation and the
# dda_frames Frame_Helpers (load_image/load_bytes with the bounded HTTP
# timeout and the allowed-URI-prefix restriction).
PRODUCE_FRAME_ENVIRONMENT = (
    'RUNTIME ENVIRONMENT (Python_Bridge frame producer):\n'
    '- produce_frame(context): called EXACTLY ONCE per workflow run. '
    '`context` is the Trigger_Context that started the run: for MQTT '
    'triggers {topic, payload, payload_json, qos, timestamp} (payload_json '
    'is the payload parsed as JSON, or None); for OPC UA triggers '
    '{endpoint, node_id, value, source_timestamp}; {} for manual runs.\n'
    '- Return the frame: a NumPy uint8 array (H x W grayscale, H x W x 3 '
    'BGR, or H x W x 4 BGRA — OpenCV channel order), or '
    '{"array": arr, "format": "RGB"|"RGBA"|"GRAY8"} to skip channel '
    'conversion, or {"data": bytes, "width": W, "height": H, "format": ...}. '
    'Returning None fails the run.\n'
    '- cv2, np, and numpy are pre-bound; `import dda_frames` provides '
    'load_image(source) -> BGR uint8 array and load_bytes(source) -> raw '
    'bytes for local paths, s3://bucket/key URIs, and http(s):// URLs '
    '(bounded network timeout; fetches may be restricted to the node\'s '
    'allowed URI prefixes).\n'
    '- Never write to stdout - it belongs to the framed frame protocol; '
    'use sys.stderr for diagnostics.\n'
    '- Extra pip packages may be imported freely; the portal derives the '
    "node's pip requirements from the module's import statements, so emit "
    'a normal import statement for any library the user asks for.'
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
    'produce_frame': {
        'entry_points': frozenset({'produce_frame'}),
        'require_exactly_one': False,
        'signature': 'produce_frame(context)',
        'environment': PRODUCE_FRAME_ENVIRONMENT,
    },
    # Plugin_Source_Contract (custom-node-source-lifecycle 5.9): no entry
    # point; validation accepts any non-empty file content.
    'plugin_source': {
        'entry_points': frozenset(),
        'require_exactly_one': False,
        'signature': 'complete file content',
        'environment': PLUGIN_SOURCE_ENVIRONMENT,
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
    if context:
        err = validate_context(context)
        if err:
            return err

    diagnostics = body.get('diagnostics')
    if diagnostics is not None:
        err = validate_diagnostics(diagnostics)
        if err:
            return err

    return None


def validate_context(context: Dict) -> Optional[Dict]:
    """Validate the multi-file context fields (custom-node-source-lifecycle
    5.6): `files` is a path->text map within MAX_CONTEXT_FILES /
    MAX_CONTEXT_FILES_BYTES, `file_paths` a list of strings, and
    `active_file` a member of `file_paths` when both are present."""
    files = context.get('files')
    if files is not None:
        if (not isinstance(files, dict)
                or not all(isinstance(k, str) and isinstance(v, str)
                           for k, v in files.items())):
            return error_response(400, 'INVALID_CONTEXT',
                                  'context.files must map file paths to text')
        if len(files) > MAX_CONTEXT_FILES:
            return error_response(
                400, 'INVALID_CONTEXT',
                f'context.files may carry at most {MAX_CONTEXT_FILES} files',
                {'count': len(files), 'max_files': MAX_CONTEXT_FILES})
        total = sum(len(v.encode('utf-8')) for v in files.values())
        if total > MAX_CONTEXT_FILES_BYTES:
            return error_response(
                400, 'INVALID_CONTEXT',
                'context.files exceeds the total size limit',
                {'bytes': total, 'max_bytes': MAX_CONTEXT_FILES_BYTES})

    file_paths = context.get('file_paths')
    if file_paths is not None and (
            not isinstance(file_paths, list)
            or not all(isinstance(p, str) for p in file_paths)):
        return error_response(400, 'INVALID_CONTEXT',
                              'context.file_paths must be a list of paths')

    active_file = context.get('active_file')
    if active_file is not None and not isinstance(active_file, str):
        return error_response(400, 'INVALID_CONTEXT',
                              'context.active_file must be a string')
    if (isinstance(active_file, str) and isinstance(file_paths, list)
            and active_file not in file_paths):
        return error_response(400, 'INVALID_CONTEXT',
                              'context.active_file must be one of context.file_paths',
                              {'active_file': active_file})

    kind = context.get('kind')
    if kind is not None and kind not in ('scaffold', 'generated', 'imported'):
        return error_response(400, 'INVALID_CONTEXT',
                              'context.kind must be scaffold, generated, or imported')
    return None


def validate_diagnostics(diagnostics: Any) -> Optional[Dict]:
    """Validate a Diagnostic_Context (custom-node-source-lifecycle 5.4):
    kind in DIAGNOSTIC_KINDS, optional architecture in
    DEVICE_ARCHITECTURES, text a string of at most MAX_DIAGNOSTICS_CHARS."""
    if not isinstance(diagnostics, dict):
        return error_response(400, 'INVALID_DIAGNOSTICS',
                              'diagnostics must be an object when present')
    kind = diagnostics.get('kind')
    if kind not in DIAGNOSTIC_KINDS:
        return error_response(
            400, 'INVALID_DIAGNOSTICS',
            f"diagnostics.kind must be one of: {', '.join(sorted(DIAGNOSTIC_KINDS))}",
            {'kind': kind})
    architecture = diagnostics.get('architecture')
    if architecture is not None and architecture not in DEVICE_ARCHITECTURES:
        return error_response(400, 'INVALID_DIAGNOSTICS',
                              'diagnostics.architecture is not a Target_Architecture',
                              {'architecture': architecture,
                               'valid': list(DEVICE_ARCHITECTURES)})
    text = diagnostics.get('text')
    if not isinstance(text, str) or not text.strip():
        return error_response(400, 'INVALID_DIAGNOSTICS',
                              'diagnostics.text must be a non-empty string')
    if len(text) > MAX_DIAGNOSTICS_CHARS:
        return error_response(
            400, 'INVALID_DIAGNOSTICS',
            f'diagnostics.text must be at most {MAX_DIAGNOSTICS_CHARS} characters',
            {'length': len(text), 'max_length': MAX_DIAGNOSTICS_CHARS})
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

def build_system_prompt(contract: str, context: Optional[Dict] = None,
                        diagnostics: Optional[Dict] = None) -> str:
    """System prompt carrying the contract's entry-point signature, its
    runtime environment description, and the generation rules. For
    frame_hook, the declared element parameters from ``context.parameters``
    are embedded so the model addresses `params` correctly. Node_Designer
    contracts additionally carry the Plugin_Scaffold layout and the
    per-architecture build platforms, and a DIAGNOSTIC MODE block when a
    Diagnostic_Context is attached (custom-node-source-lifecycle 5.5,
    5.7, 5.10)."""
    spec = CONTRACTS[contract]
    is_plugin_source = contract == 'plugin_source'

    parts = [
        'You are the custom node code assistant of the DDA edge computer '
        'vision portal. Users describe the processing code, filter, or fix '
        'they need in natural language; you write the complete file that '
        'implements it.',
        '',
        f"TARGET ENTRY POINT: {spec['signature']}" if not is_plugin_source
        else spec['environment'],
        '',
    ]
    if not is_plugin_source:
        parts.append(spec['environment'])

    if contract in NODE_DESIGNER_CONTRACTS:
        parts += ['', SCAFFOLD_LAYOUT, '', 'BUILD PLATFORMS:']
        parts += describe_build_platforms()

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

    if diagnostics:
        kind = diagnostics.get('kind', 'user')
        arch = diagnostics.get('architecture')
        target = f' for architecture {arch}' if arch else ''
        parts += [
            '',
            'DIAGNOSTIC MODE:',
            f'- The user attached {kind} error output{target}. Diagnose the '
            'root cause from the DIAGNOSTIC OUTPUT block, explain it in one '
            'short paragraph in `notes`, and return the corrected COMPLETE '
            'file in `code`.',
        ]
        if contract in NODE_DESIGNER_CONTRACTS:
            parts.append(
                '- If the fix belongs in a different file among FILE PATHS '
                'than the ACTIVE FILE, set `target_file` to that path and '
                'return that file\'s complete corrected content; otherwise '
                'omit `target_file`.')

    rules = [
        '',
        'Rules:',
        f'- Always respond by calling the {TOOL_NAME} tool with the COMPLETE '
        + ('file content' if is_plugin_source else 'Python module source')
        + ' in `code` and one short paragraph for the user in `notes`. Do '
        'not answer with prose only.',
    ]
    if spec['require_exactly_one']:
        rules.append(
            '- Define EXACTLY ONE of the entry points process_frame(frame, '
            'metadata) or handle(frame_bytes, metadata) - never both '
            '(process_frame for decoded frame processing, handle for raw '
            'bytes).')
    elif not is_plugin_source:
        rules.append(
            f"- The module must define the entry point {spec['signature']} "
            'at the top level.')
    if not is_plugin_source:
        rules.append(
            '- Emit a normal `import` statement for every non-builtin library '
            'the code uses, including any library the user explicitly asks for.')
    rules.append(
        '- Keep the file complete and self-contained: when a CURRENT '
        + ('FILE CONTENT' if is_plugin_source else 'MODULE CODE')
        + ' block is provided, apply the requested change to that '
        'content and return the ENTIRE modified file - never a fragment, a '
        'diff, or content unrelated to the current file.')
    if contract in NODE_DESIGNER_CONTRACTS:
        rules.append(
            '- `target_file`, when set, MUST be one of the paths listed under '
            'FILE PATHS; the code you return is the complete content of that '
            'file.')

    return '\n'.join(parts + rules)


def build_user_message(prompt: str, current_code: Optional[str],
                       context: Optional[Dict] = None,
                       diagnostics: Optional[Dict] = None,
                       contract: Optional[str] = None) -> str:
    """The user turn sent to the model: the prompt verbatim, plus the
    current editor code in a modify-not-regenerate block if and only if it
    contains at least one non-whitespace character (Requirements 2.6,
    2.10). A whitespace-only editor is treated as empty - the prompt is
    sent alone and nothing is presented as code to modify.

    Node_Designer requests append the ACTIVE FILE path, the OTHER FILES of
    the Source_Tree (fenced), the complete FILE PATHS list (omitted
    contents noted), and any DIAGNOSTIC OUTPUT (custom-node-source-
    lifecycle 5.5, 5.6). Workflow_Builder requests append only the
    diagnostic output."""
    block_label = ('CURRENT FILE CONTENT' if contract == 'plugin_source'
                   else 'CURRENT MODULE CODE')
    sections = [prompt]
    if current_code and current_code.strip():
        sections.append(
            f'{block_label}:\n'
            f'{current_code}\n'
            '\n'
            'Apply the requested change to this current '
            + ('file' if contract == 'plugin_source' else 'module')
            + ' rather than generating unrelated code from scratch, and '
            f'return the complete modified content via the {TOOL_NAME} tool.')

    ctx = context or {}
    if contract in NODE_DESIGNER_CONTRACTS:
        active = ctx.get('active_file')
        files = ctx.get('files') or {}
        file_paths = list(ctx.get('file_paths') or [])
        if active:
            sections.append(f'ACTIVE FILE: {active}')
        if files:
            other = ['OTHER FILES:']
            for path in sorted(files):
                if path == active:
                    continue
                other.append(f'--- {path} ---\n{files[path]}\n--- end {path} ---')
            if len(other) > 1:
                sections.append('\n'.join(other))
        if file_paths:
            listed = []
            for path in file_paths:
                if path == active or path in files:
                    listed.append(f'- {path}')
                else:
                    listed.append(f'- {path} [content omitted]')
            sections.append('FILE PATHS:\n' + '\n'.join(listed))

    if diagnostics and isinstance(diagnostics.get('text'), str):
        kind = diagnostics.get('kind', 'user')
        arch = diagnostics.get('architecture')
        header = f'DIAGNOSTIC OUTPUT ({kind}' + (f', {arch}' if arch else '') + '):'
        sections.append(f"{header}\n{diagnostics['text']}")

    return '\n\n'.join(sections)


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
    if not spec['entry_points']:
        # Plugin_Source_Contract: any non-empty file content is valid (5.9).
        return None if code.strip() else 'empty file content'
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
                            'description': 'the complete file content'
                        },
                        'notes': {
                            'type': 'string',
                            'description': 'one short paragraph for the user'
                        },
                        'target_file': {
                            'type': 'string',
                            'description': (
                                'the Source_Tree path the code applies to '
                                'when it is not the active file; must be '
                                'one of the FILE PATHS')
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

def generate_code(contract: str, system_prompt: str, user_message: str,
                  context: Optional[Dict] = None) -> Dict:
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

    # Target_File resolution (custom-node-source-lifecycle 5.7-5.9): the
    # model may redirect the fix to another provided Source_Tree file; the
    # effective contract follows the resolved target.
    target_file, effective_contract, target_error = resolve_target(
        tool_input.get('target_file'), contract, context)
    if target_error is not None:
        logger.error(f"Model named a target file outside the Source_Tree: "
                     f"{target_error}")
        return error_response(
            422, 'INVALID_TARGET_FILE',
            'The assistant named a file that is not part of this plugin. '
            'Please retry.',
            {'target_file': target_error})

    defect = validate_entry_point(code, effective_contract)
    if defect is not None:
        logger.error(f"Generated code rejected ({effective_contract}): {defect}")
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
            f"({CONTRACTS[effective_contract]['signature']}). Please retry or "
            'rephrase the prompt.',
            {'defect': defect, 'contract': effective_contract}
        )

    notes = tool_input.get('notes')
    payload: Dict[str, Any] = {
        'code': code,
        'notes': notes if isinstance(notes, str) else '',
        'model_id': config['model_id'],
        'contract': effective_contract,
    }
    if target_file is not None:
        payload['target_file'] = target_file
    return create_response(200, payload)


def resolve_target(target_file: Any, contract: str, context: Optional[Dict]
                   ) -> Tuple[Optional[str], str, Optional[str]]:
    """
    Resolve the Target_File and effective contract of a model response
    (custom-node-source-lifecycle 5.7-5.9, Property 15). Returns
    (target_file, effective_contract, invalid_target) where exactly one of
    target_file/invalid_target is meaningful when the response named a
    file:

    - Workflow_Builder contracts ignore `target_file` (no Source_Tree):
      (None, contract, None).
    - Node_Designer contracts: an absent/blank `target_file` resolves to
      context.active_file; a named file must be among context.file_paths
      (else invalid_target). The effective contract is `frame_hook` when
      the resolved target is the Frame_Processing_Hook and
      `plugin_source` otherwise; without any file context the request's
      contract stands.
    """
    if contract not in NODE_DESIGNER_CONTRACTS:
        return None, contract, None
    ctx = context or {}
    file_paths = [str(p) for p in (ctx.get('file_paths') or [])]
    active = ctx.get('active_file') if isinstance(ctx.get('active_file'), str) else None

    named = target_file if isinstance(target_file, str) and target_file.strip() else None
    if named is not None:
        named = named.strip()
        if named not in file_paths:
            return None, contract, named
        resolved = named
    else:
        resolved = active

    if resolved is None:
        return None, contract, None
    effective = 'frame_hook' if resolved == HOOK_FILE else 'plugin_source'
    return resolved, effective, None


def handle_code_assist(event: Dict, user: Dict) -> Dict:
    """
    POST /code-assist
    Body: {usecase_id, surface, contract, prompt, current_code?, context?,
           diagnostics?}

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
    context = body.get('context')
    diagnostics = body.get('diagnostics')
    return generate_code(
        contract=contract,
        system_prompt=build_system_prompt(contract, context, diagnostics),
        user_message=build_user_message(body['prompt'], body.get('current_code'),
                                        context, diagnostics, contract),
        context=context,
    )
