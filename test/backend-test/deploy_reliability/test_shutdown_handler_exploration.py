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
"""Bug-condition exploration test (Task 1, case 5) for edge-deploy-reliability.

Property 1: Bug Condition — Backend survives or recovers from deployment
restarts (Defect A, `isBugCondition_A`, behavioral leg): the unfixed
`shutdown_event` in src/backend/app.py runs its cleanup
(`cleanup_workflow_digital_inputs()` then `disconnect_all_cameras()`) INLINE,
so a slow `terminate_digital_input_task` holds the SIGTERM handler past
Docker's grace window and the container is SIGKILLed mid-cleanup (the
incident's exit 137 at t+13s).

**This test asserts the FIXED (post-fix) behavior — the handler returns
within a bounded cleanup budget even when the cleanup work blocks far past
it — so it is EXPECTED TO FAIL on the UNFIXED tree.** The failure is the
counterexample: with cleanup blocked, the handler only returns when the
blocking work finishes, i.e. it is unbounded.

The SAME test is re-run in task 3.5 against the fixed handler, where it must
PASS (the `asyncio.wait_for` budget abandons the blocked cleanup and returns).

TESTABLE SEAM (documented limitation)
-------------------------------------
Importing src/backend/app.py on the host is infeasible: it imports the full
backend graph (structlog, alembic, panorama, the SQLAlchemy DAO, ...) that
only exists inside the flask-app image (see
test/backend-test/preservation/test_preservation_fastapi_endpoints.py, which
importorskips for the same reason). Following the task's guidance, this test
extracts the REAL `shutdown_event` handler from app.py's source via AST
(decorators stripped) and executes it in a controlled namespace where the two
cleanup callables are mocked — the handler body under test is the genuine
app.py code, byte-for-byte.

TIME SCALING (documented)
-------------------------
The real numbers are a ~30s blocking cleanup vs a 20s budget under a 120s
grace period. Sleeping 30s in a unit test is pointless, so the test scales
time down while preserving the invariant *bounded handler < blocking cleanup*:
the mocked cleanup blocks for BLOCK_SECONDS (4s) and the handler must return
within RETURN_BUDGET_SECONDS (2s). If the fixed module exposes its budget
constant (SHUTDOWN_CLEANUP_BUDGET_SECONDS per the design), the test overrides
it to 0.5s in the extracted namespace so the fixed handler is exercised
quickly; the unfixed handler has no budget at all and blocks the full 4s.

Validates: Requirements 1.1, 1.3
"""
import ast
import asyncio
import os
import threading
import time
from unittest.mock import MagicMock

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
APP_PY_PATH = os.path.join(REPO_ROOT, "src", "backend", "app.py")

#: Scaled stand-in for the ~30s blocking terminate_digital_input_task.
BLOCK_SECONDS = 4.0
#: Scaled stand-in for the 20-second cleanup budget (Requirement 2.3): the
#: handler must return in strictly less time than the blocked cleanup.
RETURN_BUDGET_SECONDS = 2.0
#: Value injected for the fixed module's budget constant (if present) so the
#: bounded path resolves fast in tests.
SCALED_BUDGET_SECONDS = 0.5


def _extract_shutdown_handler(namespace):
    """Compile the REAL `shutdown_event` from app.py source into `namespace`.

    Strips decorators (`@app.on_event(...)`) so no FastAPI app is needed, and
    copies any module-level constant assignments the handler body references
    (e.g. the fixed SHUTDOWN_CLEANUP_BUDGET_SECONDS) into the namespace.
    """
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


def test_shutdown_handler_returns_within_cleanup_budget():
    """isBugCondition_A (behavioral): a blocking cleanup must not hold the
    SIGTERM handler past the cleanup budget. On the unfixed handler the
    inline `cleanup_workflow_digital_inputs()` blocks the coroutine for the
    full BLOCK_SECONDS — the shape that exceeds Docker's 10s grace window on
    a real device and gets the backend SIGKILLed (exit 137).

    Validates: Requirements 1.1, 1.3 (expected behavior 2.3)
    """
    release = threading.Event()
    calls = []

    def blocking_cleanup_workflow_digital_inputs():
        calls.append("cleanup_workflow_digital_inputs")
        # Time-stubbed block: waits on an event (released in teardown), with
        # BLOCK_SECONDS as the scaled stand-in for the ~30s incident cleanup.
        release.wait(BLOCK_SECONDS)

    def disconnect_all_cameras():
        calls.append("disconnect_all_cameras")

    namespace = {
        "asyncio": asyncio,
        "time": time,
        "logger": MagicMock(name="logger"),
        "cleanup_workflow_digital_inputs":
            blocking_cleanup_workflow_digital_inputs,
        "disconnect_all_cameras": disconnect_all_cameras,
    }
    handler = _extract_shutdown_handler(namespace)

    # Scale the fixed module's budget (when it exists) so the bounded path
    # resolves quickly; the unfixed handler has no budget name to scale.
    if "SHUTDOWN_CLEANUP_BUDGET_SECONDS" in namespace:
        namespace["SHUTDOWN_CLEANUP_BUDGET_SECONDS"] = SCALED_BUDGET_SECONDS

    loop = asyncio.new_event_loop()
    try:
        started = time.monotonic()
        loop.run_until_complete(handler())
        elapsed = time.monotonic() - started
    finally:
        release.set()  # let any abandoned executor thread finish promptly
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()

    assert calls and calls[0] == "cleanup_workflow_digital_inputs", (
        "the handler no longer starts with the digital-input cleanup — "
        "extraction seam or handler contract changed")
    assert elapsed < RETURN_BUDGET_SECONDS, (
        "COUNTEREXAMPLE (Defect A): shutdown_event returned only after "
        "{:.2f}s while its cleanup blocked for {:.0f}s — the handler runs "
        "cleanup inline with no bound, so on a real device a slow "
        "terminate_digital_input_task holds SIGTERM handling past Docker's "
        "grace window and the backend is SIGKILLed mid-cleanup (exit 137)"
        .format(elapsed, BLOCK_SECONDS))
