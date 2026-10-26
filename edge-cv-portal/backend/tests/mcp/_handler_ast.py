"""AST machinery shared by the portal catalog honesty suites (test-only).

Spec: .kiro/specs/mcp-servers/ (design.md Decision point 4 "Classification
rules"; Testing Strategy rows "Portal classification honesty",
"`handler_audits` honesty", "Catalog arguments <-> route parameters",
"Catalog routes are registered Portal_API methods"; tasks.md 5.6, 5.8-5.10).

One parse of every `functions/*.py` module and of every first-party module of
`layers/shared/python/` (the modules no `*.dist-info` distribution owns).
The call graph is the one Decision point 4 defines for the classification
walk: from a spec's `handler_function`, every function reached through
same-module calls (`f()`, `self.f()`/`cls.f()` in the class and its
first-party bases, `C.f()`, `C()` -> `C.__init__`) and calls into the other
`functions/*.py` modules (`module.f()` through an imported module object, a
name imported from such a module, and `getattr(<module>, '<literal>')` bound
to a name and then called). A call that resolves to a `functions/` function
is followed, never counted as a write. Shared-layer functions are not
entered: the derived `SHARED_LAYER_WRITERS` stand for them.
"""
import ast
import functools
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FUNCTIONS_DIR = os.path.join(BACKEND_DIR, 'functions')
LAYER_DIR = os.path.join(BACKEND_DIR, 'layers', 'shared', 'python')

# Decision point 4: the named write verbs and the ten verb prefixes.
WRITE_VERBS = frozenset((
    'put_item', 'update_item', 'delete_item', 'batch_write_item',
    'transact_write_items', 'batch_writer', 'put_object', 'delete_object',
    'delete_objects', 'copy_object', 'upload_file', 'upload_fileobj',
    'start_execution', 'invoke', 'send_message', 'publish', 'put_parameter'))
WRITE_PREFIX_RE = re.compile(
    r'^(create|update|delete|put|cancel|start|stop|terminate|reboot|reset)_')

AUDIT_WRITERS = ('log_audit_event', 'record_audit_event_strict')
# `log_audit_event` is neither a derived writer nor traversed (Decision point 4).
NOT_A_WRITER = 'log_audit_event'


# ------------------------------------------------------------------ modules

@dataclass(frozen=True)
class FuncRef:
    module: str
    qualname: str

    @property
    def key(self) -> str:
        return f'{self.module}.{self.qualname}'

    @property
    def short(self) -> str:
        return self.qualname.rsplit('.', 1)[-1]


@dataclass
class Module:
    name: str
    path: str
    source: str
    tree: ast.Module
    layer: bool
    functions: Dict[str, ast.AST] = field(default_factory=dict)
    classes: Dict[str, ast.ClassDef] = field(default_factory=dict)
    # local name -> ('module', module) | ('symbol', module, name)
    imports: Dict[str, tuple] = field(default_factory=dict)
    # module-level NAME -> str | tuple (of str or of tuples)
    constants: Dict[str, object] = field(default_factory=dict)
    assigns: Dict[str, ast.AST] = field(default_factory=dict)

    def __post_init__(self):
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.classes[node.name] = node
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.functions[f'{node.name}.{item.name}'] = item
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and node.value is not None:
                        self.assigns[target.id] = node.value
                        value = literal(node.value)
                        if value is not None:
                            self.constants[target.id] = value
        # Imports anywhere in the module (handlers import lazily inside
        # functions to avoid import cycles).
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split('.')[0]
                    self.imports.setdefault(local, ('module', alias.name if alias.asname else alias.name.split('.')[0]))
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    self.imports.setdefault(alias.asname or alias.name,
                                            ('symbol', node.module, alias.name))


