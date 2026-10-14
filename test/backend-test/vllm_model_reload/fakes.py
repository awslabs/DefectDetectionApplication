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
"""Suite-shared fakes and harness for ``test/backend-test/vllm_model_reload``
(vllm-model-reload-after-backend-restart).

Honesty guard (design Testing Strategy): nothing here loads a real vLLM
engine, touches a GPU, restarts a real container, or drives real
Greengrass lifecycles. The engine is SIMULATED through the manager's
public injectable ``engine_factory`` seam (``VllmRuntimeManager.__init__``
documents it as the way tests drive the manager with a fake
``AsyncLLMEngine`` and no GPU); staged repositories are valid
Triton_vLLM_Repository trees written under ``tmp_path``; and a "backend
restart" is modeled as tearing down and reconstructing manager + server
(+ on the fixed tree, the reconciler) over the surviving directory tree —
exactly what a container restart does to the in-process state while the
staged repository under ``VLLM_MODEL_DIR`` survives on disk.

Pieces:

- :class:`RecordingEngineFactory` — a recording fake for the manager's
  injectable ``engine_factory`` seam; every call is recorded (this is how
  the exploration suite proves "zero load requests after restart").
- :func:`build_staged_repo` — writes a valid staged repository layout
  (``config.pbtxt`` declaring ``backend: "vllm"`` + ``1/model.json``).
- :func:`first_life_load` — the model's first life: a manager over the
  tree drives the load to READY (the component-Startup outcome), then the
  manager object is discarded, modeling the backend process dying.
- :class:`RestartedBackend` / :func:`restarted_backend` — the "restarted
  backend" harness: a FRESH ``VllmRuntimeManager`` over the surviving
  tree, a REAL ``VllmRuntimeServer`` on an ephemeral loopback port, and —
  when the fixed tree provides ``vllm_runtime.reconciler`` — the
  reconciler started with a fast test backoff. On the unfixed tree the
  reconciler is simply absent and the harness mirrors today's
  ``app.py::start_vllm_runtime()`` wiring verbatim.
- :func:`import_with_awsiot_stubs` — the established sys.modules-stubbing
  importer (test_feature_configs_vllm_merge.py / model_gpu_fallback_
  visibility pattern) for importing ``utils.feature_configs_utils``
  host-side.
"""
import asyncio
import importlib
import inspect
import json
import socket
import sys
import time
import types
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union
from unittest.mock import MagicMock

from vllm_runtime.manager import ModelState, VllmRuntimeManager
from vllm_runtime.server import VllmRuntimeServer

#: The incident's model (jetson-thor1, 2026-08-16).
DEFAULT_MODEL_NAME = "qwen3-vl-8b-instruct"

#: The text every fake engine generates to completion.
GENERATED_TEXT = "fake-generated-text"

#: How long the harness waits for a model to reach a terminal state after
#: the "restart". The fake engine loads instantly and the fixed tree's
#: reconciler is started with FAST_TEST_BACKOFF, so a passing run needs a
#: fraction of this; on the unfixed tree the budget is burned in full and
#: the state observed at exhaustion is the counterexample.
WAIT_BUDGET_SECONDS = 3.0

#: Poll interval for the wait helpers.
POLL_INTERVAL_SECONDS = 0.05

#: Backoff schedule injected into the (fixed-tree) reconciler so retries
#: stay inside the test budget.
FAST_TEST_BACKOFF = (0.05, 0.1, 0.2)

#: Manager states that end a reconciliation wait.
TERMINAL_STATES = (ModelState.READY, ModelState.FAILED)


# ---------------------------------------------------------------------------
# Recording fake engine (the manager's injectable engine_factory seam)
# ---------------------------------------------------------------------------

class _FakeCompletion:
    def __init__(self, text: str):
        self.text = text


class _FakeRequestOutput:
    """The ``RequestOutput`` shape ``VllmRuntimeManager._output_text``
    reads: ``.outputs[0].text``."""

    def __init__(self, text: str):
        self.outputs = [_FakeCompletion(text)]
        self.finished = True


