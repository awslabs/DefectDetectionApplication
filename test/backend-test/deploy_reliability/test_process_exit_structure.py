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
"""Deterministic-process-exit structure tests for edge-deploy-reliability
(on-hardware verification finding 2).

Observed on the live JP6 (LocalServer.arm64JP6 v1.0.44): `docker stop` of the
backend container took EXACTLY 120s and ended in SIGKILL (ExitCode 137,
OOMKilled=false). The SIGTERM log showed "Cleaning up digital input
workflows" and then silence — the bounded shutdown_event completed within its
20s budget (no budget warning), uvicorn finished its graceful shutdown, and
serve() returned... into nothing: `main()` detached `server.serve()` with
`loop.create_task(...)` and the `__main__` guard parked the process in
`loop.run_forever()`, which NOTHING ever stopped. /proc sampling during the
stop showed the main thread idle in `do_epoll_wait` for the full 120s with
144 threads alive (vLLM engine, torch/NCCL, Triton client) — hypothesis (b):
the shutdown handler completed, but the process could never exit.

The fixed contract, encoded here as source-structure assertions (importing
app.py on the host is infeasible — it needs the full in-image backend graph,
same seam as test_shutdown_handler_exploration.py):

  1. `main()` AWAITS `server.serve()` (not `create_task`), so it returns when
     uvicorn's graceful shutdown completes;
  2. the `__main__` guard has NO `loop.run_forever()` — `run_until_complete`
     returns when serve() does;
  3. the `__main__` guard ends the process with `os._exit(...)` after cleanup
     (loop close + vLLM runtime stop): interpreter teardown would otherwise
     join the non-daemon vLLM/torch/Triton threads and multiprocessing
     children far past the 120s grace window;
  4. the vLLM runtime server is explicitly stopped on the exit path.

Validates: Requirements 2.3, 2.7 (backend exits well within the
stop_grace_period on SIGTERM; a docker stop can never escalate to SIGKILL)
"""
import ast
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
APP_PY_PATH = os.path.join(REPO_ROOT, "src", "backend", "app.py")


def _parse_app():
    with open(APP_PY_PATH, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=APP_PY_PATH)


def _find_main_coroutine(tree):
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main":
            return node
    raise AssertionError("async def main() not found in src/backend/app.py — "
                         "update this test's extraction seam")


def _find_main_guard(tree):
    """The `if __name__ == "__main__":` block."""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"):
            return node
    raise AssertionError('`if __name__ == "__main__":` guard not found in '
                         "src/backend/app.py")


def _calls_of_attr(node, attr_name):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr_name]


def test_main_awaits_server_serve():
    """serve() must be awaited so main() returns when uvicorn's graceful
    shutdown completes — the detached `loop.create_task(server.serve())`
    left nothing to observe the shutdown finishing."""
    main_fn = _find_main_coroutine(_parse_app())

    awaited_serve = [
        n for n in ast.walk(main_fn)
        if isinstance(n, ast.Await)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Attribute)
        and n.value.func.attr == "serve"
    ]
    assert awaited_serve, (
        "COUNTEREXAMPLE (finding 2): main() does not await server.serve() — "
        "a detached serve task means graceful shutdown completion is never "
        "observed and the process cannot exit on SIGTERM")

    for call in _calls_of_attr(main_fn, "create_task"):
        for arg in call.args:
            for inner in ast.walk(arg):
                if (isinstance(inner, ast.Attribute)
                        and inner.attr == "serve"):
                    raise AssertionError(
                        "COUNTEREXAMPLE (finding 2): main() still detaches "
                        "server.serve() via loop.create_task(...)")


def test_main_guard_never_runs_forever():
    """The `__main__` guard must not park the process in loop.run_forever():
    nothing stops that loop after uvicorn's serve() task finishes, so a
    docker stop idles in epoll until the 120s SIGKILL (the observed exit
    137 at exactly 120s on the JP6)."""
    guard = _find_main_guard(_parse_app())
    run_forever_calls = _calls_of_attr(guard, "run_forever")
    assert not run_forever_calls, (
        "COUNTEREXAMPLE (finding 2): the __main__ guard still calls "
        "loop.run_forever() — the process can never exit after graceful "
        "shutdown and is SIGKILLed at the stop_grace_period")


def test_main_guard_exits_deterministically():
    """After cleanup the guard must end the process with os._exit(...):
    interpreter teardown would otherwise join the non-daemon vLLM/torch/
    Triton threads (144 observed live) and multiprocessing children,
    blocking exit far past the grace window."""
    guard = _find_main_guard(_parse_app())
    os_exit_calls = [
        n for n in ast.walk(guard)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_exit"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "os"
    ]
    assert os_exit_calls, (
        "COUNTEREXAMPLE (finding 2): the __main__ guard never calls "
        "os._exit(...) — interpreter teardown joins the engine's non-daemon "
        "threads/children and hangs past the 120s stop grace window")


def test_main_guard_stops_vllm_runtime_on_exit():
    """The exit path explicitly stops the companion vLLM runtime server
    (VllmRuntimeServer.stop is internally bounded), so the loopback
    generate/model-control listener is shut down before the process ends."""
    guard = _find_main_guard(_parse_app())
    stop_calls = _calls_of_attr(guard, "stop")
    assert stop_calls, (
        "the __main__ guard exit path no longer stops the vLLM runtime "
        "server — update this test if the shutdown ownership moved")
