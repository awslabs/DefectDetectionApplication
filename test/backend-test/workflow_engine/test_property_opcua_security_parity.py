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
"""Property test for OPC UA security mapping parity (Task 8.3).

# Feature: trigger-activation-runtime, Property 15: OPC UA security mapping parity

*For any generated combination of the seven security parameters, the
subscription session's client configuration calls (`set_user`,
`set_password`, `set_security_string`) equal those the `opcua_write`
executor makes for the same parameter values.*

How the two sides are exercised (documented decision):

- **Subscription side**: the worker's ``client_factory`` injection seam
  hands back a recording stub client and ``worker._build_session()`` is
  called directly — this IS the worker's complete security-application
  path (``client_factory(endpoint)`` → ``_apply_security`` →
  ``connect()``), and calling it directly avoids starting any
  watchdog/poll threads (cheapest deterministic drive of the session
  build; the task explicitly allows this).
- **Writer side (oracle)**: the REAL ``output_bindings._default_opcua_writer``
  is invoked — a fake ``opcua`` module is injected via ``sys.modules``
  whose ``Client`` is the same recording stub — with the security dict
  produced by ``_opcua_security_from_params`` for the same parameters,
  exactly as ``OutputBindingProcessor`` computes it before calling the
  writer. This is a true parity check against the production writer
  code, not a transcription of its rules.

The recorded (`set_user`, `set_password`, `set_security_string`) call
sequences from both sides are asserted equal.

**Validates: Requirements 6.6**
"""
import sys
import types
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.output_bindings import (
    _default_opcua_writer,
    _opcua_security_from_params,
)
from workflow_engine.trigger_runtime import (
    OpcuaSubscribeWorker,
    TriggerHealth,
)

_ENDPOINT = "opc.tcp://plc.example:4840/freeopcua/server/"
_NODE_ID = "ns=2;i=5"


# ---------------------------------------------------------------------------
# Recording stub client (used by BOTH sides, so the recorded call shapes
# are directly comparable)
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Stub opcua client recording the security-configuration calls; the
    write-path extras (``get_node``/``set_value``/``disconnect``) are
    tolerated no-ops so the real writer runs to completion."""

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.security_calls = []
        self.connected = False

    def set_user(self, username):
        self.security_calls.append(("set_user", username))

    def set_password(self, password):
        self.security_calls.append(("set_password", password))

    def set_security_string(self, security_string):
        self.security_calls.append(("set_security_string", security_string))

    def connect(self):
        self.connected = True

    def get_node(self, node_id):
        return _RecordingNode()

    def disconnect(self):
        self.connected = False


class _RecordingNode:
    def set_value(self, value):
        pass


def _fake_opcua_module(created):
    """A fake ``opcua`` module whose ``Client`` records into ``created``;
    ``ua`` is explicitly None so the writer deterministically takes its
    native-write fallback (no typed-variant resolution)."""

    module = types.ModuleType("opcua")

    class Client(_RecordingClient):
        def __init__(self, endpoint):
            super().__init__(endpoint)
            created.append(self)

    module.Client = Client
    module.ua = None
    return module


# ---------------------------------------------------------------------------
# Generators: each of the seven security parameters independently
# absent / None / empty / a valid value
# ---------------------------------------------------------------------------

_ABSENT = object()


def _param(valid_values):
    return st.one_of(
        st.just(_ABSENT),
        st.none(),
        st.just(""),
        st.sampled_from(valid_values),
    )


_SECURITY_PARAMS = st.fixed_dictionaries(
    {
        "username": _param(["operator", "svc-account", "u"]),
        "password": _param(["hunter2", "p@ss w0rd", "0"]),
        "security_policy": _param(
            ["Basic256Sha256", "Basic256", "Basic128Rsa15"]
        ),
        "security_mode": _param(["Sign", "SignAndEncrypt"]),
        "client_cert_path": _param(["/certs/client.der", "certs/c.pem"]),
        "client_key_path": _param(["/certs/client.key", "certs/k.pem"]),
        "server_cert_path": _param(["/certs/server.der", "certs/s.pem"]),
    }
).map(
    lambda entries: {
        key: value for key, value in entries.items() if value is not _ABSENT
    }
)


# ---------------------------------------------------------------------------
# Property 15
# ---------------------------------------------------------------------------


# Feature: trigger-activation-runtime, Property 15: OPC UA security mapping parity
@settings(max_examples=100)
@given(security_params=_SECURITY_PARAMS)
def test_subscription_security_calls_equal_writer_security_calls(
    security_params,
):
    """For any combination of the seven security parameters, the
    subscription session build issues exactly the (`set_user`,
    `set_password`, `set_security_string`) call sequence the real
    `opcua_write` executor (`_default_opcua_writer`) issues for the same
    parameter values, and both sides connect.

    **Validates: Requirements 6.6**
    """
    parameters = {
        "endpoint": _ENDPOINT,
        "node_id": _NODE_ID,
        **security_params,
    }

    # -- Subscription side: worker session build via the client_factory seam
    worker_clients = []

    def client_factory(endpoint):
        client = _RecordingClient(endpoint)
        worker_clients.append(client)
        return client

    worker = OpcuaSubscribeWorker(
        parameters,
        lambda context: None,
        lambda error: None,
        TriggerHealth("trig-opcua-1", "opcua_subscribe"),
        client_factory=client_factory,
    )
    session = worker._build_session()

    assert len(worker_clients) == 1
    worker_client = worker_clients[0]
    assert session is worker_client
    assert worker_client.endpoint == _ENDPOINT
    assert worker_client.connected

    # -- Writer side (oracle): the REAL _default_opcua_writer, security
    #    dict computed the way OutputBindingProcessor computes it
    security = _opcua_security_from_params(parameters)
    writer_clients = []
    with patch.dict(sys.modules, {"opcua": _fake_opcua_module(writer_clients)}):
        _default_opcua_writer(_ENDPOINT, _NODE_ID, 1, security)

    assert len(writer_clients) == 1
    writer_client = writer_clients[0]
    assert writer_client.connected is False  # writer disconnects after write

    # -- Parity: identical client configuration call sequences
    assert worker_client.security_calls == writer_client.security_calls
