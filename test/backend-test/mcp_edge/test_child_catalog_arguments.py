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
"""Edge catalog arguments against the routes (``mcp_edge/child/catalog.py``).

Spec: .kiro/specs/mcp-servers/ (design.md Testing Strategy row "Edge
classification honesty", ``test_child_catalog_arguments.py`` part, Property 2
"argument / route consistency", "no free-form RO arguments", "no path
arguments"; review-6 M5, review-7 M1/M2/N6, review-9 M2, review-10 M1,
review-11 N1/N5; tasks.md 11.13). Requirements 8.2, 8.5, 14.1, 14.2.

For each spec the route function is parsed (never imported) and its FastAPI
signature collected: path/query parameters, the fields of ``Depends()``-
injected models such as ``PageParams``, and the fields of a request-model
parameter. Every forwarded argument (a ``param_map`` target or a
pass-through name not in ``SERVER_CONSUMED_ARGS``) and every
``body_constants`` key must be among them, and every parameter or field the
route requires must be supplied by a required argument or a body constant.
A ``body_fields`` spec is checked against ``body_fields`` instead, on a body
annotated ``Dict``/``Any`` whose module carries each name as a literal.
"""
import ast
import dataclasses
import re

import pytest

import _edge_ast as ea
from mcp_edge.child import catalog
from mcp_edge.child.server import validation_failure

SKIP_ANNOTATIONS = ('Request', 'Session', 'BackgroundTasks', 'Response', 'UploadFile')
MULTIPART_SPECS = ('edge_static_image_pin',)
FREE_TEXT_PAIRS = {
    ('edge_textgen_generate', 'prompt'), ('edge_textgen_generate', 'image'),
    ('edge_results_list', 'text_note_filter'), ('edge_execution_node_image_get', 'port'),
}
BY_NAME = {s.name: s for s in catalog.CATALOG}


@dataclasses.dataclass
class RouteParam:
    name: str
    required: bool
    le: object = None
    source: str = 'param'           # 'param', 'depends:<Model>', 'model:<Model>'


@dataclasses.dataclass
class Signature:
    params: dict
    body_any: bool = False
    body_model: bool = False


def _call_name(node):
    return ea._call_name(node.func) if isinstance(node, ast.Call) else None


def _kw(call, name):
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _le(uni, module, node):
    """The `le=` bound of a `conint(...)`, `Field(...)` or `Query(...)`."""
    if isinstance(node, ast.Call):
        value = _kw(node, 'le')
        if isinstance(value, ast.Constant):
            return value.value
        if isinstance(value, ast.Name):
            const = uni.modules[module].globals.get(value.id)
            return const.value if isinstance(const, ast.Constant) else None
    return None


def _model_fields(uni, cls):
    module, name = cls
    node = uni.modules[module].classes[name]
    fields = {}
    for base in uni.class_bases(cls):
        fields.update(_model_fields(uni, base))
    for item in node.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            value = item.value
            required = value is None or (
                isinstance(value, ast.Call) and _call_name(value) in ('Field', 'pydantic.Field')
                and value.args and isinstance(value.args[0], ast.Constant)
                and value.args[0].value is Ellipsis)
            fields[item.target.id] = RouteParam(item.target.id, required,
                                                _le(uni, module, item.annotation)
                                                or _le(uni, module, value),
                                                f'model:{name}')
    return fields