class FakeEngine:
    """A fake ``AsyncLLMEngine`` exposing exactly the surface the manager
    uses: ``generate(prompt, sampling_params, request_id)`` yielding
    request outputs, plus the ``errored`` flag."""

    def __init__(self, engine_args: Mapping[str, Any], text: str = GENERATED_TEXT):
        self.engine_args = dict(engine_args)
        self.text = text
        self.errored = False

    async def generate(self, prompt, sampling_params, request_id):
        yield _FakeRequestOutput(self.text)


class RecordingEngineFactory:
    """Recording fake for ``VllmRuntimeManager``'s public injectable
    ``engine_factory`` seam. Every invocation (one per engine
    construction, i.e. per driven load) is recorded with its parsed
    engine args — zero recorded calls after a "restart" is the
    orphaned-model counterexample (defect 1.1)."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.engines: List[FakeEngine] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __call__(self, engine_args: Mapping[str, Any]) -> FakeEngine:
        self.calls.append(dict(engine_args))
        engine = FakeEngine(engine_args)
        self.engines.append(engine)
        return engine


# ---------------------------------------------------------------------------
# Staged-repo tree builder
# ---------------------------------------------------------------------------

def build_staged_repo(
    model_dir: Union[str, Path],
    model_name: str = DEFAULT_MODEL_NAME,
    engine_args: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write a valid staged Triton_vLLM_Repository for ``model_name``
    under ``model_dir`` (the ``VLLM_MODEL_DIR`` stand-in): ``config.pbtxt``
    declaring ``backend: "vllm"`` and ``1/model.json`` holding a JSON
    object of engine args — the exact layout ``parse_repository``
    validates."""
    repo = Path(model_dir) / model_name
    (repo / "1").mkdir(parents=True, exist_ok=True)
    (repo / "config.pbtxt").write_text('backend: "vllm"\n')
    (repo / "1" / "model.json").write_text(
        json.dumps(engine_args if engine_args is not None else {"model": model_name})
    )
    return repo


# ---------------------------------------------------------------------------
# First life: the component-Startup outcome (model READY, then process dies)
# ---------------------------------------------------------------------------

def first_life_load(
    model_dir: Union[str, Path], model_name: str = DEFAULT_MODEL_NAME
) -> RecordingEngineFactory:
    """Drive the staged model to READY in a first-life manager, then
    discard the manager — the in-process engine dies with the "process"
    while the staged tree survives on disk. This is test PRECONDITION
    setup (the 21:52:32Z 'loaded successfully' leg of the incident), not
    the behavior under test; it raises AssertionError if the fake load
    itself misbehaves."""
    factory = RecordingEngineFactory()
    manager = VllmRuntimeManager(
        model_dir=model_dir,
        engine_factory=factory,
        sampling_params_factory=dict,
    )
    status = asyncio.run(manager.load(model_name))
    assert status.state is ModelState.READY, (
        "harness precondition failed: first-life load did not reach READY "
        "(got {}: {})".format(status.state, status.reason)
    )
    assert factory.call_count == 1
    # The manager object (and its engine) is dropped here: the backend
    # process is gone. Only the directory tree survives.
    return factory


# ---------------------------------------------------------------------------
# The "restarted backend" harness
# ---------------------------------------------------------------------------