def literal(node: ast.AST):
    """A str, or a tuple of literals (tuple/list/set displays), else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        items = [literal(elt) for elt in node.elts]
        if items and all(item is not None for item in items):
            return tuple(items)
    return None


def _dist_owned(directory: str) -> Set[str]:
    owned = set()
    for entry in os.listdir(directory):
        if not entry.endswith('.dist-info'):
            continue
        record = os.path.join(directory, entry, 'RECORD')
        if not os.path.exists(record):
            continue
        with open(record, encoding='utf-8') as handle:
            for line in handle:
                owned.add(line.split(',', 1)[0].split('/', 1)[0])
    return owned


@functools.lru_cache(maxsize=None)
def universe() -> 'Universe':
    return Universe()


class Universe:
    def __init__(self):
        self.modules: Dict[str, Module] = {}
        for entry in sorted(os.listdir(FUNCTIONS_DIR)):
            if entry.endswith('.py'):
                self._load(entry[:-3], os.path.join(FUNCTIONS_DIR, entry), layer=False)
        owned = _dist_owned(LAYER_DIR)
        for entry in sorted(os.listdir(LAYER_DIR)):
            if entry.endswith('.py') and entry not in owned and not entry.startswith('test_'):
                self._load(entry[:-3], os.path.join(LAYER_DIR, entry), layer=True)

    def _load(self, name: str, path: str, layer: bool) -> None:
        with open(path, encoding='utf-8') as handle:
            source = handle.read()
        self.modules[name] = Module(name, path, source, ast.parse(source), layer)

    # ------------------------------------------------------------ queries

    def module(self, name: str) -> Module:
        return self.modules[name]

    def is_functions_module(self, name: str) -> bool:
        module = self.modules.get(name)
        return module is not None and not module.layer

    def is_layer_module(self, name: str) -> bool:
        module = self.modules.get(name)
        return module is not None and module.layer

    def node(self, ref: FuncRef) -> ast.AST:
        return self.modules[ref.module].functions[ref.qualname]

    def exists(self, ref: FuncRef) -> bool:
        module = self.modules.get(ref.module)
        return module is not None and ref.qualname in module.functions

    def find_function(self, module: str, name: str) -> Optional[FuncRef]:
        """A module-level function, else the one method of that name."""
        mod = self.modules.get(module)
        if mod is None:
            return None
        if name in mod.functions:
            return FuncRef(module, name)
        methods = [q for q in mod.functions if q.endswith('.' + name)]
        return FuncRef(module, methods[0]) if len(methods) == 1 else None

    # ------------------------------------------------------ resolution

    def _class_method(self, module: str, cls: str, attr: str,
                      seen: Optional[Set[str]] = None) -> Optional[FuncRef]:
        mod = self.modules.get(module)
        if mod is None or cls not in mod.classes:
            return None
        if f'{cls}.{attr}' in mod.functions:
            return FuncRef(module, f'{cls}.{attr}')
        seen = seen or set()
        seen.add(f'{module}.{cls}')
        for base in mod.classes[cls].bases:
            target = self._resolve_class(module, base)
            if target and f'{target[0]}.{target[1]}' not in seen:
                found = self._class_method(target[0], target[1], attr, seen)
                if found:
                    return found
        return None

    def _resolve_class(self, module: str, node: ast.AST) -> Optional[Tuple[str, str]]:
        mod = self.modules[module]
        if isinstance(node, ast.Name):
            if node.id in mod.classes:
                return module, node.id
            imp = mod.imports.get(node.id)
            if imp and imp[0] == 'symbol' and imp[1] in self.modules \
                    and imp[2] in self.modules[imp[1]].classes:
                return imp[1], imp[2]
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            imp = mod.imports.get(node.value.id)
            if imp and imp[0] == 'module' and imp[1] in self.modules \
                    and node.attr in self.modules[imp[1]].classes:
                return imp[1], node.attr
        return None

    def resolve_name(self, module: str, name: str, *, layer_scope: bool) -> Optional[FuncRef]:
        """`name` as a function of the walk's scope (functions/ modules, or
        the layer modules for the writer derivation)."""
        mod = self.modules[module]
        if name in mod.functions:
            return FuncRef(module, name)
        if name in mod.classes:
            return self._class_method(module, name, '__init__')
        imp = mod.imports.get(name)
        if imp and imp[0] == 'symbol':
            target = self.modules.get(imp[1])
            if target is not None and target.layer == layer_scope:
                if imp[2] in target.functions:
                    return FuncRef(imp[1], imp[2])
                if imp[2] in target.classes:
                    return self._class_method(imp[1], imp[2], '__init__')
        return None

    def resolve_call(self, ctx: FuncRef, call: ast.Call, aliases: Dict[str, FuncRef],
                     *, layer_scope: bool = False) -> Optional[FuncRef]:
        func = call.func
        module = ctx.module
        mod = self.modules[module]
        if isinstance(func, ast.Name):
            if func.id in aliases:
                return aliases[func.id]
            return self.resolve_name(module, func.id, layer_scope=layer_scope)
        if isinstance(func, ast.Attribute):
            value = func.value
            if isinstance(value, ast.Name):
                imp = mod.imports.get(value.id)
                if imp and imp[0] == 'module' and value.id not in mod.functions:
                    target = self.modules.get(imp[1])
                    if target is not None and target.layer == layer_scope:
                        if func.attr in target.functions:
                            return FuncRef(imp[1], func.attr)
                        if func.attr in target.classes:
                            return self._class_method(imp[1], func.attr, '__init__')
                    return None
                if value.id in ('self', 'cls') and '.' in ctx.qualname:
                    cls = ctx.qualname.split('.', 1)[0]
                    return self._class_method(module, cls, func.attr)
                target = self._resolve_class(module, value)
                if target and self.modules[target[0]].layer == layer_scope:
                    return self._class_method(target[0], target[1], func.attr)
        return None

    def getattr_aliases(self, ctx: FuncRef) -> Dict[str, FuncRef]:
        """`name = getattr(<imported functions module>, '<literal>')`."""
        mod = self.modules[ctx.module]
        aliases = {}
        for node in ast.walk(self.node(ctx)):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) \
                    and isinstance(node.value, ast.Call) \
                    and isinstance(node.value.func, ast.Name) \
                    and node.value.func.id == 'getattr' and len(node.value.args) >= 2:
                target, attr = node.value.args[0], node.value.args[1]
                if isinstance(target, ast.Name) and isinstance(attr, ast.Constant) \
                        and isinstance(attr.value, str):
                    imp = mod.imports.get(target.id)
                    if imp and imp[0] == 'module' and self.is_functions_module(imp[1]) \
                            and attr.value in self.modules[imp[1]].functions:
                        aliases[node.targets[0].id] = FuncRef(imp[1], attr.value)
        return aliases

    def calls(self, ref: FuncRef) -> Iterator[ast.Call]:
        for node in ast.walk(self.node(ref)):
            if isinstance(node, ast.Call):
                yield node

    def edges(self, ref: FuncRef, *, layer_scope: bool = False) -> List[Tuple[ast.Call, FuncRef]]:
        aliases = self.getattr_aliases(ref) if not layer_scope else {}
        result = []
        for call in self.calls(ref):
            target = self.resolve_call(ref, call, aliases, layer_scope=layer_scope)
            if target is not None and self.exists(target):
                result.append((call, target))
        return result

    def reach(self, start: FuncRef, *, layer_scope: bool = False,
              skip: Iterable[str] = ()) -> List[FuncRef]:
        """`start` and every function it reaches (breadth-first, stable)."""
        skip = set(skip)
        order, seen, queue = [], {start}, [start]
        while queue:
            ref = queue.pop(0)
            order.append(ref)
            for _call, target in self.edges(ref, layer_scope=layer_scope):
                if target not in seen and target.short not in skip:
                    seen.add(target)
                    queue.append(target)
        return order

    # ------------------------------------------------------------ writes

    def _layer_object_names(self, ref: FuncRef) -> Set[str]:
        """Local names bound to a layer object: imported from a layer module,
        or assigned from a call of such a name (`dao = UserRolesDAO()`)."""
        mod = self.modules[ref.module]
        names = {local for local, imp in mod.imports.items()
                 if imp[0] == 'symbol' and self.is_layer_module(imp[1])}
        module_level = [ast.Assign(targets=[ast.Name(id=name)], value=value)
                        for name, value in mod.assigns.items()]
        for node in [*module_level, *ast.walk(self.node(ref))]:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                callee = node.value.func
                if isinstance(callee, ast.Name) and callee.id in names \
                        or isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name) \
                        and mod.imports.get(callee.value.id, ('',))[0] == 'module' \
                        and self.is_layer_module(mod.imports[callee.value.id][1]):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)
        return names

    def write_calls(self, ref: FuncRef, writer_names: Set[str], *,
                    layer_scope: bool = False) -> List['WriteCall']:
        """The write calls in one function's own body (Decision point 4)."""
        mod = self.modules[ref.module]
        aliases = self.getattr_aliases(ref) if not layer_scope else {}
        layer_objects = self._layer_object_names(ref) if not layer_scope else set()
        found = []
        for call in self.calls(ref):
            if self.resolve_call(ref, call, aliases, layer_scope=layer_scope) is not None:
                continue
            func = call.func
            if isinstance(func, ast.Attribute):
                attr, receiver = func.attr, func.value
                if attr == NOT_A_WRITER:
                    continue
                module_receiver = (isinstance(receiver, ast.Name)
                                   and mod.imports.get(receiver.id, ('',))[0] == 'module'
                                   and receiver.id not in mod.functions)
                layer_receiver = (
                    module_receiver and self.is_layer_module(mod.imports[receiver.id][1])
                    or isinstance(receiver, ast.Name) and receiver.id in layer_objects
                    or isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name)
                    and receiver.func.id in layer_objects)
                if (attr in WRITE_VERBS or WRITE_PREFIX_RE.match(attr)) and not module_receiver:
                    found.append(WriteCall(ref, attr, ast.unparse(receiver), call.lineno))
                elif attr in writer_names and (layer_receiver or layer_scope and not module_receiver):
                    found.append(WriteCall(ref, attr, ast.unparse(receiver), call.lineno))
            elif isinstance(func, ast.Name) and func.id in writer_names and func.id != NOT_A_WRITER:
                imp = mod.imports.get(func.id)
                if layer_scope or (imp and imp[0] == 'symbol' and self.is_layer_module(imp[1])):
                    found.append(WriteCall(ref, func.id, '', call.lineno))
        return found


@dataclass(frozen=True)
class WriteCall:
    function: FuncRef
    call: str
    receiver: str
    lineno: int

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.function.key, self.call, self.receiver)


