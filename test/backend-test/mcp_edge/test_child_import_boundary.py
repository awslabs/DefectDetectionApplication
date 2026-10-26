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
"""The import boundary between the Backend_API process and the MCP child.

Spec: .kiro/specs/mcp-servers/ (design.md §5, Testing Strategy row "Import
boundary"; tasks.md 11.11). Requirements 10.8, 12.5.

* Every ``mcp_edge/child`` module imports in a fresh interpreter in which
  ``utils``, ``local_auth`` and ``dao`` raise on import: the child can never
  reach the credential cache, the session secret or the authorization
  settings.
* ``app.py``, the backend-side ``mcp_edge`` modules and the ``/mcp`` proxy
  route never import ``mcp`` (or ``mcp_types``): the SDK lives only in the
  child's virtualenv.
"""
import ast
import os
import subprocess
import sys

import pytest

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..',
                                       'src', 'backend'))
CHILD = os.path.join(BACKEND, 'mcp_edge', 'child')
CHILD_MODULES = sorted(
    'mcp_edge.child' + ('' if name == '__init__.py' else '.' + name[:-3])
    for name in os.listdir(CHILD) if name.endswith('.py'))
BACKEND_SIDE = ['app.py', 'endpoints/mcp_proxy.py', 'mcp_edge/__init__.py',
                'mcp_edge/config.py', 'mcp_edge/supervisor.py', 'mcp_edge/log_relay.py',
                'mcp_edge/proxy.py', 'mcp_edge/status.py']
FORBIDDEN_FOR_CHILD = ('utils', 'local_auth', 'dao')

BLOCKER = '''
import importlib.abc, sys
class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in {forbidden!r}:
            raise ImportError('blocked for the MCP child: ' + name)
        return None
sys.meta_path.insert(0, Blocker())
import importlib
importlib.import_module({module!r})
print('ok')
'''


def test_child_module_list():
    assert {'mcp_edge.child', 'mcp_edge.child.server', 'mcp_edge.child.dispatch',
            'mcp_edge.child.catalog', 'mcp_edge.child.shaping', 'mcp_edge.child.authz',
            'mcp_edge.child.principal', 'mcp_edge.child.logging',
            'mcp_edge.child.__main__'} <= set(CHILD_MODULES)


@pytest.mark.parametrize('module', CHILD_MODULES)
def test_child_module_imports_without_backend_packages(module):
    code = BLOCKER.format(forbidden=FORBIDDEN_FOR_CHILD, module=module)
    env = dict(os.environ, PYTHONPATH=BACKEND, PYTHONDONTWRITEBYTECODE='1')
    completed = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True,
                               text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip() == 'ok'


def _imported_roots(path: str) -> set:
    tree = ast.parse(open(path, encoding='utf-8').read(), filename=path)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split('.')[0])
        elif isinstance(node, ast.Call) and getattr(node.func, 'attr', None) == \
                'import_module' and node.args and isinstance(node.args[0], ast.Constant):
            roots.add(str(node.args[0].value).split('.')[0])
    return roots


@pytest.mark.parametrize('relative', BACKEND_SIDE)
def test_backend_side_never_imports_the_sdk(relative):
    roots = _imported_roots(os.path.join(BACKEND, relative))
    assert not roots & {'mcp', 'mcp_types'}, relative


def test_backend_side_imports_no_child_module():
    for relative in BACKEND_SIDE:
        tree = ast.parse(open(os.path.join(BACKEND, relative), encoding='utf-8').read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not [n for n in names if n.startswith('mcp_edge.child')], relative
