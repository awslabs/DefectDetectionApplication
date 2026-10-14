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
"""asyncua client lifecycle and end-to-end tests for the OPC UA nodes.

dependabot-remediation replaced python-opcua (unmaintained; advisory
GHSA-mfpj-3qhm-976m has no fixed release) with asyncua's synchronous
client. asyncua brings behaviors the python-opcua code never had to
handle, pinned here:

- ``asyncua.sync.Client`` starts a NON-daemon event-loop thread in its
  constructor and only ``disconnect()`` stops it, so every exit path of the
  ``opcua_write`` writer and of the trigger worker's session build must
  disconnect, including a failed ``connect()``. Otherwise each attempt
  leaks a thread and the LocalServer process can no longer exit.
- Subscription callbacks run ON that loop, and asyncua also calls
  ``status_change_notification`` on the handler.
- asyncua warns on every connect to a server that revises the requested
  session timeout; the writer connects once per write, so that record is
  demoted to DEBUG.

The first half drives the production code against scripted fakes at the
``asyncua`` import boundary and the worker's ``client_factory`` seam (always
runs). The second half runs the real writer and the real trigger worker
against an in-process ``asyncua.sync.Server`` on localhost (skipped where
asyncua is not installed).
"""
import logging
import socket
import sys
import threading
import time
import types
from datetime import datetime
from unittest.mock import patch

import pytest

from workflow_engine.output_bindings import (
    ASYNCUA_CLIENT_LOGGER,
    _default_opcua_writer,
    _new_opcua_client,
)
from workflow_engine.trigger_runtime import (
    HEALTH_POLLING,
    HEALTH_SUBSCRIBED,
    MECHANISM_POLL,
    MECHANISM_SUBSCRIBE,
    OpcuaSubscribeWorker,
    TriggerHealth,
    _OpcuaDataChangeHandler,
)

ENDPOINT = "opc.tcp://plc.local:4840"
DATA_NODE = "ns=2;s=DefectFlag"
TRIGGER_ID = "trig-opcua-asyncua"

CERT_SECURITY = {
    "security_policy": "Basic256Sha256",
    "client_cert_path": "/certs/client.der",
    "client_key_path": "/certs/client.key",
}

#: Upper bound for cross-thread waits against the local server; the
#: expected events land in well under a second.
WAIT_SECONDS = 15.0


# ---------------------------------------------------------------------------
# Scripted fakes
# ---------------------------------------------------------------------------


class _Script:
    """Failure script shared by every client it builds; exposes the fake
    ``asyncua`` modules (writer side) and a ``client_factory`` (worker
    side)."""

    def __init__(self, connect_error=None, disconnect_error=None,
                 security_error=None):
        self.connect_error = connect_error
        self.disconnect_error = disconnect_error
        self.security_error = security_error
        self.clients = []

    def modules(self):
        """``sys.modules`` entries for a fake ``asyncua`` whose sync
        ``Client`` follows this script. There is no ``ua`` attribute, so
        the writer takes its native-write path."""
        script = self

        class Client(_ScriptedClient):
            def __init__(self, endpoint):
                super().__init__(endpoint, script)

        sync_module = types.ModuleType("asyncua.sync")
        sync_module.Client = Client
        package = types.ModuleType("asyncua")
        package.sync = sync_module
        return {"asyncua": package, "asyncua.sync": sync_module}

    def factory(self, endpoint):
        return _ScriptedClient(endpoint, self)


class _ScriptedClient:
    def __init__(self, endpoint, script):
        self.endpoint = endpoint
        self._script = script
        self.events = []
        script.clients.append(self)

    def set_user(self, username):
        self.events.append("set_user")

    def set_password(self, password):
        self.events.append("set_password")

    def set_security_string(self, value):
        self.events.append("set_security_string")
        if self._script.security_error is not None:
            raise self._script.security_error

    def connect(self):
        self.events.append("connect")
        if self._script.connect_error is not None:
            raise self._script.connect_error

    def get_node(self, node_id):
        return _ScriptedNode(self)

    def disconnect(self):
        self.events.append("disconnect")
        if self._script.disconnect_error is not None:
            raise self._script.disconnect_error


class _ScriptedNode:
    def __init__(self, client):
        self._client = client

    def set_value(self, value):
        self._client.events.append(("set_value", value))


def _write(script, security=None):
    with patch.dict(sys.modules, script.modules()):
        _default_opcua_writer(ENDPOINT, DATA_NODE, True, security)