# ------------------------------------------------- shared-layer writer set

@functools.lru_cache(maxsize=None)
def shared_layer_writers() -> Tuple[str, ...]:
    """Decision point 4 (revision 10, review-9 N2): every first-party function
    of `layers/shared/python/*.py` whose body, transitively within the layer,
    contains a write call, minus `log_audit_event` (neither a member nor
    traversed). Returned as `module.qualname` keys, sorted."""
    uni = universe()
    direct: Dict[FuncRef, bool] = {}
    refs = [FuncRef(name, qual) for name, mod in uni.modules.items() if mod.layer
            for qual in mod.functions if not qual.endswith(NOT_A_WRITER)]
    for ref in refs:
        direct[ref] = bool(uni.write_calls(ref, set(), layer_scope=True))
    writers = []
    for ref in refs:
        reached = uni.reach(ref, layer_scope=True, skip=(NOT_A_WRITER,))
        if any(direct.get(r) for r in reached):
            writers.append(ref.key)
    return tuple(sorted(writers))


def writer_short_names() -> Set[str]:
    return {key.rsplit('.', 1)[-1] for key in shared_layer_writers()}


def spec_writes(spec) -> List[WriteCall]:
    """Every write call on the call graph of a spec's `handler_function`."""
    uni = universe()
    start = uni.find_function(spec.handler_module, spec.handler_function)
    if start is None:
        raise LookupError(f'{spec.name}: {spec.handler_module}.{spec.handler_function} not found')
    names = writer_short_names()
    found = []
    for ref in uni.reach(start):
        found.extend(uni.write_calls(ref, names))
    return found


# ------------------------------------------------------------ audit calls

DENIAL_DECORATORS = ('rbac_check', 'require_permission', 'super_user_only',
                     'require_portal_admin')
DENIAL_DECORATOR_PREFIX = 'require_builds_'
SUCCESS_RESULTS = ('success', 'pending')
IGNORED_RESULTS = ('failure', 'failed', 'rejected')


@dataclass(frozen=True)
class AuditCall:
    function: FuncRef
    lineno: int
    result: object          # the literal, None when absent, '<expr>' otherwise
    kind: str               # 'success_path' | 'denial' | 'ignored'


