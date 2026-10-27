"""The Portal_MCP_Server tool catalog: `ToolSpec`, rendering and listing.

`CATALOG` is the single source of truth for every portal tool (design.md
"Tool catalogs"; the catalog tables are transcribed row by row). Every row of
every phase is authored; `mcp_portal.DELIVERED_PHASES` is the only phase gate,
so a spec outside it is neither listed nor accepted (Requirement 17.3).

This module holds the declarative machinery only, no I/O:

* `ToolSpec` and `AcceptedWrite` (design "2. `mcp_portal` package",
  `catalog.py` row), with the definition-time schema generation for the
  pagination arguments and the path-parameter identifier pattern, and the
  bare-identifier rule of the Tool-catalogs conventions;
* `render_description` / `classification_text` / `to_mcp_tool` (D35);
* `tools_for_view` (D8: filtered by delivered phase and the mutating switch,
  never by principal; `(domain, name)` order; deprecated aliases listed);
* `catalog_routes` / `catalog_tools` and `python -m mcp_portal.catalog
  --export-routes`, which writes `catalog_routes.json` and
  `catalog_tools.json` next to this module.
"""
import copy
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Literal, Mapping, Optional, Sequence, Tuple, Union

from mcp_portal import DELIVERED_PHASES, VIEWS

# ---------------------------------------------------------------- constants

TOOL_NAME_RE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')

# Identifier arguments (Input validation). An identifier that fills a path
# parameter may not contain `/` and may not be `.` or `..`, so a crafted
# value can neither add a path segment nor climb one.
IDENTIFIER_PATTERN = r'^[A-Za-z0-9._:/-]{1,256}$'
PATH_PARAM_PATTERN = r'^(?!\.{1,2}$)[A-Za-z0-9._:@+=-]{1,256}$'

# IoT thing / thing-group names as the devices routes accept them
# (`devices.py` THING_NAME_PATTERN).
THING_NAME_PATTERN = r'^[a-zA-Z0-9:_-]{1,128}$'

LIMIT_MAX = 200
LIMIT_DEFAULT = 50
NEXT_TOKEN_MAX_LENGTH = 2048
FREE_TEXT_MAX_LENGTH = 20_000

# Arguments the server consumes itself. A member is forwarded to the backing
# route iff `param_map` names it; nothing in the set is passed implicitly.
SERVER_CONSUMED_ARGS = ('confirm', 'usecase_id', 'limit', 'offset')

# Argument names that would collide with a token claim (Input validation).
CLAIM_NAMED_ARGS = ('sub', 'iss', 'aud', 'scope', 'client_id', 'token_use',
                    'username', 'email')

# The fixed note an `RO` spec with `accepted_writes` carries (Decision point
# 4, "Accepted reading — status sync").
STATUS_SYNC_NOTE = (
    'status sync: the GET refreshes its own stored record from the '
    'authoritative external state — the same write the web UI performs when '
    'it shows the record; nothing the caller supplied is persisted')

READ_ONLY = 'read_only'
MUTATING = 'mutating'

Scope = Literal['usecase', 'global', 'usecase_or_global']
Classification = Literal['read_only', 'mutating']
UsecaseNotForwarded = Optional[Literal['stored_record', 'injected', 'dead_filter']]
ParamLocation = Literal['path', 'query', 'body']

_PATH_SEGMENT_RE = re.compile(r'\{([^{}+]+)\+?\}')
_BODY_METHODS = ('POST', 'PUT', 'PATCH')


class CatalogError(ValueError):
    """A spec that cannot be defined as written (raised at import)."""


# ------------------------------------------------- schema authoring helpers

def identifier(description: Optional[str] = None) -> dict:
    """An identifier argument; the path pattern is applied automatically when
    the argument fills a path parameter."""
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
            description: Optional[str] = None) -> dict:
    prop = {'type': 'integer'}
    if minimum is not None:
        prop['minimum'] = minimum
    if maximum is not None:
        prop['maximum'] = maximum
    if description:
        prop['description'] = description
    return prop


def number(minimum: Optional[float] = None, maximum: Optional[float] = None,
           description: Optional[str] = None) -> dict:
    prop = {'type': 'number'}
    if minimum is not None:
        prop['minimum'] = minimum
    if maximum is not None:
        prop['maximum'] = maximum
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


def string_map(description: Optional[str] = None) -> dict:
    """An object of string values (tags, labels)."""
    prop = {'type': 'object',
            'additionalProperties': {'type': 'string', 'maxLength': 256}}
    if description:
        prop['description'] = description
    return prop


def json_object(description: Optional[str] = None) -> dict:
    """A free-form JSON object the route validates itself (mutating specs only)."""
    prop = {'type': 'object'}
    if description:
        prop['description'] = description
    return prop


def confirm_arg(description: Optional[str] = None) -> dict:
    """The `confirm` argument of an `M!` spec. Not listed in `required`: a
    missing value is `confirmation_required`, not `invalid_input`."""
    prop = {'type': 'string', 'maxLength': 256}
    if description:
        prop['description'] = description
    return prop


USECASE_ID = {'type': 'string', 'pattern': IDENTIFIER_PATTERN,
              'description': 'The Use_Case the call acts in.'}
# A Use_Case argument that may not name the account-level 'global' row
# (Requirement 5.7; the two role rows of the admin view).
USECASE_ID_NOT_GLOBAL = dict(USECASE_ID, **{'not': {'const': 'global'}})


def uc_schema(properties: Optional[Mapping[str, dict]] = None,
              required: Sequence[str] = (),
              usecase: Optional[dict] = None, **groups) -> dict:
    """`schema()` with the required `usecase_id` of a Use_Case-scoped spec."""
    props = {'usecase_id': dict(usecase or USECASE_ID)}
    props.update(properties or {})
    return schema(props, ['usecase_id', *required], **groups)


# Placeholders for the generated pagination properties: `ToolSpec` replaces
# each with its canonical schema (a `description` given here is kept).
LIMIT = {'type': 'integer'}
OFFSET = {'type': 'integer'}
PAGE = {'type': 'integer'}
NEXT_TOKEN = {'type': 'string'}


def schema(properties: Optional[Mapping[str, dict]] = None,
           required: Sequence[str] = (),
           one_of: Optional[Sequence[Sequence[str]]] = None,
           any_of: Optional[Sequence[Sequence[str]]] = None) -> dict:
    """A Draft 2020-12 object schema with `additionalProperties: false`.

    `one_of` / `any_of` take the `required` sets of the branches: a top-level
    group may differ only in `required` (an exactly-one / at-least-one
    argument group the route enforces)."""
    result = {'type': 'object', 'properties': dict(properties or {}),
              'additionalProperties': False}
    if required:
        result['required'] = list(required)
    if one_of is not None:
        result['oneOf'] = [{'required': list(branch)} for branch in one_of]
    if any_of is not None:
        result['anyOf'] = [{'required': list(branch)} for branch in any_of]
    return result


# ----------------------------------------------------------------- specs

@dataclass(frozen=True)
class AcceptedWrite:
    """A write call the classification walk tolerates on an `RO` spec,
    matched on `(function, call, receiver)` with exactly `count` sites."""
    function: str   # '<module>.<function>'
    call: str       # the called attribute, e.g. 'update_item'
    receiver: str   # the receiver's source text, e.g. 'table'
    count: int = 1


def _camel(name: str) -> str:
    head, *rest = name.split('_')
    return head + ''.join(part[:1].upper() + part[1:] for part in rest)


@dataclass(frozen=True)
class ToolSpec:
    """One portal tool. Field semantics: design.md §2, `catalog.py` row."""
    name: str
    domain: str
    title: str
    description: str
    resource_type: str
    input_schema: dict
    backend: Optional[str]            # McpBackendKey; None for the local spec
    handler_module: str
    handler_function: str
    method: Optional[str]
    resource: Optional[str]
    permissions: Tuple = ()
    scope: Scope = 'usecase'
    classification: Classification = READ_ONLY
    phase: int = 1
    path_template: Optional[str] = None
    param_map: Mapping[str, str] = field(default_factory=dict)
    # Where a forwarded argument travels when it does not fill a path
    # parameter: 'query' or 'body'. Default: 'body' for POST/PUT/PATCH,
    # 'query' otherwise.
    param_in: Mapping[str, str] = field(default_factory=dict)
    usecase_arg: Optional[str] = None
    usecase_not_forwarded: UsecaseNotForwarded = None
    destructive: bool = False
    idempotent: bool = True
    confirm_target: Union[str, Tuple[str, ...], None] = None
    confirm_literal: Optional[str] = None
    handler_audits: bool = False
    handler_audits_denials: bool = False
    audit_args: Tuple[str, ...] = ()
    accepted_writes: Tuple[AcceptedWrite, ...] = ()
    accepted_writes_note: Optional[str] = None
    bind_usecase_path: Optional[str] = None
    result_shape: Literal['object', 'collection'] = 'object'
    collection_key: Optional[str] = None
    deprecated_alias: Optional[str] = None
    alias_removed_in: Optional[str] = None
    status_tool: Optional[str] = None
    limit_max: Optional[int] = None
    limit_default: Optional[int] = None
    resource_id_path: Optional[str] = None

    def __post_init__(self):
        set_ = object.__setattr__
        set_(self, 'permissions', tuple(self.permissions))
        set_(self, 'audit_args', tuple(self.audit_args))
        set_(self, 'accepted_writes', tuple(self.accepted_writes))
        if isinstance(self.confirm_target, list):
            set_(self, 'confirm_target', tuple(self.confirm_target))
        if self.path_template is None:
            set_(self, 'path_template', self.resource)
        if self.usecase_arg is None and self.scope != 'global':
            set_(self, 'usecase_arg', 'usecase_id')
        input_schema = copy.deepcopy(self.input_schema)
        properties = input_schema.setdefault('properties', {})
        param_map = dict(self.param_map)
        self._apply_bare_identifier_rule(properties, param_map)
        self._generate_pagination(properties)
        for argument, target in param_map.items():
            if target in self.path_parameters and argument in properties:
                prop = dict(properties[argument], type='string',
                            pattern=PATH_PARAM_PATTERN)
                authored = properties[argument].get('pattern')
                if authored and authored not in (IDENTIFIER_PATTERN,
                                                 PATH_PARAM_PATTERN):
                    # A narrower authored pattern (THING_NAME_PATTERN) is
                    # kept alongside the path-parameter pattern.
                    prop['allOf'] = [{'pattern': authored}]
                properties[argument] = prop
        set_(self, 'input_schema', input_schema)
        set_(self, 'param_map', param_map)
        set_(self, 'param_in', dict(self.param_in))

    # ------------------------------------------------ definition-time rules

    @property
    def path_parameters(self) -> Tuple[str, ...]:
        return tuple(_PATH_SEGMENT_RE.findall(self.resource or ''))

    def _apply_bare_identifier_rule(self, properties: dict,
                                    param_map: dict) -> None:
        """Fill path parameters from bare identifier arguments.

        A bare identifier (a name ending in `_id`, not `usecase_id`, with no
        `param_map` entry) fills the path parameter of its own name or its
        camelCase form; else, when exactly one remains, the one path
        parameter left unmapped. Two bare identifiers that would claim one
        path parameter are a definition error (revision 11, review-10 N2).
        A bare identifier left once every path parameter is filled travels
        under its own name.
        """
        path_parameters = self.path_parameters
        filled = {target for target in param_map.values()
                  if target in path_parameters}
        bare = [name for name in properties
                if name.endswith('_id') and name != 'usecase_id'
                and name not in param_map]
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
                      'default': self.limit_default or LIMIT_DEFAULT},
            'offset': {'type': 'integer', 'minimum': 0, 'default': 0},
            'page': {'type': 'integer', 'minimum': 1, 'default': 1},
            'next_token': {'type': 'string', 'maxLength': NEXT_TOKEN_MAX_LENGTH},
        }
        for name, canonical in generated.items():
            if name in properties:
                description = properties[name].get('description')
                properties[name] = dict(canonical)
                if description:
                    properties[name]['description'] = description

    # ------------------------------------------------------------ queries

    @property
    def read_only(self) -> bool:
        return self.classification == READ_ONLY

    def location_of(self, argument: str) -> ParamLocation:
        """Where a forwarded argument travels: its `param_map` target fills a
        path parameter ('path'), else `param_in`, else by method."""
        target = self.param_map.get(argument, argument)
        if argument in self.param_map and target in self.path_parameters:
            return 'path'
        if argument in self.param_in:
            return self.param_in[argument]
        return 'body' if (self.method or '').upper() in _BODY_METHODS else 'query'


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
    """D35: the base sentence, what it touches, and the classification."""
    scope_text = ' in the Use_Case named by usecase_id' if spec.usecase_arg else ''
    return (f'{spec.description} Touches: {spec.resource_type}{scope_text}. '
            f'{classification_text(spec)}')


def alias_description(spec: ToolSpec) -> str:
    return (f'Deprecated alias of {spec.name}; removed in '
            f'{spec.alias_removed_in}. {render_description(spec)}')


def to_mcp_tool(spec: ToolSpec, alias: bool = False):
    """The `mcp.types.Tool` of a spec, or of its deprecated alias."""
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


# --------------------------------------------------------------- listing

def _listed(spec: ToolSpec, view: Optional[str], delivered_phases,
            mutating_enabled: bool) -> bool:
    if view is not None and spec.domain != view:
        return False
    if spec.phase not in delivered_phases:
        return False
    return spec.read_only or bool(mutating_enabled)


def tools_for_view(view: Optional[str],
                   delivered_phases: Sequence[int] = DELIVERED_PHASES,
                   mutating_enabled: bool = False,
                   catalog: Optional[Sequence[ToolSpec]] = None) -> list:
    """The `tools/list` result of one endpoint (D8).

    `view` is one of `VIEWS`, or None for the aggregate `/mcp` endpoint.
    A spec is listed iff its phase is delivered and it is read-only or the
    mutating switch is on; never filtered by principal. Each listed spec
    with a `deprecated_alias` contributes a second entry under the alias
    name. Sorted by `(domain, name)`, aliases by their own name.
    """
    if view is not None and view not in VIEWS:
        raise ValueError(f'unknown view: {view!r}')
    specs = CATALOG if catalog is None else catalog
    entries = []
    for spec in specs:
        if not _listed(spec, view, delivered_phases, mutating_enabled):
            continue
        entries.append(((spec.domain, spec.name), to_mcp_tool(spec)))
        if spec.deprecated_alias:
            entries.append(((spec.domain, spec.deprecated_alias),
                            to_mcp_tool(spec, alias=True)))
    entries.sort(key=lambda entry: entry[0])
    return [tool for _key, tool in entries]


# ---------------------------------------------------------------- export

def catalog_routes(catalog: Optional[Sequence[ToolSpec]] = None) -> list:
    """Sorted, de-duplicated `{method, resource, backend}` of every spec with
    a backing route (the local whoami spec contributes none)."""
    specs = CATALOG if catalog is None else catalog
    triples = {(spec.method, spec.resource, spec.backend) for spec in specs
               if spec.backend is not None}
    return [{'method': method, 'resource': resource, 'backend': backend}
            for method, resource, backend in sorted(triples,
                                                    key=lambda t: (t[1], t[0], t[2]))]


def catalog_tools(catalog: Optional[Sequence[ToolSpec]] = None) -> list:
    """`{name, classification, destructive, phase, status_tool}` per spec,
    sorted by name."""
    specs = CATALOG if catalog is None else catalog
    return [{'name': spec.name, 'classification': spec.classification,
             'destructive': spec.destructive, 'phase': spec.phase,
             'status_tool': spec.status_tool}
            for spec in sorted(specs, key=lambda s: s.name)]


ROUTES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'catalog_routes.json')
TOOLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'catalog_tools.json')


def dumps_export(data: list) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + '\n'


def export(routes_file: str = ROUTES_FILE, tools_file: str = TOOLS_FILE,
           catalog: Optional[Sequence[ToolSpec]] = None) -> None:
    with open(routes_file, 'w', encoding='utf-8') as handle:
        handle.write(dumps_export(catalog_routes(catalog)))
    with open(tools_file, 'w', encoding='utf-8') as handle:
        handle.write(dumps_export(catalog_tools(catalog)))


# ----------------------------------------------------------------- rows

# Transcribed from design.md "Tool catalogs", one view at a time. Line
# numbers in comments are documentation only; the honesty suites (tasks
# 5.6-5.11) hold every row to the handler source.

PAGE_ARGS = {'limit': LIMIT, 'offset': OFFSET}

# ------------------------------------------------------------ admin view
# Handlers: usecases.py, user_management.py, user_admin.py, audit_logs.py.

