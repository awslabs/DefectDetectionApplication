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
"""The Edge MCP supervisor, configuration view and status document.

Feature: mcp-servers (design §5 ``__init__.py``, ``config.py``,
``supervisor.py`` and ``status.py`` rows, Decision point 3 "Process model",
Data Models "``GET /mcp/status``"; tasks.md 10.5). Requirements 10.7, 12.1,
12.2, 12.3, 12.8, 15.3.

The child process, the clock and the readiness probe are fakes; nothing is
spawned.
"""
import os
import signal
import subprocess
import threading

import pytest

import mcp_edge
from mcp_edge import config as mcp_config
from mcp_edge import status as mcp_status
from mcp_edge import supervisor as sup
from mcp_edge.config import McpConfig, McpState


# ------------------------------------------------------------------ fakes

class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeChild:
    def __init__(self, argv, ignore_sigterm=False):
        self.argv = argv
        self.returncode = None
        self.stdout = object()
        self.stderr = object()
        self.signals = []
        self.ignore_sigterm = ignore_sigterm

    def poll(self):
        return self.returncode

    def exit(self, code):
        self.returncode = code

    def terminate(self):
        self.signals.append(signal.SIGTERM)
        if not self.ignore_sigterm:
            self.returncode = -signal.SIGTERM

    def kill(self):
        self.signals.append(signal.SIGKILL)
        self.returncode = -signal.SIGKILL

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("child", timeout)
        return self.returncode


class FakePopen:
    def __init__(self, ignore_sigterm=False, fail=False):
        self.calls = []
        self.children = []
        self.ignore_sigterm = ignore_sigterm
        self.fail = fail

    def __call__(self, argv, **kwargs):
        if self.fail:
            raise FileNotFoundError("no such file: venv python")
        self.calls.append((argv, kwargs))
        child = FakeChild(argv, self.ignore_sigterm)
        self.children.append(child)
        return child

    @property
    def last(self):
        return self.children[-1]


class FakeConfig:
    def __init__(self, enabled=True, mutating=False, origins=()):
        self.state = McpState(enabled, mutating, tuple(origins))
        self.callbacks = []
        self.started = 0
        self.stopped = 0

    def set(self, enabled=True, mutating=False, origins=()):
        self.state = McpState(enabled, mutating, tuple(origins))

    def on_change(self, callback):
        self.callbacks.append(callback)

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


def make(config=None, ready=True, features=("vllm",), **kwargs):
    clock = FakeClock()
    popen = kwargs.pop("popen", FakePopen())
    readiness = {"ready": ready, "probes": 0}

    def probe(port):
        readiness["probes"] += 1
        return readiness["ready"]

    supervisor = sup.McpSupervisor(
        config or FakeConfig(), "https://127.0.0.1:5443", features=features,
        version="1.0.0", component_version="1.0.130", popen=popen, probe=probe,
        clock=clock, sleep=clock.sleep, start_relays=lambda out, err: [], **kwargs)
    return supervisor, popen, clock, readiness


# ------------------------------------------------------------- constants

def test_package_constants():
    assert mcp_edge.__version__ == "1.0.0"
    assert mcp_edge.DELIVERED_PHASES == (1,)
    assert mcp_edge.VENV_PYTHON == "/opt/dda/mcp-venv/bin/python"
    assert mcp_edge.CHILD_PORT == 5790


def test_backend_root_is_the_parent_of_the_package():
    expected = os.path.dirname(os.path.dirname(os.path.abspath(mcp_edge.__file__)))
    assert sup.BACKEND_ROOT == expected
    assert os.path.isdir(os.path.join(sup.BACKEND_ROOT, "mcp_edge"))


# ------------------------------------------------------------- spawning