def _callee_name(call: ast.Call) -> Optional[str]:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def builds_403(node: ast.AST) -> bool:
    """The function builds a 403 response itself: a call whose first
    argument is the literal 403, or a `statusCode: 403` dict."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and sub.args and isinstance(sub.args[0], ast.Constant) \
                and sub.args[0].value == 403:
            return True
        if isinstance(sub, ast.Dict):
            for key, value in zip(sub.keys, sub.values):
                if isinstance(key, ast.Constant) and key.value == 'statusCode' \
                        and isinstance(value, ast.Constant) and value.value == 403:
                    return True
    return False


def audit_calls(refs: Iterable[FuncRef]) -> List[AuditCall]:
    """`log_audit_event`/`record_audit_event_strict` calls classified by their
    `result` argument (5th positional or `result=`)."""
    uni = universe()
    found = []
    for ref in refs:
        node = uni.node(ref)
        denial_ok = builds_403(node)
        for call in uni.calls(ref):
            if _callee_name(call) not in AUDIT_WRITERS:
                continue
            arg = call.args[4] if len(call.args) >= 5 else next(
                (k.value for k in call.keywords if k.arg == 'result'), None)
            if arg is None:
                result, kind = None, 'success_path'
            elif not isinstance(arg, ast.Constant):
                result, kind = '<expr>', 'success_path'
            elif arg.value in SUCCESS_RESULTS:
                result, kind = arg.value, 'success_path'
            elif arg.value == 'denied' and denial_ok:
                result, kind = arg.value, 'denial'
            else:
                result, kind = arg.value, 'ignored'
            found.append(AuditCall(ref, call.lineno, result, kind))
    return found


def denial_decorated(node: ast.AST) -> bool:
    for decorator in getattr(node, 'decorator_list', ()):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = target.id if isinstance(target, ast.Name) else getattr(target, 'attr', '')
        if name in DENIAL_DECORATORS or name.startswith(DENIAL_DECORATOR_PREFIX):
            return True
    return False


# ---------------------------------------------------------- argument reads

QUERY, BODY, EVENT, PATH, RAWBODY = 'query', 'body', 'event', 'path', 'rawbody'
QUERY_PARAM_NAMES = ('query_params', 'query_parameters', 'params', 'query',
                     'qs', 'query_string_parameters', 'qsp', 'query_string')
BODY_PARAM_NAMES = ('body', 'request_body', 'payload', 'data', 'body_data',
                    'event_body', 'req_body', 'request_data')
EVENT_PARAM_NAMES = ('event',)
ERROR_BUILDERS = ('create_response', 'error_response', '_error', '_err',
                  'bad_request', '_bad_request', 'json_response', '_response')


@dataclass(frozen=True)
class Read:
    kind: str           # 'query' | 'body' | 'path'
    key: str
    function: FuncRef
    lineno: int


def _has_constant(node: ast.AST, value: str) -> bool:
    return any(isinstance(n, ast.Constant) and n.value == value for n in ast.walk(node))


class ReadCollector:
    """Keys a spec's route reads from the query string, the body and the path
    parameters, with string-constant folding (revision 10, review-9 N3), and
    the keys it requires (a `400` branch on `not <var>` for an unreassigned
    read, or `validate_required_fields`)."""

    def __init__(self, uni: 'Universe'):
        self.uni = uni
        self.required_any: List[frozenset] = []

    # ------------------------------------------------------- folding

    def _module_value_node(self, module: str, name: str) -> Optional[ast.AST]:
        mod = self.uni.modules.get(module)
        if mod is None:
            return None
        if name in mod.assigns:
            return mod.assigns[name]
        imp = mod.imports.get(name)
        if imp and imp[0] == 'symbol' and imp[1] in self.uni.modules:
            return self.uni.modules[imp[1]].assigns.get(imp[2])
        return None

    def value_node(self, ref: FuncRef, node: ast.AST, local: Dict[str, ast.AST]) -> Optional[ast.AST]:
        """The literal display a Name / module.CONST refers to."""
        if isinstance(node, ast.Name):
            if node.id in local:
                return local[node.id]
            return self._module_value_node(ref.module, node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            imp = self.uni.modules[ref.module].imports.get(node.value.id)
            if imp and imp[0] == 'module' and imp[1] in self.uni.modules:
                return self.uni.modules[imp[1]].assigns.get(node.attr)
        return None

    def fold(self, ref: FuncRef, node: ast.AST, local: Dict[str, ast.AST],
             loop: Dict[str, Set[str]], depth: int = 0) -> Set[str]:
        if depth > 4 or node is None:
            return set()
        if isinstance(node, ast.Constant):
            return {node.value} if isinstance(node.value, str) else set()
        if isinstance(node, ast.Name) and node.id in loop:
            return set(loop[node.id])
        target = self.value_node(ref, node, local)
        if target is not None and target is not node:
            return self.fold(ref, target, local, loop, depth + 1)
        return set()

    def sequence(self, ref: FuncRef, node: ast.AST, local: Dict[str, ast.AST],
                 depth: int = 0) -> List[ast.AST]:
        """The element nodes of a tuple/list/set display the expression names
        (or the keys / items of a dict display)."""
        if depth > 4 or node is None:
            return []
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return list(node.elts)
        if isinstance(node, ast.Dict):
            return [k for k in node.keys if k is not None]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ('items', 'keys') and not node.args:
            base = self.value_node(ref, node.func.value, local)
            if isinstance(base, ast.Dict):
                if node.func.attr == 'keys':
                    return [k for k in base.keys if k is not None]
                return [ast.Tuple(elts=[k, v]) for k, v in zip(base.keys, base.values)
                        if k is not None]
            return []
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ('tuple', 'list', 'set', 'sorted', 'frozenset') and node.args:
            return self.sequence(ref, node.args[0], local, depth + 1)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self.sequence(ref, node.left, local, depth + 1) + \
                self.sequence(ref, node.right, local, depth + 1)
        target = self.value_node(ref, node, local)
        if target is not None and target is not node:
            return self.sequence(ref, target, local, depth + 1)
        return []

    def _bind_loop(self, ref, target, iter_node, local, loop) -> None:
        elements = self.sequence(ref, iter_node, local)
        if not elements:
            return
        if isinstance(target, ast.Name):
            values = set()
            for element in elements:
                values |= self.fold(ref, element, local, loop)
            if values:
                loop[target.id] = values
        elif isinstance(target, ast.Tuple):
            for position, sub in enumerate(target.elts):
                if not isinstance(sub, ast.Name):
                    continue
                values = set()
                for element in elements:
                    if isinstance(element, (ast.Tuple, ast.List)) and len(element.elts) > position:
                        values |= self.fold(ref, element.elts[position], local, loop)
                if values:
                    loop[sub.id] = values

    # ------------------------------------------------------- sources

    def source_kind(self, node: ast.AST, env: Dict[str, str]) -> Optional[str]:
        if node is None:
            return None
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                kind = self.source_kind(value, env)
                if kind:
                    return kind
            return None
        if isinstance(node, ast.IfExp):
            return self.source_kind(node.body, env) or self.source_kind(node.orelse, env)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, 'attr', '')
            if name in ('loads',) and node.args:
                inner = node.args[0]
                if self.source_kind(inner, env) in (RAWBODY, BODY):
                    return BODY
                return None
            if name in ('dict', 'copy', 'deepcopy') and node.args:
                return self.source_kind(node.args[0], env)
            if isinstance(func, ast.Attribute) and func.attr == 'copy':
                return self.source_kind(func.value, env)
            if 'body' in name.lower() and any(self.source_kind(a, env) == EVENT for a in node.args):
                return BODY
            if ('query' in name.lower() or 'params' in name.lower()) \
                    and any(self.source_kind(a, env) == EVENT for a in node.args):
                return QUERY
            if isinstance(func, ast.Attribute) and func.attr == 'get' and node.args \
                    and self.source_kind(func.value, env) == EVENT \
                    and isinstance(node.args[0], ast.Constant):
                return self._event_key_kind(node.args[0].value)
            return None
        if isinstance(node, ast.Dict):
            # an event the route synthesizes for a callee from the request body
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == 'body' \
                        and isinstance(value, ast.Call) and _callee_name(value) == 'dumps' \
                        and value.args and self.source_kind(value.args[0], env) == BODY:
                    return EVENT
            return None
        if isinstance(node, ast.Subscript) and self.source_kind(node.value, env) == EVENT:
            key = node.slice
            if isinstance(key, ast.Constant):
                return self._event_key_kind(key.value)
        return None

    @staticmethod
    def _event_key_kind(key) -> Optional[str]:
        return {'queryStringParameters': QUERY, 'multiValueQueryStringParameters': QUERY,
                'pathParameters': PATH, 'body': RAWBODY}.get(key)

    def seed_env(self, ref: FuncRef) -> Dict[str, str]:
        node = self.uni.node(ref)
        env = {}
        for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            if arg.arg in EVENT_PARAM_NAMES:
                env[arg.arg] = EVENT
            elif arg.arg in QUERY_PARAM_NAMES:
                env[arg.arg] = QUERY
            elif arg.arg in BODY_PARAM_NAMES:
                env[arg.arg] = BODY
        return env

    def local_env(self, ref: FuncRef, env: Dict[str, str]) -> Dict[str, str]:
        """Propagate source kinds through assignments (two passes)."""
        env = dict(env)
        node = self.uni.node(ref)
        for _ in range(2):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1 \
                        and isinstance(sub.targets[0], ast.Name):
                    kind = self.source_kind(sub.value, env)
                    if kind and sub.targets[0].id not in env:
                        env[sub.targets[0].id] = kind
                elif isinstance(sub, ast.Assign) and len(sub.targets) == 1 \
                        and isinstance(sub.targets[0], ast.Tuple) and sub.targets[0].elts \
                        and isinstance(sub.targets[0].elts[0], ast.Name):
                    # `body, err = parse_body(event)`
                    kind = self.source_kind(sub.value, env)
                    if kind == BODY:
                        env.setdefault(sub.targets[0].elts[0].id, kind)
                elif isinstance(sub, ast.NamedExpr):
                    kind = self.source_kind(sub.value, env)
                    if kind:
                        env.setdefault(sub.target.id, kind)
        return env

    def local_literals(self, ref: FuncRef) -> Dict[str, ast.AST]:
        local = {}
        for sub in ast.walk(self.uni.node(ref)):
            if isinstance(sub, ast.Assign) and len(sub.targets) == 1 \
                    and isinstance(sub.targets[0], ast.Name) \
                    and isinstance(sub.value, (ast.Tuple, ast.List, ast.Set, ast.Dict, ast.Constant)):
                local.setdefault(sub.targets[0].id, sub.value)
        return local

    # ------------------------------------------------------- reads

    def function_reads(self, ref: FuncRef, env: Dict[str, str]) -> List[Read]:
        node = self.uni.node(ref)
        local = self.local_literals(ref)
        loop: Dict[str, Set[str]] = {}
        for sub in ast.walk(node):
            if isinstance(sub, (ast.For, ast.AsyncFor)):
                self._bind_loop(ref, sub.target, sub.iter, local, loop)
            elif isinstance(sub, ast.comprehension):
                self._bind_loop(ref, sub.target, sub.iter, local, loop)
        reads = []

        def add(kind, key_node, lineno):
            if kind in (QUERY, BODY, PATH):
                for key in self.fold(ref, key_node, local, loop):
                    reads.append(Read(kind, key, ref, lineno))

        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                    and sub.func.attr in ('get', 'pop', 'setdefault') and sub.args:
                add(self.source_kind(sub.func.value, env), sub.args[0], sub.lineno)
            elif isinstance(sub, ast.Subscript) and isinstance(sub.ctx, ast.Load):
                add(self.source_kind(sub.value, env), sub.slice, sub.lineno)
            elif isinstance(sub, ast.Compare) and len(sub.ops) == 1 \
                    and isinstance(sub.ops[0], (ast.In, ast.NotIn)):
                add(self.source_kind(sub.comparators[0], env), sub.left, sub.lineno)
            elif isinstance(sub, ast.Call) and _callee_name(sub) == 'validate_required_fields' \
                    and len(sub.args) >= 2:
                kind = self.source_kind(sub.args[0], env)
                for element in self.sequence(ref, sub.args[1], local):
                    add(kind, element, sub.lineno)
        return reads

    def required(self, ref: FuncRef, env: Dict[str, str]) -> Set[Tuple[str, str]]:
        """(kind, key) pairs the function requires."""
        node = self.uni.node(ref)
        local = self.local_literals(ref)
        loop: Dict[str, Set[str]] = {}
        result = set()
        # validate_required_fields(body, [...])
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and _callee_name(sub) == 'validate_required_fields' \
                    and len(sub.args) >= 2:
                kind = self.source_kind(sub.args[0], env)
                if kind not in (QUERY, BODY):
                    continue
                for element in self.sequence(ref, sub.args[1], local):
                    for key in self.fold(ref, element, local, loop):
                        result.add((kind, key))
        # var = <read>; ...; if not var [or ...]: return <400>
        read_vars: Dict[str, Tuple[str, str, int]] = {}
        assign_lines: Dict[str, List[int]] = {}
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                for target in targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name):
                            assign_lines.setdefault(name.id, []).append(sub.lineno)
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1 \
                        and isinstance(sub.targets[0], ast.Name):
                    read = self._read_of(ref, sub.value, env, local, loop)
                    if read:
                        read_vars.setdefault(sub.targets[0].id, (read[0], read[1], sub.lineno))
        # missing = [f for f in FIELDS if not body.get(f)]; if missing: return <400>
        missing_vars: Dict[str, Set[Tuple[str, str]]] = {}
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign) and len(sub.targets) == 1 \
                    and isinstance(sub.targets[0], ast.Name) \
                    and isinstance(sub.value, ast.ListComp) and len(sub.value.generators) == 1:
                gen = sub.value.generators[0]
                if not isinstance(gen.target, ast.Name) or len(gen.ifs) != 1:
                    continue
                cond = gen.ifs[0]
                if not (isinstance(cond, ast.UnaryOp) and isinstance(cond.op, ast.Not)
                        and isinstance(cond.operand, ast.Call)
                        and isinstance(cond.operand.func, ast.Attribute)
                        and cond.operand.func.attr == 'get' and cond.operand.args
                        and isinstance(cond.operand.args[0], ast.Name)
                        and cond.operand.args[0].id == gen.target.id):
                    continue
                kind = self.source_kind(cond.operand.func.value, env)
                if kind not in (QUERY, BODY):
                    continue
                keys = set()
                for element in self.sequence(ref, gen.iter, local):
                    keys |= {(kind, k) for k in self.fold(ref, element, local, loop)}
                missing_vars[sub.targets[0].id] = keys
        # value, rejection = validator(<read>); if rejection: return <400>,
        # where the validator rejects an absent value (`not isinstance(v, str)`
        # or `not v` on its first parameter, returning early)
        rejection_vars: Dict[str, Tuple[str, str]] = {}
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign) and len(sub.targets) == 1 \
                    and isinstance(sub.targets[0], ast.Tuple) and len(sub.targets[0].elts) == 2 \
                    and isinstance(sub.targets[0].elts[1], ast.Name) \
                    and isinstance(sub.value, ast.Call) and sub.value.args:
                read = self._read_of(ref, sub.value.args[0], env, local, loop)
                callee = self.uni.resolve_call(ref, sub.value, {})
                if read and callee and self.uni.exists(callee) and self._rejects_absent(callee):
                    rejection_vars[sub.targets[0].elts[1].id] = read
        for sub in ast.walk(node):
            if not isinstance(sub, ast.If) or not self._returns_400(sub.body):
                continue
            test = sub.test
            if isinstance(test, ast.Compare) and len(test.ops) == 1 \
                    and isinstance(test.ops[0], ast.IsNot) \
                    and isinstance(test.comparators[0], ast.Constant) \
                    and test.comparators[0].value is None:
                test = test.left      # `if rejection is not None:`
            if isinstance(test, ast.Name) and test.id in rejection_vars:
                result.add(rejection_vars[test.id])
        # `if var is None: return <default>` before the check: absence is handled
        none_returns: Dict[str, List[int]] = {}
        for sub in ast.walk(node):
            if isinstance(sub, ast.If) and isinstance(sub.test, ast.Compare) \
                    and isinstance(sub.test.left, ast.Name) and len(sub.test.ops) == 1 \
                    and isinstance(sub.test.ops[0], ast.Is) \
                    and isinstance(sub.test.comparators[0], ast.Constant) \
                    and sub.test.comparators[0].value is None \
                    and any(isinstance(s, ast.Return) for s in sub.body) \
                    and not self._returns_400(sub.body):
                none_returns.setdefault(sub.test.left.id, []).append(sub.lineno)
        for sub in ast.walk(node):
            if not isinstance(sub, ast.If) or not self._returns_400(sub.body):
                continue
            if isinstance(sub.test, ast.Name) and sub.test.id in missing_vars:
                result |= missing_vars[sub.test.id]
                continue
            group = isinstance(sub.test, ast.BoolOp) and isinstance(sub.test.op, ast.And)
            found = set()
            for name in self._negated_names(sub.test):
                if name not in read_vars:
                    continue
                kind, key, line = read_vars[name]
                between = [l for l in assign_lines.get(name, []) if line < l < sub.lineno]
                defaulted = [l for l in none_returns.get(name, []) if line < l < sub.lineno]
                if not between and not defaulted:
                    found.add((kind, key))
            if group:
                # `if not a and not b: 400` -> at least one of them
                if len(found) > 1:
                    self.required_any.append(frozenset(found))
            else:
                result |= found
        return result

    def _read_of(self, ref, value, env, local, loop) -> Optional[Tuple[str, str]]:
        # strip `.strip()` / `str(...)` / `x or ''` wrappers
        while True:
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) \
                    and value.func.attr in ('strip', 'lower', 'upper') and not value.args:
                value = value.func.value
            elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                    and value.func.id in ('str', 'int') and value.args:
                value = value.args[0]
            elif isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or) \
                    and all(isinstance(v, (ast.Constant, ast.Dict, ast.List, ast.Tuple))
                            for v in value.values[1:]):
                # `x or ''` keeps the read required; `x or <other source>`
                # is a fallback and makes it optional.
                value = value.values[0]
            else:
                break
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) \
                and value.func.attr == 'get' and value.args:
            kind = self.source_kind(value.func.value, env)
            keys = self.fold(ref, value.args[0], local, loop)
        elif isinstance(value, ast.Subscript):
            kind = self.source_kind(value.value, env)
            keys = self.fold(ref, value.slice, local, loop)
        else:
            return None
        if kind in (QUERY, BODY, PATH) and len(keys) == 1:
            return kind, next(iter(keys))
        return None

    def _rejects_absent(self, ref: FuncRef) -> bool:
        node = self.uni.node(ref)
        params = [a.arg for a in node.args.args]
        if not params:
            return False
        first = params[0]
        for stmt in node.body:
            if not isinstance(stmt, ast.If) or not any(isinstance(s, ast.Return) for s in stmt.body):
                continue
            test = stmt.test
            if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                operand = test.operand
                if isinstance(operand, ast.Name) and operand.id == first:
                    return True
                if isinstance(operand, ast.Call) and _callee_name(operand) == 'isinstance' \
                        and operand.args and isinstance(operand.args[0], ast.Name) \
                        and operand.args[0].id == first:
                    return True
        return False

    @staticmethod
    def _negated_names(test: ast.AST) -> List[str]:
        names = []
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) \
                and isinstance(test.operand, ast.Name):
            names.append(test.operand.id)
        elif isinstance(test, ast.BoolOp):
            for value in test.values:
                names.extend(ReadCollector._negated_names(value))
        return names

    @staticmethod
    def _returns_400(body: List[ast.stmt]) -> bool:
        for stmt in body:
            if isinstance(stmt, ast.Return) and stmt.value is not None:
                for sub in ast.walk(stmt.value):
                    if isinstance(sub, ast.Call) and sub.args \
                            and isinstance(sub.args[0], ast.Constant) and sub.args[0].value == 400:
                        return True
                    if isinstance(sub, ast.Dict):
                        for key, value in zip(sub.keys, sub.values):
                            if isinstance(key, ast.Constant) and key.value == 'statusCode' \
                                    and isinstance(value, ast.Constant) and value.value == 400:
                                return True
        return False

    # ------------------------------------------------------- per spec

    def _call_kinds(self, call: ast.Call, callee: FuncRef, env: Dict[str, str]) -> Dict[str, str]:
        node = self.uni.node(callee)
        params = [a.arg for a in [*node.args.posonlyargs, *node.args.args]]
        if params and params[0] in ('self', 'cls'):
            params = params[1:]
        kinds = {}
        for position, arg in enumerate(call.args):
            if position < len(params):
                kind = self.source_kind(arg, env)
                if kind:
                    kinds[params[position]] = kind
        for keyword in call.keywords:
            if keyword.arg:
                kind = self.source_kind(keyword.value, env)
                if kind:
                    kinds[keyword.arg] = kind
        return kinds

    def entry_seed(self, start: FuncRef) -> Dict[str, str]:
        """Parameter kinds of `start` from its call sites in its module."""
        seed = self.seed_env(start)
        mod = self.uni.modules[start.module]
        for qual in mod.functions:
            caller = FuncRef(start.module, qual)
            if caller == start:
                continue
            env = None
            for call in self.uni.calls(caller):
                target = self.uni.resolve_call(caller, call, {})
                if target != start:
                    continue
                if env is None:
                    env = self.local_env(caller, self.seed_env(caller))
                for name, kind in self._call_kinds(call, start, env).items():
                    seed.setdefault(name, kind)
        return seed

    def injected_keys(self, ref: FuncRef, env: Dict[str, str]) -> Set[str]:
        """Keys a function writes into a request-derived body before handing
        it on (`training_body['usecase_id'] = meta['usecase_id']`)."""
        local = self.local_literals(ref)
        keys = set()
        for sub in ast.walk(self.uni.node(ref)):
            if isinstance(sub, ast.Assign):
                for target in sub.targets:
                    if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) \
                            and env.get(target.value.id) == BODY:
                        keys |= self.fold(ref, target.slice, local, {})
        return keys

    def collect(self, start: FuncRef):
        """(reads, required, at-least-one groups, call graph) of a route."""
        envs: Dict[FuncRef, Dict[str, str]] = {start: self.entry_seed(start)}
        injected: Dict[FuncRef, Set[str]] = {}
        graph = self.uni.reach(start)
        for _ in range(3):
            changed = False
            for ref in graph:
                env = self.local_env(ref, {**self.seed_env(ref), **envs.get(ref, {})})
                aliases = self.uni.getattr_aliases(ref)
                for call in self.uni.calls(ref):
                    callee = self.uni.resolve_call(ref, call, aliases)
                    if callee is None or not self.uni.exists(callee):
                        continue
                    target_env = envs.setdefault(callee, {})
                    kinds = self._call_kinds(call, callee, env)
                    for name, kind in kinds.items():
                        if name not in target_env:
                            target_env[name] = kind
                            changed = True
                    if EVENT in kinds.values():
                        extra = self.injected_keys(ref, env) | injected.get(ref, set())
                        if not extra <= injected.get(callee, set()):
                            injected.setdefault(callee, set()).update(extra)
                            changed = True
            if not changed:
                break
        reads, required = [], set()
        self.required_any = []
        for ref in graph:
            env = self.local_env(ref, {**self.seed_env(ref), **envs.get(ref, {})})
            reads.extend(self.function_reads(ref, env))
            # A requirement counts only where the request itself reaches the
            # function: a callee handed a dict its caller built (an event
            # synthesized with injected values) reads, but does not require.
            required_env = env if ref == start else self.local_env(ref, envs.get(ref, {}))
            required |= {(kind, key) for kind, key in self.required(ref, required_env)
                         if key not in injected.get(ref, set())}
        return reads, required, list(self.required_any), graph


def _int_value(uni: 'Universe', ref: FuncRef, node: ast.AST) -> Optional[int]:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name):
        value = uni.modules[ref.module].assigns.get(node.id)
        if isinstance(value, ast.Constant) and isinstance(value.value, int):
            return value.value
    return None


def limit_cap(graph: Iterable[FuncRef]) -> Optional[int]:
    """The route's own page-size cap: `min(<limit expression>, N)` with an
    integer literal or module constant `N`."""
    uni = universe()
    caps = []
    for ref in graph:
        for call in uni.calls(ref):
            if _callee_name(call) != 'min' or len(call.args) < 2:
                continue
            mentions = any(
                isinstance(n, ast.Constant) and n.value == 'limit'
                or isinstance(n, ast.Name) and 'limit' in n.id.lower()
                for arg in call.args for n in ast.walk(arg))
            if not mentions:
                continue
            for arg in call.args:
                value = _int_value(uni, ref, arg)
                if value is not None and value > 1:
                    caps.append(value)
    return min(caps) if caps else None


USECASE_DECORATORS = ('rbac_check', 'require_permission')
GLOBAL_DECORATOR_PREFIX = 'require_builds_'


def decorator_usecase(node: ast.AST) -> Tuple[bool, bool]:
    """(reads usecase_id, falls back to 'global') for the route decorators."""
    reads = fallback = False
    for decorator in getattr(node, 'decorator_list', ()):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = target.id if isinstance(target, ast.Name) else getattr(target, 'attr', '')
        if name in USECASE_DECORATORS:
            reads = True
            if isinstance(decorator, ast.Call) and any(
                    k.arg == 'allow_global' and isinstance(k.value, ast.Constant)
                    and k.value.value is True for k in decorator.keywords):
                fallback = True
        elif name.startswith(GLOBAL_DECORATOR_PREFIX):
            reads = fallback = True
    return reads, fallback


# ------------------------------------------------------- route dispatch

ENTRY_POINTS = ('handler', 'lambda_handler')
_TEMPLATE_PARAM_RE = re.compile(r'\{[^{}]+\}')


@dataclass(frozen=True)
class Site:
    """A call of a route function from a module's entry point, with the
    `if` conditions on the path to it (`(test, taken)`: `taken` False for
    an `else` branch or for the code after an `if` whose body returns)."""
    target: FuncRef
    guards: Tuple[Tuple[ast.AST, bool, 'FuncRef'], ...]
    table_key: Optional[Tuple[str, str]] = None
    lineno: int = 0
    caller: Optional[FuncRef] = None


def _terminates(stmts: List[ast.stmt]) -> bool:
    return bool(stmts) and isinstance(stmts[-1], (ast.Return, ast.Raise))


def concrete(resource: str) -> str:
    return _TEMPLATE_PARAM_RE.sub('x0x', resource)


class DispatchWalker:
    """Every route-function call reachable from an entry point, through
    same-module dispatcher functions and forwarding shims into another
    module's entry point, with its guard conditions and route-table keys."""

    def __init__(self, uni: 'Universe'):
        self.uni = uni

    def entry(self, module: str) -> Optional[FuncRef]:
        for name in ENTRY_POINTS:
            if name in self.uni.modules[module].functions:
                return FuncRef(module, name)
        return None

    def sites(self, module: str) -> List[Site]:
        start = self.entry(module)
        if start is None:
            return []
        found: List[Site] = []
        self._walk_function(start, (), found, {start})
        return found

    def _walk_function(self, ref, guards, found, seen) -> None:
        node = self.uni.node(ref)
        self._walk(ref, node.body, tuple(guards), found, seen)

    def _walk(self, ref, stmts, guards, found, seen) -> None:
        guards = tuple(guards)
        for stmt in stmts:
            if isinstance(stmt, ast.If):
                self._calls_in(ref, stmt.test, guards, found, seen)
                self._walk(ref, stmt.body, guards + ((stmt.test, True, ref),), found, seen)
                self._walk(ref, stmt.orelse, guards + ((stmt.test, False, ref),), found, seen)
                if _terminates(stmt.body):
                    guards = guards + ((stmt.test, False, ref),)
            elif isinstance(stmt, ast.Try):
                self._walk(ref, stmt.body, guards, found, seen)
                for handler in stmt.handlers:
                    self._walk(ref, handler.body, guards, found, seen)
                self._walk(ref, stmt.orelse, guards, found, seen)
                self._walk(ref, stmt.finalbody, guards, found, seen)
            elif isinstance(stmt, (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)):
                self._walk(ref, stmt.body, guards, found, seen)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            else:
                self._calls_in(ref, stmt, guards, found, seen)

    def _calls_in(self, ref, node, guards, found, seen) -> None:
        mod = self.uni.modules[ref.module]
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                target = self.uni.resolve_call(ref, sub, self.uni.getattr_aliases(ref))
                if target is None or not self.uni.exists(target):
                    self._table_call(ref, sub, guards, found)
                    continue
                found.append(Site(target, guards, None, sub.lineno, ref))
                same_module_dispatcher = (target.module == ref.module
                                          and target not in seen and '.' not in target.qualname)
                shim = target.short in ENTRY_POINTS and target.module != ref.module
                if (same_module_dispatcher or shim) and target not in seen:
                    seen.add(target)
                    self._walk_function(target, guards, found, seen)
            elif isinstance(sub, ast.Name) and sub.id in mod.assigns \
                    and isinstance(mod.assigns[sub.id], ast.Dict):
                self._table(ref, mod.assigns[sub.id], guards, found)

    def _table_call(self, ref, call, guards, found) -> None:
        return None

    def _table(self, ref, table: ast.Dict, guards, found) -> None:
        mod = self.uni.modules[ref.module]
        for key, value in zip(table.keys, table.values):
            key_value = literal(key) if key is not None else None
            if not (isinstance(key_value, tuple) and len(key_value) == 2):
                continue
            for sub in ast.walk(value):
                name = None
                if isinstance(sub, ast.Name) and sub.id in mod.functions:
                    name = sub.id
                if name:
                    found.append(Site(FuncRef(ref.module, name), guards,
                                      (key_value[0], key_value[1]), getattr(key, 'lineno', 0),
                                      ref))


