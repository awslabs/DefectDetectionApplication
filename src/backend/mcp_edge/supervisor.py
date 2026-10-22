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
"""Supervision of the Edge MCP child process (backend process; design §5
``supervisor.py`` and Decision point 3 "Process model").

``McpSupervisor`` runs a daemon thread that keeps the child in the state
``McpConfig`` asks for:

- spawns ``VENV_PYTHON -m mcp_edge.child --port 5790 --backend-url <url>
  --server-version <catalog>+<component> [--allowed-origins <csv>]
  [--features vllm] [--mutating-enabled]`` with ``cwd=BACKEND_ROOT``, an
  environment of ``PYTHONPATH``, ``PATH`` and ``DDA_MCP_LOG_FORMAT`` only
  (nothing else is inherited), both pipes relayed by ``log_relay`` and a new
  session;
- the child is ready when ``GET http://127.0.0.1:5790/healthz`` answers 200
  within 20 s;
- a crashed (or never ready) child is restarted with exponential backoff,
  1, 2, 4 ... 60 s; the backoff resets once a child has run for 60 s. Exit
  code 2 (the child rejected its arguments) is not retried until the
  configuration changes;
- a change of the mutating switch, the allowed origins or the feature set
  restarts the child (it is stateless); ``McpServerEnabled`` false stops it;
- ``stop()`` sends SIGTERM, waits 10 s, then SIGKILL.

Every exception is caught and logged; the thread never dies and nothing
propagates to the Backend_API (Requirement 12.3).
"""
import logging
import os
import re
import subprocess
import threading
import time
import urllib.request
from typing import Callable, Iterable, List, Optional

import mcp_edge
from mcp_edge import CHILD_PORT, VENV_PYTHON, __version__, log_relay
from mcp_edge.config import McpConfig, McpState

logger = logging.getLogger(__name__)

# The parent of the mcp_edge package: src/backend on a host, / in the image.
BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(mcp_edge.__file__)))

READINESS_TIMEOUT_S = 20.0
READINESS_POLL_S = 0.25
STOP_TIMEOUT_S = 10.0
BACKOFF_INITIAL_S = 1.0
BACKOFF_MAX_S = 60.0
STABLE_AFTER_S = 60.0
LOOP_TICK_S = 1.0
EXIT_USAGE = 2
KNOWN_FEATURES = ("vllm",)
COMPONENT_VERSION_RE = re.compile(r"^[A-Za-z0-9.\-]{1,64}$")


def restart_requested(previous: McpState, current: McpState) -> bool:
    """True when a running child must be restarted for ``current``: the
    server stays enabled and the mutating switch or the origins changed.
    Pure."""
    return (previous.server_enabled and current.server_enabled
            and (previous.mutating_enabled != current.mutating_enabled
                 or previous.allowed_origins != current.allowed_origins))


def clean_component_version(value) -> str:
    """The component version as the child's ``--server-version`` accepts it
    (``[A-Za-z0-9.-]+`` after the ``+``); anything else, such as the
    ``NOT_FOUND`` fallback of ``system.get_local_server_component_version``,
    becomes ``unknown`` so the child is not refused its arguments. Pure."""
    if isinstance(value, str) and COMPONENT_VERSION_RE.match(value):
        return value
    return "unknown"


def _default_probe(port: int) -> bool:
    try:
        with urllib.request.urlopen(  # nosec B310 — fixed loopback http URL
                f"http://127.0.0.1:{port}/healthz", timeout=1.0) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 — not ready yet
        return False


