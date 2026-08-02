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
"""Agent isolation tests for the server_setup camera-registry wiring
(camera-registry-sync task 2.9, Requirement 11.2).

A raising ``CameraDiscovery``/``EdgeSyncAgent`` constructor or ``.start()``
inside ``start_camera_registry_sync`` is swallowed by
``_start_camera_registry_sync_isolated``: the failure is logged, no
exception propagates into server setup, and the module globals
(``camera_discovery`` / ``camera_sync_agent``) stay ``None``. A fully
successful start is also covered, showing the guard does not over-swallow.

Import approach (documented per the task): ``utils.server_setup`` has heavy
module-level imports (``awsiot.greengrasscoreipc.connect()`` at import
time, gi/GStreamer, grpc, panorama, the edge-agent gRPC stubs), so the
module is imported under the same stubs ``LocalServerBaseTestCase`` uses
for the API-endpoint suites (the ``mock_gi`` / ``mock_edge_agent_pb2*``
sys.modules stubs plus a patched IPC ``connect`` and the test environment
variables). Packages that exist only on device images (awsiot, grpc,
panorama, deprecated) get guarded sys.modules stubs installed in
``setUpClass`` and removed again in ``tearDownClass`` — together with
every module this class's import of server_setup added to ``sys.modules``
— so the stubs are strictly scoped to this class and the rest of the test
session observes exactly the same import behavior as before.
``utils.digital_input_process_manager`` is imported before server_setup —
the same order ``app.py`` uses — because that is the direction in which
the digital_input/server_setup import cycle resolves.

Importing server_setup also spawns its ``camera-sync-startup`` daemon
thread; the import is performed with the Camera_Discovery constructor
stubbed to raise, so that thread terminates immediately through the very
guard under test (the import completing anyway is itself Requirement
11.2), and the thread is joined before any assertion runs. Each test then
calls ``_start_camera_registry_sync_isolated`` directly (on the test
thread, with the module globals pinned to ``None``) so the isolation
behavior is observed deterministically.
"""
import importlib
import importlib.util
import os
import sys
import threading
import types
import unittest
from unittest import mock

_STARTUP_THREAD_NAME = "camera-sync-startup"

#: Same test environment LocalServerBaseTestCase provides for the import.
_TEST_ENV = {
    "COMPONENT_WORK_PATH": "/tmp",
    "AWS_IOT_THING_NAME": "iot_thing_test",
    "LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": "comp_decomp_path_test",
    "KERNEL_ROOT_PATH": ".",
}


def _join_startup_thread(timeout: float = 15.0) -> None:
    """Wait for the import-time camera-sync startup thread to finish so
    tests observe a settled module state."""
    for thread in threading.enumerate():
        if thread.name == _STARTUP_THREAD_NAME:
            thread.join(timeout)


def _install_missing_dependency_stubs():
    """sys.modules stubs for the device-image-only packages in
    server_setup's import chain. Guarded: a really installed package
    always wins. Returns the installed module names for removal."""
    installed = []

    def _package_missing(package):
        if package in sys.modules:
            return False  # really installed or already stubbed
        return importlib.util.find_spec(package) is None

    missing = {
        package: _package_missing(package)
        for package in ("awsiot", "grpc", "panorama", "deprecated")
    }

    def _install(name, module):
        if missing[name.split(".")[0]]:
            sys.modules[name] = module
            installed.append(name)

    # awsiot: greengrasscoreipc.connect() runs at server_setup import.
    awsiot_module = types.ModuleType("awsiot")
    greengrasscoreipc = mock.MagicMock(name="awsiot.greengrasscoreipc")
    model_module = mock.MagicMock(name="awsiot.greengrasscoreipc.model")
    client_module = mock.MagicMock(name="awsiot.greengrasscoreipc.client")
    # A real class where production code subclasses the SDK type.
    client_module.SubscribeToIoTCoreStreamHandler = type(
        "SubscribeToIoTCoreStreamHandler", (), {}
    )
    greengrasscoreipc.model = model_module
    greengrasscoreipc.client = client_module
    awsiot_module.greengrasscoreipc = greengrasscoreipc
    _install("awsiot", awsiot_module)
    _install("awsiot.greengrasscoreipc", greengrasscoreipc)
    _install("awsiot.greengrasscoreipc.model", model_module)
    _install("awsiot.greengrasscoreipc.client", client_module)

    # grpc: edgeagent/lfv_edge_agent.py imports it at module level.
    _install("grpc", mock.MagicMock(name="grpc"))

    # panorama: dda_triton/message_broker_client.py imports it.
    panorama_module = types.ModuleType("panorama")
    for submodule_name in ("messagebroker", "application", "media"):
        submodule = mock.MagicMock(name="panorama." + submodule_name)
        setattr(panorama_module, submodule_name, submodule)
        _install("panorama." + submodule_name, submodule)
    _install("panorama", panorama_module)

    # deprecated: defect_detection_config uses @deprecated at class level.
    deprecated_module = types.ModuleType("deprecated")

    def _deprecated(*args, **kwargs):  # passthrough decorator
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def wrap(func):
            return func

        return wrap

    deprecated_module.deprecated = _deprecated
    _install("deprecated", deprecated_module)

    return installed