def _conjuncts(test: ast.AST, taken: bool) -> List[ast.AST]:
    """The positive literals a guard asserts on its branch."""
    if taken:
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
            return [c for v in test.values for c in _conjuncts(v, True)]
        return [test]
    # not (a or b) == not a and not b; `not x` false -> x
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _conjuncts(test.operand, True)
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        return [c for v in test.values for c in _conjuncts(v, False)]
    if isinstance(test, ast.Compare) and len(test.ops) == 1 \
            and isinstance(test.ops[0], (ast.NotEq, ast.NotIn)):
        op = ast.Eq() if isinstance(test.ops[0], ast.NotEq) else ast.In()
        return [ast.Compare(left=test.left, ops=[op], comparators=test.comparators)]
    return [ast.UnaryOp(op=ast.Not(), operand=test)]


def _str_values(uni, ref, node) -> List[str]:
    value = literal(node)
    if value is None and isinstance(node, ast.Name):
        value = uni.modules[ref.module].constants.get(node.id)
    if value is None and isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        imp = uni.modules[ref.module].imports.get(node.value.id)
        if imp and imp[0] == 'module' and imp[1] in uni.modules:
            value = uni.modules[imp[1]].constants.get(node.attr)
    if isinstance(value, str):
        return [value]
    if isinstance(value, tuple):
        return [v for v in value if isinstance(v, str)]
    return []


