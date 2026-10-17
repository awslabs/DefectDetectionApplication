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
"""Suite-shared fakes for ``test/backend-test/triton_cleanup_wedge``
(triton-model-cleanup-wedge).

Honesty guard: nothing here starts Triton, the backend, Docker or
Greengrass. The backend is a fake HTTP session answering the two routes the
host scripts use (``/feature-configurations`` and the model start/stop
routes); Triton is a fake client exposing the methods the gates and the
tracker call; repositories and lock directories live under ``tmp_path``.
The only real cross-process mechanism exercised is ``flock`` between two
host processes (:func:`spawn_lock_holder`).

This module must stay importable under Python 3.8 (no PEP 585 generics, no
``str.removeprefix``, no ``dict | dict``).
"""
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import types
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock

BACKEND_SRC = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "backend"))

STATE_READY = "READY"
STATE_LOADING = "LOADING"
STATE_UNKNOWN = "UNKNOWN"
STATE_UNAVAILABLE = "UNAVAILABLE"
STATE_UNLOADING = "UNLOADING"


# --------------------------------------------------------------- the clock

class FakeTime:
    """A monotonic clock that only ``sleep`` advances, plus a wall clock that
    moves with it (offset by ``wall_offset``)."""

    def __init__(self, start: float = 1000.0, wall_start: Optional[float] = None):
        self.now = float(start)
        self._wall0 = time.time() if wall_start is None else float(wall_start)
        self._mono0 = float(start)
        self.sleeps: List[float] = []
        self.wall_offset = 0.0

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> float:
        return self._wall0 + (self.now - self._mono0) + self.wall_offset

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += max(0.0, float(seconds))

    def install(self, monkeypatch, module=time) -> "FakeTime":
        """Patch ``time.monotonic`` and ``time.sleep`` (restored by monkeypatch)."""
        monkeypatch.setattr(module, "monotonic", self.monotonic)
        monkeypatch.setattr(module, "sleep", self.sleep)
        return self


# ------------------------------------------------------- the fake backend

class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class FakeBackend:
    """The LocalServer routes the host scripts call, as a ``requests``-like
    object (``get(url, headers=None, timeout=None)``).

    ``states`` maps a model to its reported status; a list value is consumed
    one entry per ``/feature-configurations`` read (the last repeats).
    ``on_stop`` decides the next state after a ``/stop`` (default
    ``UNKNOWN``). ``failure_reasons`` adds ``defaultConfiguration.failureReason``.
    """

    def __init__(self, states: Optional[Dict[str, Any]] = None, start_status: int = 200,
                 stop_status: int = 200, failure_reasons: Optional[Dict[str, str]] = None,
                 raise_on: Optional[Callable[[str], Optional[Exception]]] = None):
        self.states: Dict[str, Any] = dict(states or {})
        self.start_status = start_status
        self.stop_status = stop_status
        self.failure_reasons = dict(failure_reasons or {})
        self.raise_on = raise_on
        self.calls: List[Dict[str, Any]] = []
        self.on_stop: Dict[str, str] = {}
        self.on_read: Optional[Callable[[], None]] = None
        self._lock = threading.Lock()

    def _state(self, model: str) -> Optional[str]:
        value = self.states.get(model)
        if isinstance(value, list):
            if len(value) > 1:
                return value.pop(0)
            return value[0] if value else None
        return value

    def get(self, url, headers=None, timeout=None):
        with self._lock:
            self.calls.append({"url": url, "timeout": timeout})
        if self.raise_on is not None:
            exc = self.raise_on(url)
            if exc is not None:
                raise exc
        if url.endswith("/stop"):
            model = url.rsplit("/", 2)[-2]
            if self.stop_status == 200:
                self.states[model] = self.on_stop.get(model, STATE_UNKNOWN)
                return FakeResponse(200, {"modelName": model, "status": "UNLOADING"})
            return FakeResponse(self.stop_status, None, text="stop refused")
        if url.endswith("/start"):
            model = url.rsplit("/", 2)[-2]
            return FakeResponse(self.start_status, {"modelName": model, "status": "LOADING"},
                                text="start answer")
        if url.endswith("/feature-configurations"):
            if self.on_read is not None:
                self.on_read()
            payload = []
            for model in list(self.states):
                state = self._state(model)
                if state is None:
                    continue
                entry = {"type": "TritonModel", "modelName": model, "status": state,
                         "defaultConfiguration": {}}
                if model in self.failure_reasons:
                    entry["defaultConfiguration"]["failureReason"] = self.failure_reasons[model]
                payload.append(entry)
            return FakeResponse(200, payload)
        return FakeResponse(404, None, text="not found")

    def urls(self, suffix: str) -> List[str]:
        return [c["url"] for c in self.calls if c["url"].endswith(suffix)]


