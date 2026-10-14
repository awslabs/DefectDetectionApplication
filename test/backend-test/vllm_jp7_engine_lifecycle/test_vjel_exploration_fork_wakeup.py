# Copyright 2026 Amazon Web Services, Inc.
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
"""A-1 and Property 1: a signal a forked child handles must not run the
backend's own handler (spec vllm-jp7-engine-lifecycle, Defect A; bugfix.md
1.1, 1.2, 2.1, 2.2).

Each case runs ``wakeup_harness.py`` in a fresh interpreter. The control
(no hook) shows the bug where this interpreter has it: Python 3.10 and 3.11,
the JP6 and JP7 backend images. Where the control does not reproduce (the
build host's Python 3.14) the cases skip, saying so; run them in the
flask-app image or ``python:3.11-slim``.

On the unfixed tree ``utils.fork_signal_hygiene`` does not exist, so the
``app`` hook installs nothing and the cases FAIL with the counterexample.
"""
import ast
import signal
from functools import lru_cache

import pytest

from vllm_jp7_engine_lifecycle.harness_support import BACKEND_DIR, run_harness

APP_PY = BACKEND_DIR / "app.py"

#: Design Property 1's signal set.
SIGNALS = ("SIGTERM", "SIGINT", "SIGHUP", "SIGUSR1", "SIGCHLD")


@lru_cache(maxsize=None)
def control(signal_name):
    """The unhooked run: does this interpreter show the bug for the signal?"""
    return run_harness("none", signal_name)


def require_reproduction(signal_name):
    outcome = control(signal_name)
    if not outcome["parent_fired"]:
        pytest.skip(
            "the bug does not reproduce on Python {} for {} (the control's "
            "parent handler did not run); run this suite under Python 3.10 "
            "or 3.11 (the flask-app image)".format(outcome["python"], signal_name))
    return outcome


def test_a1_engine_core_sigterm_does_not_reach_the_backend():
    """A-1: the backend's SIGTERM handler (uvicorn's handle_exit) must not
    run when only the forked engine core gets SIGTERM."""
    require_reproduction("SIGTERM")
    outcome = run_harness("app", "SIGTERM")
    assert outcome["ready"], outcome
    assert not outcome["parent_fired"], (
        "COUNTEREXAMPLE (defect 1.1): SIGTERM sent only to a forked child ran "
        "the parent's asyncio SIGTERM handler (hook installed: {}) — on JP7 "
        "that is the backend's graceful shutdown and a container restart".format(
            outcome["hook_installed"]))
    # Preservation 3.4: the child's own handler still ran.
    assert outcome["child_handled"], outcome


@pytest.mark.parametrize("signal_name", SIGNALS)
def test_property1_no_forked_child_signal_reaches_the_backend(signal_name):
    """Property 1 over the signal set: the parent's handler does not run,
    the child's own handler does."""
    if not hasattr(signal, signal_name):
        pytest.skip("{} does not exist here".format(signal_name))
    require_reproduction(signal_name)
    outcome = run_harness("app", signal_name)
    assert not outcome["parent_fired"], (signal_name, outcome)
    assert outcome["child_handled"], (signal_name, outcome)


def test_app_installs_the_hook_first_in_main():
    """app.py's ``__main__`` block installs the hook before anything else
    (before ``TritonEdgeClient.get_instance()`` and before any fork)."""
    tree = ast.parse(APP_PY.read_text())
    main_blocks = [
        node for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    ]
    assert len(main_blocks) == 1
    body = main_blocks[0].body
    first, second = body[0], body[1]
    assert isinstance(first, ast.ImportFrom), ast.dump(first)
    assert first.module == "utils"
    assert [alias.name for alias in first.names] == ["fork_signal_hygiene"]
    assert isinstance(second, ast.Expr) and isinstance(second.value, ast.Call)
    call = second.value.func
    assert isinstance(call, ast.Attribute) and call.attr == "install"
    assert isinstance(call.value, ast.Name) and call.value.id == "fork_signal_hygiene"
    # Nothing in the block runs before it.
    source = ast.get_source_segment(APP_PY.read_text(), main_blocks[0])
    assert source.index("fork_signal_hygiene.install()") < source.index(
        "TritonEdgeClient.get_instance()")
