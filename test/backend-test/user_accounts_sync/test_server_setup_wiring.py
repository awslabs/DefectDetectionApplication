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
"""Wiring tests for the server_setup user-accounts-sync + local-login-config
section (portal-user-manager task 10.3, Requirement 11.1).

Mirrors ``camera_sync/test_server_setup_isolation.py``: ``utils.server_setup``
is imported under the same device-image dependency stubs, with the wiring
constructors stubbed to raise so the import-time startup threads terminate
immediately through the guards under test; each test then calls the isolated
entry points directly on the test thread.

Covered:

- a raising ``UserAccountsSyncAgent`` construction/start is swallowed by
  ``_start_user_accounts_sync_isolated``: logged, no exception propagates,
  the ``user_accounts_sync_agent`` module global stays ``None``,
- a successful start publishes the agent global, performs the startup
  catch-up (``agent.start()``), and subscribes a ``SubscriptionHandler``
  built from ``delta_topic_prefix``/``make_shadow_stream_handler`` with the
  module's ``publish_handler`` on a daemon thread,
- ``_start_local_login_config_isolated`` starts the ``LocalLoginConfig``
  singleton poller (Requirement 11.1) and swallows + logs any failure
  (local login then stays fail-safe disabled).
"""
import importlib
import importlib.util
import os
import sys
import threading
import types
import unittest
from unittest import mock

_STARTUP_THREAD_NAMES = (
    "camera-sync-startup",
    "user-accounts-sync-startup",
    "local-login-config-startup",
)

#: Same test environment LocalServerBaseTestCase provides for the import.
_TEST_ENV = {
    "COMPONENT_WORK_PATH": "/tmp",
    "AWS_IOT_THING_NAME": "iot_thing_test",
    "LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": "comp_decomp_path_test",
    "KERNEL_ROOT_PATH": ".",
}


def _join_startup_threads(timeout: float = 15.0) -> None:
    """Wait for the import-time startup threads so tests observe a settled
    module state."""
    for thread in threading.enumerate():
        if thread.name in _STARTUP_THREAD_NAMES:
            thread.join(timeout)


def _install_missing_dependency_stubs():
    """sys.modules stubs for the device-image-only packages in
    server_setup's import chain (same helper as the camera_sync isolation
    suite). Guarded: a really installed package always wins. Returns the
    installed module names for removal."""
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


