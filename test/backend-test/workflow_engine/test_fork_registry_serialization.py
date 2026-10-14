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
"""Regression tests for the fork-vs-registry-scan backend deadlock.

Production incident (jetson-thor1, LocalServer.arm64JP7 1.0.17): one
MQTT publish triggered three subscribing workflow executions
simultaneously and the whole backend wedged — API unresponsive for
15+ minutes, identical py-spy stacks across dumps 15 minutes apart:

- One thread blocked INSIDE ``subprocess.Popen`` of a Custom Python
  handler (``python_bridge._start_locked``). The Popen uses
  ``preexec_fn``, forcing the fork(+exec) path; CPython's before-fork
  handlers acquire the **import lock**.
- Two threads blocked in ``gst_plugins._scan_registry`` (via
  ``missing_factories`` from the executor's factory preflight). The gi
  import path holds the **import lock** while calling into
  GStreamer/GLib internals, which take their own locks.

Classic lock-order inversion between the CPython import lock and
GStreamer/GLib internals, reachable only when a handler-subprocess
fork overlaps concurrent registry scans. The fix serializes the two
critical sections against each other with one process-wide lock,
``gst_plugins.FORK_REGISTRY_LOCK``. These tests pin:

1. the lock is shared between the two modules and ``_scan_registry``
   holds it inside its gi/registry critical section;
2. ``CustomPythonBridge.start()`` holds it during ``subprocess.Popen``;
3. behaviorally, a registry scan cannot enter its critical section
   while a bridge spawn is in flight (event-sequenced, no GStreamer).

No gi/GStreamer at module level: the registry critical sections are
exercised through a stub ``gi`` injected into ``sys.modules``,
mirroring how sibling tests keep this suite runnable without gi.
"""

import os
import subprocess
import sys
import threading
import types

from workflow_engine import gst_plugins, python_bridge
from workflow_engine.gst_plugins import FORK_REGISTRY_LOCK
from workflow_engine.python_bridge import CustomPythonBridge

NODE_ID = "pynode"


def _install_gi_stub(monkeypatch, on_scan):
    """Install a fake ``gi``/``gi.repository.Gst`` in ``sys.modules``.

    ``on_scan(plugin_dir)`` runs inside ``Gst.Registry.get().scan_path``
    — i.e. inside ``_scan_registry``'s critical section — and its return
    value becomes the scan result.
    """
    gst = types.SimpleNamespace(
        init=lambda _argv: None,
        Registry=types.SimpleNamespace(
            get=lambda: types.SimpleNamespace(scan_path=on_scan)
        ),
    )
    gi_module = types.ModuleType("gi")
    gi_module.require_version = lambda _name, _version: None
    repository = types.ModuleType("gi.repository")
    repository.Gst = gst
    gi_module.repository = repository
    monkeypatch.setitem(sys.modules, "gi", gi_module)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)


def write_handler(tmp_path):
    path = os.path.join(str(tmp_path), "handler.py")
    with open(path, "w") as handle:
        handle.write("def handle(frame, metadata):\n    return None\n")
    return path


class _FakeProcess:
    pid = 4242
    stdin = None
    stdout = None
    stderr = None

    def poll(self):
        return None


class TestSharedLock:
    def test_python_bridge_uses_the_gst_plugins_lock(self):
        assert python_bridge.FORK_REGISTRY_LOCK is FORK_REGISTRY_LOCK
        assert gst_plugins.FORK_REGISTRY_LOCK is FORK_REGISTRY_LOCK

    def test_scan_registry_holds_the_lock_in_its_critical_section(
        self, monkeypatch
    ):
        observed = []

        def probe(plugin_dir):
            observed.append((plugin_dir, FORK_REGISTRY_LOCK.locked()))
            return True

        _install_gi_stub(monkeypatch, probe)
        assert gst_plugins._scan_registry("/some/plugins") is True
        assert observed == [("/some/plugins", True)]
        # Released on exit — not leaked.
        assert not FORK_REGISTRY_LOCK.locked()


class TestBridgeSpawnHoldsLock:
    def test_start_holds_the_lock_during_popen(self, monkeypatch, tmp_path):
        observed = []

        def fake_popen(*args, **kwargs):
            observed.append(FORK_REGISTRY_LOCK.locked())
            return _FakeProcess()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        bridge = CustomPythonBridge(NODE_ID, write_handler(tmp_path))
        bridge.start()
        assert observed == [True]
        # Released on exit — not leaked.
        assert not FORK_REGISTRY_LOCK.locked()


class TestSpawnAndScanAreMutuallyExclusive:
    def test_scan_waits_for_an_in_flight_spawn(self, monkeypatch, tmp_path):
        """With a spawn parked inside Popen (fork window), a concurrent
        registry scan must not enter its critical section until the
        spawn completes. Event-sequenced: the scan thread is started
        only once Popen is provably in flight, and Popen is released
        only after; the recorded order is then deterministic."""
        popen_entered = threading.Event()
        release_popen = threading.Event()
        order = []
        order_lock = threading.Lock()

        def fake_popen(*args, **kwargs):
            popen_entered.set()
            assert release_popen.wait(timeout=10), "test wiring: never released"
            with order_lock:
                order.append("spawn-completed")
            return _FakeProcess()

        def scan_probe(plugin_dir):
            with order_lock:
                order.append("scan-entered")
            return True

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        _install_gi_stub(monkeypatch, scan_probe)

        bridge = CustomPythonBridge(NODE_ID, write_handler(tmp_path))
        spawner = threading.Thread(target=bridge.start)
        spawner.start()
        try:
            assert popen_entered.wait(timeout=10)
            # The spawn now holds FORK_REGISTRY_LOCK inside Popen.
            scan_results = []
            scanner = threading.Thread(
                target=lambda: scan_results.append(
                    gst_plugins._scan_registry("/some/plugins")
                )
            )
            scanner.start()
            release_popen.set()
            scanner.join(timeout=10)
            assert not scanner.is_alive()
        finally:
            release_popen.set()
            spawner.join(timeout=10)
        assert not spawner.is_alive()
        assert scan_results == [True]
        assert order == ["spawn-completed", "scan-entered"]
