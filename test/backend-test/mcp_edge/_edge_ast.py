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
"""Static call graph of ``src/backend`` for the edge catalog honesty suites
(test-only; design Testing Strategy row "Edge classification honesty",
revision 10 review-9 N1, revision 11 review-10 N7, revision 12 review-11 N3).

``Universe`` parses every first-party module under ``src/backend`` (the
vendored packages excluded) without importing anything. ``reach(start)``
walks a route function's call graph transitively, resolving

* same-module calls, ``from m import f`` (absolute and relative) and
  imported module objects, dotted chains included;
* ``self.f`` / ``cls.f`` in the class and its first-party bases, ``C.f``,
  and constructor calls ``C(...)`` -> ``C.__init__``;
* methods of typed objects: a local name, a ``self.<attr>`` member or a
  module global bound to ``C(...)`` or to a call of a function annotated
  ``-> C`` / ``Optional[C]`` (or, unannotated, returning such a value), and
  such a call used directly as a receiver;
* function references used as values (callbacks, parameter defaults);

and it never enters ``utils/auth.py`` or ``local_auth/`` (authentication,
not the route). ``effects(ref)`` lists the effect-list calls of one function
as ``'<module>.<qualname>:<effect-list name>'``.
"""
import ast
import os
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Set, Tuple

BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', '..',
                                        'src', 'backend'))
EXCLUDED_DIRS = ('vendor', '__pycache__', 'alembic', 'tests', 'test')
NOT_ENTERED = ('utils.auth', 'local_auth')

OS_EFFECTS = ('system', 'remove', 'unlink', 'replace', 'rename', 'makedirs', 'mkdir',
              'rmdir', 'chmod')
SHUTIL_LOOKUPS = ('disk_usage', 'which', 'get_terminal_size')
TEMPFILE_CREATORS = ('mkstemp', 'mkdtemp', 'NamedTemporaryFile', 'TemporaryFile',
                     'SpooledTemporaryFile', 'TemporaryDirectory')
PATH_MUTATORS = ('unlink', 'rename', 'replace', 'touch', 'mkdir', 'rmdir', 'chmod')
PATH_DERIVED = ('parent', 'with_name', 'with_suffix', 'with_stem', 'resolve', 'absolute',
                'expanduser', 'joinpath')
PIL_CONSTRUCTORS = ('open', 'new', 'fromarray')
WRITE_MODE_CHARS = ('w', 'a', 'x', '+')


@dataclass(frozen=True)
class FuncRef:
    module: str
    qualname: str           # 'f' or 'Class.method'

    @property
    def key(self) -> str:
        return f'{self.module}.{self.qualname}'

    @property
    def cls(self) -> Optional[str]:
        return self.qualname.split('.')[0] if '.' in self.qualname else None


class Module:
    def __init__(self, name: str, path: str, is_package: bool):
        self.name, self.path, self.is_package = name, path, is_package
        with open(path, encoding='utf-8') as handle:
            self.tree = ast.parse(handle.read(), filename=path)
        self.functions: Dict[str, ast.AST] = {}
        self.classes: Dict[str, ast.ClassDef] = {}
        self.imports: Dict[str, Tuple[str, Optional[str]]] = {}   # name -> (module, attr)
        self.globals: Dict[str, ast.AST] = {}
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.classes[node.name] = node
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.functions[f'{node.name}.{item.name}'] = item
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.globals[target.id] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                    and node.value is not None:
                self.globals[node.target.id] = node.value
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.imports[alias.asname] = (alias.name, None)
                    else:
                        top = alias.name.split('.')[0]
                        self.imports.setdefault(top, (top, None))
            elif isinstance(node, ast.ImportFrom):
                base = self._absolute(node.module, node.level)
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = (base, alias.name)

    def _absolute(self, module: Optional[str], level: int) -> str:
        if level == 0:
            return module or ''
        parts = self.name.split('.')
        if not self.is_package:
            parts = parts[:-1]
        parts = parts[:len(parts) - (level - 1)] if level > 1 else parts
        return '.'.join(parts + ([module] if module else []))