def test_spawn_argv_env_cwd():
    supervisor, popen, _, _ = make(FakeConfig(enabled=True, mutating=True,
                                              origins=("https://hmi.example.com",)))
    supervisor.step()
    argv, kwargs = popen.calls[0]
    assert argv == ["/opt/dda/mcp-venv/bin/python", "-m", "mcp_edge.child",
                    "--port", "5790", "--backend-url", "https://127.0.0.1:5443",
                    "--server-version", "1.0.0+1.0.130",
                    "--allowed-origins", "https://hmi.example.com",
                    "--features", "vllm", "--mutating-enabled"]
    assert kwargs["cwd"] == sup.BACKEND_ROOT
    assert kwargs["env"] == {"PYTHONPATH": sup.BACKEND_ROOT,
                             "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                             "DDA_MCP_LOG_FORMAT": "jsonl"}
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["start_new_session"] is True
    assert supervisor.listening is True


def test_minimal_argv_without_optional_flags():
    supervisor, popen, _, _ = make(FakeConfig(enabled=True), features=())
    supervisor.step()
    assert popen.calls[0][0] == [
        "/opt/dda/mcp-venv/bin/python", "-m", "mcp_edge.child", "--port", "5790",
        "--backend-url", "https://127.0.0.1:5443", "--server-version",
        "1.0.0+1.0.130"]


def test_environment_inherits_no_aws_or_greengrass_variables(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake")
    monkeypatch.setenv("SVCUID", "fake")
    monkeypatch.setenv("AWS_GG_NUCLEUS_DOMAIN_SOCKET_FILEPATH_FOR_COMPONENT", "/x")
    assert set(sup.McpSupervisor.build_env()) == {"PYTHONPATH", "PATH",
                                                  "DDA_MCP_LOG_FORMAT"}


def test_unknown_features_are_not_passed():
    supervisor, _, _, _ = make(features=("vllm", "other"))
    assert supervisor.features == ("vllm",)


def test_disabled_never_spawns():
    supervisor, popen, _, _ = make(FakeConfig(enabled=False))
    supervisor.step()
    assert popen.calls == []
    assert supervisor.listening is False


# ------------------------------------------------------------- readiness

def test_not_ready_within_20_s_is_restarted_with_backoff():
    supervisor, popen, clock, readiness = make(ready=False)
    start = clock.now
    supervisor.step()
    assert clock.now - start >= sup.READINESS_TIMEOUT_S
    assert popen.last.signals == [signal.SIGTERM]
    assert supervisor.listening is False
    assert "not ready within 20 s" in supervisor.last_error
    readiness["ready"] = True
    supervisor.step()                       # inside the 1 s backoff: no spawn
    assert len(popen.calls) == 1
    clock.now += 1.0
    supervisor.step()
    assert len(popen.calls) == 2
    assert supervisor.restart_count == 1
    assert supervisor.listening is True


def test_child_exiting_during_readiness():
    popen = FakePopen()
    supervisor, _, clock, readiness = make(ready=False, popen=popen)
    original = popen.__call__

    def exiting(argv, **kwargs):
        child = original(argv, **kwargs)
        child.exit(1)
        return child

    supervisor._popen = exiting
    supervisor.step()
    assert supervisor.last_exit_code == 1
    assert supervisor.last_error == "child exited with code 1"


# --------------------------------------------------------------- backoff

def test_crash_backoff_doubles_to_60_s():
    supervisor, popen, clock, _ = make()
    supervisor.step()
    delays = []
    for _ in range(9):
        popen.last.exit(1)
        crashed_at = clock.now
        supervisor.step()                   # records the exit, schedules
        spawned = len(popen.calls)
        while len(popen.calls) == spawned:
            clock.now += 0.5
            supervisor.step()
        delays.append(clock.now - crashed_at)
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]
    assert supervisor.restart_count == 9
    assert supervisor.last_exit_code == 1


def test_backoff_resets_after_a_stable_run():
    supervisor, popen, clock, _ = make()
    supervisor.step()
    for _ in range(3):
        popen.last.exit(1)
        supervisor.step()
        clock.now += 10
        supervisor.step()
    assert supervisor._backoff == 8.0
    clock.now += sup.STABLE_AFTER_S
    supervisor.step()
    assert supervisor._backoff == 1.0


def test_usage_exit_is_not_retried_until_the_configuration_changes():
    config = FakeConfig(enabled=True, origins=("not a url",))
    supervisor, popen, clock, _ = make(config)
    supervisor.step()
    popen.last.exit(2)
    supervisor.step()
    clock.now += 600
    supervisor.step()
    assert len(popen.calls) == 1
    assert supervisor.last_exit_code == 2
    assert "exit 2" in supervisor.last_error
    config.set(enabled=True, origins=())
    supervisor.step()
    assert len(popen.calls) == 2


def test_spawn_failure_is_recorded_and_retried():
    popen = FakePopen(fail=True)
    supervisor, _, clock, _ = make(popen=popen)
    supervisor.step()
    assert supervisor.last_error.startswith("spawn failed:")
    popen.fail = False
    clock.now += 1.0
    supervisor.step()
    assert len(popen.calls) == 1
    assert supervisor.restart_count == 1


# -------------------------------------------------- configuration changes

@pytest.mark.parametrize("change", [
    dict(enabled=True, mutating=True),
    dict(enabled=True, origins=("https://hmi.example.com",)),
])
def test_change_restarts_the_child(change):
    config = FakeConfig(enabled=True)
    supervisor, popen, _, _ = make(config)
    supervisor.step()
    first = popen.last
    config.set(**change)
    supervisor.step()
    assert first.signals == [signal.SIGTERM]
    assert len(popen.calls) == 2
    assert supervisor.restart_count == 0     # a requested restart, not a crash
    assert ("--mutating-enabled" in popen.last.argv) == change.get("mutating", False)


def test_unchanged_configuration_keeps_the_child():
    supervisor, popen, _, _ = make()
    for _ in range(5):
        supervisor.step()
    assert len(popen.calls) == 1
    assert popen.last.signals == []


def test_disable_stops_the_child():
    config = FakeConfig(enabled=True)
    supervisor, popen, _, _ = make(config)
    supervisor.step()
    config.set(enabled=False)
    supervisor.step()
    assert popen.last.signals == [signal.SIGTERM]
    assert supervisor.listening is False
    supervisor.step()
    assert len(popen.calls) == 1


def test_restart_requested_rule():
    on = McpState(True, False, ())
    assert sup.restart_requested(on, McpState(True, True, ())) is True
    assert sup.restart_requested(on, McpState(True, False, ("https://a",))) is True
    assert sup.restart_requested(on, on) is False
    assert sup.restart_requested(on, McpState(False, True, ())) is False
    assert sup.restart_requested(McpState(False, False, ()),
                                 McpState(True, True, ())) is False


# ------------------------------------------------------------------ stop

def test_stop_sends_sigterm_then_sigkill():
    popen = FakePopen(ignore_sigterm=True)
    config = FakeConfig()
    supervisor, _, _, _ = make(config, popen=popen)
    supervisor.step()
    supervisor.stop()
    assert popen.last.signals == [signal.SIGTERM, signal.SIGKILL]
    assert supervisor.last_exit_code == -signal.SIGKILL
    assert config.stopped == 1


def test_stop_before_start_never_raises():
    supervisor, _, _, _ = make()
    supervisor.stop()


def test_step_never_raises():
    class BrokenConfig(FakeConfig):
        @property
        def state(self):
            raise RuntimeError("configuration exploded")

        @state.setter
        def state(self, value):
            pass

    supervisor, popen, _, _ = make(BrokenConfig())
    supervisor.step()
    assert popen.calls == []
    assert "configuration exploded" in supervisor.last_error


def test_start_runs_the_thread_and_the_poller():
    config = FakeConfig()
    popen = FakePopen()
    spawned = threading.Event()
    original = popen.__call__

    def recording(argv, **kwargs):
        child = original(argv, **kwargs)
        spawned.set()
        return child

    supervisor = sup.McpSupervisor(config, "http://127.0.0.1:5000",
                                   popen=recording, probe=lambda port: True,
                                   start_relays=lambda out, err: [])
    supervisor.start()
    try:
        assert spawned.wait(5)
        assert config.started == 1
        assert config.callbacks == [supervisor._wake_up]
        supervisor.start()                   # idempotent
        assert config.started == 1
    finally:
        supervisor.stop()
    assert popen.last.signals == [signal.SIGTERM]


def test_start_failure_never_raises():
    class ExplodingConfig(FakeConfig):
        def start(self):
            raise RuntimeError("no IPC")

    supervisor, _, _, _ = make(ExplodingConfig())
    supervisor.start()
    assert "no IPC" in supervisor.last_error


# ------------------------------------------------------------- McpConfig

def test_config_parsing_and_callbacks():
    values = iter([
        {"McpServerEnabled": "true", "McpMutatingToolsEnabled": " TRUE ",
         "McpAllowedOrigins": "https://a.example.com, ,https://b.example.com:8443"},
        {"McpServerEnabled": "yes", "McpMutatingToolsEnabled": True},
        RuntimeError("IPC down"),
        {"McpServerEnabled": True},
    ])

    def read():
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    config = McpConfig(read_config=read)
    changes = []
    config.on_change(lambda previous, current: changes.append(current))
    assert config.refresh() == McpState(
        True, True, ("https://a.example.com", "https://b.example.com:8443"))
    assert config.allowed_origins == ["https://a.example.com",
                                      "https://b.example.com:8443"]
    assert config.refresh() == McpState(False, True, ())
    assert config.refresh() == mcp_config.DISABLED_STATE
    assert config.refresh() == McpState(True, False, ())
    assert changes == [McpState(True, True, ("https://a.example.com",
                                             "https://b.example.com:8443")),
                       McpState(False, True, ()), mcp_config.DISABLED_STATE,
                       McpState(True, False, ())]


def test_config_starts_disabled_and_reads_through_the_shared_reader(monkeypatch):
    from local_auth import config as local_auth_config

    monkeypatch.setattr(local_auth_config, "read_component_config_via_ipc",
                        lambda: {"McpServerEnabled": "true"})
    config = McpConfig()
    assert config.state == mcp_config.DISABLED_STATE
    config.start()
    try:
        assert config.server_enabled is True
        assert config.mutating_enabled is False
    finally:
        config.stop()


def test_non_dict_configuration_is_disabled():
    assert mcp_config.parse_state(None) == mcp_config.DISABLED_STATE
    assert mcp_config.parse_state(["McpServerEnabled"]) == mcp_config.DISABLED_STATE
    assert mcp_config.parse_allowed_origins(42) == ()


def test_callback_failure_does_not_stop_the_refresh():
    config = McpConfig(read_config=lambda: {"McpServerEnabled": "true"})

    def broken(previous, current):
        raise RuntimeError("callback exploded")

    config.on_change(broken)
    assert config.refresh().server_enabled is True


# ---------------------------------------------------------------- status

def test_build_status():
    config = FakeConfig(enabled=True, mutating=False)
    supervisor, _, _, _ = make(config)
    supervisor.step()
    assert mcp_status.build_status(config, supervisor, "1.0.0", "1.0.130") == {
        "enabled": True, "mutatingToolsEnabled": False, "listening": True,
        "endpoint": "/mcp", "transport": "streamable-http",
        "protocolRevisions": ["2026-07-28", "2025-11-25", "2025-06-18",
                              "2025-03-26"],
        "serverVersion": "1.0.0", "deliveredPhases": [1],
        "componentVersion": "1.0.130", "features": ["vllm"], "restartCount": 0,
        "lastExitCode": None, "lastError": None}


def test_build_status_without_a_supervisor():
    status = mcp_status.build_status(FakeConfig(enabled=False), None, "1.0.0", None)
    assert status["listening"] is False
    assert status["features"] == []
    assert status["restartCount"] == 0
