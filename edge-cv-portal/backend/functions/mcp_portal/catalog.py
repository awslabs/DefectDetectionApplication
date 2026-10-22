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
                properties[argument] = dict(properties[argument], type='string',
                                            pattern=PATH_PARAM_PATTERN)
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

CATALOG: Tuple[ToolSpec, ...] = ADMIN


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