def _worker(script, extra_parameters=None, on_delivery=None,
            on_connection_lost=None):
    parameters = {"endpoint": ENDPOINT, "node_id": DATA_NODE}
    parameters.update(extra_parameters or {})
    return OpcuaSubscribeWorker(
        parameters,
        on_delivery or (lambda context: True),
        on_connection_lost or (lambda error: None),
        TriggerHealth(TRIGGER_ID, "opcua_subscribe"),
        client_factory=script.factory,
    )


class _Records(logging.Handler):
    """Captures records at or above ``level`` from one logger (the
    repo-level conftest's import patching makes a direct handler the
    most predictable capture)."""

    def __init__(self, level=logging.DEBUG):
        super().__init__(level=level)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def runtime_log():
    target = logging.getLogger("workflow_engine.trigger_runtime")
    handler = _Records()
    previous = target.level
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        target.removeHandler(handler)
        target.setLevel(previous)


# ---------------------------------------------------------------------------
# opcua_write writer: every exit path disconnects
# ---------------------------------------------------------------------------


def test_writer_connects_writes_then_disconnects():
    script = _Script()
    _write(script)
    (client,) = script.clients
    assert client.events == ["connect", ("set_value", True), "disconnect"]


def test_writer_disconnects_when_connect_fails():
    script = _Script(connect_error=ConnectionRefusedError("refused"))
    with pytest.raises(ConnectionRefusedError):
        _write(script)
    (client,) = script.clients
    assert client.events == ["connect", "disconnect"]


def test_writer_connect_error_is_not_masked_by_a_disconnect_error():
    script = _Script(connect_error=TimeoutError("connect timed out"),
                     disconnect_error=RuntimeError("loop already stopped"))
    with pytest.raises(TimeoutError, match="connect timed out"):
        _write(script)


def test_writer_disconnect_error_after_a_write_still_propagates():
    script = _Script(disconnect_error=RuntimeError("close failed"))
    with pytest.raises(RuntimeError, match="close failed"):
        _write(script)


def test_writer_disconnects_when_security_setup_fails():
    script = _Script(security_error=FileNotFoundError("/certs/client.der"))
    with pytest.raises(FileNotFoundError):
        _write(script, security=CERT_SECURITY)
    (client,) = script.clients
    assert client.events == ["set_security_string", "disconnect"]


def test_writer_names_asyncua_when_the_package_is_missing():
    with patch.dict(sys.modules, {"asyncua": None, "asyncua.sync": None}):
        with pytest.raises(RuntimeError) as raised:
            _default_opcua_writer(ENDPOINT, DATA_NODE, True)
    assert str(raised.value) == (
        "The 'asyncua' Python package is not available; it is delivered "
        "as a LocalServer dependency")


def test_malformed_endpoint_is_rejected_before_any_client_exists():
    constructed = []
    with pytest.raises(ValueError, match="Invalid OPC UA endpoint"):
        _new_opcua_client(constructed.append, "opc.tcp://[fe80::1:4840")
    assert constructed == []


# ---------------------------------------------------------------------------
# Trigger worker: a failed session build disconnects before re-raising
# ---------------------------------------------------------------------------


def test_worker_start_disconnects_when_connect_fails():
    script = _Script(connect_error=ConnectionRefusedError("refused"))
    with pytest.raises(ConnectionRefusedError):
        _worker(script).start()
    (client,) = script.clients
    assert client.events == ["connect", "disconnect"]


def test_session_build_connect_error_survives_a_failing_disconnect():
    script = _Script(connect_error=ConnectionRefusedError("refused"),
                     disconnect_error=RuntimeError("loop already stopped"))
    with pytest.raises(ConnectionRefusedError):
        _worker(script)._build_session()
    (client,) = script.clients
    assert client.events == ["connect", "disconnect"]


def test_session_build_disconnects_when_security_setup_fails():
    script = _Script(security_error=FileNotFoundError("/certs/client.der"))
    with pytest.raises(FileNotFoundError):
        _worker(script, CERT_SECURITY)._build_session()
    (client,) = script.clients
    assert client.events == ["set_security_string", "disconnect"]


def test_worker_names_asyncua_when_the_package_is_missing():
    worker = OpcuaSubscribeWorker(
        {"endpoint": ENDPOINT, "node_id": DATA_NODE},
        lambda context: True,
        lambda error: None,
        TriggerHealth(TRIGGER_ID, "opcua_subscribe"),
    )
    with patch.dict(sys.modules, {"asyncua": None, "asyncua.sync": None}):
        with pytest.raises(RuntimeError, match="'asyncua' Python package"):
            worker._build_session()


