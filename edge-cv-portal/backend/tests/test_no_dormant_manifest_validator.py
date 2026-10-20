"""
Repository test: the unrouted manifest validator module stays removed
(portal-authorization-and-iam-scoping, tasks.md 4.1; design I4, D4).

The module read caller-supplied S3 URIs on the execution role, had no CDK
handler, import, route or test, and was deleted. This test fails if the file
returns, if any Lambda definition in infrastructure/lib names it as a handler,
or if a backend module imports or cites it.

Validates: Requirements 6.1, 6.2
"""
import ast
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_FUNCTIONS_DIR = os.path.join(_BACKEND, "functions")
_SHARED_LAYER = os.path.join(_BACKEND, "layers", "shared", "python")
_INFRA_LIB = os.path.abspath(os.path.join(_BACKEND, "..", "infrastructure", "lib"))

MODULE = "manifest_validator"
HANDLER_RE = re.compile(r"""\bhandler\s*:\s*['"`]([^'"`]+)['"`]""")


def _python_modules(*roots):
    for root in roots:
        for name in sorted(os.listdir(root)):
            if name.endswith(".py"):
                yield os.path.join(root, name)


def test_module_file_is_absent():
    assert not os.path.exists(os.path.join(_FUNCTIONS_DIR, f"{MODULE}.py"))
    assert not os.path.exists(os.path.join(_SHARED_LAYER, f"{MODULE}.py"))


def test_no_lambda_handler_names_the_module():
    handlers = []
    for name in sorted(os.listdir(_INFRA_LIB)):
        if not name.endswith(".ts") or name.endswith(".d.ts"):
            continue
        with open(os.path.join(_INFRA_LIB, name), encoding="utf-8") as fh:
            handlers += HANDLER_RE.findall(fh.read())
    assert len(handlers) > 20, "the handler scan found no Lambda definitions"
    assert not [handler for handler in handlers if handler.split(".")[0] == MODULE]
    assert "data_management.handler" in handlers


def test_no_backend_module_imports_or_cites_the_module():
    citing, importing = [], []
    for path in _python_modules(_FUNCTIONS_DIR, _SHARED_LAYER):
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        if MODULE in source:
            citing.append(os.path.basename(path))
        for node in ast.walk(ast.parse(source, filename=path)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] == MODULE for name in names):
                importing.append(os.path.basename(path))
    assert importing == []
    assert citing == []