METHODS = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS')


def guard_facts(uni, guards) -> Tuple[Set[str], List[Tuple[str, str]], List[Tuple[str, bool]]]:
    """(methods asserted, path assertions (op, constant), path-parameter
    presence assertions (name, present)) on a branch."""
    methods: Optional[Set[str]] = None
    paths: List[Tuple[str, str]] = []
    params: List[Tuple[str, bool]] = []
    for test, taken, ref in guards:
        for lit in _conjuncts(test, taken):
            negated = isinstance(lit, ast.UnaryOp) and isinstance(lit.op, ast.Not)
            core = lit.operand if negated else lit
            if isinstance(core, ast.Compare) and len(core.ops) == 1 and not negated:
                op = core.ops[0]
                left = _str_values(uni, ref, core.left)
                right = _str_values(uni, ref, core.comparators[0])
                consts = left + right
                if isinstance(op, ast.Eq):
                    if consts and all(c in METHODS for c in consts):
                        found_methods = set(consts)
                        methods = found_methods if methods is None else methods & found_methods
                    else:
                        paths.extend(('eq', c) for c in consts if '/' in c)
                elif isinstance(op, ast.In):
                    if right and all(c in METHODS for c in right):
                        methods = set(right) if methods is None else methods & set(right)
                    elif left:
                        paths.extend(('in', c) for c in left)
                    else:
                        paths.extend(('eq', c) for c in right if '/' in c)
                continue
            if isinstance(core, ast.Call) and isinstance(core.func, ast.Attribute):
                attr = core.func.attr
                if attr in ('endswith', 'startswith') and core.args and not negated:
                    for c in _str_values(uni, ref, core.args[0]):
                        paths.append((attr, c))
                    continue
                if attr in ('match', 'fullmatch', 'search') and not negated:
                    pattern = None
                    if isinstance(core.func.value, ast.Name) and core.func.value.id == 're' and core.args:
                        pattern = _str_values(uni, ref, core.args[0])
                    elif isinstance(core.func.value, ast.Name):
                        compiled = uni.modules[ref.module].assigns.get(core.func.value.id)
                        if isinstance(compiled, ast.Call) and _callee_name(compiled) == 'compile' \
                                and compiled.args:
                            pattern = _str_values(uni, ref, compiled.args[0])
                    for p in pattern or ():
                        paths.append(('re', p))
                    continue
                if attr == 'get' and core.args and isinstance(core.args[0], ast.Constant) \
                        and _mentions_path_parameters(core.func.value):
                    params.append((core.args[0].value, not negated))
                    continue
            if isinstance(core, ast.Name):
                key = _path_parameter_name(uni, ref, core.id)
                if key:
                    params.append((key, not negated))
            elif isinstance(core, ast.Compare) and negated and len(core.ops) == 1:
                continue
    return (methods if methods is not None else set()), paths, params