# ---------------------------------------------------------------------------
# Subscription handler: status changes are logged, never acted on
# ---------------------------------------------------------------------------


def test_status_change_is_logged_and_never_routes_loss(runtime_log):
    deliveries, losses = [], []
    worker = _worker(_Script(), on_delivery=deliveries.append,
                     on_connection_lost=losses.append)
    handler = _OpcuaDataChangeHandler(worker, worker._generation)
    status = types.SimpleNamespace(Status=types.SimpleNamespace(
        name="BadShutdown"))

    handler.status_change_notification(status)

    assert deliveries == [] and losses == []
    warnings = [r.getMessage() for r in runtime_log
                if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert TRIGGER_ID in warnings[0] and "BadShutdown" in warnings[0]


def test_status_change_from_a_torn_down_session_is_silent(runtime_log):
    worker = _worker(_Script())
    stale = _OpcuaDataChangeHandler(worker, worker._generation - 1)

    stale.status_change_notification(types.SimpleNamespace(Status="Bad"))

    assert [r for r in runtime_log if r.levelno >= logging.WARNING] == []


# ---------------------------------------------------------------------------
# Real asyncua: in-process server on localhost
# ---------------------------------------------------------------------------


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _LocalServer:
    """An ``asyncua.sync.Server`` with one writable variable per type the
    writer coerces to."""

    #: Every test server's own event-loop thread (the module-scoped server
    #: is still running while a test starts a dedicated one).
    _server_loops = []

    def __init__(self):
        pytest.importorskip("asyncua")
        from asyncua import ua
        from asyncua.sync import Server

        self.endpoint = f"opc.tcp://127.0.0.1:{_free_port()}/dda/test/"
        self.server = Server()
        self.server.set_endpoint(self.endpoint)
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        idx = self.server.register_namespace("urn:dda:test")
        plc = self.server.nodes.objects.add_object(idx, "PLC")
        self.nodes = {
            "flag": plc.add_variable(idx, "DefectFlag", False),
            "count": plc.add_variable(idx, "Count", 0,
                                      varianttype=ua.VariantType.Int32),
            "score": plc.add_variable(idx, "Score", 0.0),
            "label": plc.add_variable(idx, "Label", ""),
            "subscribed": plc.add_variable(idx, "Subscribed", 0.0),
            "polled": plc.add_variable(idx, "Polled", 0.0),
        }
        for node in self.nodes.values():
            node.set_writable()
        self.server.start()
        self._server_loops.append(self.server.tloop)
        self._running = True

    def node_id(self, name):
        return self.nodes[name].nodeid.to_string()

    def read(self, name):
        return self.nodes[name].read_value()

    def write(self, name, value):
        self.nodes[name].write_value(value)

    def client_loops(self):
        """Alive client event-loop threads (every asyncua ThreadLoop
        except the test servers' own)."""
        from asyncua.sync import ThreadLoop

        return [thread for thread in threading.enumerate()
                if isinstance(thread, ThreadLoop) and thread.is_alive()
                and not any(thread is loop for loop in self._server_loops)]

    def stop(self):
        if self._running:
            self._running = False
            self.server.stop()


@pytest.fixture(scope="module")
def opcua_server():
    server = _LocalServer()
    try:
        yield server
    finally:
        server.stop()


class _Deliveries:
    """Thread-safe on_delivery recorder with value-based waits."""

    def __init__(self):
        self.contexts = []
        self._changed = threading.Condition()

    def __call__(self, context):
        with self._changed:
            self.contexts.append(context)
            self._changed.notify_all()
        return True

    def wait_for_value(self, values, timeout):
        wanted = set(values)
        with self._changed:
            self._changed.wait_for(
                lambda: any(c["value"] in wanted for c in self.contexts),
                timeout)
            return [c for c in self.contexts if c["value"] in wanted]


def _real_worker(server, node_name, deliveries, losses, **parameters):
    """A production-wired worker (no client_factory: the real asyncua
    client) and its health handle."""
    health = TriggerHealth(TRIGGER_ID, "opcua_subscribe")
    worker = OpcuaSubscribeWorker(
        {"endpoint": server.endpoint, "node_id": server.node_id(node_name),
         **parameters},
        deliveries,
        losses.append,
        health,
    )
    return worker, health


def test_real_writer_coerces_to_each_node_type(opcua_server):
    _default_opcua_writer(opcua_server.endpoint,
                          opcua_server.node_id("flag"), 1)
    _default_opcua_writer(opcua_server.endpoint,
                          opcua_server.node_id("count"), "7")
    _default_opcua_writer(opcua_server.endpoint,
                          opcua_server.node_id("score"), "0.5")
    _default_opcua_writer(opcua_server.endpoint,
                          opcua_server.node_id("label"), 5)

    assert opcua_server.read("flag") is True
    assert opcua_server.read("count") == 7
    assert opcua_server.read("score") == pytest.approx(0.5)
    assert opcua_server.read("label") == "5"
    assert opcua_server.client_loops() == []


def test_real_writer_failed_connect_leaves_no_loop_thread(opcua_server):
    closed = f"opc.tcp://127.0.0.1:{_free_port()}/nothing-listens/"
    with pytest.raises(OSError):
        _default_opcua_writer(closed, "ns=2;i=2", True)
    assert opcua_server.client_loops() == []


def test_real_writer_demotes_the_session_timeout_warning(opcua_server):
    # The local server caps sessions below the client's 1 h request, so
    # asyncua emits its revision warning on this connect.
    client_logger = logging.getLogger(ASYNCUA_CLIENT_LOGGER)
    console = _Records(level=logging.WARNING)
    debug = _Records(level=logging.DEBUG)
    client_logger.addHandler(console)
    client_logger.addHandler(debug)
    try:
        _default_opcua_writer(opcua_server.endpoint,
                              opcua_server.node_id("flag"), 0)
    finally:
        client_logger.removeHandler(console)
        client_logger.removeHandler(debug)

    def revisions(records):
        return [r for r in records
                if r.getMessage().startswith("Requested session timeout")]

    assert revisions(console.records) == []
    demoted = revisions(debug.records)
    assert demoted and all(r.levelno == logging.DEBUG for r in demoted)
    assert opcua_server.read("flag") is False


def test_real_trigger_subscription_delivers_data_changes(opcua_server):
    deliveries, losses = _Deliveries(), []
    worker, health = _real_worker(opcua_server, "subscribed", deliveries,
                                  losses, sampling_interval_ms=50)
    worker.start()
    try:
        assert health.state == HEALTH_SUBSCRIBED
        assert health.mechanism == MECHANISM_SUBSCRIBE
        opcua_server.write("subscribed", 0.75)
        (context,) = deliveries.wait_for_value([0.75], WAIT_SECONDS)
    finally:
        worker.stop()

    assert context["endpoint"] == opcua_server.endpoint
    assert context["node_id"] == opcua_server.node_id("subscribed")
    # The server stamps every write; asyncua hands back an aware datetime.
    assert datetime.fromisoformat(context["source_timestamp"]).tzinfo
    assert losses == []
    assert opcua_server.client_loops() == []


def test_real_trigger_poll_mode_fires_on_change(opcua_server):
    deliveries, losses = _Deliveries(), []
    worker, health = _real_worker(opcua_server, "polled", deliveries, losses,
                                  mode=MECHANISM_POLL, poll_interval_ms=50)
    worker.start()
    written = []
    try:
        assert health.state == HEALTH_POLLING
        assert health.mechanism == MECHANISM_POLL
        # Keep changing the value until one change lands after the poll
        # loop primed on its first read.
        for step in range(1, 20):
            value = 1.0 + step
            opcua_server.write("polled", value)
            written.append(value)
            if deliveries.wait_for_value(written, 0.5):
                break
        fired = deliveries.wait_for_value(written, WAIT_SECONDS)
    finally:
        worker.stop()

    assert fired, "poll mode never fired on a value change"
    assert fired[0]["source_timestamp"] is None
    assert losses == []
    assert opcua_server.client_loops() == []


def test_real_watchdog_detects_server_loss_and_releases_the_session():
    server = _LocalServer()
    deliveries, losses = _Deliveries(), []
    lost = threading.Event()

    def on_lost(error):
        losses.append(error)
        lost.set()

    health = TriggerHealth(TRIGGER_ID, "opcua_subscribe")
    worker = OpcuaSubscribeWorker(
        {"endpoint": server.endpoint, "node_id": server.node_id("score")},
        deliveries,
        on_lost,
        health,
        watchdog_interval=0.2,
    )
    try:
        worker.start()
        assert health.state == HEALTH_SUBSCRIBED
        server.stop()
        assert lost.wait(WAIT_SECONDS), "watchdog never reported the loss"
        deadline = time.monotonic() + WAIT_SECONDS
        while server.client_loops() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.client_loops() == []
        assert len(losses) == 1
    finally:
        worker.stop()
        server.stop()