def signature(uni, module, function) -> Signature:
    ref = ea.FuncRef(module, function)
    node = uni.node(ref)
    assert node is not None, f'{module}.{function}'
    positional = node.args.args
    defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
    pairs = list(zip(positional, defaults)) + list(zip(node.args.kwonlyargs,
                                                       node.args.kw_defaults))
    sig = Signature(params={})
    for arg, default in pairs:
        annotation = ea._call_name(arg.annotation) if isinstance(
            arg.annotation, (ast.Name, ast.Attribute)) else None
        sub = arg.annotation.value if isinstance(arg.annotation, ast.Subscript) else None
        if annotation in SKIP_ANNOTATIONS:
            continue
        is_any = annotation in ('Any', 'Dict', 'dict') or (
            sub is not None and ea._call_name(sub) in ('Dict', 'dict'))
        if is_any:
            if isinstance(default, ast.Call) and _call_name(default) == 'Depends':
                continue
            sig.body_any = True
            continue
        if isinstance(default, ast.Call) and _call_name(default) == 'Depends':
            target = default.args[0] if default.args else arg.annotation
            resolved = uni.resolve_expr(ref, target) if target is not None else None
            if resolved and resolved[0] == 'class':
                for field in _model_fields(uni, resolved[1]).values():
                    field.source = f'depends:{resolved[1][1]}'
                    sig.params[field.name] = field
            continue
        model = ea._annotation_class(arg.annotation)    # unwraps Optional[...]
        resolved = uni.resolve_expr(ref, model) if model is not None else None
        if resolved and resolved[0] == 'class' and not (
                isinstance(default, ast.Call) and _call_name(default) in ('Query', 'Path')):
            sig.body_model = True
            sig.params.update(_model_fields(uni, resolved[1]))
            continue
        name, required, le = arg.arg, default is None, None
        if isinstance(default, ast.Call) and _call_name(default) in ('Query', 'Path', 'Body'):
            alias = _kw(default, 'alias')
            if isinstance(alias, ast.Constant):
                name = alias.value
            has_default = _kw(default, 'default') is not None or (
                default.args and not (isinstance(default.args[0], ast.Constant)
                                      and default.args[0].value is Ellipsis))
            required = not has_default
            le = _le(uni, module, default)
        sig.params[name] = RouteParam(name, required, le)
    return sig


def forwarded(spec):
    """(argument, target) of every argument the child forwards."""
    out = []
    for prop in spec.input_schema.get('properties', {}):
        if prop == 'confirm' or prop == spec.text_tail_arg:
            continue
        if prop in catalog.SERVER_CONSUMED_ARGS and prop not in spec.param_map:
            continue
        out.append((prop, spec.param_map.get(prop, prop)))
    return out


def required_args(spec):
    required = set(spec.input_schema.get('required', ()))
    for group in spec.input_schema.get('oneOf', ()) + spec.input_schema.get('anyOf', ()):
        required |= set(group.get('required', ()))
    return required


@pytest.fixture(scope='module')
def uni():
    return ea.universe()


ROUTED = [s for s in catalog.CATALOG if s.name not in MULTIPART_SPECS]


@pytest.mark.parametrize('spec', ROUTED, ids=lambda s: s.name)
def test_arguments_name_route_parameters(uni, spec):
    sig = signature(uni, spec.handler_module, spec.handler_function)
    names = set(sig.params)
    if spec.body_fields is not None:
        assert sig.body_any, f'{spec.name}: body_fields on a request-model route'
        source = open(uni.modules[spec.handler_module].path, encoding='utf-8').read()
        for field in spec.body_fields:
            assert re.search(r'["\']' + re.escape(field) + r'["\']', source), field
        names |= set(spec.body_fields)
    for prop, target in forwarded(spec):
        assert target in names, f'{spec.name}: {prop} -> {target} not read by the route'
    optional = set(spec.input_schema.get('properties', {})) - required_args(spec)
    for key in spec.body_constants:
        assert key in names, f'{spec.name}: body constant {key}'
        # A constant may share its field with a mapped argument only as that
        # optional argument's default (the edge row of
        # edge_results_summary_reset: reset_time? -> resetTime, NOW_EPOCH_S);
        # dispatch lets the argument override the constant.
        overlapping = [a for a, t in spec.param_map.items() if t == key]
        assert all(a in optional for a in overlapping), (spec.name, key)
    supplied = {spec.param_map.get(a, a) for a in required_args(spec)} | set(spec.body_constants)
    for param in sig.params.values():
        if param.required:
            assert param.name in supplied, f'{spec.name}: the route requires {param.name}'