class _FakeAgent:
    """Stand-in for UserAccountsSyncAgent; records lifecycle calls."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.thing_name = "iot_thing_test"
        self.shadow_name = "dda-user-accounts"
        self.started = False

    def start(self):
        self.started = True


class _RaisingStartAgent(_FakeAgent):
    def start(self):
        raise RuntimeError("agent start boom")


class _FakeSubscription:
    """Stand-in for mqtt.SubscriptionHandler.SubscriptionHandler whose
    subscribe() returns immediately (the real one blocks forever) and
    signals that it ran."""

    last_instance = None

    def __init__(self, topic_prefix, handler, publish_handler):
        self.topic_prefix = topic_prefix
        self.handler = handler
        self.publish_handler = publish_handler
        self.subscribed = threading.Event()
        _FakeSubscription.last_instance = self


    def subscribe(self):
        self.subscribed.set()


# --- the tests -------------------------------------------------------------------


class TestServerSetupUserAccountsSyncWiring(unittest.TestCase):
    """Task 10.3 / Requirement 11.1: the user-accounts sync agent and the
    LocalLoginConfig poller are started from server_setup on daemon threads
    behind guards that never let a failure reach LocalServer startup."""

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
        import user_accounts_sync as user_accounts_sync_pkg

        self.user_accounts_sync_pkg = user_accounts_sync_pkg
        self.local_auth_config = importlib.import_module("local_auth.config")
        self.server_setup = self._import_server_setup()

        # Snapshot the module global, then pin it to None so each test
        # observes only its own call.
        self._saved_agent_global = self.server_setup.user_accounts_sync_agent
        self.server_setup.user_accounts_sync_agent = None

    def tearDown(self):
        self.server_setup.user_accounts_sync_agent = self._saved_agent_global
        super().tearDown()

    def _import_server_setup(self):
        """Import utils.server_setup under the class-level stubs.

        On the first import of the session every startup-thread constructor
        is stubbed to raise (camera discovery, the user-accounts agent) and
        the LocalLoginConfig singleton accessor is stubbed, so the module's
        import-time startup threads terminate immediately through the very
        guards under test — the import completing anyway is itself the
        crash-isolation requirement. The threads are joined before any
        assertion runs.
        """
        if "utils.server_setup" in sys.modules:
            module = sys.modules["utils.server_setup"]
            _join_startup_threads()
            return module
        camera_discovery_pkg = importlib.import_module("camera_discovery")
        boom = RuntimeError("import-time constructor stub")
        with mock.patch.object(
            camera_discovery_pkg, "CameraDiscovery", mock.Mock(side_effect=boom)
        ), mock.patch.object(
            self.user_accounts_sync_pkg,
            "UserAccountsSyncAgent",
            mock.Mock(side_effect=boom),
        ), mock.patch.object(
            self.local_auth_config,
            "get_local_login_config",
            mock.Mock(return_value=mock.Mock()),
        ):
            # app.py's import order: the digital_input/server_setup import
            # cycle only resolves in this direction.
            importlib.import_module("utils.digital_input_process_manager")
            module = importlib.import_module("utils.server_setup")
            _join_startup_threads()
        return module

    def _call_isolated_and_assert_swallowed(self):
        """Call the guard directly: it must not raise, must log the
        failure, and must leave the module global None."""
        with self.assertLogs("utils.server_setup", level="ERROR") as logs:
            self.server_setup._start_user_accounts_sync_isolated()
        self.assertIsNone(self.server_setup.user_accounts_sync_agent)
        self.assertTrue(
            any(
                "User accounts sync failed to start" in line
                for line in logs.output
            ),
            "the startup failure was not logged: {}".format(logs.output),
        )

    # --- user-accounts sync agent -----------------------------------------

    def test_raising_agent_constructor_is_isolated(self):
        with mock.patch.object(
            self.user_accounts_sync_pkg,
            "UserAccountsSyncAgent",
            mock.Mock(side_effect=RuntimeError("agent constructor boom")),
        ):
            self._call_isolated_and_assert_swallowed()

    def test_raising_agent_start_is_isolated(self):
        agent = _RaisingStartAgent()
        with mock.patch.object(
            self.user_accounts_sync_pkg,
            "UserAccountsSyncAgent",
            lambda *a, **kw: agent,
        ), mock.patch.object(
            self.user_accounts_sync_pkg,
            "make_shadow_stream_handler",
            lambda a: mock.Mock(),
        ), mock.patch(
            "mqtt.SubscriptionHandler.SubscriptionHandler", _FakeSubscription
        ):
            self._call_isolated_and_assert_swallowed()

    def test_successful_start_sets_global_and_subscribes(self):
        """The guard does not over-swallow: a clean start publishes the
        agent global, runs the startup catch-up, and subscribes to the
        dda-user-accounts shadow delta topic with the module's
        publish_handler on a daemon thread."""
        agent = _FakeAgent()
        handler = object()
        _FakeSubscription.last_instance = None
        with mock.patch.object(
            self.user_accounts_sync_pkg,
            "UserAccountsSyncAgent",
            lambda *a, **kw: agent,
        ), mock.patch.object(
            self.user_accounts_sync_pkg,
            "make_shadow_stream_handler",
            lambda a: handler,
        ), mock.patch(
            "mqtt.SubscriptionHandler.SubscriptionHandler", _FakeSubscription
        ):
            self.server_setup._start_user_accounts_sync_isolated()

        self.assertIs(self.server_setup.user_accounts_sync_agent, agent)
        self.assertTrue(agent.started)  # startup catch-up ran

        subscription = _FakeSubscription.last_instance
        self.assertIsNotNone(subscription)
        self.assertEqual(
            subscription.topic_prefix,
            "$aws/things/iot_thing_test/shadow/name/dda-user-accounts/update/",
        )
        self.assertIs(subscription.handler, handler)
        self.assertIs(
            subscription.publish_handler, self.server_setup.publish_handler
        )
        # subscribe() runs on its own daemon thread.
        self.assertTrue(
            subscription.subscribed.wait(10),
            "the shadow subscription thread never ran subscribe()",
        )

    # --- LocalLoginConfig poller (Requirement 11.1) ------------------------

    def test_local_login_config_poller_is_started(self):
        config = mock.Mock()
        with mock.patch.object(
            self.local_auth_config,
            "get_local_login_config",
            mock.Mock(return_value=config),
        ):
            with self.assertLogs("utils.server_setup", level="INFO") as logs:
                self.server_setup._start_local_login_config_isolated()
        config.start.assert_called_once_with()
        self.assertTrue(
            any(
                "Local login configuration poller started" in line
                for line in logs.output
            ),
            "the poller start was not logged: {}".format(logs.output),
        )

    def test_local_login_config_failure_is_isolated(self):
        config = mock.Mock()
        config.start.side_effect = RuntimeError("poller boom")
        with mock.patch.object(
            self.local_auth_config,
            "get_local_login_config",
            mock.Mock(return_value=config),
        ):
            with self.assertLogs("utils.server_setup", level="ERROR") as logs:
                # must not raise
                self.server_setup._start_local_login_config_isolated()
        self.assertTrue(
            any(
                "Local login configuration poller failed to start" in line
                for line in logs.output
            ),
            "the poller failure was not logged: {}".format(logs.output),
        )