# --- fakes ---------------------------------------------------------------------


class _FakeDiscovery:
    """Stand-in for CameraDiscovery; records how it was started."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started_with = None

    def start(self, on_change=None):
        self.started_with = on_change


class _RaisingStartDiscovery(_FakeDiscovery):
    def start(self, on_change=None):
        raise RuntimeError("discovery start boom")


class _FakeAgent:
    """Stand-in for EdgeSyncAgent; records lifecycle calls."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.thing_name = "iot_thing_test"
        self.shadow_name = "dda-camera-registry"
        self.started = False
        self.applied = []
        self.discovery_changes = []

    def start(self):
        self.started = True

    def on_discovery_change(self, snapshot):
        self.discovery_changes.append(snapshot)

    def apply_desired_changes(self, changes):
        self.applied.append(dict(changes))


class _RaisingStartAgent(_FakeAgent):
    def start(self):
        raise RuntimeError("agent start boom")


class _FakeSubscription:
    """Stand-in for mqtt.SubscriptionHandler.SubscriptionHandler whose
    subscribe() returns immediately (the real one blocks forever)."""

    def __init__(self, topic_prefix, handler, publish_handler):
        self.topic_prefix = topic_prefix
        self.handler = handler
        self.publish_handler = publish_handler

    def subscribe(self):
        return None


class _FakeShadow:
    def __init__(self, state=None):
        self.state = state

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        return self.state


# --- the tests -------------------------------------------------------------------


