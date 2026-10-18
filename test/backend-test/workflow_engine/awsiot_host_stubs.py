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
"""A guarded ``awsiot`` stub for the host test lane.

``utils.ipc_client`` and ``mqtt.SubscriptionHandler`` import
``awsiot.greengrasscoreipc`` at module level. The device image ships the
SDK; the host lane does not (``awsiot`` is absent on CPython 3.9.5 there),
so a test that needs either module cannot be collected on the host unless
the SDK is stood in for first. This helper installs the smallest module
family those imports need, **only when the real package is missing**
(``importlib.util.find_spec`` says so), following the guarded pattern of
``camera_sync/test_server_setup_isolation.py``: a really installed SDK
always wins and nothing is installed on the device image.

Import this module before ``utils.ipc_client`` (the telemetry publisher
tests, the ``publish_via_ipc`` factoring test and the debug agent tests do).
The exception classes are plain classes, so a test that raises them through
``publish_via_ipc`` exercises the production ``except`` clauses, and the
``connect`` stub refuses to connect: tests must inject their IPC client.
"""
import importlib.util
import sys
import types

INSTALLED = []


class QOS:
    AT_MOST_ONCE = 0
    AT_LEAST_ONCE = 1


class PublishToIoTCoreRequest:
    def __init__(self, topic_name=None, payload=None, qos=None, retain=None):
        self.topic_name = topic_name
        self.payload = payload
        self.qos = qos
        self.retain = retain


class SubscribeToIoTCoreRequest:
    def __init__(self, topic_name=None, qos=None):
        self.topic_name = topic_name
        self.qos = qos


class UnauthorizedError(Exception):
    """The nucleus denial."""


class ServiceError(Exception):
    """The nucleus service error (spooler full, among others)."""


class SubscribeToIoTCoreStreamHandler:
    def on_stream_event(self, event):
        pass

    def on_stream_error(self, error):
        return True

    def on_stream_closed(self):
        pass


def _refuse_connect(*args, **kwargs):
    raise AssertionError("host tests must inject their IPC client; the awsiot "
                         "stub never connects")


def _permissive(module):
    """Any name the stub does not define resolves to a ``MagicMock`` (PEP 562
    module ``__getattr__``), as the MagicMock modules of
    ``camera_sync/test_server_setup_isolation.py`` do, so the heavier import
    chains (``dao.iotshadow``, ``utils.gg_utils``, ``endpoints.system``)
    import under this stub too. Dunder names stay undefined."""
    from unittest import mock

    def __getattr__(name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = mock.MagicMock(name="{0}.{1}".format(module.__name__, name))
        setattr(module, name, value)
        return value

    module.__getattr__ = __getattr__
    return module


def install_awsiot_stubs_if_missing():
    """Install the stub family into ``sys.modules`` when ``awsiot`` is not
    importable. Idempotent. Returns the names installed by this call."""
    if "awsiot" in sys.modules or importlib.util.find_spec("awsiot") is not None:
        return []
    awsiot = types.ModuleType("awsiot")
    ggc = _permissive(types.ModuleType("awsiot.greengrasscoreipc"))
    model = _permissive(types.ModuleType("awsiot.greengrasscoreipc.model"))
    client = _permissive(types.ModuleType("awsiot.greengrasscoreipc.client"))
    model.QOS = QOS
    model.PublishToIoTCoreRequest = PublishToIoTCoreRequest
    model.SubscribeToIoTCoreRequest = SubscribeToIoTCoreRequest
    model.UnauthorizedError = UnauthorizedError
    model.ServiceError = ServiceError
    client.SubscribeToIoTCoreStreamHandler = SubscribeToIoTCoreStreamHandler
    ggc.model = model
    ggc.client = client
    ggc.connect = _refuse_connect
    awsiot.greengrasscoreipc = ggc
    awsiot.__path__ = []  # a package, so ``awsiot.eventstreamrpc`` is ImportError
    ggc.__path__ = []
    installed = []
    for name, module in (
        ("awsiot", awsiot),
        ("awsiot.greengrasscoreipc", ggc),
        ("awsiot.greengrasscoreipc.model", model),
        ("awsiot.greengrasscoreipc.client", client),
    ):
        sys.modules[name] = module
        installed.append(name)
    INSTALLED.extend(installed)
    return installed


install_awsiot_stubs_if_missing()
