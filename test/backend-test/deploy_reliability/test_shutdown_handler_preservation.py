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
"""Preservation property test (Task 2) for edge-deploy-reliability.

Property 5: Preservation — Clean shutdowns and crash recovery unchanged.

**Validates: Requirements 3.1, 3.6**

Observation-first (observed on UNFIXED code): a fast `shutdown_event` in
src/backend/app.py executes `cleanup_workflow_digital_inputs()` and then
`disconnect_all_cameras()` — exactly those two cleanup actions, in exactly
that order — and returns promptly. This is the golden behavior that must
KEEP holding after the Defect A fix (the bounded `asyncio.wait_for` cleanup
budget): for any cleanup whose duration fits comfortably within the budget
(NOT isBugCondition_A), the fixed handler must execute the same two calls in
the same order and complete just as promptly.

These tests PASS on the unfixed tree and must still PASS after the fix.

TESTABLE SEAM
-------------
Same AST-extraction seam as test_shutdown_handler_exploration.py in this
directory: importing src/backend/app.py on the host is infeasible (it pulls
the full backend graph that only exists inside the flask-app image), so the
REAL `shutdown_event` is extracted from app.py's source via AST (decorators
stripped) and executed in a controlled namespace where the two cleanup
callables are mocked. The handler body under test is the genuine app.py
code, byte-for-byte — unfixed today, fixed after task 3.1.

PROPERTY-BASED (Hypothesis)
---------------------------
The preservation guarantee is universal — "for any shutdown whose cleanup
completes within the cleanup budget" — so Hypothesis generates the two
cleanup durations (0..20ms each, far inside the 20s budget the fix
introduces and trivially inside the unfixed inline path) and the invariant
is asserted for each: same two calls, same order, prompt return.
"""
import ast
import asyncio
import os
import time
from unittest.mock import MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
APP_PY_PATH = os.path.join(REPO_ROOT, "src", "backend", "app.py")

#: A fast shutdown must return well within this bound (generous CI margin;
#: the generated cleanup work totals at most ~40ms).
PROMPT_RETURN_SECONDS = 2.0

#: Upper bound of each generated cleanup duration — comfortably inside the
#: fixed handler's 20s budget (NOT isBugCondition_A) and trivially fast for
#: the unfixed inline path.
MAX_CLEANUP_SECONDS = 0.02


def _extract_shutdown_handler(namespace):
    """Compile the REAL `shutdown_event` from app.py source into `namespace`
    (same seam as test_shutdown_handler_exploration.py): decorators stripped
    so no FastAPI app is needed, and module-level literal constants the
    handler references (e.g. the fixed SHUTDOWN_CLEANUP_BUDGET_SECONDS)
    copied into the namespace."""
    with open(APP_PY_PATH, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=APP_PY_PATH)

    handler_node = None
    module_constants = {}
    for node in tree.body:
        if (isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
                and node.name == "shutdown_event"):
            handler_node = node
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                module_constants[node.targets[0].id] = ast.literal_eval(
                    node.value)
            except ValueError:
                pass  # not a literal constant; the handler gets it mocked

    assert handler_node is not None, (
        "shutdown_event not found in src/backend/app.py — the handler moved; "
        "update this test's extraction seam")
    assert isinstance(handler_node, ast.AsyncFunctionDef), (
        "shutdown_event is no longer an async handler")

    referenced = {n.id for n in ast.walk(handler_node)
                  if isinstance(n, ast.Name)}
    for name in referenced & set(module_constants):
        namespace[name] = module_constants[name]

    handler_node.decorator_list = []
    module = ast.Module(body=[handler_node], type_ignores=[])
    exec(compile(module, APP_PY_PATH, "exec"), namespace)
    return namespace["shutdown_event"]


def _run_fast_shutdown(cleanup_seconds, disconnect_seconds):
    """Execute the extracted handler with the two cleanup callables mocked
    to take the given (small) durations; returns (calls, elapsed)."""
    calls = []

    def cleanup_workflow_digital_inputs():
        calls.append("cleanup_workflow_digital_inputs")
        if cleanup_seconds:
            time.sleep(cleanup_seconds)

    def disconnect_all_cameras():
        calls.append("disconnect_all_cameras")
        if disconnect_seconds:
            time.sleep(disconnect_seconds)

    namespace = {
        "asyncio": asyncio,
        "time": time,
        "logger": MagicMock(name="logger"),
        "cleanup_workflow_digital_inputs": cleanup_workflow_digital_inputs,
        "disconnect_all_cameras": disconnect_all_cameras,
    }
    handler = _extract_shutdown_handler(namespace)

    loop = asyncio.new_event_loop()
    try:
        started = time.monotonic()
        loop.run_until_complete(handler())
        elapsed = time.monotonic() - started
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
    return calls, elapsed


def _assert_golden_shutdown_behavior(calls, elapsed):
    assert calls == ["cleanup_workflow_digital_inputs",
                     "disconnect_all_cameras"], (
        "PRESERVATION REGRESSION (Property 5): a fast shutdown must execute "
        "exactly the digital-input cleanup followed by the camera "
        "disconnect, in that order; got {}".format(calls))
    assert elapsed < PROMPT_RETURN_SECONDS, (
        "PRESERVATION REGRESSION (Property 5): a fast shutdown (cleanup "
        "within the budget) returned only after {:.2f}s — clean shutdowns "
        "must stay prompt (Requirement 3.1)".format(elapsed))


def test_immediate_cleanup_runs_both_actions_in_order():
    """Golden example observed on unfixed code: zero-duration cleanup —
    the handler runs both cleanup actions, in order, and returns at once.

    Validates: Requirements 3.1, 3.6
    """
    calls, elapsed = _run_fast_shutdown(0.0, 0.0)
    _assert_golden_shutdown_behavior(calls, elapsed)


@settings(max_examples=15, deadline=None)
@given(
    cleanup_seconds=st.floats(min_value=0.0, max_value=MAX_CLEANUP_SECONDS,
                              allow_nan=False, allow_infinity=False),
    disconnect_seconds=st.floats(min_value=0.0, max_value=MAX_CLEANUP_SECONDS,
                                 allow_nan=False, allow_infinity=False),
)
def test_any_cleanup_within_budget_preserves_actions_order_and_promptness(
        cleanup_seconds, disconnect_seconds):
    """Property 5: for ANY cleanup duration within the budget (NOT
    isBugCondition_A), `shutdown_event` executes the same two cleanup
    actions in the same order as the original and returns promptly — on the
    unfixed inline handler and on the fixed bounded handler alike.

    Validates: Requirements 3.1, 3.6
    """
    calls, elapsed = _run_fast_shutdown(cleanup_seconds, disconnect_seconds)
    _assert_golden_shutdown_behavior(calls, elapsed)
