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
"""Shared fakes for ``test/backend-test/vllm_jp7_engine_lifecycle``
(spec vllm-jp7-engine-lifecycle).

Nothing here loads vLLM or touches a GPU. A "construction" is the
manager's injectable ``engine_factory``:

- :class:`EngineCoreLikeFactory` mimics vLLM 0.11's ``wait_for_engine_startup``:
  it starts a child process that names itself like vLLM's engine core
  (``/proc/self/comm`` = ``VLLM::EngineCor``), then polls the child's
  liveness and raises only when the child has died, exactly as vLLM raises
  "Engine core initialization failed" when a process sentinel fires. The
  child either sleeps forever (H1: a live but stuck engine core), burns CPU
  forever (a busy construction that never finishes), or "becomes ready"
  after a delay (a construction that completes).
- :class:`BlockingFactory` is the H2 shape: the construction blocks on an
  Event without starting any process, so there is nothing to stop.

The child is started with ``subprocess`` (fork + exec), not a bare fork: the
watchdog only needs a live child of the backend that looks like an engine
core, and exec keeps pytest's threads out of the child.
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from vllm_model_reload.fakes import FakeEngine, build_staged_repo  # noqa: F401

from vllm_runtime.manager import VllmRuntimeManager

DEFAULT_MODEL_NAME = "qwen3-vl-8b-instruct"

from vllm_jp7_engine_lifecycle.harness_support import (  # noqa: F401
    BACKEND_DIR,
    HARNESS_PATH,
    run_harness,
)

#: Child behaviours.
CHILD_SLEEP = "sleep"   # live, idle, never ready (H1)
CHILD_SPIN = "spin"     # live, busy, never ready
CHILD_READY = "ready"   # becomes "ready" after a delay

_CHILD_CODE = r"""
import sys, time
try:
    with open("/proc/self/comm", "w") as fh:
        fh.write("VLLM::EngineCore")
except OSError:
    pass
mode = sys.argv[1]
if mode == "spin":
    while True:
        pass
time.sleep(3600)
"""


def child_env() -> Dict[str, str]:
    """The environment for a child Python: the backend-test root conftest
    sets PYTHONHOME to the interpreter path, which breaks a child's start."""
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    return env


class EngineCoreLikeFactory:
    """A blocking engine factory shaped like vLLM 0.11's V1 construction."""

    def __init__(self, mode: str = CHILD_SLEEP, ready_after_s: float = 0.0,
                 poll_s: float = 0.05):
        self.mode = mode
        self.ready_after_s = ready_after_s
        self.poll_s = poll_s
        self.children: List[subprocess.Popen] = []
        self.calls = 0
        self.raised: List[str] = []

    def __call__(self, engine_args: Mapping[str, Any]) -> Any:
        self.calls += 1
        child_mode = CHILD_SLEEP if self.mode == CHILD_READY else self.mode
        child = subprocess.Popen(  # nosec B603 - fixed argv, test fake
            [sys.executable, "-c", _CHILD_CODE, child_mode],
            env=child_env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        started = time.monotonic()
        while True:
            code = child.poll()
            if code is not None:
                message = ("Engine core initialization failed. See root cause "
                           "above. Failed core proc(s): {{'EngineCore_DP0': {}}}"
                           .format(code))
                self.raised.append(message)
                raise RuntimeError(message)
            if (self.mode == CHILD_READY
                    and time.monotonic() - started >= self.ready_after_s):
                child.kill()
                child.wait()
                return FakeEngine(engine_args)
            time.sleep(self.poll_s)

    def cleanup(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.kill()
            try:
                child.wait(5)
            except subprocess.TimeoutExpired:
                pass


class BlockingFactory:
    """The H2 shape: blocks on an Event without starting any process.
    ``release()`` lets it return an engine (or raise, if ``raise_on_release``)."""

    def __init__(self, raise_on_release: bool = False):
        self.entered = threading.Event()
        self._release = threading.Event()
        self.raise_on_release = raise_on_release
        self.engines: List[FakeEngine] = []

    def __call__(self, engine_args: Mapping[str, Any]) -> Any:
        self.entered.set()
        self._release.wait()
        if self.raise_on_release:
            raise RuntimeError("released with an error")
        engine = FakeEngine(engine_args)
        engine.shutdowns = 0

        def shutdown_background_loop():
            engine.shutdowns += 1
        engine.shutdown_background_loop = shutdown_background_loop
        self.engines.append(engine)
        return engine

    def release(self) -> None:
        self._release.set()


class SelfRestartRecorder:
    """Test double for Decision 3's self-restart."""

    def __init__(self):
        self.calls = 0
        self.called = threading.Event()

    def __call__(self) -> None:
        self.calls += 1
        self.called.set()


#: Fixed ``/proc/meminfo`` for the device memory preflight, so a test's
#: log lines do not depend on the build host's free memory.
FIXED_MEMINFO = "MemTotal:       131072000 kB\nMemAvailable:   120000000 kB\n"


def make_manager(model_dir, factory, diagnostics_dir=None, bound_s=5.0,
                 stall_window_s=1.0, grace_s=0.5, sample_period_s=0.05,
                 stall_cpu_seconds=0.2, self_restart=None,
                 **watchdog_options) -> VllmRuntimeManager:
    """A manager over ``model_dir`` with short watchdog settings. On the
    unfixed tree (no watchdog parameters) it builds the plain manager, so
    the exploration tests observe the real unbounded behaviour instead of a
    TypeError."""
    import inspect

    # The production threshold (1 CPU-second per 120 s window) scaled to a
    # one-second test window: a spinning child accrues ~1 CPU-second per
    # window (5x this), an idle one ~0.
    options = {"sample_period_s": sample_period_s,
               "stall_cpu_seconds": stall_cpu_seconds}
    options.update(watchdog_options)
    kwargs: Dict[str, Any] = dict(
        model_dir=model_dir,
        engine_factory=factory,
        sampling_params_factory=dict,
        memory_reader=lambda: FIXED_MEMINFO,
    )
    if "construction_bound_s" in inspect.signature(VllmRuntimeManager).parameters:
        kwargs.update(
            construction_bound_s=bound_s,
            stall_window_s=stall_window_s,
            unblock_grace_s=grace_s,
            diagnostics_dir=str(diagnostics_dir) if diagnostics_dir else None,
            watchdog_options=options,
            self_restart=self_restart or SelfRestartRecorder(),
        )
    return VllmRuntimeManager(**kwargs)


def run_load_on_thread(manager: VllmRuntimeManager, model_name: str,
                       budget_s: float):
    """Drive ``manager.load`` on a worker thread (its own event loop, like
    the runtime server's). Returns ``(status or None, elapsed_s, thread)``;
    ``None`` means the load had not returned within ``budget_s`` (the
    unfixed hang), and the caller must unblock the factory and join."""
    import asyncio

    result: Dict[str, Any] = {}

    def _target():
        loop = asyncio.new_event_loop()
        try:
            result["status"] = loop.run_until_complete(manager.load(model_name))
        except BaseException as err:  # noqa: BLE001 - surfaced to the test
            result["error"] = err
        finally:
            loop.close()

    started = time.monotonic()
    thread = threading.Thread(target=_target, name="test-load", daemon=True)
    thread.start()
    thread.join(budget_s)
    elapsed = time.monotonic() - started
    if "error" in result:
        raise result["error"]
    return result.get("status"), elapsed, thread


def diagnostics_files(directory) -> List[Path]:
    return sorted(Path(directory).glob("vllm-construction-timeout-*.txt"))