class McpSupervisor:
    """Keeps the Edge MCP child in the configured state."""

    def __init__(
        self,
        config,
        backend_base_url: str,
        features: Iterable[str] = (),
        version: str = __version__,
        component_version: str = "unknown",
        *,
        popen: Callable = subprocess.Popen,
        probe: Optional[Callable[[int], bool]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        start_relays: Callable = log_relay.start_relays,
        venv_python: str = VENV_PYTHON,
        port: int = CHILD_PORT,
    ):
        self.config = config
        self.backend_base_url = backend_base_url
        self.features = tuple(sorted(f for f in set(features) if f in KNOWN_FEATURES))
        self.version = version
        self.component_version = clean_component_version(component_version)
        self._popen = popen
        self._probe = probe or _default_probe
        self._clock = clock
        self._sleep = sleep
        self._start_relays = start_relays
        self.venv_python = venv_python
        self.port = port

        self.restart_count = 0
        self.last_exit_code: Optional[int] = None
        self.last_error: Optional[str] = None
        self.listening = False

        self._child = None
        self._argv: Optional[List[str]] = None
        self._ready_at: Optional[float] = None
        self._crashed = False
        self._backoff = BACKOFF_INITIAL_S
        self._next_attempt = 0.0
        self._blocked_argv: Optional[List[str]] = None
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ spawn

    @property
    def server_version(self) -> str:
        return f"{self.version}+{self.component_version}"

    def build_argv(self, mutating: bool, allowed_origins: Iterable[str]) -> List[str]:
        argv = [self.venv_python, "-m", "mcp_edge.child",
                "--port", str(self.port),
                "--backend-url", self.backend_base_url,
                "--server-version", self.server_version]
        origins = list(allowed_origins)
        if origins:
            argv += ["--allowed-origins", ",".join(origins)]
        if self.features:
            argv += ["--features", ",".join(self.features)]
        if mutating:
            argv.append("--mutating-enabled")
        return argv

    @staticmethod
    def build_env() -> dict:
        return {"PYTHONPATH": BACKEND_ROOT,
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "DDA_MCP_LOG_FORMAT": "jsonl"}

    def desired_argv(self) -> Optional[List[str]]:
        """The child's argv for the configured state, or None when the
        server is disabled."""
        state = self.config.state
        if not state.server_enabled:
            return None
        return self.build_argv(state.mutating_enabled, state.allowed_origins)

    # ------------------------------------------------------- control loop

    def step(self) -> None:
        """One pass of the control loop; never raises."""
        try:
            with self._lock:
                self._step()
        except Exception as error:  # noqa: BLE001 — the supervisor never dies
            self.last_error = f"supervisor error: {error}"
            logger.exception("MCP supervisor step failed")

    def _step(self) -> None:
        child = self._child
        if child is not None and child.poll() is not None:
            self._on_exit(child.returncode)
        desired = self.desired_argv()
        if desired is None:
            if self._child is not None:
                logger.info("MCP server disabled; stopping the child")
                self._terminate()
            self._blocked_argv = None
            self._reset_backoff()
            return
        if self._child is not None and self._argv != desired:
            logger.info("MCP configuration changed; restarting the child")
            self._terminate()
            self._reset_backoff()
        if self._child is not None:
            if (self._ready_at is not None
                    and self._clock() - self._ready_at >= STABLE_AFTER_S):
                self._backoff = BACKOFF_INITIAL_S
            return
        if self._blocked_argv is not None:
            if self._blocked_argv == desired:
                return
            self._blocked_argv = None
            self._reset_backoff()
        if self._clock() < self._next_attempt:
            return
        self._spawn(desired)

    def _reset_backoff(self) -> None:
        self._backoff = BACKOFF_INITIAL_S
        self._next_attempt = 0.0
        self._crashed = False

    def _schedule_retry(self) -> None:
        self._next_attempt = self._clock() + self._backoff
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)

    def _spawn(self, argv: List[str]) -> None:
        if self._crashed:
            self.restart_count += 1
            self._crashed = False
        try:
            child = self._popen(argv, cwd=BACKEND_ROOT, env=self.build_env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        except Exception as error:  # noqa: BLE001 — e.g. the venv is missing
            self.last_error = f"spawn failed: {error}"
            logger.error("Could not start the MCP child: %s", error)
            self._crashed = True
            self._schedule_retry()
            return
        self._child, self._argv, self._ready_at = child, argv, None
        try:
            self._start_relays(child.stdout, child.stderr)
        except Exception:  # noqa: BLE001 — relays are best effort
            logger.exception("Could not start the MCP child log relays")
        if self._wait_ready(child):
            self.listening = True
            self._ready_at = self._clock()
            self.last_error = None
            logger.info("MCP child ready on 127.0.0.1:%d", self.port)
            return
        if child.poll() is not None:
            self._on_exit(child.returncode)
            return
        if self._stop_event.is_set():
            self._terminate()
            return
        self.last_error = (f"child not ready within {READINESS_TIMEOUT_S:.0f} s")
        logger.error("MCP child not ready within %.0f s; restarting",
                     READINESS_TIMEOUT_S)
        self._terminate()
        self._crashed = True
        self._schedule_retry()

    def _wait_ready(self, child) -> bool:
        deadline = self._clock() + READINESS_TIMEOUT_S
        while self._clock() < deadline:
            # stop() must not wait out the 20 s readiness window.
            if self._stop_event.is_set() or child.poll() is not None:
                return False
            if self._probe(self.port):
                return True
            self._sleep(READINESS_POLL_S)
        return False

    def _on_exit(self, code: Optional[int]) -> None:
        self.listening = False
        self.last_exit_code = code
        argv = self._argv
        self._child, self._argv, self._ready_at = None, None, None
        if code == EXIT_USAGE:
            self.last_error = ("child rejected its arguments (exit 2); not "
                               "restarted until the configuration changes")
            logger.error("MCP child exited with code 2 (invalid arguments)")
            self._blocked_argv = argv
            return
        self.last_error = f"child exited with code {code}"
        logger.warning("MCP child exited with code %s; restarting in %.0f s",
                       code, self._backoff)
        self._crashed = True
        self._schedule_retry()

    def _terminate(self) -> None:
        child = self._child
        self._child, self._argv, self._ready_at = None, None, None
        self.listening = False
        if child is None:
            return
        try:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=STOP_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=STOP_TIMEOUT_S)
            self.last_exit_code = child.returncode
        except Exception as error:  # noqa: BLE001 — stopping never raises
            logger.warning("Could not stop the MCP child cleanly: %s", error)

    # ---------------------------------------------------------- thread

    def _wake_up(self, *_args) -> None:
        self._wake.set()

    def start(self) -> None:
        """Start the configuration poller and the supervision thread;
        never raises. Idempotent."""
        try:
            if self._thread is not None and self._thread.is_alive():
                return
            self.config.on_change(self._wake_up)
            self.config.start()
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run,
                                            name="mcp-supervisor", daemon=True)
            self._thread.start()
        except Exception as error:  # noqa: BLE001 — never propagates (12.3)
            self.last_error = f"supervisor start failed: {error}"
            logger.exception("Could not start the MCP supervisor")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.step()
            self._wake.wait(LOOP_TICK_S)
            self._wake.clear()

    def stop(self) -> None:
        """Stop the thread, the poller and the child (SIGTERM, 10 s, SIGKILL);
        never raises."""
        try:
            self._stop_event.set()
            self._wake.set()
            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=READINESS_TIMEOUT_S + STOP_TIMEOUT_S * 2 + 1)
            self._thread = None
            with self._lock:
                self._terminate()
            self.config.stop()
        except Exception:  # noqa: BLE001 — never propagates (12.3)
            logger.exception("Could not stop the MCP supervisor cleanly")


# The process's supervisor, read by the /mcp routes (endpoints/mcp_proxy.py).
_ACTIVE: Optional[McpSupervisor] = None


def start_supervisor(backend_base_url: str, features: Iterable[str] = (),
                     component_version: str = "unknown",
                     config: Optional[McpConfig] = None) -> McpSupervisor:
    """Create the process's supervisor over a new ``McpConfig``, start it and
    make it the active one. ``McpSupervisor.start`` never raises."""
    global _ACTIVE
    supervisor = McpSupervisor(config if config is not None else McpConfig(),
                               backend_base_url, features=features,
                               component_version=component_version)
    supervisor.start()
    _ACTIVE = supervisor
    return supervisor


def active_supervisor() -> Optional[McpSupervisor]:
    """The supervisor ``start_supervisor`` created, or None."""
    return _ACTIVE