# ---------------------------------------------------------- the fake Triton

class FakeTritonClient:
    """What the gates, the tracker and ``start_model_triton`` call.

    ``statuses`` is consumed one entry per ``get_model_status`` (the last
    repeats). ``start_raises`` is raised from ``start_triton_model``; a
    callable is called with the model id and may return an exception.
    """

    def __init__(self, statuses=None, start_raises=None, index=None):
        self._statuses = list(statuses or [STATE_UNKNOWN])
        self.start_raises = start_raises
        self.start_calls: List[str] = []
        self.status_calls: List[str] = []
        self.index: Dict[str, Any] = dict(index or {})
        self.on_start: Optional[Callable[[str], None]] = None

    def get_model_status(self, model_id):
        self.status_calls.append(model_id)
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]

    def push_statuses(self, statuses):
        self._statuses = list(statuses)

    def start_triton_model(self, model_id):
        self.start_calls.append(model_id)
        if self.on_start is not None:
            self.on_start(model_id)
        exc = self.start_raises
        if callable(exc) and not isinstance(exc, BaseException):
            exc = exc(model_id)
        if exc is not None:
            raise exc
        return STATE_LOADING

    def list_triton_models(self, quiet=False):
        out = []
        for name, value in self.index.items():
            state, reason = value if isinstance(value, tuple) else (value, None)
            record = {"model_component": name, "status": state}
            if reason:
                record["reason"] = reason
            out.append(record)
        return out

    def index_states(self):
        return {name: (value if isinstance(value, tuple) else (value, None))
                for name, value in self.index.items()}


class FakeNativeTriton:
    """The ``panorama.mlops`` server object ``TritonEdgeClient`` wraps."""

    def __init__(self, index=None, list_answers=None):
        self.index: Dict[str, Any] = dict(index or {})
        self.list_answers = list(list_answers or [])
        self.load_calls: List[str] = []
        self.list_calls = 0
        self.load_raises: Optional[BaseException] = None

    def list_models(self):
        self.list_calls += 1
        if self.list_answers:
            return self.list_answers.pop(0)
        return json.dumps({name: ({"state": v[0], "reason": v[1]} if isinstance(v, tuple)
                                  else {"state": v}) for name, v in self.index.items()})

    def load_model(self, model_id):
        self.load_calls.append(model_id)
        if self.load_raises is not None:
            raise self.load_raises
        self.index[model_id] = STATE_LOADING

    def get_model_status(self, model_id):
        value = self.index.get(model_id, STATE_UNKNOWN)
        return value[0] if isinstance(value, tuple) else value

    def model_metadata(self, model_id):
        return json.dumps({"name": model_id, "state": self.get_model_status(model_id)})

    def unload_model(self, model_id):
        self.index[model_id] = STATE_UNKNOWN


def install_fake_panorama(monkeypatch, native=None):
    """Make ``dda_triton.triton_edge_client`` importable on the host with a
    fake ``panorama.mlops`` whose ``create_triton_inference_server`` returns
    ``native``. Returns the (fresh) module and the native fake."""
    native = native if native is not None else FakeNativeTriton()
    panorama = types.ModuleType("panorama")
    mlops = types.ModuleType("panorama.mlops")
    mlops.create_triton_inference_server = MagicMock(return_value=native)
    panorama.mlops = mlops
    monkeypatch.setitem(sys.modules, "panorama", panorama)
    monkeypatch.setitem(sys.modules, "panorama.mlops", mlops)
    # Record the current entry (or its absence) so the test's teardown puts
    # it back, then import a fresh copy bound to the fake panorama.
    name = "dda_triton.triton_edge_client"
    monkeypatch.setitem(sys.modules, name, sys.modules.get(name))
    sys.modules.pop(name)
    import importlib
    module = importlib.import_module(name)
    monkeypatch.setattr(module.TritonEdgeClient, "_instance", None)
    return module, native


def import_with_awsiot_stubs(module_name: str):
    """Import a backend module with the runtime-image-only ``awsiot``
    modules stubbed, then drop the stubs and the imported module from
    ``sys.modules`` (the pattern of ``vllm_model_reload/fakes.py``)."""
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
    ggipc_model.ResourceNotFoundError = type("ResourceNotFoundError", (Exception,), {})
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


