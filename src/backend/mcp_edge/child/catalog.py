# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The Edge_MCP_Server tool catalog (child process; design §5
``child/catalog.py`` and the "Edge catalog" table).

``CATALOG`` holds every edge tool of every phase, transcribed row by row
from the design table; ``mcp_edge.DELIVERED_PHASES`` is the only phase gate,
so a spec outside it is neither listed nor accepted. The machinery mirrors
the portal catalog's (``ToolSpec``, schema generation for the pagination
arguments, the path-parameter identifier pattern, the bare-identifier rule,
``render_description``), minus the portal's ``scope``/``permissions``: the
edge gate is ``role_policy`` (D13). Line numbers in comments are
documentation only; the edge catalog honesty suites (task 11.13) hold every
row to the route source.

No I/O; ``mcp.types`` is imported only by ``to_mcp_tool``.
"""
import copy
import re
from dataclasses import dataclass, field
from typing import Literal, Mapping, Optional, Sequence, Tuple, Union

from mcp_edge import DELIVERED_PHASES

# ---------------------------------------------------------------- constants

TOOL_NAME_RE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
IDENTIFIER_PATTERN = r'^[A-Za-z0-9._:/-]{1,256}$'
# An argument that fills a path parameter: no '/', not '.' or '..'.
PATH_PARAM_PATTERN = r'^(?!\.{1,2}$)[A-Za-z0-9._:@+=-]{1,256}$'
# Argument names that would name a device path (Property 2; no edge
# input_schema property may match it). A param_map target may, only when
# the mapped property's pattern is anchored on a URL scheme.
PATH_KEY_RE = re.compile(
    r'^(.*[Pp]ath|.*[Dd]ir(ectory)?|.*[Ll]ocation|.*[Ff]ile[Nn]ame|.*[Ff]older)$')
STREAM_URL_PATTERN = r'^rtsps?://|^rtmps?://'
# Bare names that become part of a file name on the device.
BARE_NAME_PATTERN = r'^[A-Za-z0-9._-]{1,64}$'

LIMIT_MAX = 200
LIMIT_DEFAULT = 50
FREE_TEXT_MAX_LENGTH = 20_000
# Base64 arguments: ~700 KiB decoded, under the proxy's 1 MiB request cap
# with the JSON-RPC envelope.
BASE64_MAX_LENGTH = 960_000

# Arguments the child consumes itself; a member is forwarded iff param_map
# names it (the edge has no usecase_id).
SERVER_CONSUMED_ARGS = ('confirm', 'limit', 'offset', 'tail_chars')

# The fixed note an RO spec with accepted_effects carries (Decision point 4).
ACCEPTED_EFFECT_NOTE = (
    "process-level housekeeping: the device runtime's own effect, the same "
    "one the HMI's use of the route has; nothing the caller supplied is "
    "persisted")

# body_constants sentinel: replaced by int(time.time()) at dispatch.
NOW_EPOCH_S = '__now_epoch_s__'

# (tool, argument) pairs of RO string properties that carry neither a
# pattern nor an enum.
RO_FREE_TEXT_ARGS = frozenset({
    ('edge_textgen_generate', 'prompt'),        # generation input
    ('edge_textgen_generate', 'image'),         # generation input (base64)
    ('edge_results_list', 'text_note_filter'),  # a result filter, <= 50 chars
    ('edge_execution_node_image_get', 'port'),  # selects a frame the run reported
})

READ_ONLY = 'read_only'
MUTATING = 'mutating'

Classification = Literal['read_only', 'mutating']
RolePolicy = Literal['read', 'mutate']
ParamLocation = Literal['path', 'query', 'body']

_PATH_SEGMENT_RE = re.compile(r'\{([^{}+]+)\+?\}')
_BODY_METHODS = ('POST', 'PUT', 'PATCH')


class CatalogError(ValueError):
    """A spec that cannot be defined as written (raised at import)."""


# ------------------------------------------------- schema authoring helpers

def identifier(description: Optional[str] = None) -> dict:
    prop = {'type': 'string', 'pattern': IDENTIFIER_PATTERN}
    if description:
        prop['description'] = description
    return prop


def text(max_length: int = FREE_TEXT_MAX_LENGTH,
         description: Optional[str] = None) -> dict:
    prop = {'type': 'string', 'maxLength': max_length}
    if description:
        prop['description'] = description
    return prop


def enum(*values, description: Optional[str] = None) -> dict:
    prop = {'type': 'string', 'enum': list(values)}
    if description:
        prop['description'] = description
    return prop


def boolean(description: Optional[str] = None) -> dict:
    prop = {'type': 'boolean'}
    if description:
        prop['description'] = description
    return prop


def integer(minimum: Optional[int] = None, maximum: Optional[int] = None,
            default: Optional[int] = None,
            description: Optional[str] = None) -> dict:
    prop = {'type': 'integer'}
    if minimum is not None:
        prop['minimum'] = minimum
    if maximum is not None:
        prop['maximum'] = maximum
    if default is not None:
        prop['default'] = default
    if description:
        prop['description'] = description
    return prop


def array(items: dict, min_items: Optional[int] = None,
          max_items: Optional[int] = None,
          description: Optional[str] = None) -> dict:
    prop = {'type': 'array', 'items': items}
    if min_items is not None:
        prop['minItems'] = min_items
    if max_items is not None:
        prop['maxItems'] = max_items
    if description:
        prop['description'] = description
    return prop


def json_object(description: Optional[str] = None) -> dict:
    """A JSON object the route validates itself (mutating specs only)."""
    prop = {'type': 'object'}
    if description:
        prop['description'] = description
    return prop


def bare_name(description: Optional[str] = None) -> dict:
    prop = {'type': 'string', 'pattern': BARE_NAME_PATTERN}
    if description:
        prop['description'] = description
    return prop


def confirm_arg(description: Optional[str] = None) -> dict:
    """``confirm`` of an ``M!`` spec; never in ``required`` (a missing value
    is ``confirmation_required``, not ``invalid_input``)."""
    prop = {'type': 'string', 'maxLength': 256}
    if description:
        prop['description'] = description
    return prop


LIMIT = {'type': 'integer'}
OFFSET = {'type': 'integer'}
PAGE = {'type': 'integer'}


def schema(properties: Optional[Mapping[str, dict]] = None,
           required: Sequence[str] = (),
           one_of: Optional[Sequence[Sequence[str]]] = None) -> dict:
    """A Draft 2020-12 object schema with ``additionalProperties: false``."""
    result = {'type': 'object', 'properties': dict(properties or {}),
              'additionalProperties': False}
    if required:
        result['required'] = list(required)
    if one_of is not None:
        result['oneOf'] = [{'required': list(branch)} for branch in one_of]
    return result


PAGE_ARGS = {'limit': LIMIT, 'offset': OFFSET}


# ----------------------------------------------------------------- specs

def _camel(name: str) -> str:
    head, *rest = name.split('_')
    return head + ''.join(part[:1].upper() + part[1:] for part in rest)


@dataclass(frozen=True)
class ToolSpec:
    """One edge tool. Field semantics: design.md §5 ``child/catalog.py``."""
    name: str
    title: str
    description: str
    resource_type: str
    input_schema: dict
    handler_module: str              # e.g. 'endpoints.system'
    handler_function: str
    method: str
    resource: str                    # the route template, e.g. '/workflows/{workflowId}'
    classification: Classification = READ_ONLY
    phase: int = 1
    role_policy: Optional[RolePolicy] = None   # derived from classification
    feature: Optional[str] = None              # None or 'vllm'
    path_template: Optional[str] = None
    param_map: Mapping[str, str] = field(default_factory=dict)
    # Where a forwarded non-path argument travels ('query' | 'body');
    # default body for POST/PUT/PATCH, query otherwise.
    param_in: Mapping[str, str] = field(default_factory=dict)
    # Renames inside the items of an array argument:
    # {argument: {item key: route item key}}.
    item_param_map: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    destructive: bool = False
    idempotent: bool = True
    confirm_target: Union[str, Tuple[str, ...], None] = None
    confirm_literal: Optional[str] = None
    credential_in_query: bool = False
    accepted_effects: Tuple[str, ...] = ()
    accepted_effects_note: Optional[str] = None
    scrub_paths: bool = True
    image_field: Optional[str] = None
    result_constant: Optional[dict] = None
    drop_fields: Tuple[str, ...] = ()
    result_shape: Literal['object', 'collection'] = 'object'
    collection_key: Optional[str] = None       # None: the body is a root array
    limit_max: Optional[int] = None
    limit_default: Optional[int] = None
    body_constants: Mapping[str, object] = field(default_factory=dict)
    timeout_s: float = 60.0
    body_fields: Optional[Tuple[str, ...]] = None
    text_tail_arg: Optional[str] = None
    resource_id_from_body: bool = False
    resource_id_path: Optional[str] = None
    status_tool: Optional[str] = None
    deprecated_alias: Optional[str] = None
    alias_removed_in: Optional[str] = None

    def __post_init__(self):
        set_ = object.__setattr__
        set_(self, 'accepted_effects', tuple(self.accepted_effects))
        set_(self, 'drop_fields', tuple(self.drop_fields))
        if self.body_fields is not None:
            set_(self, 'body_fields', tuple(self.body_fields))
        if isinstance(self.confirm_target, list):
            set_(self, 'confirm_target', tuple(self.confirm_target))
        if self.role_policy is None:
            set_(self, 'role_policy', 'read' if self.read_only else 'mutate')
        if self.path_template is None:
            set_(self, 'path_template', self.resource)
        input_schema = copy.deepcopy(self.input_schema)
        properties = input_schema.setdefault('properties', {})
        param_map = dict(self.param_map)
        self._apply_bare_identifier_rule(properties, param_map)
        self._generate_pagination(properties)
        for argument, target in param_map.items():
            if target in self.path_parameters and argument in properties:
                properties[argument] = dict(properties[argument], type='string',
                                            pattern=PATH_PARAM_PATTERN)
        set_(self, 'input_schema', input_schema)
        set_(self, 'param_map', param_map)
        set_(self, 'param_in', dict(self.param_in))
        set_(self, 'item_param_map', {k: dict(v) for k, v in self.item_param_map.items()})
        set_(self, 'body_constants', dict(self.body_constants))

    @property
    def path_parameters(self) -> Tuple[str, ...]:
        return tuple(_PATH_SEGMENT_RE.findall(self.resource or ''))

    def _apply_bare_identifier_rule(self, properties: dict, param_map: dict) -> None:
        """A bare identifier (a name ending in ``_id`` with no ``param_map``
        entry) fills the path parameter of its own name or its camelCase
        form; else, when exactly one remains, the one path parameter left
        unmapped. Two bare identifiers that would claim one path parameter
        are a definition error; a bare identifier left once every path
        parameter is filled travels under its own name."""
        path_parameters = self.path_parameters
        filled = {target for target in param_map.values() if target in path_parameters}
        bare = [name for name in properties
                if name.endswith('_id') and name not in param_map]
        remaining = []
        for name in bare:
            for candidate in (name, _camel(name)):
                if candidate in path_parameters and candidate not in filled:
                    param_map[name] = candidate
                    filled.add(candidate)
                    break
            else:
                remaining.append(name)
        unfilled = [p for p in path_parameters if p not in filled]
        if not remaining or not unfilled:
            return
        if len(remaining) == 1 and len(unfilled) == 1:
            param_map[remaining[0]] = unfilled[0]
            return
        raise CatalogError(
            f'{self.name}: bare identifiers {remaining} would claim the path '
            f'parameter(s) {unfilled} of {self.resource}; write the mapping '
            'explicitly')

    def _generate_pagination(self, properties: dict) -> None:
        generated = {
            'limit': {'type': 'integer', 'minimum': 1,
                      'maximum': self.limit_max or LIMIT_MAX,
                      'default': self.limit_default or min(
                          LIMIT_DEFAULT, self.limit_max or LIMIT_MAX)},
            'offset': {'type': 'integer', 'minimum': 0, 'default': 0},
            'page': {'type': 'integer', 'minimum': 1, 'default': 1},
        }
        for name, canonical in generated.items():
            if name in properties:
                description = properties[name].get('description')
                properties[name] = dict(canonical)
                if description:
                    properties[name]['description'] = description

    @property
    def read_only(self) -> bool:
        return self.classification == READ_ONLY

    def location_of(self, argument: str) -> ParamLocation:
        target = self.param_map.get(argument, argument)
        if argument in self.param_map and target in self.path_parameters:
            return 'path'
        if argument in self.param_in:
            return self.param_in[argument]
        return 'body' if self.method.upper() in _BODY_METHODS else 'query'


# ------------------------------------------------------------- rendering

def confirm_text(spec: ToolSpec) -> str:
    if isinstance(spec.confirm_target, tuple):
        return '<' + ' | '.join(spec.confirm_target) + '>'
    if spec.confirm_target:
        return spec.confirm_target
    return repr(spec.confirm_literal)


def classification_text(spec: ToolSpec) -> str:
    if spec.read_only:
        return 'Read-only.'
    if not spec.destructive:
        return 'MUTATING.'
    return f'MUTATING, DESTRUCTIVE — requires confirm={confirm_text(spec)}.'


def render_description(spec: ToolSpec) -> str:
    """D35: the base sentence, what it touches on this device, the class."""
    return (f'{spec.description} Touches: {spec.resource_type} on this device. '
            f'{classification_text(spec)}')


def alias_description(spec: ToolSpec) -> str:
    return (f'Deprecated alias of {spec.name}; removed in '
            f'{spec.alias_removed_in}. {render_description(spec)}')


def to_mcp_tool(spec: ToolSpec, alias: bool = False):
    from mcp.types import Tool, ToolAnnotations

    return Tool(
        name=spec.deprecated_alias if alias else spec.name,
        title=spec.title,
        description=alias_description(spec) if alias else render_description(spec),
        input_schema=spec.input_schema,
        annotations=ToolAnnotations(
            read_only_hint=spec.read_only,
            destructive_hint=spec.destructive,
            idempotent_hint=spec.idempotent,
            open_world_hint=False))


def listed(spec: ToolSpec, delivered_phases, mutating_enabled: bool,
           features: Sequence[str]) -> bool:
    """A spec is listed (and accepted) iff its phase is delivered, its
    feature (if any) is present, and it is read-only or the mutating switch
    is on."""
    if spec.phase not in delivered_phases:
        return False
    if spec.feature is not None and spec.feature not in set(features):
        return False
    return spec.read_only or bool(mutating_enabled)


def tools_for(delivered_phases: Sequence[int] = DELIVERED_PHASES,
              mutating_enabled: bool = False, features: Sequence[str] = (),
              catalog: Optional[Sequence[ToolSpec]] = None) -> list:
    """The ``tools/list`` result, sorted by name, with alias entries."""
    specs = CATALOG if catalog is None else catalog
    entries = []
    for spec in specs:
        if not listed(spec, delivered_phases, mutating_enabled, features):
            continue
        entries.append((spec.name, to_mcp_tool(spec)))
        if spec.deprecated_alias:
            entries.append((spec.deprecated_alias, to_mcp_tool(spec, alias=True)))
    entries.sort(key=lambda entry: entry[0])
    return [tool for _name, tool in entries]


def find(name: str, catalog: Optional[Sequence[ToolSpec]] = None) -> Optional[ToolSpec]:
    """The spec named ``name`` (or whose deprecated alias it is), or None."""
    specs = CATALOG if catalog is None else catalog
    for spec in specs:
        if name in (spec.name, spec.deprecated_alias):
            return spec
    return None


# ----------------------------------------------------------------- rows

SWEEP = 'stream_ingest.protocol.sweep_segments:os.unlink'
PIPELINE_EFFECTS = (
    SWEEP,
    'utils.utils.run_command:subprocess.run',
    'utils.captured_images_utils.delete_image:os.remove',
    'workflow_engine.csi_capture.write_csi_config:open',
    'workflow_engine.csi_capture.write_csi_config:os.makedirs',
    'workflow_engine.csi_capture.write_csi_config:os.chmod',
    'gstreamer.pipeline_builder.GstPipelineBuilder._stage_decoded_png:Image.save',
)


def _effects(*effects: str) -> dict:
    return {'accepted_effects': effects, 'accepted_effects_note': ACCEPTED_EFFECT_NOTE}


WORKFLOW_ID = identifier('The workflow id.')
REGISTRATION_ID = identifier('The workflow registration id.')
EXECUTION_ID = identifier('The workflow execution id.')
CAPTURE_ID = identifier('The capture id (an inference result id).')
CAMERA_ID = identifier('The camera id.')
IMAGE_SOURCE_ID = identifier('The image source id.')
IMAGE_SOURCE_TYPES = ('Camera', 'ICam', 'NvidiaCSI', 'RTSP', 'RTMP')
STREAM_URL = {'type': 'string', 'pattern': STREAM_URL_PATTERN, 'maxLength': 2048,
              'description': 'The rtsp(s):// or rtmp(s):// Stream_URL.'}


def _execution_read(suffix: str, name: str, title: str, description: str,
                    function: str, **extra) -> ToolSpec:
    properties = {'execution_id': EXECUTION_ID, **extra.pop('properties', {})}
    return ToolSpec(
        name=name, title=title, description=description,
        resource_type='workflow_execution',
        input_schema=schema(properties, required=['execution_id']),
        handler_module='workflow_engine.api', handler_function=function,
        method='GET', resource=f'/workflows/executions/{{execution_id}}{suffix}',
        **extra)


CATALOG: Tuple[ToolSpec, ...] = (
    # ----------------------------------------------------------- identity
    ToolSpec(
        name='edge_whoami', title='Who am I',
        description=("Returns the caller's username, synced role, credential "
                     'source and token expiry.'),
        resource_type='principal', input_schema=schema(),
        handler_module='endpoints.local_auth', handler_function='local_auth_whoami',
        method='GET', resource='/local-auth/whoami'),
    # ------------------------------------------------------------- system
    ToolSpec(
        name='edge_system_health_get', title='Get system health',
        description='Returns CPU, memory, disk and GPU health of the device.',
        resource_type='system', input_schema=schema(),
        handler_module='endpoints.system', handler_function='get_system_health',
        method='GET', resource='/system-health'),
    ToolSpec(
        name='edge_system_component_status_get', title='Get DDA component status',
        description='Returns the status of the DDA LocalServer component.',
        resource_type='system', input_schema=schema(),
        handler_module='endpoints.system', handler_function='get_dda_component_status',
        method='GET', resource='/dda-component-status'),
    ToolSpec(
        name='edge_system_greengrass_components_list',
        title='List Greengrass components',
        description='Lists the Greengrass components on the device and their state.',
        resource_type='component', input_schema=schema(PAGE_ARGS),
        handler_module='endpoints.system',
        handler_function='list_greengrass_components', method='GET',
        resource='/greengrass-components', result_shape='collection',
        collection_key='components'),
    ToolSpec(
        name='edge_system_snapshot_create', title='Create a system snapshot',
        description=('Writes a new diagnostic snapshot archive, deleting every earlier '
                     'one; the archive stays on the device.'),
        resource_type='snapshot', input_schema=schema({'confirm': confirm_arg(
            "The literal 'snapshot'.")}),
        handler_module='endpoints.system', handler_function='get_snapshot',
        method='GET', resource='/snapshot', classification=MUTATING, phase=2,
        destructive=True, confirm_literal='snapshot', idempotent=False,
        status_tool='edge_system_health_get'),
    ToolSpec(
        name='edge_system_station_get', title='Get the station',
        description='Returns the station name and settings (the logo image is dropped).',
        resource_type='station', input_schema=schema(),
        handler_module='endpoints.system', handler_function='get_station',
        method='GET', resource='/system/station', drop_fields=('logoImage',)),
    ToolSpec(
        name='edge_system_restart_dda', title='Restart DDA',
        description='Restarts the DDA LocalServer component.',
        resource_type='system', input_schema=schema({'confirm': confirm_arg(
            "The literal 'restart-dda'.")}),
        handler_module='endpoints.system', handler_function='restart_dda',
        method='POST', resource='/restart-dda', classification=MUTATING, phase=3,
        destructive=True, confirm_literal='restart-dda', idempotent=False,
        status_tool='edge_system_health_get'),
    ToolSpec(
        name='edge_mcp_status_get', title='Get MCP server status',
        description=('Returns whether this MCP server is enabled and listening, its '
                     'version and restart count.'),
        resource_type='mcp_server', input_schema=schema(),
        handler_module='endpoints.mcp_proxy', handler_function='get_mcp_status',
        method='GET', resource='/mcp/status'),
    # ---------------------------------------------------------- workflows
    ToolSpec(
        name='edge_workflows_list', title='List workflows',
        description='Lists the workflows defined on the device.',
        resource_type='workflow', input_schema=schema(PAGE_ARGS),
        handler_module='endpoints.workflow', handler_function='list_workflows',
        method='GET', resource='/workflows', result_shape='collection',
        **_effects(SWEEP)),
    ToolSpec(
        name='edge_workflow_get', title='Get a workflow',
        description='Returns one workflow definition with its sources and configurations.',
        resource_type='workflow',
        input_schema=schema({'workflow_id': WORKFLOW_ID}, required=['workflow_id']),
        handler_module='endpoints.workflow', handler_function='get_workflow',
        method='GET', resource='/workflows/{workflowId}', **_effects(SWEEP)),
    ToolSpec(
        name='edge_workflow_images_list', title='List workflow images',
        description=('Returns at most two recent images of a digital-input-triggered '
                     'workflow; page with offset.'),
        resource_type='image',
        input_schema=schema({'workflow_id': WORKFLOW_ID, **PAGE_ARGS},
                            required=['workflow_id']),
        handler_module='endpoints.workflow', handler_function='list_images',
        method='GET', resource='/workflows/{workflow_id}/images',
        param_map={'limit': 'maxResults', 'offset': 'startingPoint'},
        result_shape='collection', collection_key='images',
        limit_max=2, limit_default=2,
        drop_fields=('images[].image', 'images[].inferenceResult.mask_image')),
    ToolSpec(
        name='edge_workflow_capture_task_get', title='Get a workflow capture task',
        description="Returns the state of a workflow's background capture task.",
        resource_type='workflow',
        input_schema=schema({'workflow_id': WORKFLOW_ID}, required=['workflow_id']),
        handler_module='endpoints.workflow',
        handler_function='get_capture_task_status', method='GET',
        resource='/workflows/{workflow_id}/capture-task'),
    ToolSpec(
        name='edge_workflow_create', title='Create a workflow',
        description=('Creates an empty workflow and returns its id; define it with '
                     'edge_workflow_update.'),
        resource_type='workflow', input_schema=schema(),
        handler_module='endpoints.workflow', handler_function='create_workflow',
        method='POST', resource='/workflows', classification=MUTATING, phase=3,
        idempotent=False, resource_id_from_body=True,
        status_tool='edge_workflows_list'),
    ToolSpec(
        name='edge_workflow_update', title='Replace a workflow definition',
        description=('Replaces the whole workflow definition (omitted fields are reset) '
                     'and restarts its digital-input process; read it first with '
                     'edge_workflow_get.'),
        resource_type='workflow',
        input_schema=schema({
            'workflow_id': WORKFLOW_ID,
            'confirm': confirm_arg('Repeat workflow_id.'),
            'name': text(256),
            'imageSources': array({
                'type': 'object',
                'properties': {'imageSourceId': identifier()},
                'required': ['imageSourceId'], 'additionalProperties': False},
                min_items=1),
            'description': text(2000),
            'featureConfigurations': array({
                'type': 'object',
                'properties': {'type': {'type': 'string', 'enum': ['LFVModel']},
                               'modelName': identifier()},
                'required': ['type'], 'additionalProperties': False}),
            'inputConfigurations': array(json_object()),
            'outputConfigurations': array(json_object()),
        }, required=['workflow_id', 'name', 'imageSources']),
        handler_module='endpoints.workflow', handler_function='modify_workflows',
        method='PATCH', resource='/workflows/{workflowId}',
        classification=MUTATING, phase=3, destructive=True,
        confirm_target='workflow_id', status_tool='edge_workflow_get'),
    ToolSpec(
        name='edge_workflow_run', title='Run a workflow',
        description=('Runs a workflow once (or starts its capture task); fetch the '
                     'output frame with edge_capture_output_image_get.'),
        resource_type='workflow',
        input_schema=schema({
            'workflow_id': WORKFLOW_ID,
            'capture_image_count': integer(1, 100),
            'capture_time_interval': integer(1, 120, description='Seconds.'),
            'capture_prefix': bare_name(),
            'return_partial_results_early': boolean(),
        }, required=['workflow_id']),
        handler_module='endpoints.workflow',
        handler_function='run_inference_for_stream', method='POST',
        resource='/workflows/{workflow_id}/run',
        param_map={'capture_image_count': 'captureImageCount',
                   'capture_time_interval': 'captureTimeInterval',
                   'capture_prefix': 'capturePrefix',
                   'return_partial_results_early': 'returnPartialResultsEarly'},
        body_constants={'returnImageString': False},
        drop_fields=('inferenceResult.mask_image',),
        classification=MUTATING, phase=3, idempotent=False,
        status_tool='edge_results_list'),
    ToolSpec(
        name='edge_workflow_retry', title='Restart a workflow',
        description="Restarts a workflow's digital-input process.",
        resource_type='workflow',
        input_schema=schema({'workflow_id': WORKFLOW_ID,
                             'confirm': confirm_arg('Repeat workflow_id.')},
                            required=['workflow_id']),
        handler_module='endpoints.workflow', handler_function='retry_dio_workflow',
        method='GET', resource='/workflows/{workflow_id}/retry',
        classification=MUTATING, phase=3, destructive=True,
        confirm_target='workflow_id', status_tool='edge_workflow_get'),
    ToolSpec(
        name='edge_workflow_delete', title='Delete a workflow',
        description="Deletes a workflow and disconnects its camera.",
        resource_type='workflow',
        input_schema=schema({'workflow_id': WORKFLOW_ID,
                             'confirm': confirm_arg('Repeat workflow_id.')},
                            required=['workflow_id']),
        handler_module='endpoints.workflow', handler_function='delete_workflow',
        method='DELETE', resource='/workflows/{workflow_id}',
        classification=MUTATING, phase=3, destructive=True,
        confirm_target='workflow_id', status_tool='edge_workflows_list'),
    # ------------------------------------------------------ registrations
    ToolSpec(
        name='edge_registrations_list', title='List workflow registrations',
        description='Lists the workflow-engine registrations, optionally with retired ones.',
        resource_type='workflow_registration',
        input_schema=schema({'include_inactive': boolean(), **PAGE_ARGS}),
        handler_module='workflow_engine.api',
        handler_function='list_workflow_registrations', method='GET',
        resource='/workflows/registrations',
        param_map={'include_inactive': 'includeInactive'},
        result_shape='collection'),
    ToolSpec(
        name='edge_registration_get', title='Get a workflow registration',
        description=('Returns one registration with its trigger health; list its '
                     'executions with edge_registration_executions_list.'),
        resource_type='workflow_registration',
        input_schema=schema({'registration_id': REGISTRATION_ID},
                            required=['registration_id']),
        handler_module='workflow_engine.api',
        handler_function='get_workflow_registration', method='GET',
        resource='/workflows/registrations/{registration_id}',
        drop_fields=('executions',)),
    ToolSpec(
        name='edge_registration_graph_get', title='Get a registration graph',
        description='Returns the node graph of a workflow registration.',
        resource_type='workflow_registration',
        input_schema=schema({'registration_id': REGISTRATION_ID},
                            required=['registration_id']),
        handler_module='workflow_engine.api',
        handler_function='get_workflow_registration_graph', method='GET',
        resource='/workflows/registrations/{registration_id}/graph'),
    ToolSpec(
        name='edge_registration_executions_list', title='List registration executions',
        description='Returns the newest executions of a registration (at most 50).',
        resource_type='workflow_execution',
        input_schema=schema({'registration_id': REGISTRATION_ID, 'limit': LIMIT,
                             'notable': boolean()}, required=['registration_id']),
        handler_module='workflow_engine.api',
        handler_function='list_registration_executions', method='GET',
        resource='/workflows/registrations/{registration_id}/executions',
        param_map={'limit': 'limit', 'notable': 'notable'},
        result_shape='collection', limit_max=50),
    ToolSpec(
        name='edge_registration_continuous_status_get',
        title='Get continuous-run status',
        description='Returns the continuous-run state of a registration.',
        resource_type='workflow_registration',
        input_schema=schema({'registration_id': REGISTRATION_ID},
                            required=['registration_id']),
        handler_module='workflow_engine.api', handler_function='get_continuous_status',
        method='GET', resource='/workflows/registrations/{registration_id}/continuous'),
    ToolSpec(
        name='edge_registration_trigger', title='Trigger a registration',
        description='Starts one execution of a workflow registration.',
        resource_type='workflow_execution',
        input_schema=schema({'registration_id': REGISTRATION_ID},
                            required=['registration_id']),
        handler_module='workflow_engine.api', handler_function='trigger_workflow',
        method='POST', resource='/workflows/registrations/{registration_id}/trigger',
        classification=MUTATING, phase=3, idempotent=False,
        status_tool='edge_registration_executions_list'),
    ToolSpec(
        name='edge_registration_continuous_pause', title='Pause continuous runs',
        description='Pauses the continuous runs of a registration.',
        resource_type='workflow_registration',
        input_schema=schema({'registration_id': REGISTRATION_ID,
                             'confirm': confirm_arg('Repeat registration_id.')},
                            required=['registration_id']),
        handler_module='workflow_engine.api',
        handler_function='pause_continuous_workflow', method='POST',
        resource='/workflows/registrations/{registration_id}/continuous/pause',
        classification=MUTATING, phase=3, destructive=True,
        confirm_target='registration_id',
        status_tool='edge_registration_continuous_status_get'),
    ToolSpec(
        name='edge_registration_continuous_resume', title='Resume continuous runs',
        description='Resumes the continuous runs of a registration.',
        resource_type='workflow_registration',
        input_schema=schema({'registration_id': REGISTRATION_ID,
                             'confirm': confirm_arg('Repeat registration_id.')},
                            required=['registration_id']),
        handler_module='workflow_engine.api',
        handler_function='resume_continuous_workflow', method='POST',
        resource='/workflows/registrations/{registration_id}/continuous/resume',
        classification=MUTATING, phase=3, destructive=True,
        confirm_target='registration_id',
        status_tool='edge_registration_continuous_status_get'),
    # --------------------------------------------------------- executions
    _execution_read('', 'edge_execution_get', 'Get a workflow execution',
                    'Returns one workflow execution record.', 'get_workflow_execution'),
    _execution_read('/results', 'edge_execution_results_get',
                    'Get execution results',
                    'Returns the results and artifacts an execution reported.',
                    'get_workflow_execution_results'),
    _execution_read('/log', 'edge_execution_log_get', 'Get an execution log',
                    ('Returns the tail of an execution log as text (tail_chars '
                     'characters at most).'),
                    'get_workflow_execution_log',
                    properties={'tail_chars': integer(1, 150_000, 150_000)},
                    text_tail_arg='tail_chars'),
    _execution_read('/node-status', 'edge_execution_node_status_get',
                    'Get execution node status',
                    'Returns the per-node status of an execution.',
                    'get_workflow_execution_node_status'),
    _execution_read('/metadata', 'edge_execution_metadata_get',
                    'Get execution metadata',
                    'Returns the metadata an execution recorded.',
                    'get_workflow_execution_metadata'),
    _execution_read('/overlay', 'edge_execution_overlay_get', 'Get an execution overlay',
                    'Returns the overlay mask of an execution as an image.',
                    'get_workflow_execution_overlay', image_field='maskImage'),
    ToolSpec(
        name='edge_execution_output_image_get', title='Get an execution output image',
        description='Returns the output image of a workflow execution.',
        resource_type='image',
        input_schema=schema({'execution_id': EXECUTION_ID}, required=['execution_id']),
        handler_module='endpoints.download_file',
        handler_function='load_workflow_execution_output_image', method='GET',
        resource='/workflows/executions/{execution_id}/output-image',
        credential_in_query=True),
    ToolSpec(
        name='edge_execution_overlay_image_get', title='Get an execution overlay image',
        description='Returns the overlay image of a workflow execution.',
        resource_type='image',
        input_schema=schema({'execution_id': EXECUTION_ID}, required=['execution_id']),
        handler_module='endpoints.download_file',
        handler_function='load_workflow_execution_overlay_image', method='GET',
        resource='/workflows/executions/{execution_id}/overlay-image',
        credential_in_query=True),
    ToolSpec(
        name='edge_execution_node_image_get', title='Get an execution node image',
        description=('Returns the frame a node reported on a port, as listed by '
                     'edge_execution_results_get.'),
        resource_type='image',
        input_schema=schema({'execution_id': EXECUTION_ID,
                             'node_id': identifier('The node id.'),
                             'port': text(256, 'The node output port.')},
                            required=['execution_id', 'node_id', 'port']),
        handler_module='endpoints.download_file',
        handler_function='load_workflow_execution_node_image', method='GET',
        resource='/workflows/executions/{execution_id}/node-image',
        param_map={'node_id': 'nodeId', 'port': 'port'}, credential_in_query=True),
    ToolSpec(
        name='edge_capture_input_image_get', title='Get a capture input image',
        description='Returns the input image of one capture of a workflow.',
        resource_type='image',
        input_schema=schema({'workflow_id': WORKFLOW_ID, 'capture_id': CAPTURE_ID},
                            required=['workflow_id', 'capture_id']),
        handler_module='endpoints.download_file',
        handler_function='load_input_image_from_worflow_by_capture_id', method='GET',
        resource='/workflows/{workflowId}/capture-details/{captureId}/input-image',
        credential_in_query=True),
    ToolSpec(
        name='edge_capture_output_image_get', title='Get a capture output image',
        description='Returns the output image of one capture of a workflow.',
        resource_type='image',
        input_schema=schema({'workflow_id': WORKFLOW_ID, 'capture_id': CAPTURE_ID},
                            required=['workflow_id', 'capture_id']),
        handler_module='endpoints.download_file',
        handler_function='load_output_image_from_worflow_by_capture_id', method='GET',
        resource='/workflows/{workflowId}/capture-details/{captureId}/output-image',
        credential_in_query=True),
    # ------------------------------------------------------------ results
    ToolSpec(
        name='edge_results_summary_get', title='Get the results summary',
        description="Returns a workflow's result statistics since the last reset.",
        resource_type='inference_result',
        input_schema=schema({'workflow_id': WORKFLOW_ID}, required=['workflow_id']),
        handler_module='endpoints.inference_result',
        handler_function='get_inference_result_summary', method='GET',
        resource='/workflows/{workflow_id}/results/summary'),
    ToolSpec(
        name='edge_results_list', title='List inference results',
        description='Returns one page of a workflow\'s inference results, newest first.',
        resource_type='inference_result',
        input_schema=schema({
            'workflow_id': WORKFLOW_ID,
            'page': PAGE, 'limit': LIMIT,
            'prediction': enum('Normal', 'Anomaly'),
            'downloaded': boolean(),
            'text_note_filter': text(50),
            'human_classification_provided': boolean(),
            'human_review_required': boolean(),
            'capture_type': enum('Inference', 'Capture'),
        }, required=['workflow_id']),
        handler_module='endpoints.inference_result',
        handler_function='list_inference_results', method='GET',
        resource='/workflows/{workflow_id}/results',
        param_map={'page': 'page', 'limit': 'size',
                   'text_note_filter': 'textNoteFilter',
                   'human_classification_provided': 'humanClassificationProvided',
                   'human_review_required': 'humanReviewRequired',
                   'capture_type': 'captureType'},
        result_shape='collection', collection_key='results', limit_max=100,
        drop_fields=('results[].maskImage',)),
    ToolSpec(
        name='edge_result_get', title='Get an inference result',
        description='Returns one inference result; its mask, if any, as an image.',
        resource_type='inference_result',
        input_schema=schema({'workflow_id': WORKFLOW_ID, 'capture_id': CAPTURE_ID},
                            required=['workflow_id', 'capture_id']),
        handler_module='endpoints.inference_result',
        handler_function='get_inference_result', method='GET',
        resource='/workflows/{workflow_id}/results/{capture_id}',
        image_field='maskImage'),
    ToolSpec(
        name='edge_results_update', title='Update inference results',
        description='Sets review flags, classifications and notes on inference results.',
        resource_type='inference_result',
        input_schema=schema({
            'workflow_id': WORKFLOW_ID,
            'inference_results': array({
                'type': 'object',
                'properties': {
                    'capture_id': identifier(),
                    'flag_for_review': boolean(),
                    'downloaded': boolean(),
                    'human_classification': enum('Normal', 'Anomaly'),
                    'text_note': text(50),
                },
                'required': ['capture_id'], 'additionalProperties': False,
            }, min_items=1, max_items=100),
        }, required=['workflow_id', 'inference_results']),
        handler_module='endpoints.inference_result',
        handler_function='update_inference_results', method='PATCH',
        resource='/workflows/{workflow_id}/results',
        param_map={'inference_results': 'inferenceResults'},
        item_param_map={'inference_results': {
            'capture_id': 'captureId', 'flag_for_review': 'flagForReview',
            'human_classification': 'humanClassification', 'text_note': 'textNote'}},
        classification=MUTATING, phase=2, status_tool='edge_results_list'),
    ToolSpec(
        name='edge_results_summary_reset', title='Reset the results summary',
        description='Restarts the result statistics of a workflow from now (or reset_time).',
        resource_type='inference_result',
        input_schema=schema({'workflow_id': WORKFLOW_ID,
                             'reset_time': integer(minimum=0,
                                                   description='Epoch seconds.'),
                             'confirm': confirm_arg('Repeat workflow_id.')},
                            required=['workflow_id']),
        handler_module='endpoints.inference_result',
        handler_function='reset_inference_result_summary_start_time', method='POST',
        resource='/workflows/{workflow_id}/results/reset',
        param_map={'reset_time': 'resetTime'},
        body_constants={'resetTime': NOW_EPOCH_S},
        classification=MUTATING, phase=2, destructive=True,
        confirm_target='workflow_id', status_tool='edge_results_summary_get'),
    ToolSpec(
        name='edge_result_delete', title='Delete an inference result',
        description='Deletes one inference result.',
        resource_type='inference_result',
        input_schema=schema({'workflow_id': WORKFLOW_ID, 'capture_id': CAPTURE_ID,
                             'confirm': confirm_arg('Repeat capture_id.')},
                            required=['workflow_id', 'capture_id']),
        handler_module='endpoints.inference_result',
        handler_function='delete_inference_result_by_capture_id', method='DELETE',
        resource='/workflows/{workflow_id}/results/{capture_id}',
        classification=MUTATING, phase=3, destructive=True,
        confirm_target='capture_id', status_tool='edge_results_list'),
    # ------------------------------------------------------------ cameras
    ToolSpec(
        name='edge_cameras_list', title='List cameras',
        description='Lists the GenICam/USB cameras the device detected.',
        resource_type='camera', input_schema=schema(PAGE_ARGS),
        handler_module='endpoints.camera', handler_function='getcameras',
        method='GET', resource='/cameras', result_shape='collection'),
    ToolSpec(
        name='edge_camera_feature_bounds_get', title='Get camera feature bounds',
        description='Returns the adjustable features of a camera and their bounds.',
        resource_type='camera',
        input_schema=schema({'camera_id': CAMERA_ID}, required=['camera_id']),
        handler_module='endpoints.camera',
        handler_function='get_camera_feature_bounds_endpoint', method='GET',
        resource='/cameras/{cameraId}/feature-bounds'),
    ToolSpec(
        name='edge_cameras_rescan', title='Rescan cameras',
        description='Re-enumerates the GenICam/USB camera bus.',
        resource_type='camera', input_schema=schema(),
        handler_module='endpoints.camera', handler_function='rescan_cameras_endpoint',
        method='POST', resource='/cameras/rescan', classification=MUTATING, phase=2,
        status_tool='edge_cameras_list'),
    ToolSpec(
        name='edge_camera_connect', title='Connect a camera',
        description='Connects a camera for acquisition.',
        resource_type='camera',
        input_schema=schema({'camera_id': CAMERA_ID}, required=['camera_id']),
        handler_module='endpoints.camera', handler_function='connect_camera_endpoint',
        method='GET', resource='/cameras/{cameraId}/connect',
        classification=MUTATING, phase=2, status_tool='edge_cameras_list'),
    ToolSpec(
        name='edge_camera_disconnect', title='Disconnect a camera',
        description='Disconnects a camera, stopping its acquisition.',
        resource_type='camera',
        input_schema=schema({'camera_id': CAMERA_ID,
                             'confirm': confirm_arg('Repeat camera_id.')},
                            required=['camera_id']),
        handler_module='endpoints.camera',
        handler_function='disconnect_camera_endpoint', method='GET',
        resource='/cameras/{cameraId}/disconnect', classification=MUTATING,
        phase=3, destructive=True, confirm_target='camera_id',
        status_tool='edge_cameras_list'),
    ToolSpec(
        name='edge_camera_features_apply', title='Apply camera features',
        description='Applies GenICam feature values to a live camera.',
        resource_type='camera',
        input_schema=schema({
            'camera_id': CAMERA_ID,
            'features': array({
                'type': 'object',
                'properties': {'feature': text(256), 'type': text(64), 'value': {}},
                'required': ['feature', 'type', 'value'],
                'additionalProperties': False}),
        }, required=['camera_id']),
        handler_module='endpoints.camera',
        handler_function='apply_camera_features_endpoint', method='POST',
        resource='/cameras/{cameraId}/features', classification=MUTATING, phase=3,
        status_tool='edge_camera_feature_bounds_get'),
    # ------------------------------------------------------ image sources
    ToolSpec(
        name='edge_image_sources_list', title='List image sources',
        description='Lists the image sources defined on the device with their status.',
        resource_type='image_source', input_schema=schema(PAGE_ARGS),
        handler_module='endpoints.image_source', handler_function='list_image_sources',
        method='GET', resource='/image-sources', result_shape='collection',
        **_effects(SWEEP)),
    ToolSpec(
        name='edge_image_source_get', title='Get an image source',
        description='Returns one image source with its status.',
        resource_type='image_source',
        input_schema=schema({'image_source_id': IMAGE_SOURCE_ID},
                            required=['image_source_id']),
        handler_module='endpoints.image_source', handler_function='get_image_source',
        method='GET', resource='/image-sources/{imageSourceId}', **_effects(SWEEP)),
    ToolSpec(
        name='edge_image_source_configurations_get',
        title='Get image source configurations',
        description='Returns the stored image-source acquisition configurations.',
        resource_type='image_source', input_schema=schema(),
        handler_module='endpoints.image_source',
        handler_function='read_image_source_configurations', method='GET',
        resource='/image-source-configurations'),
    ToolSpec(
        name='edge_image_source_stream_health_get', title='Get stream health',
        description="Returns the ingest health of a stream image source.",
        resource_type='image_source',
        input_schema=schema({'image_source_id': IMAGE_SOURCE_ID},
                            required=['image_source_id']),
        handler_module='endpoints.image_source', handler_function='get_stream_health',
        method='GET', resource='/image-sources/{imageSourceId}/stream-health',
        **_effects(SWEEP)),
    ToolSpec(
        name='edge_image_source_test_connection', title='Test a stream connection',
        description=('Tests the connection of a stream image source and returns its '
                     'first frame.'),
        resource_type='image_source',
        input_schema=schema({'image_source_id': IMAGE_SOURCE_ID},
                            required=['image_source_id']),
        handler_module='endpoints.image_source',
        handler_function='test_stream_connection', method='POST',
        resource='/image-sources/{imageSourceId}/test-connection',
        image_field='image', **_effects(*PIPELINE_EFFECTS)),
    ToolSpec(
        name='edge_image_source_preview', title='Preview an image source',
        description='Returns a preview frame of an image source with its stored settings.',
        resource_type='image_source',
        input_schema=schema({'image_source_id': IMAGE_SOURCE_ID},
                            required=['image_source_id']),
        handler_module='endpoints.image_source', handler_function='preview_image',
        method='POST', resource='/image-sources/{imageSourceId}/preview',
        image_field='image', **_effects(*PIPELINE_EFFECTS)),
    ToolSpec(
        name='edge_image_source_capture', title='Capture an image',
        description=('Captures one frame from an image source to its capture folder '
                     'and returns the frame.'),
        resource_type='image_source',
        input_schema=schema({'image_source_id': IMAGE_SOURCE_ID,
                             'file_prefix': bare_name()},
                            required=['image_source_id']),
        handler_module='endpoints.image_source', handler_function='capture',
        method='POST', resource='/image-sources/{imageSourceId}/capture',
        param_map={'file_prefix': 'filePrefix'}, image_field='image',
        result_constant={'captured': True}, classification=MUTATING, phase=2,
        idempotent=False, status_tool='edge_image_source_get'),
    ToolSpec(
        name='edge_image_source_add', title='Add an image source',
        description='Adds a camera, NVIDIA CSI or RTSP/RTMP stream image source.',
        resource_type='image_source',
        input_schema=schema({
            'type': enum(*IMAGE_SOURCE_TYPES),
            'name': text(256), 'description': text(2000),
            'camera_id': identifier(),
            'stream_url': STREAM_URL,
            'stream_settings': json_object(),
            'credentials': json_object('Stream credentials; stored write-only.'),
        }),
        handler_module='endpoints.image_source', handler_function='add_image_source',
        method='POST', resource='/image-sources',
        param_map={'camera_id': 'cameraId', 'stream_url': 'location',
                   'stream_settings': 'streamSettings'},
        classification=MUTATING, phase=3, idempotent=False,
        status_tool='edge_image_sources_list'),
    ToolSpec(
        name='edge_image_source_update', title='Update an image source',
        description='Updates the settings of an image source.',
        resource_type='image_source',
        input_schema=schema({
            'image_source_id': IMAGE_SOURCE_ID,
            'name': text(256), 'description': text(2000),
            'image_source_configuration': json_object(),
            'stream_url': STREAM_URL,
            'stream_settings': json_object(),
            'credentials': json_object('Stream credentials; stored write-only.'),
            'clear_credentials': boolean(),
        }, required=['image_source_id']),
        handler_module='endpoints.image_source',
        handler_function='update_image_source', method='PATCH',
        resource='/image-sources/{image_source_id}',
        param_map={'image_source_configuration': 'imageSourceConfiguration',
                   'stream_url': 'location', 'stream_settings': 'streamSettings',
                   'clear_credentials': 'clearCredentials'},
        classification=MUTATING, phase=3, status_tool='edge_image_source_get'),
    ToolSpec(
        name='edge_image_source_delete', title='Delete an image source',
        description='Deletes an image source.',
        resource_type='image_source',
        input_schema=schema({'image_source_id': IMAGE_SOURCE_ID,
                             'confirm': confirm_arg('Repeat image_source_id.')},
                            required=['image_source_id']),
        handler_module='endpoints.image_source',
        handler_function='delete_image_source', method='DELETE',
        resource='/image-sources/{image_source_id}', classification=MUTATING,
        phase=3, destructive=True, confirm_target='image_source_id',
        status_tool='edge_image_sources_list'),
    # ------------------------------------------------------------ streams
    ToolSpec(
        name='edge_streams_capabilities_get', title='Get stream capabilities',
        description="Returns the device's stream codec capabilities.",
        resource_type='stream', input_schema=schema(),
        handler_module='endpoints.streams', handler_function='get_stream_capabilities',
        method='GET', resource='/streams/capabilities',
        **_effects(SWEEP, 'stream_ingest.capabilities.run_probe:subprocess.run')),
    ToolSpec(
        name='edge_stream_viewers_get', title='Get stream viewers',
        description='Returns the live viewers of a camera stream.',
        resource_type='stream',
        input_schema=schema({'camera_id': CAMERA_ID}, required=['camera_id']),
        handler_module='endpoints.streams', handler_function='get_stream_viewers',
        method='GET', resource='/streams/{camera_id}/viewers'),
    # --------------------------------------------------------- static pins
    ToolSpec(
        name='edge_static_image_pin_get', title='Get the static image pin',
        description='Returns whether a static image is pinned and its metadata.',
        resource_type='static_pin', input_schema=schema(),
        handler_module='endpoints.static_image_camera', handler_function='get_pin_status',
        method='GET', resource='/static-image-camera/pin'),
    ToolSpec(
        name='edge_static_video_pin_get', title='Get the static video pin',
        description='Returns whether a static video is pinned and its metadata.',
        resource_type='static_pin', input_schema=schema(),
        handler_module='endpoints.static_video_camera',
        handler_function='get_video_pin_status', method='GET',
        resource='/static-video-camera/pin'),
    ToolSpec(
        name='edge_static_image_pin', title='Pin a static image',
        description=('Pins a JPEG, PNG or BMP image (base64, about 700 KiB at most) as '
                     'the static image camera; larger images are pinned in the HMI.'),
        resource_type='static_pin',
        input_schema=schema({
            'image_base64': {'type': 'string', 'maxLength': BASE64_MAX_LENGTH,
                             'pattern': r'^[A-Za-z0-9+/]*={0,2}$'},
            'file_name': {'type': 'string', 'pattern': r'^[A-Za-z0-9._-]{1,128}$'},
        }, required=['image_base64', 'file_name']),
        handler_module='endpoints.static_image_camera',
        handler_function='pin_static_image', method='POST',
        resource='/static-image-camera/pin', classification=MUTATING, phase=2,
        status_tool='edge_static_image_pin_get'),
    ToolSpec(
        name='edge_static_image_unpin', title='Remove the static image pin',
        description='Removes the pinned static image.',
        resource_type='static_pin',
        input_schema=schema({'confirm': confirm_arg("The literal 'static-image'.")}),
        handler_module='endpoints.static_image_camera',
        handler_function='unpin_static_image', method='DELETE',
        resource='/static-image-camera/pin', classification=MUTATING, phase=2,
        destructive=True, confirm_literal='static-image',
        status_tool='edge_static_image_pin_get'),
    ToolSpec(
        name='edge_static_video_unpin', title='Remove the static video pin',
        description='Removes the pinned static video.',
        resource_type='static_pin',
        input_schema=schema({'confirm': confirm_arg("The literal 'static-video'.")}),
        handler_module='endpoints.static_video_camera',
        handler_function='unpin_static_video', method='DELETE',
        resource='/static-video-camera/pin', classification=MUTATING, phase=2,
        destructive=True, confirm_literal='static-video',
        status_tool='edge_static_video_pin_get'),
    # ---------------------------------------------- feature configurations
    ToolSpec(
        name='edge_feature_configurations_list', title='List feature configurations',
        description='Lists the models deployed to the device and their state.',
        resource_type='feature_configuration', input_schema=schema(PAGE_ARGS),
        handler_module='endpoints.feature_config',
        handler_function='list_feature_configs', method='GET',
        resource='/feature-configurations', result_shape='collection'),
    ToolSpec(
        name='edge_gpu_status_get', title='Get GPU status',
        description='Returns the GPU state of the inference runtime.',
        resource_type='feature_configuration', input_schema=schema(),
        handler_module='endpoints.feature_config', handler_function='get_gpu_status',
        method='GET', resource='/feature-configurations/gpu-status'),
    ToolSpec(
        name='edge_input_configurations_list', title='List input configurations',
        description='Lists the digital-input configurations.',
        resource_type='input_configuration', input_schema=schema(PAGE_ARGS),
        handler_module='endpoints.feature_config',
        handler_function='list_input_configs', method='GET',
        resource='/input-configurations', result_shape='collection'),
    ToolSpec(
        name='edge_output_configurations_list', title='List output configurations',
        description='Lists the output configurations.',
        resource_type='output_configuration', input_schema=schema(PAGE_ARGS),
        handler_module='endpoints.feature_config',
        handler_function='list_output_configs', method='GET',
        resource='/output-configurations', result_shape='collection'),
    ToolSpec(
        name='edge_model_start', title='Start a model',
        description='Starts (loads) a deployed model in the inference runtime.',
        resource_type='model',
        input_schema=schema({'model_name': identifier('The model name (model-...).')},
                            required=['model_name']),
        handler_module='endpoints.feature_config',
        handler_function='start_feature_config', method='GET',
        resource='/feature-configurations/models/{modelName}/start',
        param_map={'model_name': 'modelName'}, classification=MUTATING, phase=3,
        status_tool='edge_feature_configurations_list'),
    ToolSpec(
        name='edge_model_stop', title='Stop a model',
        description='Stops (unloads) a model in the inference runtime.',
        resource_type='model',
        input_schema=schema({'model_name': identifier('The model name (model-...).'),
                             'confirm': confirm_arg('Repeat model_name.')},
                            required=['model_name']),
        handler_module='endpoints.feature_config',
        handler_function='stop_feature_configs', method='GET',
        resource='/feature-configurations/models/{modelName}/stop',
        param_map={'model_name': 'modelName'}, classification=MUTATING, phase=3,
        destructive=True, confirm_target='model_name',
        status_tool='edge_feature_configurations_list'),
    # ----------------------------------------------------- text generation
    ToolSpec(
        name='edge_textgen_models_list', title='List text-generation models',
        description='Lists the vLLM text-generation models loaded on the device.',
        resource_type='text_generation_model', input_schema=schema(PAGE_ARGS),
        handler_module='endpoints.text_generation',
        handler_function='list_text_generation_models', method='GET',
        resource='/text-generation/models', feature='vllm',
        result_shape='collection'),
    ToolSpec(
        name='edge_textgen_generate', title='Generate text',
        description=('Generates text with a loaded vLLM model from a prompt and an '
                     'optional base64 image; may take up to 130 s.'),
        resource_type='text_generation_model',
        input_schema=schema({
            'model_name': identifier('A loaded text-generation model.'),
            'prompt': text(),
            'image': text(BASE64_MAX_LENGTH, 'Base64 image (about 700 KiB at most).'),
            'max_tokens': integer(minimum=1),
        }, required=['model_name', 'prompt']),
        handler_module='endpoints.text_generation', handler_function='generate_text',
        method='POST', resource='/text-generation/{model_name}/generate',
        param_map={'model_name': 'model_name'}, feature='vllm',
        body_fields=('prompt', 'image', 'max_tokens'), timeout_s=130.0),
)