def free_port() -> int:
    """An ephemeral loopback port for the real runtime server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def maybe_start_reconciler(manager: VllmRuntimeManager, port: int):
    """Construct and start the fixed tree's ``VllmReconciler`` when the
    module exists; return None on the unfixed tree (module absent).

    Constructor kwargs are matched against the live signature so the
    harness follows the real seam (design File 1: ``VllmReconciler(
    manager, port=..., backoff=..., request_fn=None)``) without pinning
    it byte-for-byte before it exists.
    """
    try:
        module = importlib.import_module("vllm_runtime.reconciler")
    except ImportError:
        return None
    reconciler_cls = getattr(module, "VllmReconciler", None)
    if reconciler_cls is None:
        return None
    parameters = inspect.signature(reconciler_cls).parameters
    kwargs: Dict[str, Any] = {}
    if "port" in parameters:
        kwargs["port"] = port
    if "backoff" in parameters:
        kwargs["backoff"] = FAST_TEST_BACKOFF
    reconciler = reconciler_cls(manager, **kwargs)
    start = getattr(reconciler, "start", None)
    if callable(start):
        start()
    return reconciler


class RestartedBackend:
    """The restarted LocalServer backend, reduced to the vLLM wiring
    ``app.py::start_vllm_runtime()`` performs: a FRESH manager (empty
    model table) over the surviving tree, the REAL ``VllmRuntimeServer``
    on an ephemeral loopback port, and — on the fixed tree only — the
    reconciler. Use through :func:`restarted_backend`."""

    def __init__(self, model_dir: Union[str, Path]):
        self.model_dir = Path(model_dir)
        self.factory = RecordingEngineFactory()
        self.manager = VllmRuntimeManager(
            model_dir=self.model_dir,
            engine_factory=self.factory,
            sampling_params_factory=dict,
        )
        self.port = free_port()
        self.server = VllmRuntimeServer(self.manager, port=self.port)
        self.reconciler = None

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:{}".format(self.port)

    def start(self) -> "RestartedBackend":
        self.server.start()
        self.reconciler = maybe_start_reconciler(self.manager, self.port)
        return self

    def stop(self) -> None:
        stop = getattr(self.reconciler, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:  # noqa: BLE001 - teardown must never mask a test
                pass
        self.server.stop()

    def wait_for_terminal_state(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        budget_seconds: float = WAIT_BUDGET_SECONDS,
    ):
        """Poll ``manager.state(model_name)`` until READY/FAILED or the
        budget is exhausted; return the last observed status (the
        counterexample when non-terminal)."""
        deadline = time.monotonic() + budget_seconds
        status = self.manager.state(model_name)
        while status.state not in TERMINAL_STATES and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            status = self.manager.state(model_name)
        return status


class restarted_backend:
    """``with restarted_backend(tree) as backend:`` — start/stop wrapper."""

    def __init__(self, model_dir: Union[str, Path]):
        self._backend = RestartedBackend(model_dir)

    def __enter__(self) -> RestartedBackend:
        return self._backend.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self._backend.stop()


# ---------------------------------------------------------------------------
# awsiot stubbing importer (test_feature_configs_vllm_merge.py pattern)
# ---------------------------------------------------------------------------

def import_with_awsiot_stubs(module_name: str):
    """Import a backend module with the runtime-image-only ``awsiot``
    modules stubbed, then drop the stubs AND the imported module from
    ``sys.modules`` so nothing leaks into other test modules. The returned
    module object keeps its own references to the stubs."""
    installed = []

    def _register(name, module):
        if name in sys.modules:
            return
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = module
            installed.append(name)

    awsiot = types.ModuleType("awsiot")
    ggipc = types.ModuleType("awsiot.greengrasscoreipc")
    ggipc.connect = MagicMock()
    ggipc_model = types.ModuleType("awsiot.greengrasscoreipc.model")
    ggipc_model.ResourceNotFoundError = type(
        "ResourceNotFoundError", (Exception,), {})
    ggipc_model.UnauthorizedError = type("UnauthorizedError", (Exception,), {})
    ggipc_model.GetConfigurationRequest = MagicMock()
    awsiot.greengrasscoreipc = ggipc
    ggipc.model = ggipc_model
    _register("awsiot", awsiot)
    _register("awsiot.greengrasscoreipc", ggipc)
    _register("awsiot.greengrasscoreipc.model", ggipc_model)

    try:
        module = __import__(module_name, fromlist=["_"])
    finally:
        for name in installed:
            sys.modules.pop(name, None)
        if installed:
            sys.modules.pop(module_name, None)
    return module