# ------------------------------------------------------------ repositories

ENSEMBLE_CONFIG = textwrap.dedent('''\
    name: "{m}"
    platform: "ensemble"
    ensemble_scheduling {{
      step {{
        model_name: "base_{m}"
        model_version: -1
      }}
      step {{
        model_name: "marshal_{m}"
        model_version: -1
      }}
    }}
    ''')
STEP_CONFIG = 'name: "{n}"\nbackend: "python"\nmax_batch_size: 0\n'


def write_entry(repo: str, model: str, version: str = "1", age_s: Optional[float] = 3600.0,
                skip=()) -> Dict[str, str]:
    """Write a complete Triton entry (``<m>``, ``base_<m>``, ``marshal_<m>``)
    by hand, every path aged ``age_s`` seconds (``None`` leaves the mtimes as
    written). ``skip`` names relative paths not to write. Returns the paths."""
    os.makedirs(repo, exist_ok=True)
    paths: Dict[str, str] = {}

    def _file(rel, text):
        if rel in skip:
            return
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        paths[rel] = path

    _file(os.path.join(model, "config.pbtxt"), ENSEMBLE_CONFIG.format(m=model))
    os.makedirs(os.path.join(repo, model, version), exist_ok=True)
    _file(os.path.join(model, version, "ensemble_model"), "ensemble\n")
    for step in ("base_" + model, "marshal_" + model):
        os.makedirs(os.path.join(repo, step, version), exist_ok=True)
        _file(os.path.join(step, "config.pbtxt"), STEP_CONFIG.format(n=step))
        _file(os.path.join(step, version, "model.py"), "# model\n")
    if age_s is not None:
        age_tree(repo, age_s)
    return paths


def age_tree(root: str, age_s: float, now: Optional[float] = None) -> None:
    stamp = (time.time() if now is None else now) - age_s
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            os.utime(os.path.join(dirpath, name), (stamp, stamp), follow_symlinks=False)
        os.utime(dirpath, (stamp, stamp))


def set_age(path: str, age_s: float) -> None:
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def write_fixture_model(deployed_dir: str, runtime: str = "dlr", payload: str = "stub") -> str:
    """An unarchived model component as the convertor consumes it."""
    os.makedirs(deployed_dir, exist_ok=True)
    manifest = {"runtime": runtime, "dataset": {"image_height": 224, "image_width": 224}}
    with open(os.path.join(deployed_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    with open(os.path.join(deployed_dir, "compiled.so"), "w", encoding="utf-8") as handle:
        handle.write(payload)
    return deployed_dir


def published_dirs(repo: str, model: str) -> List[str]:
    return [os.path.join(repo, "base_" + model), os.path.join(repo, "marshal_" + model),
            os.path.join(repo, model)]


def hidden_entries(repo: str) -> List[str]:
    try:
        return sorted(n for n in os.listdir(repo) if n.startswith("."))
    except OSError:
        return []


# ----------------------------------------------------- real cross-process flock

_HOLDER = textwrap.dedent('''\
    import fcntl, os, sys, time
    path, mode = sys.argv[1], sys.argv[2]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX if mode == "ex" else fcntl.LOCK_SH)
    sys.stdout.write("locked\\n")
    sys.stdout.flush()
    sys.stdin.readline()
    ''')


class LockHolder:
    """A separate process that holds ``flock`` on ``path`` until released."""

    def __init__(self, path: str, exclusive: bool):
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, path, "ex" if exclusive else "sh"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, universal_newlines=True)
        line = self.proc.stdout.readline()
        if line.strip() != "locked":
            self.proc.kill()
            raise RuntimeError("lock holder did not lock {0}".format(path))

    def release(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write("\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            self.proc.wait(timeout=10)

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.kill()


def spawn_lock_holder(path: str, exclusive: bool) -> LockHolder:
    return LockHolder(path, exclusive)


def lock_is_free(path: str, exclusive: bool = True) -> bool:
    """Probe ``path`` from a fresh process with a non-blocking flock."""
    probe = textwrap.dedent('''\
        import fcntl, os, sys
        fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if sys.argv[2] == "ex" else fcntl.LOCK_SH)
                        | fcntl.LOCK_NB)
        except OSError:
            sys.exit(1)
        sys.exit(0)
        ''')
    rc = subprocess.call([sys.executable, "-c", probe, path, "ex" if exclusive else "sh"])
    return rc == 0


def records_in(directory: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for name in names:
        if name.endswith(".json"):
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                out[name[:-5]] = json.load(handle)
    return out