class TestServerSetupCameraSyncIsolation(unittest.TestCase):
    """Requirement 11.2: camera-sync failures never propagate into server
    setup; Requirement 11.4 is covered by test_no_migration_smoke.py."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Everything sys.modules gains from here on (dependency stubs plus
        # the freshly imported server_setup chain) is removed again in
        # tearDownClass, so this class leaves the session's import state
        # exactly as it found it.
        cls._modules_before = set(sys.modules)
        cls._installed_stubs = _install_missing_dependency_stubs()

        # The gi / edge_agent_pb2* stubs install themselves on import,
        # exactly like local_server_base_test_case.py uses them.
        import mock_gi  # noqa: F401
        import mock_edge_agent_pb2  # noqa: F401
        import mock_edge_agent_pb2_grpc  # noqa: F401

        cls._patchers = [
            mock.patch(
                "edge_agent_pb2_grpc.EdgeAgentStub", return_value=None, create=True
            ),
            mock.patch("awsiot.greengrasscoreipc.connect"),
            mock.patch.dict(os.environ, _TEST_ENV),
            mock.patch(
                "utils.constants.DEFAULT_CAMERA_CONFIG_FILE_PATH",
                "./src/backend/utils/config/default_camera_configurations.json",
            ),
        ]
        for patcher in cls._patchers:
            patcher.start()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        for patcher in reversed(cls._patchers):
            patcher.stop()
        # Scope the stubs to this class: drop them and every module this
        # class's imports added, restoring the session's import state.
        for name in set(sys.modules) - cls._modules_before:
            sys.modules.pop(name, None)
        for name in cls._installed_stubs:
            sys.modules.pop(name, None)

    def setUp(self):
        super().setUp()
        import camera_discovery as camera_discovery_pkg
        import camera_sync as camera_sync_pkg

        self.camera_discovery_pkg = camera_discovery_pkg
        self.camera_sync_pkg = camera_sync_pkg
        self.server_setup = self._import_server_setup()

        # Snapshot the module globals and the active-agent hook, then pin
        # the globals to None so each test observes only its own call.
        self._saved_globals = (
            self.server_setup.camera_discovery,
            self.server_setup.camera_sync_agent,
        )
        self._saved_active_agent = camera_sync_pkg.get_active_agent()
        self.server_setup.camera_discovery = None
        self.server_setup.camera_sync_agent = None
        camera_sync_pkg.clear_active_agent()

    def tearDown(self):
        (
            self.server_setup.camera_discovery,
            self.server_setup.camera_sync_agent,
        ) = self._saved_globals
        if self._saved_active_agent is not None:
            self.camera_sync_pkg.set_active_agent(self._saved_active_agent)
        else:
            self.camera_sync_pkg.clear_active_agent()
        super().tearDown()

    def _import_server_setup(self):
        """Import utils.server_setup under the class-level stubs.

        On the first import of the session the CameraDiscovery constructor
        is stubbed to raise, so the module's import-time startup thread
        exercises (and dies through) the isolation guard; either way the
        thread is joined before returning. The import completing at all is
        itself Requirement 11.2: a camera-sync failure leaves server-setup
        module initialization untouched.
        """
        if "utils.server_setup" in sys.modules:
            module = sys.modules["utils.server_setup"]
            _join_startup_thread()
            return module
        boom = mock.Mock(side_effect=RuntimeError("import-time constructor stub"))
        with mock.patch.object(self.camera_discovery_pkg, "CameraDiscovery", boom):
            # app.py's import order: the digital_input/server_setup import
            # cycle only resolves in this direction.
            importlib.import_module("utils.digital_input_process_manager")
            module = importlib.import_module("utils.server_setup")
            _join_startup_thread()
        return module

    def _call_isolated_and_assert_swallowed(self):
        """Call the guard directly: it must not raise, must log the
        failure, and must leave the module globals None (11.2)."""
        with self.assertLogs("utils.server_setup", level="ERROR") as logs:
            self.server_setup._start_camera_registry_sync_isolated()
        self.assertIsNone(self.server_setup.camera_discovery)
        self.assertIsNone(self.server_setup.camera_sync_agent)
        self.assertTrue(
            any(
                "Camera registry sync failed to start" in line
                for line in logs.output
            ),
            "the startup failure was not logged: {}".format(logs.output),
        )

    def test_raising_discovery_constructor_is_isolated(self):
        with mock.patch.object(
            self.camera_discovery_pkg,
            "CameraDiscovery",
            mock.Mock(side_effect=RuntimeError("discovery constructor boom")),
        ):
            self._call_isolated_and_assert_swallowed()
        self.assertIsNone(self.camera_sync_pkg.get_active_agent())

    def test_raising_agent_constructor_is_isolated(self):
        discovery = _FakeDiscovery()
        with mock.patch.object(
            self.camera_discovery_pkg, "CameraDiscovery", lambda **kw: discovery
        ), mock.patch.object(
            self.camera_sync_pkg,
            "EdgeSyncAgent",
            mock.Mock(side_effect=RuntimeError("agent constructor boom")),
        ):
            self._call_isolated_and_assert_swallowed()
        self.assertIsNone(discovery.started_with)  # discovery never started
        self.assertIsNone(self.camera_sync_pkg.get_active_agent())

    def test_raising_agent_start_is_isolated(self):
        discovery = _FakeDiscovery()
        agent = _RaisingStartAgent()
        with mock.patch.object(
            self.camera_discovery_pkg, "CameraDiscovery", lambda **kw: discovery
        ), mock.patch.object(
            self.camera_sync_pkg, "EdgeSyncAgent", lambda *a, **kw: agent
        ), mock.patch.object(
            self.camera_sync_pkg,
            "make_shadow_stream_handler",
            lambda a: mock.Mock(),
        ), mock.patch(
            "mqtt.SubscriptionHandler.SubscriptionHandler", _FakeSubscription
        ):
            self._call_isolated_and_assert_swallowed()
        self.assertIsNone(discovery.started_with)  # never reached
        self.assertIsNone(self.camera_sync_pkg.get_active_agent())

    def test_raising_discovery_start_is_isolated(self):
        discovery = _RaisingStartDiscovery()
        agent = _FakeAgent()
        with mock.patch.object(
            self.camera_discovery_pkg, "CameraDiscovery", lambda **kw: discovery
        ), mock.patch.object(
            self.camera_sync_pkg, "EdgeSyncAgent", lambda *a, **kw: agent
        ), mock.patch.object(
            self.camera_sync_pkg,
            "make_shadow_stream_handler",
            lambda a: mock.Mock(),
        ), mock.patch(
            "mqtt.SubscriptionHandler.SubscriptionHandler", _FakeSubscription
        ):
            self._call_isolated_and_assert_swallowed()
        self.assertTrue(agent.started)  # failure struck after agent start...
        # ...but the guard still isolated it: no active agent was published.
        self.assertIsNone(self.camera_sync_pkg.get_active_agent())

    def test_successful_start_sets_globals_and_hooks(self):
        """The guard does not over-swallow: a clean start publishes the
        discovery/agent globals, wires discovery on_change through to the
        agent, registers the CRUD-hook active agent, and applies desired
        changes pending in the shadow."""
        discovery = _FakeDiscovery()
        agent = _FakeAgent()
        pending = {"cfg-1": {"op": "delete", "portalChangeId": "pc-1"}}
        shadow = _FakeShadow({"desired": {"changes": dict(pending)}})
        with mock.patch.object(
            self.camera_discovery_pkg, "CameraDiscovery", lambda **kw: discovery
        ), mock.patch.object(
            self.camera_sync_pkg, "EdgeSyncAgent", lambda *a, **kw: agent
        ), mock.patch.object(
            self.camera_sync_pkg,
            "make_shadow_stream_handler",
            lambda a: mock.Mock(),
        ), mock.patch(
            "mqtt.SubscriptionHandler.SubscriptionHandler", _FakeSubscription
        ), mock.patch.object(
            self.server_setup, "iot_shadow_accessor", shadow
        ):
            self.server_setup._start_camera_registry_sync_isolated()

        self.assertIs(self.server_setup.camera_discovery, discovery)
        self.assertIs(self.server_setup.camera_sync_agent, agent)
        self.assertTrue(agent.started)
        # Discovery was started with an on_change that reaches the agent
        # (server_setup wraps it so extra listeners can be dispatched too).
        self.assertIsNotNone(discovery.started_with)
        snapshot = object()
        discovery.started_with(snapshot)
        self.assertEqual(agent.discovery_changes, [snapshot])
        self.assertIs(self.camera_sync_pkg.get_active_agent(), agent)
        self.assertEqual(agent.applied, [pending])
