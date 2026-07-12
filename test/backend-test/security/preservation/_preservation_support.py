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
"""Shared helpers for the security preservation baseline tests (Task 2 of
security-injection-deserialization-fixes).

These tests implement **Property 2: Preservation — F(X) = F'(X) for every
legitimate (non-bug-condition) input** (bugfix.md Req 3.1–3.7, design.md
"Preservation Checking"). Methodology: observation-first — capture the baseline
behavior on the UNFIXED tree (task 2, PASS now) and re-run the SAME files against
the FIXED tree (task 13) to prove no legitimate behavior changed.

``load_module_from_path`` loads a single source file as a module WITHOUT pulling
in the heavy backend package graph, optionally injecting stub modules for its
imports, so the REAL target function is exercised in isolation. The pure-
validation sites (#1 Snapshotter, #2 deploy.py, #3 run_command callers, #8
model_converter) are loaded this way, so task 13 genuinely re-exercises the
fixed source through these same tests.
"""
import importlib.util
import os
import sys

# preservation/ -> security/ -> backend-test/ -> test/ -> repo root (4 up).
REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..")
)


def load_module_from_path(mod_name, rel_path, injected_modules=None):
    """Load ``REPO_ROOT/rel_path`` as a standalone module named ``mod_name``.

    ``injected_modules`` is a mapping of module-name -> stub module registered in
    ``sys.modules`` for the duration of the import (so heavy / unavailable
    dependencies are stubbed). The real ``sys.modules`` entries are restored
    afterward; functions in the loaded module keep the references they bound at
    import time.
    """
    injected = injected_modules or {}
    saved = {name: sys.modules.get(name) for name in injected}
    sys.modules.update(injected)
    try:
        path = os.path.join(REPO_ROOT, rel_path)
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod  # register before exec for self-references
        spec.loader.exec_module(mod)
        return mod
    finally:
        for name, prev in saved.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


def read_repo_file(rel_path, binary=False):
    """Read a repo file by path relative to REPO_ROOT."""
    full = os.path.join(REPO_ROOT, rel_path)
    mode = "rb" if binary else "r"
    kwargs = {} if binary else {"encoding": "utf-8", "errors": "replace"}
    with open(full, mode, **kwargs) as fh:
        return fh.read()