@pytest.mark.parametrize('spec', [s for s in ROUTED if 'limit' in s.param_map],
                         ids=lambda s: s.name)
def test_limit_maximum_is_the_route_bound(uni, spec):
    sig = signature(uni, spec.handler_module, spec.handler_function)
    bound = sig.params[spec.param_map['limit']].le
    if bound is not None:
        assert spec.input_schema['properties']['limit']['maximum'] == bound


def test_named_route_bounds(uni):
    page = signature(uni, 'endpoints.inference_result',
                     BY_NAME['edge_results_list'].handler_function).params['size']
    assert page.source == 'depends:PageParams' and page.le == 100
    assert BY_NAME['edge_results_list'].input_schema['properties']['limit']['maximum'] == 100
    images = BY_NAME['edge_workflow_images_list']
    source = open(uni.modules[images.handler_module].path, encoding='utf-8').read()
    assert 'MaxNumberOfResultsValidator(maxResults=maxResults)' in source
    assert 'max_results_value > 2' in source
    assert images.input_schema['properties']['limit']['maximum'] == 2
    const = uni.modules['workflow_engine.api'].globals['EXECUTIONS_LIMIT_MAX'].value
    assert BY_NAME['edge_registration_executions_list'].input_schema[
        'properties']['limit']['maximum'] == const == 50


def test_path_key_targets_only_for_stream_urls():
    for spec in catalog.CATALOG:
        props = spec.input_schema.get('properties', {})
        for name in props:
            assert not catalog.PATH_KEY_RE.search(name), (spec.name, name)
            if name in ('file_name', 'file_prefix', 'capture_prefix'):
                assert '/' not in props[name].get('pattern', '/') or \
                    re.fullmatch(props[name]['pattern'], 'a/b') is None
        for arg, target in spec.param_map.items():
            if catalog.PATH_KEY_RE.search(target):
                assert props[arg].get('pattern') == catalog.STREAM_URL_PATTERN, (spec.name, arg)


def ro_free_text_violations(specs):
    out = set()
    for spec in specs:
        if not spec.read_only:
            continue
        for name, prop in spec.input_schema.get('properties', {}).items():
            if prop.get('type') == 'string' and 'pattern' not in prop and 'enum' not in prop:
                out.add((spec.name, name))
    return out


def test_ro_string_arguments_are_patterned_or_listed():
    found = ro_free_text_violations(catalog.CATALOG)
    assert found == set(catalog.RO_FREE_TEXT_ARGS) == FREE_TEXT_PAIRS
    variant = dataclasses.replace(
        BY_NAME['edge_image_source_preview'],
        input_schema=catalog.schema({'image_source_id': catalog.identifier('x'),
                                     'processing_pipeline': {'type': 'string'}},
                                    required=['image_source_id']))
    assert ro_free_text_violations([variant]) - set(catalog.RO_FREE_TEXT_ARGS) == {
        ('edge_image_source_preview', 'processing_pipeline')}


def test_no_object_typed_ro_property_and_the_preview_schema():
    for spec in catalog.CATALOG:
        if spec.read_only:
            for name, prop in spec.input_schema.get('properties', {}).items():
                assert prop.get('type') != 'object', (spec.name, name)
    preview = BY_NAME['edge_image_source_preview']
    assert set(preview.input_schema['properties']) == {'image_source_id'}
    failure = validation_failure(preview, {
        'image_source_id': 'cam-1',
        'image_source_configuration': {'processingPipeline': 'videoconvert ! filesink location=/tmp/x'}})
    assert failure is not None and failure.category == 'invalid_input'


# ----------------------------------------------------------- fixed cases