def _path_parameter_name(uni, ref: FuncRef, name: str) -> Optional[str]:
    """`name = <pathParameters>.get('k')` / `[...]['k']` in the function."""
    for sub in ast.walk(uni.node(ref)):
        if isinstance(sub, ast.Assign) and len(sub.targets) == 1 \
                and isinstance(sub.targets[0], ast.Name) and sub.targets[0].id == name:
            value = sub.value
            if isinstance(value, ast.BoolOp):
                value = value.values[0]
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) \
                    and value.func.attr == 'get' and value.args \
                    and isinstance(value.args[0], ast.Constant) \
                    and _mentions_path_parameters(value.func.value):
                return value.args[0].value
            if isinstance(value, ast.Subscript) and isinstance(value.slice, ast.Constant) \
                    and _mentions_path_parameters(value.value):
                return value.slice.value
    return None


def _mentions_path_parameters(node: ast.AST) -> bool:
    return _has_constant(node, 'pathParameters') or any(
        isinstance(n, ast.Name) and 'path_param' in n.id.lower() for n in ast.walk(node))


def path_matches(op: str, constant: str, resource: str) -> bool:
    filled = concrete(resource)
    if op == 'eq':
        return constant == resource or constant == filled
    if op == 'endswith':
        return resource.endswith(constant) or filled.endswith(constant)
    if op == 'startswith':
        return resource.startswith(constant) or filled.startswith(constant)
    if op == 'in':
        return constant in resource or constant in filled
    if op == 're':
        try:
            return re.search(constant, filled) is not None or re.search(constant, resource) is not None
        except re.error:
            return False
    return False