def _modules() -> Dict[str, Module]:
    found = {}
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith('.')]
        for filename in files:
            if not filename.endswith('.py'):
                continue
            path = os.path.join(root, filename)
            rel = os.path.relpath(path, BACKEND)[:-3].split(os.sep)
            is_package = rel[-1] == '__init__'
            if is_package:
                rel = rel[:-1]
            if not rel:
                continue
            try:
                found['.'.join(rel)] = Module('.'.join(rel), path, is_package)
            except SyntaxError:
                continue
    return found


def _call_name(func: ast.AST) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        inner = _call_name(func.value)
        return f'{inner}.{func.attr}' if inner else None
    return None


def _annotation_class(node: Optional[ast.AST]) -> Optional[ast.AST]:
    """The class expression of an annotation `C`, `Optional[C]`, `'C'`."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return ast.parse(node.value, mode='eval').body
        except SyntaxError:
            return None
    if isinstance(node, ast.Subscript) and _call_name(node.value) in ('Optional', 'typing.Optional'):
        return node.slice
    if isinstance(node, (ast.Name, ast.Attribute)):
        return node
    return None


class Universe:
    def __init__(self):
        self.modules = _modules()

    # ----------------------------------------------------------- lookups
    def node(self, ref: FuncRef) -> Optional[ast.AST]:
        module = self.modules.get(ref.module)
        return module.functions.get(ref.qualname) if module else None

    def _resolve_attr(self, module_name: str, attr: str, depth: int = 0):
        """What `attr` of module `module_name` names: ('module', m),
        ('function', FuncRef) or ('class', (module, class)), or None."""
        if depth > 8:
            return None
        if f'{module_name}.{attr}' in self.modules:
            return ('module', f'{module_name}.{attr}')
        module = self.modules.get(module_name)
        if module is None:
            return None
        if attr in module.classes:
            return ('class', (module_name, attr))
        if attr in module.functions:
            return ('function', FuncRef(module_name, attr))
        if attr in module.globals:
            return ('global', (module_name, attr))
        if attr in module.imports:
            target, name = module.imports[attr]
            if name is None:
                return ('module', target) if target in self.modules else None
            return self._resolve_attr(target, name, depth + 1)
        return None

    def resolve_name(self, module_name: str, name: str):
        module = self.modules[module_name]
        if name in module.classes:
            return ('class', (module_name, name))
        if name in module.functions:
            return ('function', FuncRef(module_name, name))
        if name in module.imports:
            target, attr = module.imports[name]
            if attr is None:
                return ('module', target) if target in self.modules else ('external', target)
            resolved = self._resolve_attr(target, attr)
            return resolved if resolved else ('external', f'{target}.{attr}')
        return None

    def resolve_expr(self, ctx: FuncRef, node: ast.AST):
        """Resolve a Name/Attribute expression to a module, class or function."""
        if isinstance(node, ast.Name):
            return self.resolve_name(ctx.module, node.id)
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in ('self', 'cls') and ctx.cls:
                method = self.class_method((ctx.module, ctx.cls), node.attr)
                return ('function', method) if method else None
            base = self.resolve_expr(ctx, node.value)
            if base is None:
                return None
            kind, value = base
            if kind == 'module':
                return self._resolve_attr(value, node.attr)
            if kind == 'class':
                method = self.class_method(value, node.attr)
                return ('function', method) if method else None
            if kind == 'global':
                cls = self.global_class(value)
                method = self.class_method(cls, node.attr) if cls else None
                return ('function', method) if method else None
            if kind == 'external':
                return ('external', f'{value}.{node.attr}')
        return None

    def class_bases(self, cls: Tuple[str, str]) -> List[Tuple[str, str]]:
        module = self.modules[cls[0]]
        out = []
        for base in module.classes[cls[1]].bases:
            resolved = self.resolve_expr(FuncRef(cls[0], '_'), base)
            if resolved and resolved[0] == 'class':
                out.append(resolved[1])
        return out

    def class_method(self, cls: Tuple[str, str], attr: str, depth: int = 0) -> Optional[FuncRef]:
        if depth > 8 or cls[0] not in self.modules or cls[1] not in self.modules[cls[0]].classes:
            return None
        qual = f'{cls[1]}.{attr}'
        if qual in self.modules[cls[0]].functions:
            return FuncRef(cls[0], qual)
        for base in self.class_bases(cls):
            found = self.class_method(base, attr, depth + 1)
            if found:
                return found
        return None

    # ----------------------------------------------------- typed objects
    def class_of_value(self, ctx: FuncRef, value: Optional[ast.AST],
                       local: Dict[str, Tuple[str, str]], depth: int = 0):
        """The first-party class a value expression produces, or None."""
        if value is None or depth > 4:
            return None
        if isinstance(value, ast.Call):
            target = self.resolve_expr(ctx, value.func)
            if target and target[0] == 'class':
                return target[1]
            if target and target[0] == 'function':
                return self.returned_class(target[1], depth + 1)
            return None
        if isinstance(value, ast.Name) and value.id in local:
            return local[value.id]
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) \
                and value.value.id == 'self' and ctx.cls:
            return self.member_class((ctx.module, ctx.cls), value.attr, depth + 1)
        if isinstance(value, ast.Name):
            module = self.modules[ctx.module]
            if value.id in module.globals:
                return self.global_class((ctx.module, value.id), depth + 1)
            resolved = self.resolve_name(ctx.module, value.id)
            if resolved and resolved[0] == 'global':
                return self.global_class(resolved[1], depth + 1)
        if isinstance(value, ast.Attribute):
            resolved = self.resolve_expr(ctx, value)
            if resolved and resolved[0] == 'global':
                return self.global_class(resolved[1], depth + 1)
        return None

    def global_class(self, ref: Tuple[str, str], depth: int = 0):
        """The class of a module global bound to `C(...)` or a typed call."""
        module = self.modules.get(ref[0])
        if module is None or ref[1] not in module.globals or depth > 4:
            return None
        return self.class_of_value(FuncRef(ref[0], '_'), module.globals[ref[1]], {}, depth + 1)

    def returned_class(self, ref: FuncRef, depth: int = 0):
        node = self.node(ref)
        if node is None or depth > 4:
            return None
        annotated = _annotation_class(node.returns)
        if annotated is not None:
            resolved = self.resolve_expr(ref, annotated)
            if resolved and resolved[0] == 'class':
                return resolved[1]
            return None
        for sub in ast.walk(node):
            if isinstance(sub, ast.Return) and sub.value is not None:
                cls = self.class_of_value(ref, sub.value, {}, depth + 1)
                if cls:
                    return cls
        return None

    def member_class(self, cls: Tuple[str, str], attr: str, depth: int = 0):
        if cls[0] not in self.modules or cls[1] not in self.modules[cls[0]].classes:
            return None
        for item in ast.walk(self.modules[cls[0]].classes[cls[1]]):
            if isinstance(item, (ast.Assign, ast.AnnAssign)):
                targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                            and target.value.id == 'self' and target.attr == attr:
                        found = self.class_of_value(FuncRef(cls[0], f'{cls[1]}._'),
                                                    item.value, {}, depth + 1)
                        if found:
                            return found
        for base in self.class_bases(cls):
            found = self.member_class(base, attr, depth + 1)
            if found:
                return found
        return None

    def local_classes(self, ref: FuncRef) -> Dict[str, Tuple[str, str]]:
        node = self.node(ref)
        out: Dict[str, Tuple[str, str]] = {}
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                cls = self.class_of_value(ref, sub.value, out)
                for target in sub.targets:
                    if isinstance(target, ast.Name) and cls:
                        out[target.id] = cls
            elif isinstance(sub, (ast.With, ast.AsyncWith)):
                for item in sub.items:
                    cls = self.class_of_value(ref, item.context_expr, out)
                    if cls and isinstance(item.optional_vars, ast.Name):
                        out[item.optional_vars.id] = cls
        for arg in node.args.args + node.args.kwonlyargs:
            annotated = _annotation_class(arg.annotation)
            if annotated is not None:
                resolved = self.resolve_expr(ref, annotated)
                if resolved and resolved[0] == 'class':
                    out[arg.arg] = resolved[1]
        return out

    # ----------------------------------------------------------- edges
    def edges(self, ref: FuncRef) -> Set[FuncRef]:
        node = self.node(ref)
        if node is None:
            return set()
        local = self.local_classes(ref)
        out: Set[FuncRef] = set()

        def add(target):
            if target and target[0] == 'function':
                out.add(target[1])
            elif target and target[0] == 'class':
                init = self.class_method(target[1], '__init__')
                if init:
                    out.add(init)

        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                func = sub.func
                target = self.resolve_expr(ref, func)
                if target is None and isinstance(func, ast.Attribute):
                    cls = self.class_of_value(ref, func.value, local)
                    if cls:
                        method = self.class_method(cls, func.attr)
                        target = ('function', method) if method else None
                add(target)
                # Function references passed as values (callbacks).
                for value in list(sub.args) + [k.value for k in sub.keywords]:
                    if isinstance(value, (ast.Name, ast.Attribute)):
                        resolved = self.resolve_expr(ref, value)
                        if resolved and resolved[0] == 'function':
                            out.add(resolved[1])
        # Parameter defaults that are function references.
        for default in node.args.defaults + [d for d in node.args.kw_defaults if d]:
            if isinstance(default, (ast.Name, ast.Attribute)):
                resolved = self.resolve_expr(ref, default)
                if resolved and resolved[0] == 'function':
                    out.add(resolved[1])
        return {r for r in out if self.node(r) is not None and r != ref}

    def reach(self, start: FuncRef) -> List[FuncRef]:
        seen, stack = set(), [start]
        while stack:
            ref = stack.pop()
            if ref in seen or any(ref.module == m or ref.module.startswith(m + '.')
                                  for m in NOT_ENTERED):
                continue
            seen.add(ref)
            stack.extend(self.edges(ref))
        return sorted(seen, key=lambda r: r.key)

    # --------------------------------------------------------- effects
    def _external(self, ctx: FuncRef, func: ast.AST) -> Optional[str]:
        """'module.attr' of a call into a stdlib/third-party module."""
        if isinstance(func, ast.Name):
            resolved = self.resolve_name(ctx.module, func.id)
            return resolved[1] if resolved and resolved[0] == 'external' else None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            resolved = self.resolve_name(ctx.module, func.value.id)
            if resolved and resolved[0] == 'external':
                return f'{resolved[1]}.{func.attr}'
        return None

    def effects(self, ref: FuncRef) -> List[str]:
        node = self.node(ref)
        if node is None:
            return []
        found: List[str] = []
        pillow: Set[str] = set()
        paths: Set[str] = set()

        def is_pillow(value) -> bool:
            if isinstance(value, ast.Name):
                return value.id in pillow
            if isinstance(value, ast.Call):
                name = self._external(ref, value.func) or ''
                if name in tuple(f'PIL.Image.{c}' for c in PIL_CONSTRUCTORS) \
                        or name.startswith('PIL.ImageOps.'):
                    return True
                if isinstance(value.func, ast.Attribute):
                    return is_pillow(value.func.value)
            return False

        def is_path(value) -> bool:
            if isinstance(value, ast.Name):
                return value.id in paths
            if isinstance(value, ast.Call):
                name = self._external(ref, value.func) or ''
                if name in ('pathlib.Path', 'pathlib.PurePath', 'pathlib.PosixPath'):
                    return True
                if isinstance(value.func, ast.Attribute) and value.func.attr in PATH_DERIVED:
                    return is_path(value.func.value)
            if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div):
                return is_path(value.left)
            if isinstance(value, ast.Attribute) and value.attr in PATH_DERIVED:
                return is_path(value.value)
            return False

        for arg in node.args.args + node.args.kwonlyargs:
            annotated = _annotation_class(arg.annotation)
            if annotated is not None and _call_name(annotated) in ('Path', 'pathlib.Path'):
                paths.add(arg.arg)
        # Bind names in source order (assignments and with-targets).
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                for target in sub.targets:
                    if isinstance(target, ast.Name):
                        if is_pillow(sub.value):
                            pillow.add(target.id)
                        if is_path(sub.value):
                            paths.add(target.id)
            elif isinstance(sub, (ast.With, ast.AsyncWith)):
                for item in sub.items:
                    if isinstance(item.optional_vars, ast.Name) and is_pillow(item.context_expr):
                        pillow.add(item.optional_vars.id)

        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            name = self._external(ref, sub.func)
            effect = None
            if name:
                module, _, attr = name.rpartition('.')
                if module == 'subprocess':
                    effect = name
                elif module == 'os' and attr in OS_EFFECTS:
                    effect = name
                elif module == 'shutil' and attr not in SHUTIL_LOOKUPS:
                    effect = name
                elif module == 'tempfile' and attr in TEMPFILE_CREATORS:
                    effect = name
            elif isinstance(sub.func, ast.Name) and sub.func.id == 'open' \
                    and self.resolve_name(ref.module, 'open') is None:
                if _write_mode(sub, 1):
                    effect = 'open'
            if effect is None and isinstance(sub.func, ast.Attribute):
                attr, receiver = sub.func.attr, sub.func.value
                if attr == 'save' and is_pillow(receiver):
                    effect = 'Image.save'
                elif is_path(receiver):
                    if attr.startswith('write_'):
                        effect = f'Path.{attr}'
                    elif attr in PATH_MUTATORS:
                        effect = f'Path.{attr}'
                    elif attr == 'open' and _write_mode(sub, 0):
                        effect = 'Path.open'
            if effect:
                found.append(f'{ref.key}:{effect}')
        return sorted(set(found))

    def route_effects(self, module: str, function: str) -> Tuple[List[FuncRef], List[str]]:
        start = FuncRef(module, function)
        if self.node(start) is None:
            raise LookupError(f'{module}.{function} not found under src/backend')
        walked = self.reach(start)
        found = sorted({effect for ref in walked for effect in self.effects(ref)})
        return walked, found


def _write_mode(call: ast.Call, index: int) -> bool:
    mode = None
    if len(call.args) > index:
        mode = call.args[index]
    for keyword in call.keywords:
        if keyword.arg == 'mode':
            mode = keyword.value
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(c in mode.value for c in WRITE_MODE_CHARS)
    return False


_UNIVERSE: Optional[Universe] = None


def universe() -> Universe:
    global _UNIVERSE
    if _UNIVERSE is None:
        _UNIVERSE = Universe()
    return _UNIVERSE


def iter_route_functions() -> Iterator[Tuple[str, str]]:
    """(module, function) of every FastAPI route under endpoints/ and
    workflow_engine/api.py (used by the fixed cases)."""
    uni = universe()
    for name, module in uni.modules.items():
        if not (name.startswith('endpoints.') or name == 'workflow_engine.api'):
            continue
        for fname, node in module.functions.items():
            if '.' not in fname and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                                        and d.func.attr in ('get', 'post', 'put', 'delete', 'patch')
                                        for d in getattr(node, 'decorator_list', [])):
                yield name, fname