def test_fixed_cases(uni):
    results = BY_NAME['edge_results_list']
    assert results.param_map.get('page') == 'page' and results.param_map.get('limit') == 'size'
    assert 'offset' not in results.input_schema['properties']
    executions = BY_NAME['edge_registration_executions_list']
    assert executions.param_map['limit'] == 'limit'
    route_limit = signature(uni, executions.handler_module,
                            executions.handler_function).params['limit']
    assert not route_limit.required      # default EXECUTIONS_LIMIT_DEFAULT
    assert uni.modules['workflow_engine.api'].globals['EXECUTIONS_LIMIT_DEFAULT'].value == 10
    assert 'resetTime' in BY_NAME['edge_results_summary_reset'].body_constants or \
        'resetTime' in BY_NAME['edge_results_summary_reset'].param_map.values()
    run = BY_NAME['edge_workflow_run']
    assert 'returnImageString' in set(run.body_constants) | set(run.param_map.values())
    images = BY_NAME['edge_workflow_images_list']
    assert {images.param_map['limit'], images.param_map['offset']} == {'maxResults',
                                                                       'startingPoint'}
    assert images.input_schema['properties']['limit']['default'] == 2
    assert 'filePrefix' in BY_NAME['edge_image_source_capture'].param_map.values()
    textgen = BY_NAME['edge_textgen_generate']
    assert textgen.body_fields == ('prompt', 'image', 'max_tokens')
    assert textgen.param_map['model_name'] == 'model_name'
    assert signature(uni, textgen.handler_module, textgen.handler_function).body_any
    for name, model in (('edge_image_source_add', 'AddImageSourceRequest'),
                        ('edge_image_source_update', 'EditImageSourceRequest')):
        spec = BY_NAME[name]
        assert spec.param_map['stream_url'] == 'location'
        assert spec.input_schema['properties']['stream_url']['pattern'] == catalog.STREAM_URL_PATTERN
        fields = _model_fields(uni, ('endpoints.image_source', model))
        assert 'location' in fields
    update = BY_NAME['edge_image_source_update']
    assert not {'type', 'camera_id', 'cameraId'} & set(update.input_schema['properties'])
    log = BY_NAME['edge_execution_log_get']
    assert log.text_tail_arg == 'tail_chars' and 'tail_chars' in catalog.SERVER_CONSUMED_ARGS
    assert 'tail_chars' not in signature(uni, log.handler_module, log.handler_function).params
    assert BY_NAME['edge_results_update'].param_map['inference_results'] == 'inferenceResults'
    assert set(_model_fields(uni, ('endpoints.inference_result',
                                   'UpdateInferenceResultsRequest'))) == {'inferenceResults'}
    workflow_update = BY_NAME['edge_workflow_update']
    assert {'name', 'image_sources'} <= set(workflow_update.input_schema['required']) or \
        {'name', 'imageSources'} <= set(workflow_update.input_schema['required'])
    node_image = BY_NAME['edge_execution_node_image_get']
    assert node_image.param_map['node_id'] == 'nodeId'
    assert 'port' in node_image.input_schema['required']
    for name in ('edge_model_start', 'edge_model_stop'):
        assert BY_NAME[name].param_map['model_name'] == 'modelName'


def test_static_image_pin_is_the_one_multipart_spec():
    pin = BY_NAME['edge_static_image_pin']
    assert pin.method == 'POST'
    assert [s.name for s in catalog.CATALOG if s.name in MULTIPART_SPECS] == ['edge_static_image_pin']


def test_path_parameters_are_mapped_and_patterned():
    for spec in catalog.CATALOG:
        segments = re.findall(r'\{([^{}+]+)\+?\}', spec.resource)
        targets = {t: a for a, t in spec.param_map.items()}
        for segment in segments:
            assert segment in targets, (spec.name, segment)
            prop = spec.input_schema['properties'][targets[segment]]
            pattern = prop.get('pattern')
            assert pattern, (spec.name, segment)
            for value in ('a/b', '.', '..'):
                assert re.fullmatch(pattern, value) is None, (spec.name, segment, value)