def _param_in_template(name: str, resource: str) -> bool:
    return '{' + name + '}' in resource or '{' + name + '+}' in resource


def site_facts(uni, site: Site, method: str, resource: str):
    """(method matches, resource matches, resource contradicted, has facts)."""
    methods, paths, params = guard_facts(uni, site.guards)
    path_good = [p for p in paths if path_matches(p[0], p[1], resource)]
    path_bad = [p for p in paths if p not in path_good]
    param_good = [p for p in params if _param_in_template(p[0], resource) == p[1]]
    param_bad = [p for p in params if p not in param_good]
    contradicted = bool(path_bad or param_bad)
    return (method in methods, bool(path_good or param_good) and not contradicted,
            contradicted, bool(paths or params))


def _discriminator_key(node: ast.AST) -> Optional[str]:
    """`<request dict>.get('k')` (or compared with a constant) outside the
    path parameters: a query-/body-key discriminator."""
    if isinstance(node, ast.Compare) and len(node.ops) == 1 \
            and isinstance(node.ops[0], ast.Eq):
        node = node.left
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == 'get' and node.args \
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) \
            and not _mentions_path_parameters(node.func.value) \
            and not _has_constant(node.func.value, 'headers'):
        return node.args[0].value
    return None


def site_discriminators(site: Site) -> Tuple[List[Set[str]], Set[str]]:
    """(positive key groups — one key of each must be sent, keys the
    branch requires absent)."""
    positive, absent = [], set()
    for test, taken, _ref in site.guards:
        for lit in _conjuncts(test, taken):
            negated = isinstance(lit, ast.UnaryOp) and isinstance(lit.op, ast.Not)
            core = lit.operand if negated else lit
            values = core.values if isinstance(core, ast.BoolOp) and isinstance(core.op, ast.Or) \
                else [core]
            keys = {k for k in (_discriminator_key(v) for v in values) if k}
            if not keys:
                continue
            if negated:
                absent |= keys
            else:
                positive.append(keys)
    return positive, absent


def dispatch_problem(uni, method: str, resource: str, target: FuncRef,
                     sites: List[Site], sends: Set[str] = frozenset(),
                     requires=lambda keys: False) -> Optional[str]:
    """None when an entry-point branch for `(method, resource)` reaches
    `target` (see `_dispatch_site_problem`), else the reason."""
    candidates = [s for s in sites if s.target == target]
    if not candidates:
        return f'{target.key} is not reached from the entry point'
    reasons = []
    for site in candidates:
        reason = _dispatch_site_problem(uni, method, resource, site, sites, sends, requires)
        if reason is None:
            return None
        reasons.append(reason)
    return f'{target.key} is not on a {method} {resource} branch: ' + '; '.join(reasons)


def _dispatch_site_problem(uni, method, resource, site, sites, sends, requires):
    """A route-table key, or a guard that compares the request method with
    `method` and matches `resource` (a path / resource comparison or a
    path-parameter presence test). A branch with a method guard and no path
    guard is the method's fallthrough; it counts when every other branch of
    that method in the same dispatcher is guarded to another resource or
    selected by a request key the spec does not send. Where a request-key
    discriminator selects the branch, the spec's required arguments must
    select it, and it must not send a key the branch requires absent."""
    if site.table_key is not None:
        if site.table_key == (method, resource):
            return None
        return f'route-table key {site.table_key}'
    positive, absent = site_discriminators(site)
    for keys in positive:
        if not requires(keys):
            return (f'line {site.lineno}: selected by one of {sorted(keys)}, which the '
                    'spec does not require')
    if absent & set(sends):
        return f'line {site.lineno}: the spec sends {sorted(absent & set(sends))}, ' \
               'which select another branch'
    method_ok, resource_ok, _contradicted, has_facts = site_facts(uni, site, method, resource)
    if method_ok and resource_ok:
        return None
    if method_ok and not has_facts:
        rivals = [s for s in sites if s.target != site.target and s.table_key is None
                  and s.caller == site.caller and site_facts(uni, s, method, resource)[0]]
        unseparated = []
        for rival in rivals:
            if site_facts(uni, rival, method, resource)[2]:
                continue
            rival_positive, _ = site_discriminators(rival)
            if rival_positive and all(not (keys & set(sends)) for keys in rival_positive):
                continue
            unseparated.append(rival.target.key)
        if not unseparated:
            return None
        return f'line {site.lineno}: {method} fallthrough shared with {sorted(set(unseparated))}'
    return f'line {site.lineno}: method guard {method_ok}, resource guard {resource_ok}'


def spec_graph(spec) -> List[FuncRef]:
    uni = universe()
    start = uni.find_function(spec.handler_module, spec.handler_function)
    if start is None:
        raise LookupError(f'{spec.name}: {spec.handler_module}.{spec.handler_function} not found')
    return uni.reach(start)