ADMIN: Tuple[ToolSpec, ...] = (
    ToolSpec(
        name='portal_admin_whoami', domain='admin', title='Who am I',
        description=('Returns the calling principal: user id, username, email, '
                     'effective global role and the Use_Cases it can access.'),
        resource_type='principal', input_schema=schema(),
        backend=None, handler_module='server', handler_function='whoami',
        method=None, resource=None, permissions=(), scope='global'),
    ToolSpec(
        name='portal_admin_usecases_list', domain='admin', title='List use cases',
        description='Lists the Use_Cases the caller can access.',
        resource_type='usecase', input_schema=schema(PAGE_ARGS),
        backend='useCases', handler_module='usecases',
        handler_function='list_usecases', method='GET', resource='/usecases',
        permissions=('view_usecases',), scope='global',
        result_shape='collection', collection_key='usecases',
        handler_audits=True),
    ToolSpec(
        name='portal_admin_usecase_get', domain='admin', title='Get a use case',
        description='Returns one Use_Case record.',
        resource_type='usecase', input_schema=uc_schema(),
        backend='useCases', handler_module='usecases',
        handler_function='get_usecase', method='GET', resource='/usecases/{id}',
        param_map={'usecase_id': 'id'}, permissions=('view_usecases',),
        handler_audits=True),
    ToolSpec(
        name='portal_admin_usecase_create', domain='admin', title='Create a use case',
        description=('Creates a single-account Use_Case; the caller becomes its '
                     'UseCaseAdmin.'),
        resource_type='usecase',
        input_schema=schema({
            'name': text(256, 'Display name of the Use_Case.'),
            's3_bucket': identifier('The Use_Case bucket.'),
            'description': text(2000),
            'region': identifier(),
            'owner': text(256),
            'cost_center': text(256),
            'default_device_group': identifier(),
            'tags': string_map(),
            'additional_s3_buckets': array(identifier(), max_items=10),
            'tuning_sample_export': boolean(),
            'tuning_sample_retention_days': integer(minimum=1),
            'provision_shared_components': boolean(),
        }, required=['name', 's3_bucket']),
        backend='useCases', handler_module='usecases',
        handler_function='create_usecase', method='POST', resource='/usecases',
        permissions=('create_usecases',), scope='global',
        classification=MUTATING, phase=3, idempotent=False,
        handler_audits=True, handler_audits_denials=True,
        resource_id_path='usecase.usecase_id',
        status_tool='portal_admin_usecases_list'),
    ToolSpec(
        name='portal_admin_usecase_update', domain='admin', title='Update a use case',
        description='Updates the settings and bucket list of a Use_Case.',
        resource_type='usecase',
        input_schema=uc_schema({
            'name': text(256),
            'description': text(2000),
            's3_bucket': identifier('Cannot be cleared.'),
            'data_s3_bucket': {'type': ['string', 'null'], 'maxLength': 256,
                               'description': "null or '' clears it."},
            'region': identifier(),
            'owner': text(256),
            'default_device_group': identifier(),
            'tuning_sample_export': boolean(),
            'tuning_sample_retention_days': integer(minimum=1),
            'additional_s3_buckets_add': array(identifier(), max_items=10),
            'additional_s3_buckets_remove': array(identifier(), max_items=10),
        }),
        backend='useCases', handler_module='usecases',
        handler_function='update_usecase', method='PUT', resource='/usecases/{id}',
        param_map={'usecase_id': 'id'}, permissions=('update_usecases',),
        classification=MUTATING, phase=3, handler_audits=True,
        status_tool='portal_admin_usecase_get'),
    ToolSpec(
        name='portal_admin_usecase_delete', domain='admin', title='Delete a use case',
        description='Deletes a Use_Case record and removes its account grants.',
        resource_type='usecase',
        input_schema=uc_schema({'confirm': confirm_arg('Repeat usecase_id.')}),
        backend='useCases', handler_module='usecases',
        handler_function='delete_usecase', method='DELETE', resource='/usecases/{id}',
        param_map={'usecase_id': 'id'}, permissions=('delete_usecases',),
        classification=MUTATING, destructive=True, confirm_target='usecase_id',
        phase=3, handler_audits=True, status_tool='portal_admin_usecases_list'),
    ToolSpec(
        name='portal_admin_users_list', domain='admin', title='List users',
        description=('Lists the users with a role assignment, optionally filtered '
                     'to one Use_Case.'),
        resource_type='user',
        input_schema=schema({'usecase_id': identifier(
            'Filter: only assignments in this Use_Case (not the RBAC scope).'),
            **PAGE_ARGS}),
        backend='userManagement', handler_module='user_management',
        handler_function='list_users', method='GET', resource='/users',
        param_map={'usecase_id': 'usecase_id'}, param_in={'usecase_id': 'query'},
        permissions=('manage_users',), scope='global',
        result_shape='collection', collection_key='users'),
    ToolSpec(
        name='portal_admin_user_permissions_get', domain='admin',
        title='Get user permissions',
        description="Returns a user's role and permissions in one Use_Case or globally.",
        resource_type='user',
        input_schema=schema({
            'user_id': identifier('The user (Cognito sub).'),
            'usecase_id': identifier("Filter (not the RBAC scope); absent means 'global'."),
        }, required=['user_id']),
        backend='userManagement', handler_module='user_management',
        handler_function='get_user_permissions', method='GET',
        resource='/users/{user_id}/permissions',
        param_map={'user_id': 'user_id', 'usecase_id': 'usecase_id'},
        param_in={'usecase_id': 'query'},
        permissions=('manage_users',), scope='global'),
    ToolSpec(
        name='portal_admin_role_assign', domain='admin', title='Assign a role',
        description='Assigns a role to a user in one Use_Case.',
        resource_type='user_role',
        input_schema=uc_schema({
            'user_id': identifier('The user (Cognito sub).'),
            'role': enum('Viewer', 'Operator', 'DataScientist', 'UseCaseAdmin',
                         'PortalAdmin', 'DataLabeler'),
        }, required=['user_id', 'role'], usecase=USECASE_ID_NOT_GLOBAL),
        backend='userManagement', handler_module='user_management',
        handler_function='assign_user_role', method='POST',
        resource='/users/assign-role', param_map={'usecase_id': 'usecase_id'},
        permissions=('manage_users',), classification=MUTATING, phase=3,
        handler_audits_denials=True,
        status_tool='portal_admin_user_permissions_get'),
    ToolSpec(
        name='portal_admin_role_remove', domain='admin', title='Remove a role',
        description="Removes a user's role assignment in one Use_Case.",
        resource_type='user_role',
        input_schema=uc_schema({
            'user_id': identifier('The user (Cognito sub).'),
            'confirm': confirm_arg('Repeat user_id.'),
        }, required=['user_id'], usecase=USECASE_ID_NOT_GLOBAL),
        backend='userManagement', handler_module='user_management',
        handler_function='remove_user_role', method='DELETE',
        resource='/users/{user_id}/roles/{usecase_id}',
        param_map={'usecase_id': 'usecase_id'}, permissions=('manage_users',),
        classification=MUTATING, destructive=True, confirm_target='user_id',
        phase=3, handler_audits_denials=True,
        status_tool='portal_admin_users_list'),
    ToolSpec(
        name='portal_admin_audit_logs_query', domain='admin', title='Query audit logs',
        description=('Returns one page of audit log entries, newest first within '
                     'the page; a page can be empty and still carry next_token.'),
        resource_type='audit_log',
        input_schema=schema({
            'action': identifier(),
            'user_id': identifier('Filter: entries written for this user id.'),
            'channel': enum('api', 'mcp'),
            'mcp_tool': identifier(),
            'start_time': integer(minimum=0, description='Epoch milliseconds.'),
            'end_time': integer(minimum=0, description='Epoch milliseconds.'),
            'limit': LIMIT,
            'next_token': NEXT_TOKEN,
        }),
        backend='auditLogs', handler_module='audit_logs',
        handler_function='get_audit_logs', method='GET', resource='/audit-logs',
        param_map={'limit': 'limit', 'next_token': 'next_token'},
        permissions=('system_admin',), scope='global',
        usecase_not_forwarded='dead_filter',
        result_shape='collection', collection_key='logs'),
    ToolSpec(
        name='portal_admin_accounts_list', domain='admin', title='List portal accounts',
        description='Lists the portal user accounts and their account-level roles.',
        resource_type='account', input_schema=schema(PAGE_ARGS),
        backend='userAdmin', handler_module='user_admin',
        handler_function='list_accounts', method='GET', resource='/admin/users',
        permissions=('manage_users',), scope='global',
        handler_audits_denials=True,
        result_shape='collection', collection_key='users'),
    ToolSpec(
        name='portal_admin_account_role_change', domain='admin',
        title='Change an account role',
        description="Changes a portal account's account-level role.",
        resource_type='account',
        input_schema=schema({
            'account_username': identifier('The account username.'),
            'role': enum('PortalAdmin', 'UseCaseAdmin', 'DataScientist',
                         'Operator', 'Viewer', 'DataLabeler'),
        }, required=['account_username', 'role']),
        backend='userAdmin', handler_module='user_admin',
        handler_function='change_role', method='PUT',
        resource='/admin/users/{username}/role',
        param_map={'account_username': 'username'},
        permissions=('manage_users',), scope='global',
        classification=MUTATING, phase=3,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_admin_accounts_list'),
    ToolSpec(
        name='portal_admin_edge_sync_devices_list', domain='admin',
        title='List edge account-sync devices',
        description='Lists the devices that receive portal accounts and their sync status.',
        resource_type='device', input_schema=schema(PAGE_ARGS),
        backend='userAdmin', handler_module='user_admin',
        handler_function='list_sync_devices', method='GET',
        resource='/admin/edge-sync/devices',
        permissions=('manage_users',), scope='global',
        handler_audits_denials=True,
        result_shape='collection', collection_key='devices'),
    ToolSpec(
        name='portal_admin_edge_sync_device_sync', domain='admin',
        title='Sync accounts to a device',
        description=('Sends the complete account set to one device; accounts '
                     'omitted lose local login at the next sync.'),
        resource_type='device',
        input_schema=schema({
            'device_id': identifier('The device (thing name).'),
            'usernames': array(identifier(), min_items=1,
                               description="The device's complete account set."),
            'confirm': confirm_arg('Repeat device_id.'),
        }, required=['device_id', 'usernames']),
        backend='userAdmin', handler_module='user_admin',
        handler_function='sync_device', method='POST',
        resource='/admin/edge-sync/devices/{deviceId}',
        param_map={'device_id': 'deviceId'},
        permissions=('manage_users',), scope='global',
        classification=MUTATING, destructive=True, confirm_target='device_id',
        phase=3, handler_audits_denials=True,
        status_tool='portal_admin_edge_sync_devices_list'),
)

# ---------------------------------------------------------- devices view
# Handlers: devices.py, device_logs.py, device_logs_analyzer.py,
# camera_registry.py (static-video specs: backend cameraVideoPin, the
# camera_video_pin shim's target functions), deployments.py, components.py,
# component_configuration.py, shared_components.py, device_registrations.py.

def thing_name(description: Optional[str] = None) -> dict:
    prop = {'type': 'string', 'pattern': THING_NAME_PATTERN}
    if description:
        prop['description'] = description
    return prop


DEVICE_ID = identifier('The device (IoT thing name).')
UC_QUERY = {'usecase_id': 'usecase_id'}
CAMERA_SOURCE_ID_PATTERN = r'^[A-Za-z0-9_.@+=-]{1,128}$'
TARGET_ARCHITECTURES = ('x86_64', 'x86_64_nvidia', 'arm64_cpu', 'arm64_jp5',
                        'arm64_jp6', 'arm64_jp7')

DEVICES: Tuple[ToolSpec, ...] = (
    ToolSpec(
        name='portal_devices_list', domain='devices', title='List devices',
        description='Lists the Greengrass core devices of a Use_Case with their status.',
        resource_type='device', input_schema=uc_schema(PAGE_ARGS),
        backend='devices', handler_module='devices', handler_function='list_devices',
        method='GET', resource='/devices', param_map=UC_QUERY,
        permissions=('view_devices',), result_shape='collection',
        collection_key='devices', handler_audits=True),
    ToolSpec(
        name='portal_devices_get', domain='devices', title='Get a device',
        description='Returns one device: thing details, Greengrass status and portal flags.',
        resource_type='device',
        input_schema=uc_schema({'device_id': DEVICE_ID}, required=['device_id']),
        backend='devices', handler_module='devices', handler_function='get_device',
        method='GET', resource='/devices/{id}', param_map=UC_QUERY,
        permissions=('view_devices',), handler_audits=True),
    ToolSpec(
        name='portal_devices_flags_update', domain='devices', title='Update device flags',
        description="Sets a device's test-device flag or target architecture.",
        resource_type='device',
        input_schema=uc_schema({
            'device_id': thing_name('The device (IoT thing name).'),
            'test_device': boolean(),
            'target_architecture': {'type': ['string', 'null'],
                                    'enum': [*TARGET_ARCHITECTURES, None]},
        }, required=['device_id'],
            any_of=[['test_device'], ['target_architecture']]),
        backend='devices', handler_module='devices',
        handler_function='update_device_flags', method='PUT', resource='/devices/{id}',
        param_map=UC_QUERY, param_in={'usecase_id': 'body'},
        permissions=('node-designer:manage',), classification=MUTATING, phase=3,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_devices_get'),
    ToolSpec(
        name='portal_devices_delete', domain='devices', title='Delete a device',
        description=("Removes a device's Greengrass core device and portal records; "
                     'the IoT thing and its certificates are kept.'),
        resource_type='device',
        input_schema=uc_schema({
            'device_id': thing_name('The device (IoT thing name).'),
            'confirm': confirm_arg('Repeat device_id.'),
        }, required=['device_id']),
        backend='devices', handler_module='devices', handler_function='delete_device',
        method='DELETE', resource='/devices/{id}', param_map=UC_QUERY,
        permissions=('manage_devices',), classification=MUTATING,
        destructive=True, confirm_target='device_id', phase=3,
        handler_audits=True, status_tool='portal_devices_list'),
    ToolSpec(
        name='portal_devices_logs_list', domain='devices', title='List device log groups',
        description="Lists the CloudWatch log groups of a device's components.",
        resource_type='log_group',
        input_schema=uc_schema({'device_id': DEVICE_ID, **PAGE_ARGS},
                               required=['device_id']),
        backend='deviceLogs', handler_module='device_logs',
        handler_function='list_log_groups', method='GET',
        resource='/devices/{id}/logs', param_map=UC_QUERY,
        permissions=('view_device_logs',), result_shape='collection',
        collection_key='log_groups', handler_audits=True),
    ToolSpec(
        name='portal_devices_logs_get', domain='devices', title='Get component logs',
        description=("Returns one page of a device component's log events; pass back "
                     'start_time and end_time to page a fixed window.'),
        resource_type='log_event',
        input_schema=uc_schema({
            'device_id': DEVICE_ID,
            'component': identifier('The component name.'),
            'limit': LIMIT,
            'next_token': NEXT_TOKEN,
            'start_time': integer(minimum=0, description='Epoch milliseconds.'),
            'end_time': integer(minimum=0, description='Epoch milliseconds.'),
            'filter_pattern': text(1024, 'A CloudWatch Logs filter pattern.'),
        }, required=['device_id', 'component']),
        backend='deviceLogs', handler_module='device_logs',
        handler_function='get_component_logs', method='GET',
        resource='/devices/{id}/logs/{component}',
        param_map={'usecase_id': 'usecase_id', 'component': 'component',
                   'limit': 'limit', 'next_token': 'next_token',
                   'start_time': 'start_time', 'end_time': 'end_time',
                   'filter_pattern': 'filter_pattern'},
        permissions=('view_device_logs',), result_shape='collection',
        collection_key='logs', handler_audits=True),
    ToolSpec(
        name='portal_devices_logs_analyze', domain='devices', title='Analyze device logs',
        description=("Scans a device's recent log events for known error patterns "
                     'and summarizes them.'),
        resource_type='device',
        input_schema=uc_schema({
            'device_id': DEVICE_ID,
            'hours': integer(1, 168, 'Look-back window in hours (default 1).'),
        }, required=['device_id']),
        backend='deviceLogsAnalyzer', handler_module='device_logs_analyzer',
        handler_function='analyze_device_logs', method='POST',
        resource='/devices/{id}/logs/analyze',
        param_map={'usecase_id': 'usecase_id', 'hours': 'hours'},
        param_in={'usecase_id': 'query', 'hours': 'query'},
        permissions=('view_device_logs',), phase=2),
    ToolSpec(
        name='portal_devices_cameras_list', domain='devices', title='List device cameras',
        description=("Lists a device's registered cameras and their synchronization "
                     'state; usecase_id must name the device\'s Use_Case.'),
        resource_type='camera',
        input_schema=uc_schema({'device_id': DEVICE_ID, **PAGE_ARGS},
                               required=['device_id']),
        backend='cameraRegistry', handler_module='camera_registry',
        handler_function='get_cameras', method='GET',
        resource='/devices/{id}/cameras', param_map=UC_QUERY,
        permissions=('view_devices',), handler_audits_denials=True,
        result_shape='collection', collection_key='cameras'),
    ToolSpec(
        name='portal_devices_cameras_conflicts_get', domain='devices',
        title='List camera conflicts',
        description='Lists the camera synchronization conflicts of a device, newest first.',
        resource_type='camera_conflict',
        input_schema=uc_schema({'device_id': DEVICE_ID, **PAGE_ARGS},
                               required=['device_id']),
        backend='cameraRegistry', handler_module='camera_registry',
        handler_function='get_conflicts', method='GET',
        resource='/devices/{id}/cameras/conflicts', param_map=UC_QUERY,
        permissions=('view_devices',), handler_audits_denials=True,
        result_shape='collection', collection_key='conflicts'),
    ToolSpec(
        name='portal_devices_camera_add', domain='devices', title='Add a camera',
        description=('Registers a camera on a device; stream credentials are not '
                     'accepted through this tool.'),
        resource_type='camera',
        input_schema=uc_schema({
            'device_id': DEVICE_ID,
            'name': text(256),
            'type': text(64, 'Camera type, e.g. USB, CSI, RTSP, RTMP.'),
            'params': json_object('Type-specific parameters; RTSP/RTMP need params.url.'),
            'camera_source_id': {'type': 'string', 'pattern': CAMERA_SOURCE_ID_PATTERN},
        }, required=['device_id', 'name', 'type']),
        backend='cameraRegistry', handler_module='camera_registry',
        handler_function='create_camera', method='POST',
        resource='/devices/{id}/cameras',
        param_map={'usecase_id': 'usecase_id', 'camera_source_id': 'camera_source_id'},
        permissions=('manage_devices',), classification=MUTATING, phase=2,
        idempotent=False, handler_audits=True, handler_audits_denials=True,
        resource_id_path='camera_source_id',
        status_tool='portal_devices_cameras_list'),
    ToolSpec(
        name='portal_devices_camera_update', domain='devices', title='Update a camera',
        description="Replaces a camera's name, type and parameters.",
        resource_type='camera',
        input_schema=uc_schema({
            'device_id': DEVICE_ID,
            'camera_id': identifier('The camera source id.'),
            'name': text(256),
            'type': text(64),
            'params': json_object('Replaces the stored parameters wholesale.'),
        }, required=['device_id', 'camera_id', 'name', 'type', 'params']),
        backend='cameraRegistry', handler_module='camera_registry',
        handler_function='update_camera', method='PUT',
        resource='/devices/{id}/cameras/{csid}',
        param_map={'usecase_id': 'usecase_id', 'camera_id': 'csid'},
        param_in={'usecase_id': 'query'},
        permissions=('manage_devices',), classification=MUTATING, phase=2,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_devices_cameras_list'),
    ToolSpec(
        name='portal_devices_camera_delete', domain='devices', title='Delete a camera',
        description='Removes a camera from a device.',
        resource_type='camera',
        input_schema=uc_schema({
            'device_id': DEVICE_ID,
            'camera_id': identifier('The camera source id.'),
            'confirm': confirm_arg('Repeat camera_id.'),
        }, required=['device_id', 'camera_id']),
        backend='cameraRegistry', handler_module='camera_registry',
        handler_function='delete_camera', method='DELETE',
        resource='/devices/{id}/cameras/{csid}',
        param_map={'usecase_id': 'usecase_id', 'camera_id': 'csid'},
        permissions=('manage_devices',), classification=MUTATING,
        destructive=True, confirm_target='camera_id', phase=3,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_devices_cameras_list'),
    ToolSpec(
        name='portal_devices_cameras_refresh', domain='devices', title='Refresh cameras',
        description="Re-reads a device's camera report from its shadow into the registry.",
        resource_type='camera',
        input_schema=uc_schema({'device_id': DEVICE_ID}, required=['device_id']),
        backend='cameraRegistry', handler_module='camera_registry',
        handler_function='refresh_cameras', method='POST',
        resource='/devices/{id}/cameras/refresh', param_map=UC_QUERY,
        param_in={'usecase_id': 'query'},
        permissions=('manage_devices',), classification=MUTATING, phase=2,
        handler_audits_denials=True, status_tool='portal_devices_cameras_list'),
    ToolSpec(
        name='portal_devices_static_image_get', domain='devices',
        title='Get the static-image pin',
        description="Returns the status of a device's static-image camera pin.",
        resource_type='camera_pin',
        input_schema=uc_schema({'device_id': DEVICE_ID}, required=['device_id']),
        backend='cameraRegistry', handler_module='camera_registry',
        handler_function='get_static_image_status', method='GET',
        resource='/devices/{id}/cameras/static-image',
        permissions=('view_devices',), handler_audits_denials=True),
    ToolSpec(
        name='portal_devices_static_video_get', domain='devices',
        title='Get the static-video pin',
        description="Returns the status of a device's static-video camera pin.",
        resource_type='camera_pin',
        input_schema=uc_schema({'device_id': DEVICE_ID}, required=['device_id']),
        backend='cameraVideoPin', handler_module='camera_registry',
        handler_function='get_static_video_status', method='GET',
        resource='/devices/{id}/cameras/static-video',
        permissions=('view_devices',), handler_audits_denials=True),
    ToolSpec(
        name='portal_devices_static_image_unpin', domain='devices',
        title='Remove the static-image pin',
        description="Removes a device's static-image camera pin.",
        resource_type='camera_pin',
        input_schema=uc_schema({'device_id': DEVICE_ID,
                                'confirm': confirm_arg('Repeat device_id.')},
                               required=['device_id']),
        backend='cameraRegistry', handler_module='camera_registry',
        handler_function='remove_static_image_pin', method='DELETE',
        resource='/devices/{id}/cameras/static-image/pin',
        permissions=('manage_devices',), classification=MUTATING,
        destructive=True, confirm_target='device_id', phase=2,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_devices_static_image_get'),
    ToolSpec(
        name='portal_devices_static_video_unpin', domain='devices',
        title='Remove the static-video pin',
        description="Removes a device's static-video camera pin.",
        resource_type='camera_pin',
        input_schema=uc_schema({'device_id': DEVICE_ID,
                                'confirm': confirm_arg('Repeat device_id.')},
                               required=['device_id']),
        backend='cameraVideoPin', handler_module='camera_registry',
        handler_function='remove_static_video_pin', method='DELETE',
        resource='/devices/{id}/cameras/static-video/pin',
        permissions=('manage_devices',), classification=MUTATING,
        destructive=True, confirm_target='device_id', phase=2,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_devices_static_video_get'),
    ToolSpec(
        name='portal_devices_deployments_list', domain='devices',
        title='List deployments',
        description='Lists the Greengrass deployments of a Use_Case.',
        resource_type='deployment', input_schema=uc_schema(PAGE_ARGS),
        backend='deployments', handler_module='deployments',
        handler_function='list_deployments', method='GET', resource='/deployments',
        param_map=UC_QUERY, permissions=('view_deployments',),
        result_shape='collection', collection_key='deployments'),
    ToolSpec(
        name='portal_devices_deployment_get', domain='devices', title='Get a deployment',
        description='Returns one deployment with its components and per-device status.',
        resource_type='deployment',
        input_schema=uc_schema({'deployment_id': identifier()},
                               required=['deployment_id']),
        backend='deployments', handler_module='deployments',
        handler_function='get_deployment', method='GET', resource='/deployments/{id}',
        param_map={'usecase_id': 'usecase_id', 'deployment_id': 'id'},
        permissions=('view_deployments',), handler_audits=True),
    ToolSpec(
        name='portal_devices_deployment_for_target_get', domain='devices',
        title='Get the deployment of a target',
        description=('Returns the current deployment of one device or one thing '
                     'group (exactly one of the two).'),
        resource_type='deployment',
        input_schema=uc_schema({
            'target_device': identifier('A device (thing name).'),
            'target_thing_group': identifier('A thing group name.'),
        }, one_of=[['target_device'], ['target_thing_group']]),
        backend='deployments', handler_module='deployments',
        handler_function='get_target_deployment', method='GET',
        resource='/deployments',
        param_map={'usecase_id': 'usecase_id', 'target_device': 'target_device',
                   'target_thing_group': 'target_thing_group'},
        permissions=('view_deployments',)),
    ToolSpec(
        name='portal_devices_deployment_create', domain='devices',
        title='Create a deployment',
        description=("Deploys components to one thing group or one device, superseding "
                     "the target's current deployment."),
        resource_type='deployment',
        input_schema=uc_schema({
            'components': array({
                'type': 'object',
                'properties': {'component_name': identifier(),
                               'component_version': identifier()},
                'required': ['component_name'],
                'additionalProperties': False,
            }, min_items=1),
            'target_thing_group': thing_name('A thing group name.'),
            'target_devices': array(thing_name(), min_items=1, max_items=1,
                                    description='Exactly one device.'),
            'deployment_name': text(256),
            'rollout_config': {
                'type': 'object',
                'properties': {'auto_rollback': boolean(),
                               'timeout_seconds': integer(minimum=1)},
                'additionalProperties': False,
            },
            'acknowledged_retained_components': array(identifier()),
            'confirm': confirm_arg('Repeat target_thing_group, or target_devices[0].'),
        }, required=['components'],
            one_of=[['target_thing_group'], ['target_devices']]),
        backend='deployments', handler_module='deployments',
        handler_function='create_deployment', method='POST', resource='/deployments',
        param_map=UC_QUERY, param_in={'usecase_id': 'body'},
        permissions=('create_deployments',), classification=MUTATING,
        destructive=True, confirm_target=('target_thing_group', 'target_devices[0]'),
        phase=3, idempotent=False, handler_audits=True,
        status_tool='portal_devices_deployment_for_target_get'),
    ToolSpec(
        name='portal_devices_deployment_cancel', domain='devices',
        title='Cancel a deployment',
        description='Cancels a Greengrass deployment.',
        resource_type='deployment',
        input_schema=uc_schema({'deployment_id': identifier(),
                                'confirm': confirm_arg('Repeat deployment_id.')},
                               required=['deployment_id']),
        backend='deployments', handler_module='deployments',
        handler_function='cancel_deployment', method='DELETE',
        resource='/deployments/{id}',
        param_map={'usecase_id': 'usecase_id', 'deployment_id': 'id'},
        permissions=('manage_deployments',), classification=MUTATING,
        destructive=True, confirm_target='deployment_id', phase=3,
        handler_audits=True, status_tool='portal_devices_deployments_list'),
    ToolSpec(
        name='portal_devices_components_list', domain='devices',
        title='List components',
        description='Lists the Greengrass components visible to a Use_Case.',
        resource_type='component',
        input_schema=uc_schema({
            'component_scope': enum('PRIVATE', 'PUBLIC',
                                    description='PRIVATE (default) or PUBLIC.'),
            'search': text(256),
            **PAGE_ARGS,
        }),
        backend='components', handler_module='components',
        handler_function='list_components', method='GET', resource='/components',
        param_map={'usecase_id': 'usecase_id', 'component_scope': 'scope',
                   'search': 'search'},
        permissions=('view_deployments',), result_shape='collection',
        collection_key='components'),
    ToolSpec(
        name='portal_devices_components_get', domain='devices', title='Get a component',
        description='Returns one component with its recipe and first 50 versions.',
        resource_type='component',
        input_schema=uc_schema({'component_arn': identifier(
            "The component ARN (a components_list item's arn).")},
            required=['component_arn']),
        backend='components', handler_module='components',
        handler_function='get_component_details', method='GET',
        resource='/components/{id}',
        param_map={'usecase_id': 'usecase_id', 'component_arn': 'id'},
        permissions=('view_deployments',)),
    ToolSpec(
        name='portal_devices_components_schema_get', domain='devices',
        title='Get a component configuration schema',
        description="Returns a component's configuration schema.",
        resource_type='component',
        input_schema=uc_schema({'component_name': identifier()},
                               required=['component_name']),
        backend='componentConfiguration', handler_module='component_configuration',
        handler_function='get_configuration_schema', method='GET',
        resource='/components/schema', param_map={'component_name': 'component_name'},
        permissions=('view_deployments',)),
    ToolSpec(
        name='portal_devices_component_delete', domain='devices',
        title='Delete a component',
        description='Deletes a Greengrass component version that is not deployed.',
        resource_type='component',
        input_schema=uc_schema({
            'component_arn': identifier("The component ARN (a components_list item's arn)."),
            'confirm': confirm_arg('Repeat component_arn.'),
        }, required=['component_arn']),
        backend='components', handler_module='components',
        handler_function='delete_component', method='DELETE',
        resource='/components/{id}',
        param_map={'usecase_id': 'usecase_id', 'component_arn': 'id'},
        permissions=('update_usecases',), classification=MUTATING,
        destructive=True, confirm_target='component_arn', phase=3,
        status_tool='portal_devices_components_list'),
    ToolSpec(
        name='portal_devices_shared_components_list', domain='devices',
        title='List shared components',
        description='Lists the shared components provisioned in a Use_Case.',
        resource_type='component', input_schema=uc_schema(PAGE_ARGS),
        backend='sharedComponents', handler_module='shared_components',
        handler_function='list_shared_components', method='GET',
        resource='/shared-components', param_map=UC_QUERY,
        permissions=('view_deployments',), result_shape='collection',
        collection_key='components'),
    ToolSpec(
        name='portal_devices_shared_components_available_list', domain='devices',
        title='List available shared components',
        description='Lists the shared components the portal can provision.',
        resource_type='component', input_schema=schema(PAGE_ARGS),
        backend='sharedComponents', handler_module='shared_components',
        handler_function='list_available_components', method='GET',
        resource='/shared-components/available',
        permissions=('view_deployments',), scope='global',
        result_shape='collection', collection_key='components'),
    ToolSpec(
        name='portal_devices_shared_components_status_get', domain='devices',
        title='Get shared-component update status',
        description='Returns, per Use_Case, whether its shared components need an update.',
        resource_type='component', input_schema=schema(),
        backend='sharedComponents', handler_module='shared_components',
        handler_function='get_update_status', method='GET',
        resource='/shared-components/status',
        permissions=('system_admin',), scope='global'),
    ToolSpec(
        name='portal_devices_shared_components_provision', domain='devices',
        title='Provision shared components',
        description='Provisions the shared components into a Use_Case (runs in the background).',
        resource_type='component', input_schema=uc_schema(),
        backend='sharedComponents', handler_module='shared_components',
        handler_function='provision_components', method='POST',
        resource='/shared-components/provision',
        param_map=UC_QUERY, param_in={'usecase_id': 'body'},
        # Branch base: the handler requires PortalAdmin on the named Use_Case
        # (check_user_access(..., 'PortalAdmin')); `system_admin` (U) admits
        # exactly that role.
        permissions=('system_admin',), classification=MUTATING, phase=3,
        handler_audits_denials=True,
        status_tool='portal_devices_shared_components_list'),
    ToolSpec(
        name='portal_devices_shared_components_update_all', domain='devices',
        title='Update shared components everywhere',
        description=('Re-provisions the shared components in the named Use_Cases, '
                     'or in every Use_Case; usually outlasts one call.'),
        resource_type='component',
        input_schema=schema({'usecase_ids': array(identifier(), min_items=1)}),
        backend='sharedComponents', handler_module='shared_components',
        handler_function='update_all_usecases', method='POST',
        resource='/shared-components/update-all',
        param_map={'usecase_ids': 'usecase_ids'},
        permissions=('system_admin',), scope='global', classification=MUTATING,
        phase=3, handler_audits=True,
        status_tool='portal_devices_shared_components_status_get'),
    ToolSpec(
        name='portal_devices_registrations_list', domain='devices',
        title='List device registrations',
        description='Lists the device registrations of a Use_Case.',
        resource_type='device_registration', input_schema=uc_schema(PAGE_ARGS),
        backend='deviceRegistrations', handler_module='device_registrations',
        handler_function='list_registrations', method='GET',
        resource='/device-registrations', param_map=UC_QUERY,
        permissions=('manage_devices',), result_shape='collection',
        collection_key='registrations'),
    ToolSpec(
        name='portal_devices_thing_groups_list', domain='devices',
        title='List thing groups',
        description='Lists the IoT thing groups of the Use_Case account.',
        resource_type='thing_group', input_schema=uc_schema(PAGE_ARGS),
        backend='deviceRegistrations', handler_module='device_registrations',
        handler_function='list_thing_groups', method='GET',
        resource='/device-registrations/thing-groups', param_map=UC_QUERY,
        permissions=('manage_devices',), result_shape='collection',
        collection_key='thing_groups'),
    ToolSpec(
        name='portal_devices_registration_delete', domain='devices',
        title='Delete a device registration',
        description='Deletes a device registration that has not completed.',
        resource_type='device_registration',
        input_schema=uc_schema({'registration_id': identifier(),
                                'confirm': confirm_arg('Repeat registration_id.')},
                               required=['registration_id']),
        backend='deviceRegistrations', handler_module='device_registrations',
        handler_function='delete_registration', method='DELETE',
        resource='/device-registrations/{id}', param_map={'registration_id': 'id'},
        permissions=('manage_devices',), classification=MUTATING,
        destructive=True, confirm_target='registration_id', phase=3,
        status_tool='portal_devices_registrations_list'),
)

# ----------------------------------------------------------- models view
# Handlers: models.py, model_import.py, model_converter.py, training.py,
# compilation.py, packaging.py, greengrass_publish.py. portal_models_delete
# is absent while models.py checks the role 'Admin', which no principal
# holds (Backlog 1).

def s3_uri(description: Optional[str] = None) -> dict:
    prop = {'type': 'string', 'pattern': r'^s3://\S+$', 'maxLength': 2048}
    if description:
        prop['description'] = description
    return prop


MODEL_ID = identifier('The model (training record) id.')
JOB_ID = identifier('The training job (training record) id.')
COMPILATION_TARGETS = ('jetson-xavier-jp5', 'jetson-xavier-jp6', 'x86_64-cpu',
                       'x86_64-cuda', 'arm64-cpu', 'onnx')
ENGINE_CONFIGURATION_KEYS = ('dtype', 'gpu_memory_utilization', 'max_model_len',
                             'tensor_parallel_size', 'enforce_eager',
                             'limit_mm_per_prompt')
MODEL_STAGES = ('candidate', 'staging', 'production')


def engine_configuration(min_properties: Optional[int] = None) -> dict:
    prop = {'type': 'object',
            'properties': {key: {} for key in ENGINE_CONFIGURATION_KEYS},
            'additionalProperties': False,
            'description': 'vLLM engine settings (keys of ENGINE_DEFAULTS).'}
    if min_properties is not None:
        prop['minProperties'] = min_properties
    return prop


def _status_sync(*writes: AcceptedWrite) -> dict:
    return {'accepted_writes': writes, 'accepted_writes_note': STATUS_SYNC_NOTE}


MODELS: Tuple[ToolSpec, ...] = (
    ToolSpec(
        name='portal_models_list', domain='models', title='List models',
        description='Lists the models of a Use_Case, optionally filtered by stage.',
        resource_type='model',
        input_schema=uc_schema({'stage': enum(*MODEL_STAGES), **PAGE_ARGS}),
        backend='models', handler_module='models', handler_function='list_models',
        method='GET', resource='/models', param_map=UC_QUERY,
        permissions=('view_models',), result_shape='collection',
        collection_key='models'),
    ToolSpec(
        name='portal_models_get', domain='models', title='Get a model',
        description=('Returns one model record; refreshes its compilation status '
                     'from SageMaker as the model page does.'),
        resource_type='model',
        input_schema=uc_schema({'model_id': MODEL_ID}, required=['model_id']),
        backend='models', handler_module='models', handler_function='get_model',
        method='GET', resource='/models/{id}', param_map={'model_id': 'id'},
        permissions=('view_models',),
        **_status_sync(AcceptedWrite('models.get_model', 'update_item',
                                     'training_table', 1))),
    ToolSpec(
        name='portal_models_stage_set', domain='models', title='Set a model stage',
        description='Moves a model to the candidate, staging or production stage.',
        resource_type='model',
        input_schema=uc_schema({'model_id': MODEL_ID, 'stage': enum(*MODEL_STAGES)},
                               required=['model_id', 'stage']),
        backend='models', handler_module='models',
        handler_function='update_model_stage', method='PUT',
        resource='/models/{id}/stage', param_map={'model_id': 'id'},
        permissions=('promote_models',), classification=MUTATING, phase=2,
        handler_audits=True, status_tool='portal_models_get'),
    ToolSpec(
        name='portal_models_format_spec_get', domain='models',
        title='Get the model format specification',
        description='Returns the model artifact format the portal imports.',
        resource_type='model_format', input_schema=schema(),
        backend='modelImport', handler_module='model_import',
        handler_function='get_model_format_spec', method='GET',
        resource='/models/format-spec', permissions=('view_models',),
        scope='global'),
    ToolSpec(
        name='portal_models_types_get', domain='models', title='Get the model types',
        description='Returns the model types, dimensions and frameworks the converter supports.',
        resource_type='model_format', input_schema=schema(),
        backend='modelConverter', handler_module='model_converter',
        handler_function='get_supported_types', method='GET',
        resource='/models/types', permissions=('view_models',), scope='global'),
    ToolSpec(
        name='portal_models_vllm_engine_spec_get', domain='models',
        title='Get the vLLM engine specification',
        description='Returns the vLLM engine settings a model can carry, with their defaults.',
        resource_type='model_format', input_schema=schema(),
        backend='modelImport', handler_module='model_import',
        handler_function='get_vllm_engine_spec', method='GET',
        resource='/models/vllm/engine-spec', permissions=('view_models',),
        scope='global'),
    ToolSpec(
        name='portal_models_validate', domain='models', title='Validate a model artifact',
        description='Checks that an s3:// .tar.gz model artifact has the importable format.',
        resource_type='model',
        input_schema=uc_schema({'model_s3_uri': s3_uri('An s3:// URI of a .tar.gz.')},
                               required=['model_s3_uri']),
        backend='modelImport', handler_module='model_import',
        handler_function='validate_model', method='POST', resource='/models/validate',
        param_map=UC_QUERY, permissions=('promote_models',)),
    ToolSpec(
        name='portal_models_inspect', domain='models', title='Inspect a model checkpoint',
        description='Inspects a .pt, .pth or .onnx object in a trusted bucket.',
        resource_type='model',
        input_schema=uc_schema({'model_s3_uri': s3_uri()}, required=['model_s3_uri']),
        backend='modelConverter', handler_module='model_converter',
        handler_function='inspect_model_endpoint', method='POST',
        resource='/models/inspect', param_map=UC_QUERY,
        permissions=('promote_models',)),
    ToolSpec(
        name='portal_models_import', domain='models', title='Import a model',
        description='Imports a .tar.gz model artifact as a new model record.',
        resource_type='model',
        input_schema=uc_schema({
            'model_name': text(256), 'model_version': text(64),
            'model_s3_uri': s3_uri('An s3:// URI of a .tar.gz in an allowed bucket.'),
            'description': text(2000), 'auto_compile': boolean(),
            'compilation_targets': array(enum(*COMPILATION_TARGETS)),
        }, required=['model_name', 'model_version', 'model_s3_uri']),
        backend='modelImport', handler_module='model_import',
        handler_function='import_model', method='POST', resource='/models/import',
        param_map=UC_QUERY, permissions=('promote_models',),
        classification=MUTATING, phase=2, idempotent=False, handler_audits=True,
        status_tool='portal_models_get'),
    ToolSpec(
        name='portal_models_vllm_import', domain='models', title='Register a vLLM model',
        description=('Registers a vLLM model from a Hugging Face id or an S3 artifact '
                     '(exactly one of the two).'),
        resource_type='model',
        input_schema=uc_schema({
            'model_name': text(256), 'model_version': text(64),
            'huggingface_model_id': text(256),
            's3_model_artifact': s3_uri(),
            'engine_configuration': engine_configuration(),
            'description': text(2000),
        }, required=['model_name', 'model_version'],
            one_of=[['huggingface_model_id'], ['s3_model_artifact']]),
        backend='modelImport', handler_module='model_import',
        handler_function='register_vllm_model', method='POST', resource='/models/vllm',
        param_map=UC_QUERY, permissions=('promote_models',),
        classification=MUTATING, phase=2, idempotent=False, handler_audits=True,
        status_tool='portal_models_get'),
    ToolSpec(
        name='portal_models_vllm_engine_configuration_set', domain='models',
        title='Set a vLLM engine configuration',
        description="Updates a vLLM model's engine settings.",
        resource_type='model',
        input_schema=uc_schema({'model_id': MODEL_ID,
                                'engine_configuration': engine_configuration(1)},
                               required=['model_id', 'engine_configuration']),
        backend='modelImport', handler_module='model_import',
        handler_function='update_vllm_engine_configuration', method='PUT',
        resource='/models/vllm/{training_id}/engine-configuration',
        param_map={'model_id': 'training_id'}, permissions=('promote_models',),
        classification=MUTATING, phase=2, handler_audits=True,
        status_tool='portal_models_get'),
    ToolSpec(
        name='portal_models_convert', domain='models', title='Convert a model',
        description=('Converts a checkpoint to an importable artifact, or starts a '
                     'conversion job for a .pt/.pth exported to ONNX.'),
        resource_type='model',
        input_schema=uc_schema({
            'model_s3_uri': s3_uri(), 'model_name': text(256),
            'model_type': enum('classification', 'object_detection',
                               'segmentation', 'anomaly_detection'),
            'image_width': integer(minimum=1), 'image_height': integer(minimum=1),
            'num_classes': integer(minimum=1),
            'class_names': array(text(256)),
            'export_format': enum('pytorch', 'onnx'),
            'detection_arch': enum('yolo', 'rf_detr'),
            'score_threshold': number(0, 1), 'iou_threshold': number(0, 1),
            'preserve_aspect': boolean(), 'auto_import': boolean(),
            'model_version': text(64),
        }, required=['model_s3_uri', 'model_name', 'model_type', 'image_width',
                     'image_height']),
        backend='modelConverter', handler_module='model_converter',
        handler_function='convert_model', method='POST', resource='/models/convert',
        param_map=UC_QUERY, permissions=('promote_models',),
        classification=MUTATING, phase=2, idempotent=False, handler_audits=True,
        status_tool='portal_models_get'),
    ToolSpec(
        name='portal_models_training_list', domain='models', title='List training jobs',
        description='Lists the training records of a Use_Case.',
        resource_type='training_job',
        input_schema=uc_schema(PAGE_ARGS),
        backend='training', handler_module='training',
        handler_function='list_training_jobs', method='GET', resource='/training',
        param_map=UC_QUERY, permissions=('view_training_jobs',),
        result_shape='collection', collection_key='jobs', limit_default=20),
    ToolSpec(
        name='portal_models_training_get', domain='models', title='Get a training job',
        description=('Returns one training record; refreshes its status from '
                     'SageMaker as the training page does.'),
        resource_type='training_job',
        input_schema=uc_schema({'job_id': JOB_ID}, required=['job_id']),
        backend='training', handler_module='training',
        handler_function='get_training_job', method='GET', resource='/training/{id}',
        param_map={'job_id': 'id'}, permissions=('view_training_jobs',),
        **_status_sync(
            AcceptedWrite('training.get_training_job', 'update_item', 'table', 2),
            AcceptedWrite('training.sync_conversion_record',
                          'reconcile_conversion', '', 1))),
    ToolSpec(
        name='portal_models_training_logs_get', domain='models',
        title='Get training logs',
        description=('Returns one page of a training job\'s log events, oldest first; '
                     'the token repeats at the end of the stream.'),
        resource_type='log_event',
        input_schema=uc_schema({'job_id': JOB_ID, 'limit': LIMIT,
                                'next_token': NEXT_TOKEN}, required=['job_id']),
        backend='training', handler_module='training',
        handler_function='get_training_logs', method='GET',
        resource='/training/{id}/logs',
        param_map={'job_id': 'id', 'limit': 'limit',
                   'next_token': 'nextForwardToken'},
        permissions=('view_training_jobs',), result_shape='collection',
        collection_key='logs'),
    ToolSpec(
        name='portal_models_training_create', domain='models',
        title='Create a training job',
        description='Starts a SageMaker training job on a dataset manifest.',
        resource_type='training_job',
        input_schema=uc_schema({
            'model_name': text(256), 'model_version': text(64),
            'model_type': enum('classification', 'segmentation',
                               'classification-robust', 'segmentation-robust',
                               'object_detection'),
            'dataset_manifest_s3': s3_uri('An s3:// manifest in an allowed bucket.'),
            'hyperparameters': json_object(),
            'instance_type': identifier(),
            'max_runtime_seconds': integer(minimum=1),
            'auto_compile': boolean(),
            'compilation_targets': array(enum(*COMPILATION_TARGETS)),
            'detection_arch': enum('yolo', 'rf_detr'),
            'base_model': {'type': 'object',
                           'properties': {'kind': identifier(), 'ref': text(1024)},
                           'additionalProperties': False},
            'class_names': array(text(256)),
        }, required=['model_name', 'model_version', 'model_type',
                     'dataset_manifest_s3']),
        backend='training', handler_module='training',
        handler_function='create_training_job', method='POST', resource='/training',
        param_map=UC_QUERY, permissions=('create_training_jobs',),
        classification=MUTATING, phase=2, idempotent=False, handler_audits=True,
        status_tool='portal_models_training_get'),
    ToolSpec(
        name='portal_models_manifest_transform', domain='models',
        title='Transform a manifest',
        description=('Converts a labeling manifest to the training format, writing '
                     '<source-base>-dda.manifest next to it.'),
        resource_type='manifest',
        input_schema=uc_schema({
            'source_manifest_uri': s3_uri(),
            'task_type': enum('classification', 'segmentation'),
        }, required=['source_manifest_uri']),
        backend='training', handler_module='training',
        handler_function='transform_manifest', method='POST',
        resource='/training/transform-manifest', param_map=UC_QUERY,
        permissions=('create_training_jobs',), classification=MUTATING, phase=2,
        handler_audits=True, status_tool='portal_models_training_list'),
    ToolSpec(
        name='portal_models_compilation_get', domain='models',
        title='Get compilation status',
        description=("Returns a model's compilation jobs; refreshes their status "
                     'from SageMaker as the compilation tab does.'),
        resource_type='compilation_job',
        input_schema=uc_schema({'job_id': JOB_ID}, required=['job_id']),
        backend='compilation', handler_module='compilation',
        handler_function='get_compilation_status', method='GET',
        resource='/training/{id}/compile', param_map={'job_id': 'id'},
        permissions=('view_training_jobs',),
        **_status_sync(AcceptedWrite('compilation.get_compilation_status',
                                     'update_item', 'table', 1))),
    ToolSpec(
        name='portal_models_compilation_create', domain='models',
        title='Start a compilation',
        description='Starts SageMaker Neo compilation jobs for a trained model.',
        resource_type='compilation_job',
        input_schema=uc_schema({
            'job_id': JOB_ID,
            'targets': array(enum(*COMPILATION_TARGETS), min_items=1),
        }, required=['job_id']),
        backend='compilation', handler_module='compilation',
        handler_function='start_compilation_job', method='POST',
        resource='/training/{id}/compile', param_map={'job_id': 'id'},
        permissions=('create_training_jobs',), classification=MUTATING, phase=2,
        idempotent=False, handler_audits=True,
        status_tool='portal_models_compilation_get'),
    ToolSpec(
        name='portal_models_package', domain='models', title='Package a model',
        description='Packages compiled model artifacts as Greengrass component packages.',
        resource_type='model',
        input_schema=uc_schema({'job_id': JOB_ID,
                                'targets': array(identifier(), min_items=1)},
                               required=['job_id']),
        backend='packaging', handler_module='packaging',
        handler_function='package_components', method='POST',
        resource='/training/{id}/package',
        param_map={'job_id': 'id', 'targets': 'targets'},
        permissions=('promote_models',), classification=MUTATING, phase=3,
        idempotent=False, handler_audits=True, status_tool='portal_models_get'),
    ToolSpec(
        name='portal_models_publish', domain='models', title='Publish a model component',
        description='Publishes a packaged model as a Greengrass component version.',
        resource_type='model',
        input_schema=uc_schema({
            'job_id': JOB_ID,
            'component_name': {'type': 'string', 'pattern': r'^model-[A-Za-z0-9._-]{1,120}$'},
            'component_version': {'type': 'string', 'pattern': r'^[0-9]{1,9}\.0\.0$'},
            'friendly_name': text(256),
            'targets': array(identifier(), min_items=1),
            'skip_fit_check': boolean('vLLM records only.'),
        }, required=['job_id', 'component_name', 'component_version']),
        backend='greengrassPublish', handler_module='greengrass_publish',
        handler_function='publish_component', method='POST',
        resource='/training/{id}/publish', param_map={'job_id': 'id'},
        permissions=('promote_models',), classification=MUTATING, phase=3,
        idempotent=False, handler_audits=True, status_tool='portal_models_get'),
)

# ------------------------------------------------------------- data view
# Handlers: datasets.py, pre_labeled_datasets.py, data_management.py,
# captures.py, labeling.py, dda_labeling.py, synthetic_data.py. No
# portal_data_labeling_manifest_get (its route answers no Use_Case the
# shaper could check; Backlog 18) and no portal_data_bucket_attach (a
# disabled stub; Backlog 24).

UC_PATH_ID = {'usecase_id': 'id'}
# Specs whose job/team/run Use_Case the dispatcher injects with setdefault
# (Backlog 27): usecase_id is the RBAC scope only and never forwarded.
INJECTED = {'usecase_not_forwarded': 'injected'}
TRAINING_MODEL_TYPES = ('classification', 'segmentation', 'classification-robust',
                        'segmentation-robust', 'object_detection')
JOB_ID_LABELING = identifier('The labeling job id.')
SESSION_ID = identifier('The synthetic-data session id.')
TEAM_ID = identifier('The labeling team id.')
TASK_ID = identifier('The labeling task id.')

DATA: Tuple[ToolSpec, ...] = (
    ToolSpec(
        name='portal_data_datasets_list', domain='data', title='List datasets',
        description="Lists the dataset folders under the Use_Case data bucket's prefix.",
        resource_type='dataset',
        input_schema=uc_schema({'prefix': text(1024),
                                'max_depth': integer(1, 10, 'Folder depth (default 3).'),
                                **PAGE_ARGS}),
        backend='datasets', handler_module='datasets',
        handler_function='list_datasets', method='GET', resource='/datasets',
        param_map=UC_QUERY, permissions=('view_datasets',),
        handler_audits_denials=True,
        result_shape='collection', collection_key='datasets'),
    ToolSpec(
        name='portal_data_datasets_preview_get', domain='data',
        title='Preview dataset images',
        description='Returns one page of image keys with presigned URLs under a prefix.',
        resource_type='image',
        input_schema=uc_schema({
            'prefix': text(1024), 'offset': OFFSET, 'limit': LIMIT,
            'extensions': text(256, 'Comma-separated file extensions.'),
        }, required=['prefix']),
        backend='datasets', handler_module='datasets',
        handler_function='get_image_preview', method='GET',
        resource='/datasets/preview',
        param_map={'usecase_id': 'usecase_id', 'offset': 'offset', 'limit': 'limit'},
        permissions=('view_datasets',), handler_audits_denials=True,
        result_shape='collection', collection_key='images', limit_max=50,
        limit_default=8),
    ToolSpec(
        name='portal_data_datasets_count', domain='data', title='Count dataset images',
        description='Counts the images under a prefix of the Use_Case data bucket.',
        resource_type='dataset',
        input_schema=uc_schema({'prefix': text(1024)}, required=['prefix']),
        backend='datasets', handler_module='datasets',
        handler_function='count_images', method='POST', resource='/datasets/count',
        param_map=UC_QUERY, permissions=('view_datasets',),
        handler_audits_denials=True),
    ToolSpec(
        name='portal_data_prelabeled_list', domain='data',
        title='List pre-labeled datasets',
        description='Lists the pre-labeled datasets registered in a Use_Case.',
        resource_type='dataset', input_schema=uc_schema(PAGE_ARGS),
        backend='preLabeledDatasets', handler_module='pre_labeled_datasets',
        handler_function='list_datasets', method='GET',
        resource='/datasets/pre-labeled', param_map=UC_QUERY,
        permissions=('view_datasets',), handler_audits_denials=True,
        result_shape='collection', collection_key='datasets'),
    ToolSpec(
        name='portal_data_prelabeled_get', domain='data',
        title='Get a pre-labeled dataset',
        description='Returns one pre-labeled dataset record.',
        resource_type='dataset',
        input_schema=uc_schema({'dataset_id': identifier()}, required=['dataset_id']),
        backend='preLabeledDatasets', handler_module='pre_labeled_datasets',
        handler_function='get_dataset', method='GET',
        resource='/datasets/pre-labeled/{id}', param_map={'dataset_id': 'id'},
        permissions=('view_datasets',), handler_audits_denials=True),
    ToolSpec(
        name='portal_data_manifest_validate', domain='data', title='Validate a manifest',
        description='Checks a labeled manifest in a Use_Case bucket and reports its statistics.',
        resource_type='manifest',
        input_schema=uc_schema({'manifest_s3_uri': s3_uri()},
                               required=['manifest_s3_uri']),
        backend='preLabeledDatasets', handler_module='pre_labeled_datasets',
        handler_function='validate_manifest', method='POST',
        resource='/datasets/validate-manifest', param_map=UC_QUERY,
        permissions=('manage_datasets',), handler_audits_denials=True),
    ToolSpec(
        name='portal_data_prelabeled_create', domain='data',
        title='Register a pre-labeled dataset',
        description='Registers a labeled manifest in a Use_Case bucket as a dataset.',
        resource_type='dataset',
        input_schema=uc_schema({
            'name': text(256), 'manifest_s3_uri': s3_uri(),
            'description': text(2000), 'task_type': identifier(),
            'label_attribute': identifier(), 'image_count': integer(minimum=0),
            'label_stats': json_object(),
        }, required=['name', 'manifest_s3_uri']),
        backend='preLabeledDatasets', handler_module='pre_labeled_datasets',
        handler_function='create_dataset', method='POST',
        resource='/datasets/pre-labeled', param_map=UC_QUERY,
        permissions=('manage_datasets',), classification=MUTATING, phase=2,
        idempotent=False, handler_audits_denials=True,
        resource_id_path='dataset.dataset_id',
        status_tool='portal_data_prelabeled_get'),
    ToolSpec(
        name='portal_data_prelabeled_delete', domain='data',
        title='Delete a pre-labeled dataset',
        description='Removes a pre-labeled dataset record (the manifest object is kept).',
        resource_type='dataset',
        input_schema=uc_schema({'dataset_id': identifier(),
                                'confirm': confirm_arg('Repeat dataset_id.')},
                               required=['dataset_id']),
        backend='preLabeledDatasets', handler_module='pre_labeled_datasets',
        handler_function='delete_dataset', method='DELETE',
        resource='/datasets/pre-labeled/{id}', param_map={'dataset_id': 'id'},
        permissions=('manage_datasets',), classification=MUTATING,
        destructive=True, confirm_target='dataset_id', phase=3,
        handler_audits_denials=True, status_tool='portal_data_prelabeled_list'),
    ToolSpec(
        name='portal_data_folders_list', domain='data', title='List bucket folders',
        description=('Lists the folders and files under a prefix: one S3 page of at '
                     'most 1000 keys, with is_truncated.'),
        resource_type='folder',
        input_schema=uc_schema({'bucket': identifier('Default: the Use_Case data bucket.'),
                                'prefix': text(1024)}),
        backend='dataManagement', handler_module='data_management',
        handler_function='list_folders', method='GET',
        resource='/usecases/{id}/data/folders', param_map=UC_PATH_ID,
        permissions=('view_datasets',)),
    ToolSpec(
        name='portal_data_buckets_list', domain='data', title='List data buckets',
        description='Lists the buckets a Use_Case can use for data and its current data bucket.',
        resource_type='bucket', input_schema=uc_schema(),
        backend='dataManagement', handler_module='data_management',
        handler_function='list_buckets', method='GET',
        resource='/usecases/{id}/data/buckets', param_map=UC_PATH_ID,
        permissions=('view_datasets',), handler_audits=True),
    ToolSpec(
        name='portal_data_folder_create', domain='data', title='Create a folder',
        description='Creates an S3 folder (key prefix) in a Use_Case bucket.',
        resource_type='folder',
        input_schema=uc_schema({'bucket': identifier('Default: the Use_Case data bucket.'),
                                'folder_path': text(1024, 'An S3 key prefix.')},
                               required=['folder_path']),
        backend='dataManagement', handler_module='data_management',
        handler_function='create_folder', method='POST',
        resource='/usecases/{id}/data/folders', param_map=UC_PATH_ID,
        permissions=('manage_datasets',), classification=MUTATING, phase=2,
        handler_audits=True, status_tool='portal_data_folders_list'),
    ToolSpec(
        name='portal_data_configure', domain='data', title='Set the data bucket',
        description='Sets the data bucket of a Use_Case (an eligible, unclaimed bucket).',
        resource_type='usecase',
        input_schema=uc_schema({'data_s3_bucket': identifier()},
                               required=['data_s3_bucket']),
        backend='dataManagement', handler_module='data_management',
        handler_function='configure_data_account', method='POST',
        resource='/usecases/{id}/data/configure', param_map=UC_PATH_ID,
        permissions=('update_usecases',), classification=MUTATING, phase=3,
        handler_audits=True, status_tool='portal_data_buckets_list'),
    ToolSpec(
        name='portal_data_captures_list', domain='data', title='List captures',
        description='Returns the newest captures under a prefix with presigned image URLs.',
        resource_type='capture',
        input_schema=uc_schema({'prefix': text(1024), 'device_id': identifier(),
                                'limit': LIMIT}, required=['prefix']),
        backend='captures', handler_module='captures',
        handler_function='list_captures', method='GET', resource='/captures',
        param_map={'usecase_id': 'usecase_id', 'limit': 'limit'},
        permissions=('view_datasets',), handler_audits_denials=True,
        result_shape='collection', collection_key='captures', limit_max=30,
        limit_default=20),
    ToolSpec(
        name='portal_data_labeling_jobs_list', domain='data', title='List labeling jobs',
        description=('Lists the labeling jobs of a Use_Case; refreshes Ground Truth '
                     'job status as the labeling page does.'),
        resource_type='labeling_job',
        input_schema=uc_schema({'status': identifier(), **PAGE_ARGS}),
        backend='labeling', handler_module='labeling',
        handler_function='list_labeling_jobs', method='GET', resource='/labeling',
        param_map={'usecase_id': 'usecase_id', 'status': 'status'},
        permissions=('view_labeling_jobs',), handler_audits_denials=True,
        result_shape='collection', collection_key='jobs',
        **_status_sync(AcceptedWrite('labeling.list_labeling_jobs', 'update_item',
                                     'labeling_jobs_table', 1))),
    ToolSpec(
        name='portal_data_labeling_job_get', domain='data', title='Get a labeling job',
        description=('Returns one labeling job of the named Use_Case; refreshes its '
                     'Ground Truth status as the job page does.'),
        resource_type='labeling_job',
        input_schema=uc_schema({'job_id': JOB_ID_LABELING}, required=['job_id']),
        backend='labeling', handler_module='labeling',
        handler_function='get_labeling_job', method='GET', resource='/labeling/{id}',
        param_map={'job_id': 'id'}, permissions=('view_labeling_jobs',),
        handler_audits_denials=True, bind_usecase_path='job.usecase_id',
        **_status_sync(AcceptedWrite('labeling.get_labeling_job', 'update_item',
                                     'labeling_jobs_table', 1))),
    ToolSpec(
        name='portal_data_workteams_list', domain='data', title='List workteams',
        description='Lists the SageMaker Ground Truth workteams available to a Use_Case.',
        resource_type='workteam', input_schema=uc_schema(PAGE_ARGS),
        backend='labeling', handler_module='labeling',
        handler_function='list_workteams', method='GET',
        resource='/labeling/workteams', param_map=UC_QUERY,
        permissions=('view_labeling_jobs',), result_shape='collection',
        collection_key='workteams'),
    ToolSpec(
        name='portal_data_labeling_job_create', domain='data',
        title='Create a labeling job',
        description=('Creates a Ground Truth or DDA labeling job over a dataset '
                     'prefix; the fields required depend on labeling_backend.'),
        resource_type='labeling_job',
        input_schema=uc_schema({
            'labeling_backend': enum('GroundTruth', 'DDA'),
            'job_name': text(256), 'dataset_prefix': text(1024),
            'task_type': identifier(),
            'label_categories': array(text(256), min_items=1),
            'workforce_arn': identifier(),
            'instructions': text(),
            'num_workers_per_object': integer(minimum=1),
            'enable_automated_labeling': boolean(),
            'task_time_limit': integer(minimum=1),
            'mask_prefix': text(1024),
            'label_set': array(text(256), min_items=1),
            'team_id': identifier('Required unless skip_verification.'),
            'auto_label': json_object(),
            'example_images': array(text(1024)),
            'skip_verification': boolean('UseCaseAdmin or PortalAdmin only.'),
            'bedrock_model_id': identifier(),
            'per_label_prompts': string_map(),
        }, required=['labeling_backend', 'job_name', 'dataset_prefix', 'task_type'],
            one_of=[['label_categories', 'workforce_arn'], ['label_set']]),
        backend='labeling', handler_module='labeling',
        handler_function='create_labeling_job', method='POST', resource='/labeling',
        param_map=UC_QUERY, permissions=('create_labeling_jobs',),
        classification=MUTATING, phase=2, idempotent=False,
        handler_audits_denials=True, status_tool='portal_data_labeling_job_get'),
    ToolSpec(
        name='portal_data_labeling_job_stop', domain='data', title='Stop a labeling job',
        description='Stops a running labeling job.',
        resource_type='labeling_job',
        input_schema=uc_schema({'job_id': JOB_ID_LABELING,
                                'confirm': confirm_arg('Repeat job_id.')},
                               required=['job_id']),
        backend='labeling', handler_module='labeling',
        handler_function='stop_labeling_job', method='POST',
        resource='/labeling/{id}/stop', param_map={'job_id': 'id'},
        permissions=('manage_labeling_jobs',), classification=MUTATING,
        destructive=True, confirm_target='job_id', phase=3,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_data_labeling_jobs_list', **INJECTED),
    ToolSpec(
        name='portal_data_labeling_job_delete', domain='data',
        title='Delete a labeling job',
        description='Deletes a DDA labeling job that is not running.',
        resource_type='labeling_job',
        input_schema=uc_schema({'job_id': JOB_ID_LABELING,
                                'confirm': confirm_arg('Repeat job_id.')},
                               required=['job_id']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='request_job_deletion', method='DELETE',
        resource='/labeling/{id}', param_map={'job_id': 'id'},
        permissions=('manage_labeling_jobs',), classification=MUTATING,
        destructive=True, confirm_target='job_id', phase=3,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_data_labeling_jobs_list', **INJECTED),
    ToolSpec(
        name='portal_data_labeling_prelabels_rerun', domain='data',
        title='Re-run pre-labels',
        description="Re-runs the automatic pre-labeling of a DDA job's failed tasks.",
        resource_type='labeling_job',
        input_schema=uc_schema({'job_id': JOB_ID_LABELING,
                                'prompt_overrides': string_map(
                                    'Grounded-SAM jobs only: {label: prompt}.')},
                               required=['job_id']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='rerun_prelabels', method='POST',
        resource='/labeling/{id}/rerun-prelabels', param_map={'job_id': 'id'},
        permissions=('manage_labeling_jobs',), classification=MUTATING, phase=2,
        idempotent=False, handler_audits=True, handler_audits_denials=True,
        status_tool='portal_data_labeling_job_get', **INJECTED),
    ToolSpec(
        name='portal_data_labeling_review_get', domain='data',
        title='Get the admin review',
        description="Returns one page of a DDA job's pre-labeled tasks awaiting admin review.",
        resource_type='labeling_task',
        input_schema=uc_schema({'job_id': JOB_ID_LABELING, 'limit': LIMIT,
                                'next_token': NEXT_TOKEN}, required=['job_id']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='get_admin_review', method='GET',
        resource='/labeling/{id}/review',
        param_map={'job_id': 'id', 'limit': 'limit', 'next_token': 'next_token'},
        permissions=('update_usecases',), handler_audits_denials=True,
        result_shape='collection', collection_key='items',
        limit_max=100, limit_default=50, **INJECTED),
    ToolSpec(
        name='portal_data_labeling_review_decide', domain='data',
        title='Record review decisions',
        description='Records accept or reject decisions for pre-labeled tasks of a DDA job.',
        resource_type='labeling_task',
        input_schema=uc_schema({
            'job_id': JOB_ID_LABELING,
            'decisions': {'type': 'object', 'minProperties': 1,
                          'additionalProperties': {'type': 'string',
                                                   'enum': ['accepted', 'rejected']},
                          'description': '{task_id: accepted | rejected}.'},
        }, required=['job_id', 'decisions']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='save_review_decisions', method='POST',
        resource='/labeling/{id}/review/decisions', param_map={'job_id': 'id'},
        permissions=('update_usecases',), classification=MUTATING, phase=2,
        handler_audits_denials=True, status_tool='portal_data_labeling_review_get',
        **INJECTED),
    ToolSpec(
        name='portal_data_labeling_review_finalize', domain='data',
        title='Finalize the admin review',
        description="Finalizes a DDA job's admin review once every task is decided.",
        resource_type='labeling_job',
        input_schema=uc_schema({'job_id': JOB_ID_LABELING}, required=['job_id']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='finalize_admin_review', method='POST',
        resource='/labeling/{id}/review/finalize', param_map={'job_id': 'id'},
        permissions=('update_usecases',), classification=MUTATING, phase=3,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_data_labeling_review_get', **INJECTED),
    ToolSpec(
        name='portal_data_labeling_teams_list', domain='data',
        title='List labeling teams',
        description='Lists the labeling teams of a Use_Case and their members.',
        resource_type='labeling_team', input_schema=uc_schema(PAGE_ARGS),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='list_labeling_teams', method='GET',
        resource='/labeling-teams', param_map=UC_QUERY,
        permissions=('labeling-teams:manage',), handler_audits_denials=True,
        result_shape='collection', collection_key='teams'),
    ToolSpec(
        name='portal_data_labeling_team_create', domain='data',
        title='Create a labeling team',
        description='Creates a labeling team in a Use_Case.',
        resource_type='labeling_team',
        input_schema=uc_schema({'team_name': text(128, 'Unique in the Use_Case.')},
                               required=['team_name']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='create_labeling_team', method='POST',
        resource='/labeling-teams', param_map=UC_QUERY,
        permissions=('labeling-teams:manage',), classification=MUTATING, phase=3,
        idempotent=False, handler_audits=True, handler_audits_denials=True,
        resource_id_path='team.team_id',
        status_tool='portal_data_labeling_teams_list'),
    ToolSpec(
        name='portal_data_labeling_team_member_add', domain='data',
        title='Add a labeling team member',
        description='Adds a DataLabeler user to a labeling team.',
        resource_type='labeling_team',
        input_schema=uc_schema({'team_id': TEAM_ID,
                                'user_id': identifier('A user holding DataLabeler.')},
                               required=['team_id', 'user_id']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='add_team_member', method='POST',
        resource='/labeling-teams/{teamId}/members',
        param_map={'team_id': 'teamId', 'user_id': 'user_id'},
        permissions=('labeling-teams:manage',), classification=MUTATING, phase=3,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_data_labeling_teams_list', **INJECTED),
    ToolSpec(
        name='portal_data_labeling_team_member_remove', domain='data',
        title='Remove a labeling team member',
        description=("Removes a member from a labeling team and reassigns or parks "
                     "the member's unsubmitted tasks."),
        resource_type='labeling_team',
        input_schema=uc_schema({'team_id': TEAM_ID, 'user_id': identifier(),
                                'confirm': confirm_arg('Repeat user_id.')},
                               required=['team_id', 'user_id']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='remove_team_member', method='DELETE',
        resource='/labeling-teams/{teamId}/members/{userId}',
        param_map={'team_id': 'teamId', 'user_id': 'userId'},
        permissions=('labeling-teams:manage',), classification=MUTATING,
        destructive=True, confirm_target='user_id', phase=3,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_data_labeling_teams_list', **INJECTED),
    ToolSpec(
        name='portal_data_labeling_team_delete', domain='data',
        title='Delete a labeling team',
        description='Deletes a labeling team that has no job in progress.',
        resource_type='labeling_team',
        input_schema=uc_schema({'team_id': TEAM_ID,
                                'confirm': confirm_arg('Repeat team_id.')},
                               required=['team_id']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='delete_labeling_team', method='DELETE',
        resource='/labeling-teams/{teamId}', param_map={'team_id': 'teamId'},
        permissions=('labeling-teams:manage',), classification=MUTATING,
        destructive=True, confirm_target='team_id', phase=3,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_data_labeling_teams_list', **INJECTED),
    ToolSpec(
        name='portal_data_labeler_jobs_list', domain='data',
        title='List my labeling jobs',
        description='Lists the DDA labeling jobs assigned to the caller as a labeler.',
        resource_type='labeling_job', input_schema=schema(PAGE_ARGS),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='list_labeler_jobs', method='GET', resource='/labeler/jobs',
        permissions=('labeling:tasks-self',), scope='global',
        handler_audits_denials=True, result_shape='collection',
        collection_key='jobs'),
    ToolSpec(
        name='portal_data_labeler_pool_get', domain='data',
        title='Get a labeling job pool',
        description="Returns the task pool and team progress of a job assigned to the caller.",
        resource_type='labeling_job',
        input_schema=schema({'job_id': JOB_ID_LABELING}, required=['job_id']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='get_labeler_job_pool', method='GET',
        resource='/labeler/jobs/{jobId}/pool', param_map={'job_id': 'jobId'},
        permissions=('labeling:tasks-self',), scope='global',
        handler_audits_denials=True),
    ToolSpec(
        name='portal_data_labeler_task_image_url_get', domain='data',
        title='Get a task image URL',
        description="Returns a fresh presigned URL for a labeling task's image.",
        resource_type='labeling_task',
        input_schema=schema({'task_id': TASK_ID,
                             'job_id': identifier('Narrows the task lookup.')},
                            required=['task_id']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='get_task_image_url', method='GET',
        resource='/labeler/tasks/{taskId}/image-url',
        param_map={'task_id': 'taskId', 'job_id': 'job_id'},
        permissions=('labeling:tasks-self',), scope='global',
        handler_audits_denials=True),
    ToolSpec(
        name='portal_data_labeler_task_next', domain='data',
        title='Get the next labeling task',
        description="Returns the caller's next labeling task of a job; claims nothing.",
        resource_type='labeling_task',
        input_schema=schema({'job_id': JOB_ID_LABELING}, required=['job_id']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='get_next_labeler_task', method='GET',
        resource='/labeler/jobs/{jobId}/next', param_map={'job_id': 'jobId'},
        permissions=('labeling:tasks-self',), scope='global',
        handler_audits_denials=True),
    ToolSpec(
        name='portal_data_labeler_task_steal', domain='data',
        title='Take over a labeling task',
        description="Takes over an idle task of another labeler of the caller's team.",
        resource_type='labeling_task',
        input_schema=schema({'job_id': JOB_ID_LABELING}, required=['job_id']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='steal_labeler_task', method='POST',
        resource='/labeler/jobs/{jobId}/steal', param_map={'job_id': 'jobId'},
        permissions=('labeling:tasks-self',), scope='global',
        classification=MUTATING, phase=2, idempotent=False,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_data_labeler_pool_get'),
    ToolSpec(
        name='portal_data_labeler_task_submit', domain='data',
        title='Submit a labeling task',
        description="Submits the caller's annotation for a labeling task.",
        resource_type='labeling_task',
        input_schema=schema({'task_id': TASK_ID, 'job_id': JOB_ID_LABELING,
                             'annotation': json_object()},
                            required=['task_id', 'job_id', 'annotation']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='submit_labeler_task', method='POST',
        resource='/labeler/tasks/{taskId}/submit',
        param_map={'task_id': 'taskId', 'job_id': 'job_id'},
        permissions=('labeling:tasks-self',), scope='global',
        classification=MUTATING, phase=2, handler_audits_denials=True,
        status_tool='portal_data_labeler_pool_get'),
    ToolSpec(
        name='portal_data_labeler_task_presentation_failure', domain='data',
        title='Report a task presentation failure',
        description='Withholds a labeling task whose image could not be shown.',
        resource_type='labeling_task',
        input_schema=schema({'task_id': TASK_ID, 'job_id': JOB_ID_LABELING,
                             'reason': text(1024)},
                            required=['task_id', 'job_id']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='report_presentation_failure', method='POST',
        resource='/labeler/tasks/{taskId}/presentation-failure',
        param_map={'task_id': 'taskId', 'job_id': 'job_id'},
        permissions=('labeling:tasks-self',), scope='global',
        classification=MUTATING, phase=2, handler_audits_denials=True,
        status_tool='portal_data_labeler_pool_get'),
    ToolSpec(
        name='portal_data_labeling_preview_run', domain='data',
        title='Start a labeling preview run',
        description=('Runs automatic labeling on 1-5 sample images of a dataset '
                     'prefix to preview a configuration.'),
        resource_type='labeling_preview',
        input_schema=uc_schema({
            'dataset_prefix': text(1024),
            'model': text(256, "'grounded-sam' or 'llm:<model_id>'."),
            'task_type': identifier(),
            'label_set': array(text(256), min_items=1),
            'sample_images': array(text(1024), min_items=1, max_items=5),
            'detection_prompt': text(4000, "Required for 'llm:' models."),
            'prompt_overrides': string_map(),
            'few_shot': {'type': ['boolean', 'object']},
            'downscale_max_edge': integer(minimum=1),
            'token_budget': integer(minimum=1),
        }, required=['dataset_prefix', 'model', 'task_type', 'label_set',
                     'sample_images']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='_start_preview_run', method='POST',
        resource='/labeling-preview/runs', param_map=UC_QUERY,
        permissions=('manage_labeling_jobs',), classification=MUTATING, phase=2,
        idempotent=False, handler_audits=True, handler_audits_denials=True,
        status_tool='portal_data_labeling_preview_get'),
    ToolSpec(
        name='portal_data_labeling_preview_get', domain='data',
        title='Get a labeling preview run',
        description='Returns the status and results of a labeling preview run.',
        resource_type='labeling_preview',
        input_schema=uc_schema({'run_id': identifier()}, required=['run_id']),
        backend='ddaLabeling', handler_module='dda_labeling',
        handler_function='_get_preview_run', method='GET',
        resource='/labeling-preview/runs/{runId}', param_map={'run_id': 'runId'},
        permissions=('manage_labeling_jobs',), handler_audits_denials=True,
        **INJECTED),
    ToolSpec(
        name='portal_data_synthetic_models_list', domain='data',
        title='List synthetic-data models',
        description='Lists the image-generation models available for synthetic data.',
        resource_type='generation_model', input_schema=uc_schema(PAGE_ARGS),
        backend='syntheticData', handler_module='synthetic_data',
        handler_function='get_models', method='GET', resource='/synthetic/models',
        param_map=UC_QUERY, permissions=('manage_datasets',),
        handler_audits_denials=True, result_shape='collection',
        collection_key='models'),
    ToolSpec(
        name='portal_data_synthetic_prompt_templates_get', domain='data',
        title='Get a synthetic prompt template',
        description='Returns the prompt template for an object type and defect type.',
        resource_type='prompt_template',
        input_schema=uc_schema({'object_type': text(256), 'defect_type': text(256)},
                               required=['object_type', 'defect_type']),
        backend='syntheticData', handler_module='synthetic_data',
        handler_function='get_prompt_template', method='GET',
        resource='/synthetic/prompt-templates', param_map=UC_QUERY,
        permissions=('manage_datasets',), handler_audits_denials=True),
    ToolSpec(
        name='portal_data_synthetic_sessions_list', domain='data',
        title='List synthetic-data sessions',
        description='Lists the synthetic-data sessions of a Use_Case.',
        resource_type='synthetic_session', input_schema=uc_schema(PAGE_ARGS),
        backend='syntheticData', handler_module='synthetic_data',
        handler_function='list_sessions', method='GET', resource='/synthetic/sessions',
        param_map=UC_QUERY, permissions=('manage_datasets',),
        handler_audits_denials=True, result_shape='collection',
        collection_key='sessions'),
    ToolSpec(
        name='portal_data_synthetic_session_get', domain='data',
        title='Get a synthetic-data session',
        description='Returns one synthetic-data session with its previews.',
        resource_type='synthetic_session',
        input_schema=uc_schema({'session_id': SESSION_ID}, required=['session_id']),
        backend='syntheticData', handler_module='synthetic_data',
        handler_function='get_session', method='GET',
        resource='/synthetic/sessions/{id}', param_map={'session_id': 'id'},
        permissions=('manage_datasets',), handler_audits_denials=True),
    ToolSpec(
        name='portal_data_synthetic_prompt_templates_set', domain='data',
        title='Set a synthetic prompt template',
        description='Creates or replaces the prompt template for an object and defect type.',
        resource_type='prompt_template',
        input_schema=uc_schema({'object_type': text(256), 'defect_type': text(256),
                                'template_text': text()},
                               required=['object_type', 'defect_type', 'template_text']),
        backend='syntheticData', handler_module='synthetic_data',
        handler_function='put_prompt_template', method='PUT',
        resource='/synthetic/prompt-templates', param_map=UC_QUERY,
        permissions=('manage_datasets',), classification=MUTATING, phase=2,
        handler_audits_denials=True,
        status_tool='portal_data_synthetic_prompt_templates_get'),
    ToolSpec(
        name='portal_data_synthetic_session_create', domain='data',
        title='Create a synthetic-data session',
        description='Creates a synthetic-data generation session.',
        resource_type='synthetic_session',
        input_schema=uc_schema({
            'generation_model_id': identifier(), 'object_type': text(256),
            'defect_type': text(256), 'prompt_template_text': text(),
            'source_class': text(256), 'source_images': array(text(1024)),
            'generation_params': json_object(),
            'target_dataset_prefix': text(1024), 'target_manifest_key': text(1024),
        }),
        backend='syntheticData', handler_module='synthetic_data',
        handler_function='create_session', method='POST',
        resource='/synthetic/sessions', param_map=UC_QUERY,
        permissions=('manage_datasets',), classification=MUTATING, phase=2,
        idempotent=False, handler_audits=True, handler_audits_denials=True,
        resource_id_path='session.session_id',
        status_tool='portal_data_synthetic_session_get'),
    ToolSpec(
        name='portal_data_synthetic_session_update', domain='data',
        title='Update a synthetic-data session',
        description='Updates the generation settings of a synthetic-data session.',
        resource_type='synthetic_session',
        input_schema=uc_schema({
            'session_id': SESSION_ID,
            'generation_model_id': identifier(), 'object_type': text(256),
            'defect_type': text(256), 'prompt_template_text': text(),
            'source_class': text(256), 'source_images': array(text(1024)),
            'generation_params': json_object(),
            'target_dataset_prefix': text(1024), 'target_manifest_key': text(1024),
        }, required=['session_id'], any_of=[
            ['generation_model_id'], ['object_type'], ['defect_type'],
            ['prompt_template_text'], ['source_class'], ['source_images'],
            ['generation_params'], ['target_dataset_prefix'],
            ['target_manifest_key']]),
        backend='syntheticData', handler_module='synthetic_data',
        handler_function='patch_session', method='PATCH',
        resource='/synthetic/sessions/{id}', param_map={'session_id': 'id'},
        permissions=('manage_datasets',), classification=MUTATING, phase=2,
        handler_audits_denials=True,
        status_tool='portal_data_synthetic_session_get'),
    ToolSpec(
        name='portal_data_synthetic_session_generate', domain='data',
        title='Generate synthetic images',
        description='Starts an image-generation pass of a synthetic-data session.',
        resource_type='synthetic_session',
        input_schema=uc_schema({
            'session_id': SESSION_ID,
            'generation_model_id': identifier('Required unless stored on the session.'),
            'prompt_template_text': text(), 'prompt_context': json_object(),
            'defect_type': text(256), 'source_class': text(256),
            'generation_scope': enum('all', 'source_image', 'preview'),
            'source_images': array(text(1024)), 'source_image_key': text(1024),
            'preview_id': identifier(), 'variation_count': integer(minimum=1),
            'generation_params': json_object(),
        }, required=['session_id']),
        backend='syntheticData', handler_module='synthetic_data',
        handler_function='generate', method='POST',
        resource='/synthetic/sessions/{id}/generate',
        param_map={'session_id': 'id', 'generation_scope': 'scope'},
        permissions=('manage_datasets',), classification=MUTATING, phase=2,
        idempotent=False, handler_audits_denials=True,
        status_tool='portal_data_synthetic_session_get'),
    ToolSpec(
        name='portal_data_synthetic_previews_approve', domain='data',
        title='Approve synthetic previews',
        description=('Sets the approval state of chosen previews, or of all previews, '
                     'of a synthetic-data session.'),
        resource_type='synthetic_session',
        input_schema=uc_schema({
            'session_id': SESSION_ID,
            'approval_state': enum('approved', 'rejected', 'pending'),
            'preview_ids': array(identifier(), min_items=1),
            'all': {'type': 'boolean', 'const': True},
        }, required=['session_id', 'approval_state'],
            one_of=[['preview_ids'], ['all']]),
        backend='syntheticData', handler_module='synthetic_data',
        handler_function='set_preview_approval', method='POST',
        resource='/synthetic/sessions/{id}/previews/approval',
        param_map={'session_id': 'id'},
        permissions=('manage_datasets',), classification=MUTATING, phase=2,
        handler_audits_denials=True,
        status_tool='portal_data_synthetic_session_get'),
    ToolSpec(
        name='portal_data_synthetic_session_integrate', domain='data',
        title='Integrate synthetic images',
        description="Appends a session's approved previews to the target dataset manifest.",
        resource_type='synthetic_session',
        input_schema=uc_schema({
            'session_id': SESSION_ID,
            'target_dataset_prefix': text(1024, 'Required unless stored on the session.'),
            'target_manifest_key': text(1024, 'Required unless stored on the session.'),
        }, required=['session_id']),
        backend='syntheticData', handler_module='synthetic_data',
        handler_function='integrate_session', method='POST',
        resource='/synthetic/sessions/{id}/integrate', param_map={'session_id': 'id'},
        permissions=('manage_datasets',), classification=MUTATING, phase=2,
        idempotent=False, handler_audits=True, handler_audits_denials=True,
        status_tool='portal_data_synthetic_session_get'),
    ToolSpec(
        name='portal_data_synthetic_session_retrain', domain='data',
        title='Retrain on a synthetic-data session',
        description="Starts a training job on a session's integrated manifest.",
        resource_type='training_job',
        input_schema=uc_schema({
            'session_id': SESSION_ID,
            'model_name': text(256), 'model_version': text(64),
            'model_type': enum(*TRAINING_MODEL_TYPES),
            'dataset_manifest_s3': s3_uri("Default: the session's integrated manifest."),
            'instance_type': identifier(),
            'max_runtime_seconds': integer(minimum=1),
            'hyperparameters': json_object(),
            'auto_compile': boolean(),
            'compilation_targets': array(enum(*COMPILATION_TARGETS)),
        }, required=['session_id', 'model_name', 'model_version', 'model_type']),
        backend='syntheticData', handler_module='synthetic_data',
        handler_function='retrain_session', method='POST',
        resource='/synthetic/sessions/{id}/retrain', param_map={'session_id': 'id'},
        permissions=('create_training_jobs',), classification=MUTATING, phase=3,
        idempotent=False, handler_audits=True, handler_audits_denials=True,
        usecase_not_forwarded='stored_record',
        status_tool='portal_models_training_get'),
)

# -------------------------------------------------------- workflows view
# Handlers: workflows.py, workflow_validation.py, workflow_packaging.py,
# workflow_testing.py, workflow_generator.py, the node-designer handlers
# (plugin_records.py, plugin_importer.py, node_generator.py,
# plugin_builds.py, plugin_simulator.py, custom_node_types.py,
# plugin_components.py), build_jobs.py, build_fleet.py, build_config.py.
# No portal_workflows_test_dataset_create (a presigned multipart upload).

WORKFLOW_ID = identifier('The workflow id.')
PLUGIN_ID = identifier('The plugin record id.')
PLUGIN_VERSION = identifier('The plugin version number.')
NODE_TYPE_ID = identifier('The custom node type id.')
BUILD_ID = identifier('The build job id.')
SERVER_ID = identifier('The build server id.')
VERSION_NUMBER = integer(minimum=1, description='A workflow version; absent = latest.')
SOURCE_FILES = {'type': 'object',
                'additionalProperties': {'type': 'string', 'maxLength': 1_048_576},
                'description': 'Relative file path -> file text.'}
PLUGIN_VERSION_PATH = {'plugin_id': 'id', 'version': 'v'}


def _plugin_version_read(name: str, title: str, description: str, suffix: str,
                         backend: str, module: str, function: str) -> ToolSpec:
    return ToolSpec(
        name=name, domain='workflows', title=title, description=description,
        resource_type='plugin',
        input_schema=uc_schema({'plugin_id': PLUGIN_ID, 'version': PLUGIN_VERSION},
                               required=['plugin_id', 'version']),
        backend=backend, handler_module=module, handler_function=function,
        method='GET', resource=f'/plugins/{{id}}/versions/{{v}}{suffix}',
        param_map=PLUGIN_VERSION_PATH, permissions=('node-designer:read',),
        handler_audits_denials=True)


def _plugin_version_write(name: str, title: str, description: str, suffix: str,
                          backend: str, module: str, function: str,
                          properties: Optional[dict] = None,
                          required: Sequence[str] = (), **kw) -> ToolSpec:
    groups = {k: kw.pop(k) for k in ('any_of', 'one_of') if k in kw}
    values = dict(
        name=name, domain='workflows', title=title, description=description,
        resource_type='plugin',
        input_schema=uc_schema({'plugin_id': PLUGIN_ID, 'version': PLUGIN_VERSION,
                                **(properties or {})},
                               required=['plugin_id', 'version', *required], **groups),
        backend=backend, handler_module=module, handler_function=function,
        method='POST', resource=f'/plugins/{{id}}/versions/{{v}}{suffix}',
        param_map=dict(PLUGIN_VERSION_PATH), permissions=('node-designer:manage',),
        classification=MUTATING, phase=2, handler_audits=True,
        handler_audits_denials=True)
    values.update(kw)
    return ToolSpec(**values)


WORKFLOWS: Tuple[ToolSpec, ...] = (
    ToolSpec(
        name='portal_workflows_list', domain='workflows', title='List workflows',
        description='Lists the workflows of a Use_Case.',
        resource_type='workflow', input_schema=uc_schema(PAGE_ARGS),
        backend='workflows', handler_module='workflows',
        handler_function='list_workflows', method='GET', resource='/workflows',
        param_map=UC_QUERY, permissions=('workflow:read',),
        handler_audits_denials=True, result_shape='collection',
        collection_key='workflows'),
    ToolSpec(
        name='portal_workflows_get', domain='workflows', title='Get a workflow',
        description='Returns one workflow with the definition of a version (default latest).',
        resource_type='workflow',
        input_schema=uc_schema({'workflow_id': WORKFLOW_ID, 'version': VERSION_NUMBER},
                               required=['workflow_id']),
        backend='workflows', handler_module='workflows',
        handler_function='get_workflow', method='GET', resource='/workflows/{id}',
        param_map={'workflow_id': 'id', 'version': 'version'},
        permissions=('workflow:read',), handler_audits_denials=True),
    ToolSpec(
        name='portal_workflows_versions_list', domain='workflows',
        title='List workflow versions',
        description='Lists the versions of a workflow with their validation status.',
        resource_type='workflow_version',
        input_schema=uc_schema({'workflow_id': WORKFLOW_ID, **PAGE_ARGS},
                               required=['workflow_id']),
        backend='workflows', handler_module='workflows',
        handler_function='list_versions', method='GET',
        resource='/workflows/{id}/versions', param_map={'workflow_id': 'id'},
        permissions=('workflow:read',), handler_audits_denials=True,
        result_shape='collection', collection_key='versions'),
    ToolSpec(
        name='portal_workflows_node_catalog_get', domain='workflows',
        title='Get the node catalog',
        description='Returns the workflow node types available in a Use_Case, custom nodes included.',
        resource_type='node_type', input_schema=uc_schema(),
        backend='workflowValidation', handler_module='workflow_validation',
        handler_function='get_node_catalog', method='GET',
        resource='/workflows/node-catalog', param_map=UC_QUERY,
        permissions=('workflow:read',), handler_audits_denials=True),
    ToolSpec(
        name='portal_workflows_validate', domain='workflows', title='Validate a workflow',
        description=('Validates the stored definition of a workflow version and '
                     'records its validation status.'),
        resource_type='workflow_version',
        input_schema=uc_schema({'workflow_id': WORKFLOW_ID, 'version': VERSION_NUMBER},
                               required=['workflow_id']),
        backend='workflowValidation', handler_module='workflow_validation',
        handler_function='validate_workflow', method='POST',
        resource='/workflows/{id}/validate',
        param_map={'workflow_id': 'id', 'version': 'version'},
        permissions=('workflow:edit',), classification=MUTATING, phase=2,
        handler_audits_denials=True, status_tool='portal_workflows_versions_list'),
    ToolSpec(
        name='portal_workflows_generation_job_get', domain='workflows',
        title='Get a workflow generation job',
        description='Returns the status and result of a workflow generation job.',
        resource_type='generation_job',
        input_schema=uc_schema({'job_id': identifier('The generation job id.')},
                               required=['job_id']),
        backend='workflowGenerator', handler_module='workflow_generator',
        handler_function='get_generation_job', method='GET',
        resource='/workflows/generate/{job_id}', param_map={'job_id': 'job_id'},
        permissions=('workflow:create',), handler_audits_denials=True,
        **_status_sync(AcceptedWrite('workflow_generator.record_job_failure',
                                     'update_item',
                                     'dynamodb.Table(WORKFLOW_CHAT_SESSIONS_TABLE)', 1))),
    ToolSpec(
        name='portal_workflows_create', domain='workflows', title='Create a workflow',
        description='Creates a workflow with its first version.',
        resource_type='workflow',
        input_schema=uc_schema({'name': text(128), 'definition': json_object(),
                                'description': text(2000)},
                               required=['name', 'definition']),
        backend='workflows', handler_module='workflows',
        handler_function='create_workflow', method='POST', resource='/workflows',
        param_map=UC_QUERY, permissions=('workflow:create',),
        classification=MUTATING, phase=2, idempotent=False,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_get'),
    ToolSpec(
        name='portal_workflows_duplicate', domain='workflows',
        title='Duplicate a workflow',
        description="Copies a workflow's latest definition into a new workflow.",
        resource_type='workflow',
        input_schema=uc_schema({'workflow_id': WORKFLOW_ID, 'name': text(128),
                                'description': text(2000)},
                               required=['workflow_id']),
        backend='workflows', handler_module='workflows',
        handler_function='duplicate_workflow', method='POST',
        resource='/workflows/{id}/duplicate', param_map={'workflow_id': 'id'},
        permissions=('workflow:create',), classification=MUTATING, phase=2,
        idempotent=False, handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_get'),
    ToolSpec(
        name='portal_workflows_update', domain='workflows', title='Save a workflow',
        description='Saves a new version of a workflow definition.',
        resource_type='workflow',
        input_schema=uc_schema({'workflow_id': WORKFLOW_ID, 'definition': json_object(),
                                'name': text(128), 'description': text(2000)},
                               required=['workflow_id', 'definition']),
        backend='workflows', handler_module='workflows',
        handler_function='update_workflow', method='PUT', resource='/workflows/{id}',
        param_map={'workflow_id': 'id'}, permissions=('workflow:save',),
        classification=MUTATING, phase=2, idempotent=False,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_get'),
    ToolSpec(
        name='portal_workflows_rename', domain='workflows', title='Rename a workflow',
        description='Renames a workflow.',
        resource_type='workflow',
        input_schema=uc_schema({'workflow_id': WORKFLOW_ID,
                                'name': {'type': 'string', 'minLength': 1,
                                         'maxLength': 128}},
                               required=['workflow_id', 'name']),
        backend='workflows', handler_module='workflows',
        handler_function='rename_workflow', method='PATCH',
        resource='/workflows/{id}/name', param_map={'workflow_id': 'id'},
        permissions=('workflow:save',), classification=MUTATING, phase=2,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_get'),
    ToolSpec(
        name='portal_workflows_generate', domain='workflows',
        title='Generate a workflow',
        description='Starts a Bedrock job that drafts a workflow from a prompt.',
        resource_type='generation_job',
        input_schema=uc_schema({'prompt': text()}, required=['prompt']),
        backend='workflowGenerator', handler_module='workflow_generator',
        handler_function='submit_generation', method='POST',
        resource='/workflows/generate', param_map=UC_QUERY,
        permissions=('workflow:create',), classification=MUTATING, phase=2,
        idempotent=False, handler_audits_denials=True,
        status_tool='portal_workflows_generation_job_get'),
    ToolSpec(
        name='portal_workflows_delete', domain='workflows', title='Delete a workflow',
        description='Deletes a workflow and its versions.',
        resource_type='workflow',
        input_schema=uc_schema({'workflow_id': WORKFLOW_ID,
                                'confirm': confirm_arg('Repeat workflow_id.')},
                               required=['workflow_id']),
        backend='workflows', handler_module='workflows',
        handler_function='delete_workflow', method='DELETE',
        resource='/workflows/{id}', param_map={'workflow_id': 'id'},
        permissions=('workflow:delete',), classification=MUTATING,
        destructive=True, confirm_target='workflow_id', phase=3,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_list'),
    ToolSpec(
        name='portal_workflows_package', domain='workflows', title='Package a workflow',
        description='Packages a workflow version as a Greengrass component for the architectures.',
        resource_type='workflow',
        input_schema=uc_schema({'workflow_id': WORKFLOW_ID,
                                'architectures': array(identifier(), min_items=1),
                                'version': VERSION_NUMBER},
                               required=['workflow_id', 'architectures']),
        backend='workflowPackaging', handler_module='workflow_packaging',
        handler_function='package_workflow', method='POST',
        resource='/workflows/{id}/package',
        param_map={'workflow_id': 'id', 'version': 'version'},
        permissions=('workflow:package',), classification=MUTATING, phase=3,
        idempotent=False, handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_get'),
    ToolSpec(
        name='portal_workflows_test_runs_list', domain='workflows',
        title='List workflow test runs',
        description='Lists the test runs of a workflow.',
        resource_type='test_run',
        input_schema=uc_schema({'workflow_id': WORKFLOW_ID, **PAGE_ARGS},
                               required=['workflow_id']),
        backend='workflowTesting', handler_module='workflow_testing',
        handler_function='list_test_runs', method='GET',
        resource='/workflows/{id}/test-runs', param_map={'workflow_id': 'id'},
        permissions=('workflow:read',), handler_audits_denials=True,
        result_shape='collection',
        collection_key='test_runs'),
    ToolSpec(
        name='portal_workflows_test_run_get', domain='workflows',
        title='Get a workflow test run',
        description=('Returns one test run with its node results; refreshes its status '
                     'from Step Functions as the test page does.'),
        resource_type='test_run',
        input_schema=uc_schema({'run_id': identifier('The test run id.')},
                               required=['run_id']),
        backend='workflowTesting', handler_module='workflow_testing',
        handler_function='get_test_run', method='GET', resource='/test-runs/{id}',
        param_map={'run_id': 'id'}, permissions=('workflow:read',),
        handler_audits_denials=True,
        **_status_sync(AcceptedWrite('workflow_testing.sync_run_status_from_execution',
                                     'update_item', 'dynamodb.Table(TEST_RUNS_TABLE)', 1))),
    ToolSpec(
        name='portal_workflows_test_datasets_list', domain='workflows',
        title='List test datasets',
        description='Lists the workflow test datasets of a Use_Case.',
        resource_type='test_dataset', input_schema=uc_schema(PAGE_ARGS),
        backend='workflowTesting', handler_module='workflow_testing',
        handler_function='list_test_datasets', method='GET',
        resource='/test-datasets', param_map=UC_QUERY,
        permissions=('workflow:read',), handler_audits_denials=True,
        result_shape='collection',
        collection_key='datasets'),
    ToolSpec(
        name='portal_workflows_test_dataset_get', domain='workflows',
        title='Get a test dataset',
        description='Returns one workflow test dataset.',
        resource_type='test_dataset',
        input_schema=uc_schema({'dataset_id': identifier()}, required=['dataset_id']),
        backend='workflowTesting', handler_module='workflow_testing',
        handler_function='get_test_dataset', method='GET',
        resource='/test-datasets/{id}', param_map={'dataset_id': 'id'},
        permissions=('workflow:read',),
        handler_audits_denials=True),
    ToolSpec(
        name='portal_workflows_test_run_start', domain='workflows',
        title='Start a workflow test run',
        description='Runs a workflow version over a test dataset of its Use_Case.',
        resource_type='test_run',
        input_schema=uc_schema({
            'workflow_id': WORKFLOW_ID,
            'dataset_id': identifier('A test dataset of the workflow\'s Use_Case.'),
            'version': VERSION_NUMBER,
            'simulated_inference': {
                'type': 'object',
                'properties': {'is_anomalous': boolean(), 'confidence': number(0, 1)},
                'additionalProperties': False},
        }, required=['workflow_id', 'dataset_id']),
        backend='workflowTesting', handler_module='workflow_testing',
        handler_function='start_test_run', method='POST',
        resource='/workflows/{id}/test-runs', param_map={'workflow_id': 'id'},
        permissions=('workflow:test',), handler_audits_denials=True,
        classification=MUTATING, phase=2,
        idempotent=False, handler_audits=True,
        resource_id_path='test_run.test_run_id',
        status_tool='portal_workflows_test_run_get'),
    ToolSpec(
        name='portal_workflows_test_dataset_delete', domain='workflows',
        title='Delete a test dataset',
        description='Deletes a workflow test dataset and its images.',
        resource_type='test_dataset',
        input_schema=uc_schema({'dataset_id': identifier(),
                                'confirm': confirm_arg('Repeat dataset_id.')},
                               required=['dataset_id']),
        backend='workflowTesting', handler_module='workflow_testing',
        handler_function='delete_test_dataset', method='DELETE',
        resource='/test-datasets/{id}', param_map={'dataset_id': 'id'},
        permissions=('workflow:test',), handler_audits_denials=True,
        classification=MUTATING,
        destructive=True, confirm_target='dataset_id', phase=3,
        handler_audits=True, status_tool='portal_workflows_test_datasets_list'),
    ToolSpec(
        name='portal_workflows_plugins_list', domain='workflows', title='List plugins',
        description='Lists the custom-node plugin records of a Use_Case.',
        resource_type='plugin',
        input_schema=uc_schema({'review': enum('pending', 'approved', 'rejected',
                                               description='Filter by review decision.'),
                                **PAGE_ARGS}),
        backend='pluginRecords', handler_module='plugin_records',
        handler_function='list_plugins', method='GET', resource='/plugins',
        param_map={'usecase_id': 'usecase_id', 'review': 'review'},
        permissions=('node-designer:read',), handler_audits_denials=True,
        result_shape='collection', collection_key='plugins'),
    ToolSpec(
        name='portal_workflows_plugin_get', domain='workflows', title='Get a plugin',
        description='Returns one plugin record with its versions.',
        resource_type='plugin',
        input_schema=uc_schema({'plugin_id': PLUGIN_ID}, required=['plugin_id']),
        backend='pluginRecords', handler_module='plugin_records',
        handler_function='get_plugin', method='GET', resource='/plugins/{id}',
        param_map={'plugin_id': 'id'}, permissions=('node-designer:read',),
        handler_audits_denials=True),
    _plugin_version_read('portal_workflows_plugin_version_get', 'Get a plugin version',
                         'Returns one version of a plugin record.', '',
                         'pluginRecords', 'plugin_records', 'get_version'),
    _plugin_version_read('portal_workflows_plugin_builds_get', 'Get plugin builds',
                         "Returns the per-architecture builds of a plugin version.",
                         '/builds', 'pluginBuilds', 'plugin_builds', 'get_builds'),
    _plugin_version_read('portal_workflows_plugin_component_get',
                         'Get a plugin component',
                         'Returns the Greengrass component packaged from a plugin version.',
                         '/component', 'pluginComponents', 'plugin_components',
                         'get_component'),
    _plugin_version_read('portal_workflows_plugin_source_get', 'Get plugin source',
                         'Lists the source files of a plugin version.',
                         '/source', 'pluginRecords', 'plugin_records',
                         'get_version_source'),
    _plugin_version_read('portal_workflows_plugin_gst_properties_get',
                         'Get plugin GStreamer properties',
                         'Returns the introspected GStreamer element properties of a plugin version.',
                         '/gst-properties', 'pluginRecords', 'plugin_records',
                         'get_version_gst_properties'),
    ToolSpec(
        name='portal_workflows_plugin_modules_list', domain='workflows',
        title='List plugin modules',
        description='Lists the GStreamer plugin modules known to the importer.',
        resource_type='plugin_module', input_schema=uc_schema(PAGE_ARGS),
        backend='pluginImporter', handler_module='plugin_importer',
        handler_function='list_plugin_modules', method='GET',
        resource='/plugin-modules', permissions=('node-designer:read',),
        result_shape='collection', collection_key='modules',
        **_status_sync(AcceptedWrite('plugin_importer.write_module_index_cache',
                                     'put_item', 'module_cache_table()', 1))),
    ToolSpec(
        name='portal_workflows_simulation_get', domain='workflows',
        title='Get a plugin simulation',
        description=('Returns a plugin simulation run and its results; refreshes its '
                     'status from Step Functions as the simulator page does.'),
        resource_type='simulation_run',
        input_schema=uc_schema({'run_id': identifier('The simulation run id.')},
                               required=['run_id']),
        backend='pluginSimulator', handler_module='plugin_simulator',
        handler_function='get_simulation', method='GET',
        resource='/simulations/{runId}', param_map={'run_id': 'runId'},
        permissions=('node-designer:read',),
        **_status_sync(AcceptedWrite('plugin_simulator.sync_run_status_from_execution',
                                     'update_item', 'runs_table()', 1))),
    ToolSpec(
        name='portal_workflows_custom_node_types_list', domain='workflows',
        title='List custom node types',
        description='Lists the custom node types registered from a plugin.',
        resource_type='node_type',
        input_schema=uc_schema({'plugin_id': PLUGIN_ID, **PAGE_ARGS},
                               required=['plugin_id']),
        backend='customNodeTypes', handler_module='custom_node_types',
        handler_function='list_node_types', method='GET',
        resource='/custom-node-types', param_map={'plugin_id': 'plugin_id'},
        permissions=('node-designer:read',), result_shape='collection',
        collection_key='nodeTypes'),
    ToolSpec(
        name='portal_workflows_custom_node_type_get', domain='workflows',
        title='Get a custom node type',
        description='Returns one custom node type with its versions.',
        resource_type='node_type',
        input_schema=uc_schema({'node_type_id': NODE_TYPE_ID},
                               required=['node_type_id']),
        backend='customNodeTypes', handler_module='custom_node_types',
        handler_function='get_node_type', method='GET',
        resource='/custom-node-types/{id}', param_map={'node_type_id': 'id'},
        permissions=('node-designer:read',),
        handler_audits_denials=True),
    ToolSpec(
        name='portal_workflows_plugin_create', domain='workflows',
        title='Create a plugin',
        description='Creates a custom-node plugin record (scaffold, generated or imported).',
        resource_type='plugin',
        input_schema=uc_schema({
            'name': text(128),
            'kind': enum('scaffold', 'generated', 'imported'),
            'description': text(2000), 'deepstream': boolean(),
            'declaration': json_object('Kind scaffold only.'),
        }, required=['name', 'kind']),
        backend='pluginRecords', handler_module='plugin_records',
        handler_function='create_plugin', method='POST', resource='/plugins',
        param_map=UC_QUERY, permissions=('node-designer:create',),
        classification=MUTATING, phase=2, idempotent=False,
        handler_audits=True, handler_audits_denials=True,
        resource_id_path='plugin.plugin_id',
        status_tool='portal_workflows_plugin_get'),
    ToolSpec(
        name='portal_workflows_plugin_import', domain='workflows',
        title='Import a plugin repository',
        description='Imports GStreamer plugins from a public Git repository.',
        resource_type='plugin',
        input_schema=uc_schema({
            'repo_url': text(2048, 'An HTTPS repository URL.'),
            'architectures': array(identifier(), min_items=1),
            'revision': text(256), 'name': text(128), 'description': text(2000),
            'deepstream': boolean(), 'module_name': identifier(),
            'selected_plugins': array(identifier()),
        }, required=['repo_url', 'architectures']),
        backend='pluginImporter', handler_module='plugin_importer',
        handler_function='import_repository', method='POST',
        resource='/plugins/import', param_map=UC_QUERY,
        permissions=('node-designer:import',), classification=MUTATING, phase=2,
        idempotent=False, handler_audits=True, handler_audits_denials=True,
        resource_id_path='plugin.plugin_id',
        status_tool='portal_workflows_plugin_get'),
    ToolSpec(
        name='portal_workflows_plugin_generate_start', domain='workflows',
        title='Start a plugin generation',
        description='Starts a Bedrock session that drafts a custom-node scaffold.',
        resource_type='generation_session',
        input_schema=uc_schema({'prompt': text(), 'declaration': json_object()},
                               required=['prompt', 'declaration']),
        backend='nodeGenerator', handler_module='node_generator',
        handler_function='generate_scaffold', method='POST',
        resource='/plugins/generate', param_map=UC_QUERY,
        permissions=('node-designer:generate',), classification=MUTATING,
        phase=2, idempotent=False, handler_audits_denials=True,
        status_tool='portal_workflows_plugin_generate_status_get'),
    ToolSpec(
        name='portal_workflows_plugin_generate_status_get', domain='workflows',
        title='Get a plugin generation session',
        description='Returns the state of a plugin scaffold-generation session.',
        resource_type='generation_session',
        input_schema=uc_schema({'session_id': identifier('The generation session id.')},
                               required=['session_id']),
        backend='nodeGenerator', handler_module='node_generator',
        handler_function='get_generation_status', method='GET',
        resource='/plugins/generate/{session}', param_map={'session_id': 'session'},
        permissions=('node-designer:generate',), phase=2,
        handler_audits_denials=True),
    ToolSpec(
        name='portal_workflows_plugin_generate_message', domain='workflows',
        title='Continue a plugin generation',
        description=('Sends a follow-up prompt to a plugin generation session, or '
                     'accepts its scaffold as a plugin.'),
        resource_type='generation_session',
        input_schema=uc_schema({
            'session_id': identifier('The generation session id.'),
            'prompt': text(),
            'accept': {'type': 'boolean', 'const': True},
            'name': text(128), 'description': text(2000), 'deepstream': boolean(),
        }, required=['session_id'], one_of=[['prompt'], ['accept']]),
        backend='nodeGenerator', handler_module='node_generator',
        handler_function='session_message', method='POST',
        resource='/plugins/generate/{session}/message',
        param_map={'session_id': 'session'},
        permissions=('node-designer:generate',), classification=MUTATING,
        phase=2, idempotent=False, handler_audits_denials=True,
        status_tool='portal_workflows_plugin_generate_status_get'),
    ToolSpec(
        name='portal_workflows_plugin_update', domain='workflows',
        title='Update a plugin',
        description="Updates a plugin record's name, description or flags.",
        resource_type='plugin',
        input_schema=uc_schema({
            'plugin_id': PLUGIN_ID, 'name': text(128), 'description': text(2000),
            'deepstream': boolean(), 'new_version': boolean(),
        }, required=['plugin_id'], any_of=[['name'], ['description'],
                                           ['deepstream'], ['new_version']]),
        backend='pluginRecords', handler_module='plugin_records',
        handler_function='update_plugin', method='PUT', resource='/plugins/{id}',
        param_map={'plugin_id': 'id'}, permissions=('node-designer:manage',),
        classification=MUTATING, phase=2, handler_audits=True,
        handler_audits_denials=True, status_tool='portal_workflows_plugin_get'),
    ToolSpec(
        name='portal_workflows_plugin_source_set', domain='workflows',
        title='Replace plugin source',
        description=("Writes a plugin version's source files; mode replace (the "
                     'default) deletes every stored file absent from files.'),
        resource_type='plugin',
        input_schema=uc_schema({
            'plugin_id': PLUGIN_ID, 'version': PLUGIN_VERSION,
            'files': SOURCE_FILES, 'delete': array(text(1024)),
            'mode': enum('replace', 'merge'),
            'expected_source_revision': identifier(),
            'confirm': confirm_arg('Repeat plugin_id.'),
        }, required=['plugin_id', 'version'], any_of=[['files'], ['delete']]),
        backend='pluginRecords', handler_module='plugin_records',
        handler_function='put_version_source', method='PUT',
        resource='/plugins/{id}/versions/{v}/source', param_map=PLUGIN_VERSION_PATH,
        permissions=('node-designer:manage',), classification=MUTATING,
        destructive=True, confirm_target='plugin_id', phase=2,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_plugin_source_get'),
    _plugin_version_write('portal_workflows_plugin_build', 'Build a plugin version',
                          'Starts the per-architecture builds of a plugin version.',
                          '/build', 'pluginBuilds', 'plugin_builds', 'start_builds',
                          {'architectures': array(identifier(), min_items=1)},
                          idempotent=False,
                          status_tool='portal_workflows_plugin_builds_get'),
    _plugin_version_write('portal_workflows_plugin_new_version',
                          'Save a plugin as a new version',
                          "Copies a plugin version's source to a new version with edits applied.",
                          '/new-version', 'pluginRecords', 'plugin_records',
                          'new_version_from_source',
                          {'files': SOURCE_FILES, 'delete': array(text(1024)),
                           'name': text(128), 'description': text(2000)},
                          idempotent=False, status_tool='portal_workflows_plugin_get'),
    _plugin_version_write('portal_workflows_plugin_adjust_revision',
                          'Adjust an imported plugin revision',
                          ('Rebuilds one architecture of an imported plugin version at '
                           'another source revision.'),
                          '/adjust-revision', 'pluginImporter', 'plugin_importer',
                          'adjust_revision',
                          {'architecture': identifier(), 'revision': text(256)},
                          required=('architecture', 'revision'), idempotent=False,
                          status_tool='portal_workflows_plugin_builds_get'),
    _plugin_version_write('portal_workflows_plugin_architectures_set',
                          'Add plugin architectures',
                          'Adds target architectures to a plugin version and builds them.',
                          '/architectures', 'pluginBuilds', 'plugin_builds',
                          'add_architectures',
                          {'architectures': array(identifier(), min_items=1)},
                          required=('architectures',), idempotent=False,
                          status_tool='portal_workflows_plugin_builds_get'),
    _plugin_version_write('portal_workflows_plugin_select_plugins',
                          'Select imported plugins',
                          ('Completes an import awaiting selection with the chosen '
                           'subset of the plugins found.'),
                          '/select-plugins', 'pluginImporter', 'plugin_importer',
                          'select_plugins',
                          {'selected_plugins': array(identifier(), min_items=1)},
                          required=('selected_plugins',), idempotent=False,
                          status_tool='portal_workflows_plugin_get'),
    _plugin_version_write('portal_workflows_plugin_simulate', 'Simulate a plugin',
                          ('Runs a plugin version over a test dataset or sample frames '
                           '(exactly one of the two).'),
                          '/simulate', 'pluginSimulator', 'plugin_simulator',
                          'start_simulation',
                          {'dataset_id': identifier('A test dataset of the same Use_Case.'),
                           'sample_frames': array({
                               'type': 'object',
                               'properties': {'name': text(256),
                                              'content_base64': text(10_485_760)},
                               'required': ['name', 'content_base64'],
                               'additionalProperties': False}, min_items=1),
                           'parameters': json_object(),
                           'element_factory': identifier()},
                          one_of=[['dataset_id'], ['sample_frames']],
                          permissions=('node-designer:simulate',), idempotent=False,
                          resource_id_path='simulation_run.run_id',
                          status_tool='portal_workflows_simulation_get'),
    ToolSpec(
        name='portal_workflows_custom_node_type_register', domain='workflows',
        title='Register a custom node type',
        description='Registers a node type declared by a plugin version.',
        resource_type='node_type',
        input_schema=uc_schema({'plugin_id': PLUGIN_ID, 'declaration': json_object(),
                                'plugin_version': identifier('Absent = the latest version.')},
                               required=['plugin_id', 'declaration']),
        backend='customNodeTypes', handler_module='custom_node_types',
        handler_function='register_node_type', method='POST',
        resource='/custom-node-types', permissions=('node-designer:register',),
        classification=MUTATING, phase=2, idempotent=False,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_custom_node_type_get'),
    ToolSpec(
        name='portal_workflows_custom_node_type_update', domain='workflows',
        title='Update a custom node type',
        description="Updates a custom node type's declaration or plugin version.",
        resource_type='node_type',
        input_schema=uc_schema({'node_type_id': NODE_TYPE_ID,
                                'declaration': json_object(),
                                'plugin_version': identifier()},
                               required=['node_type_id'],
                               any_of=[['declaration'], ['plugin_version']]),
        backend='customNodeTypes', handler_module='custom_node_types',
        handler_function='update_node_type', method='PUT',
        resource='/custom-node-types/{id}', param_map={'node_type_id': 'id'},
        permissions=('node-designer:manage',), classification=MUTATING, phase=2,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_custom_node_type_get'),
    _plugin_version_write('portal_workflows_plugin_promote', 'Promote a plugin version',
                          'Promotes a plugin version to the next lifecycle stage.',
                          '/promote', 'pluginRecords', 'plugin_records',
                          'promote_version',
                          {'confirm': confirm_arg('Repeat plugin_id.')},
                          permissions=('node-designer:promote-demote',),
                          destructive=True, confirm_target='plugin_id', phase=3,
                          status_tool='portal_workflows_plugin_version_get'),
    _plugin_version_write('portal_workflows_plugin_demote', 'Demote a plugin version',
                          'Demotes a plugin version to the previous lifecycle stage.',
                          '/demote', 'pluginRecords', 'plugin_records',
                          'demote_version',
                          {'confirm': confirm_arg('Repeat plugin_id.')},
                          permissions=('node-designer:promote-demote',),
                          destructive=True, confirm_target='plugin_id', phase=3,
                          status_tool='portal_workflows_plugin_version_get'),
    _plugin_version_write('portal_workflows_plugin_review', 'Review a plugin version',
                          'Records the security review decision of a plugin version.',
                          '/review', 'pluginRecords', 'plugin_records',
                          'review_version',
                          {'decision': enum('approved', 'rejected'),
                           'notes': text(4000),
                           'confirm': confirm_arg('Repeat plugin_id.')},
                          required=('decision',),
                          permissions=('node-designer:security-review',),
                          destructive=True, confirm_target='plugin_id', phase=3,
                          status_tool='portal_workflows_plugin_version_get'),
    ToolSpec(
        name='portal_workflows_plugin_delete', domain='workflows',
        title='Delete a plugin',
        description='Deletes a plugin record and all of its versions.',
        resource_type='plugin',
        input_schema=uc_schema({'plugin_id': PLUGIN_ID,
                                'confirm': confirm_arg('Repeat plugin_id.')},
                               required=['plugin_id']),
        backend='pluginRecords', handler_module='plugin_records',
        handler_function='delete_plugin', method='DELETE', resource='/plugins/{id}',
        param_map={'plugin_id': 'id'}, permissions=('node-designer:manage',),
        classification=MUTATING, destructive=True, confirm_target='plugin_id',
        phase=3, handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_plugins_list'),
    ToolSpec(
        name='portal_workflows_custom_node_type_deprecate', domain='workflows',
        title='Deprecate a custom node type',
        description='Deprecates a custom node type so new workflows cannot add it.',
        resource_type='node_type',
        input_schema=uc_schema({'node_type_id': NODE_TYPE_ID,
                                'confirm': confirm_arg('Repeat node_type_id.')},
                               required=['node_type_id']),
        backend='customNodeTypes', handler_module='custom_node_types',
        handler_function='deprecate_node_type', method='POST',
        resource='/custom-node-types/{id}/deprecate', param_map={'node_type_id': 'id'},
        permissions=('node-designer:manage',), classification=MUTATING,
        destructive=True, confirm_target='node_type_id', phase=3,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_custom_node_type_get'),
    ToolSpec(
        name='portal_workflows_custom_node_type_delete', domain='workflows',
        title='Delete a custom node type',
        description='Removes a custom node type and its versions.',
        resource_type='node_type',
        input_schema=uc_schema({'node_type_id': NODE_TYPE_ID,
                                'confirm': confirm_arg('Repeat node_type_id.')},
                               required=['node_type_id']),
        backend='customNodeTypes', handler_module='custom_node_types',
        handler_function='remove_node_type', method='DELETE',
        resource='/custom-node-types/{id}', param_map={'node_type_id': 'id'},
        permissions=('node-designer:manage',), classification=MUTATING,
        destructive=True, confirm_target='node_type_id', phase=3,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_custom_node_types_list'),
    ToolSpec(
        name='portal_workflows_builds_list', domain='workflows', title='List builds',
        description='Returns one page of LocalServer component build jobs, newest first.',
        resource_type='build_job',
        input_schema=schema({'limit': LIMIT, 'next_token': NEXT_TOKEN}),
        backend='buildJobs', handler_module='build_jobs',
        handler_function='list_builds', method='GET', resource='/builds',
        param_map={'limit': 'limit', 'next_token': 'nextToken'},
        permissions=('builds:read',), scope='global', handler_audits_denials=True,
        result_shape='collection', collection_key='jobs'),
    ToolSpec(
        name='portal_workflows_build_get', domain='workflows', title='Get a build',
        description='Returns one build job.',
        resource_type='build_job',
        input_schema=schema({'build_id': BUILD_ID}, required=['build_id']),
        backend='buildJobs', handler_module='build_jobs',
        handler_function='get_build', method='GET', resource='/builds/{id}',
        param_map={'build_id': 'id'}, permissions=('builds:read',),
        scope='global', handler_audits_denials=True),
    ToolSpec(
        name='portal_workflows_build_logs_get', domain='workflows',
        title='Get build logs',
        description='Returns one page of a build job\'s log events.',
        resource_type='log_event',
        input_schema=schema({'build_id': BUILD_ID, 'limit': LIMIT,
                             'next_token': NEXT_TOKEN}, required=['build_id']),
        backend='buildJobs', handler_module='build_jobs',
        handler_function='get_build_logs', method='GET',
        resource='/builds/{id}/logs',
        param_map={'build_id': 'id', 'limit': 'limit', 'next_token': 'nextToken'},
        permissions=('builds:read',), scope='global', handler_audits_denials=True,
        result_shape='collection', collection_key='events', limit_max=200),
    ToolSpec(
        name='portal_workflows_build_branches_list', domain='workflows',
        title='List repository branches',
        description='Lists the branches of a build source repository.',
        resource_type='branch',
        input_schema=schema({'repository': text(2048, 'An HTTPS GitHub repository URL.'),
                             **PAGE_ARGS}, required=['repository']),
        backend='buildJobs', handler_module='build_jobs',
        handler_function='list_build_branches', method='GET',
        resource='/build-branches', param_map={'repository': 'repository'},
        permissions=('builds:read',), scope='global', handler_audits_denials=True,
        result_shape='collection', collection_key='branches'),
    ToolSpec(
        name='portal_workflows_build_submit', domain='workflows', title='Submit a build',
        description='Submits LocalServer component builds for one or more build targets.',
        resource_type='build_job',
        input_schema=schema({
            'targets': array(identifier(), min_items=1),
            'execution_mode': enum('ephemeral', 'dedicated'),
            'server_id': identifier('Required when execution_mode is dedicated.'),
            'repository': text(2048, 'An HTTPS GitHub repository URL.'),
            'source_ref': text(256, 'A branch, tag or 40-hex commit SHA.'),
        }, required=['targets', 'execution_mode']),
        backend='buildJobs', handler_module='build_jobs',
        handler_function='submit_build', method='POST', resource='/builds',
        permissions=('builds:submit',), scope='global', classification=MUTATING,
        phase=2, idempotent=False, handler_audits=True, handler_audits_denials=True,
        resource_id_path='jobs[0].build_job_id',
        status_tool='portal_workflows_build_get'),
    ToolSpec(
        name='portal_workflows_build_retry', domain='workflows', title='Retry a build',
        description='Submits a new build job with the settings of a finished one.',
        resource_type='build_job',
        input_schema=schema({'build_id': BUILD_ID}, required=['build_id']),
        backend='buildJobs', handler_module='build_jobs',
        handler_function='retry_build', method='POST',
        resource='/builds/{id}/retry', param_map={'build_id': 'id'},
        permissions=('builds:submit',), scope='global', classification=MUTATING,
        phase=2, idempotent=False, handler_audits=True, handler_audits_denials=True,
        resource_id_path='job.build_job_id', status_tool='portal_workflows_build_get'),
    ToolSpec(
        name='portal_workflows_build_cancel', domain='workflows', title='Cancel a build',
        description='Cancels a queued or running build job.',
        resource_type='build_job',
        input_schema=schema({'build_id': BUILD_ID,
                             'confirm': confirm_arg('Repeat build_id.')},
                            required=['build_id']),
        backend='buildJobs', handler_module='build_jobs',
        handler_function='cancel_build', method='POST',
        resource='/builds/{id}/cancel', param_map={'build_id': 'id'},
        permissions=('builds:cancel',), scope='global', classification=MUTATING,
        destructive=True, confirm_target='build_id', phase=3,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_builds_list'),
    ToolSpec(
        name='portal_workflows_build_servers_list', domain='workflows',
        title='List build servers',
        description=('Lists the dedicated build servers; records their EC2 lifecycle '
                     'state as the fleet page does.'),
        resource_type='build_server', input_schema=schema(PAGE_ARGS),
        backend='buildFleet', handler_module='build_fleet',
        handler_function='list_build_servers', method='GET',
        resource='/build-servers', permissions=('builds:read',), scope='global',
        handler_audits_denials=True, result_shape='collection',
        collection_key='servers',
        **_status_sync(AcceptedWrite('build_fleet.apply_observed_state',
                                     'update_item', 'servers_table()', 1))),
    ToolSpec(
        name='portal_workflows_build_config_get', domain='workflows',
        title='Get the build configuration',
        description='Returns the build-system configuration parameters.',
        resource_type='build_config', input_schema=schema(),
        backend='buildConfig', handler_module='build_config',
        handler_function='get_build_config', method='GET', resource='/build-config',
        permissions=('builds:read',), scope='global', handler_audits_denials=True),
    ToolSpec(
        name='portal_workflows_build_server_create', domain='workflows',
        title='Launch a build server',
        description='Launches a dedicated EC2 build server.',
        resource_type='build_server',
        input_schema=schema({
            'name': text(128), 'architecture': identifier(),
            'ubuntu_version': enum('22.04', '24.04',
                                   description='22.04 default; 24.04 arm64 only.'),
            'ubuntu_flavor': enum('pro', 'standard'),
        }, required=['name', 'architecture']),
        backend='buildFleet', handler_module='build_fleet',
        handler_function='launch_build_server', method='POST',
        resource='/build-servers', permissions=('system_admin',), scope='global',
        classification=MUTATING, phase=3, idempotent=False,
        handler_audits=True, handler_audits_denials=True,
        resource_id_path='server.server_id',
        status_tool='portal_workflows_build_servers_list'),
    ToolSpec(
        name='portal_workflows_build_server_start', domain='workflows',
        title='Start a build server',
        description='Starts a stopped dedicated build server.',
        resource_type='build_server',
        input_schema=schema({'server_id': SERVER_ID}, required=['server_id']),
        backend='buildFleet', handler_module='build_fleet',
        handler_function='start_build_server', method='POST',
        resource='/build-servers/{id}/start', param_map={'server_id': 'id'},
        permissions=('system_admin',), scope='global', classification=MUTATING,
        phase=3, handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_build_servers_list'),
    ToolSpec(
        name='portal_workflows_build_server_stop', domain='workflows',
        title='Stop a build server',
        description='Stops a running dedicated build server.',
        resource_type='build_server',
        input_schema=schema({'server_id': SERVER_ID,
                             'confirm': confirm_arg('Repeat server_id.')},
                            required=['server_id']),
        backend='buildFleet', handler_module='build_fleet',
        handler_function='stop_build_server', method='POST',
        resource='/build-servers/{id}/stop', param_map={'server_id': 'id'},
        permissions=('system_admin',), scope='global', classification=MUTATING,
        destructive=True, confirm_target='server_id', phase=3,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_build_servers_list'),
    ToolSpec(
        name='portal_workflows_build_server_delete', domain='workflows',
        title='Terminate a build server',
        description='Terminates a dedicated build server; server_name must equal its stored name.',
        resource_type='build_server',
        input_schema=schema({'server_id': SERVER_ID,
                             'server_name': text(128, "The server's stored name."),
                             'confirm': confirm_arg('Repeat server_name.')},
                            required=['server_id', 'server_name']),
        backend='buildFleet', handler_module='build_fleet',
        handler_function='terminate_build_server', method='DELETE',
        resource='/build-servers/{id}',
        param_map={'server_id': 'id', 'server_name': 'confirm'},
        permissions=('system_admin',), scope='global', classification=MUTATING,
        destructive=True, confirm_target='server_name', phase=3,
        handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_build_servers_list'),
    ToolSpec(
        name='portal_workflows_build_config_set', domain='workflows',
        title='Update the build configuration',
        description='Updates known build-system configuration parameters.',
        resource_type='build_config',
        input_schema=schema({'config': {'type': 'object', 'minProperties': 1,
                                        'description': 'Known build-config parameters.'}},
                            required=['config']),
        backend='buildConfig', handler_module='build_config',
        handler_function='update_build_config', method='PUT', resource='/build-config',
        param_map={'config': 'config'},
        permissions=('system_admin',), scope='global', classification=MUTATING,
        phase=3, handler_audits=True, handler_audits_denials=True,
        status_tool='portal_workflows_build_config_get'),
)

CATALOG: Tuple[ToolSpec, ...] = ADMIN + DEVICES + MODELS + DATA + WORKFLOWS


def main(argv: Sequence[str]) -> int:
    if list(argv) != ['--export-routes']:
        print('usage: python -m mcp_portal.catalog --export-routes',
              file=sys.stderr)
        return 2
    export()
    print(f'wrote {ROUTES_FILE} and {TOOLS_FILE}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
