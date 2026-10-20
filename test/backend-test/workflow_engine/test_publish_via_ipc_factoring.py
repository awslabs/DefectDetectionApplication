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
"""The ``publish_via_ipc`` factoring (workflow-telemetry-dashboards design
§5.2 ``publisher.py``; workflow-debug-mode tasks.md 1.2).

``_default_greengrass_publisher`` is now the one-line ``str`` delegate of
``publish_via_ipc(topic, payload: bytes, qos, *, retain=False)``, which
carries the whole IPC body: the QoS mapping, the ``retain`` assignment behind
the SDK-field check, ``call_with_ipc_retry`` and the denial diagnosis.
``test_mqtt_publish_retain.py`` pins the retained path through the delegate;
this file pins the delegation itself and the factored function's own
behaviour.
"""
import contextlib
import inspect
import sys
import types
from unittest.mock import patch

import pytest

import awsiot_host_stubs  # noqa: F401 - stands in for the SDK on the host
import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH
import utils.ipc_client  # noqa: F401,E402 - imported before any sys.modules fake
from workflow_engine import output_bindings
from workflow_engine.output_bindings import (
    _default_greengrass_publisher,
    publish_via_ipc,
)

_TOPIC = "dda/telemetry/uc/wf/summary"


def _fake_awsiot(*, with_retain_field, raise_error=None):
    """The fake SDK tree of ``test_mqtt_publish_retain.py``: the request
    records its attribute assignments; ``raise_error`` is raised from the
    operation's response future."""
    model = types.ModuleType("awsiot.greengrasscoreipc.model")

    class QOS:
        AT_MOST_ONCE = 0
        AT_LEAST_ONCE = 1

    class PublishToIoTCoreRequest:
        def __init__(self):
            self.topic_name = None
            self.payload = None
            self.qos = None
            if with_retain_field:
                self.retain = None
            self.__dict__["assigned"] = []

        def __setattr__(self, name, value):
            if "assigned" in self.__dict__:
                self.__dict__["assigned"].append(name)
            object.__setattr__(self, name, value)

    class UnauthorizedError(Exception):
        pass

    class ServiceError(Exception):
        pass

    model.QOS = QOS
    model.PublishToIoTCoreRequest = PublishToIoTCoreRequest
    model.UnauthorizedError = UnauthorizedError
    model.ServiceError = ServiceError

    state = {"operations": 0, "connects": 0, "request": None}

    class _Future:
        def result(self, timeout=None):
            if raise_error is not None:
                raise raise_error(model)
            return None

    class _Operation:
        def activate(self, request):
            state["request"] = request

        def get_response(self):
            return _Future()

    class _IpcClient:
        def new_publish_to_iot_core(self):
            state["operations"] += 1
            return _Operation()

    def connect():
        state["connects"] += 1
        return _IpcClient()

    greengrasscoreipc = types.ModuleType("awsiot.greengrasscoreipc")
    greengrasscoreipc.model = model
    greengrasscoreipc.connect = connect
    awsiot = types.ModuleType("awsiot")
    awsiot.greengrasscoreipc = greengrasscoreipc
    modules = {
        "awsiot": awsiot,
        "awsiot.greengrasscoreipc": greengrasscoreipc,
        "awsiot.greengrasscoreipc.model": model,
    }
    return modules, state


@contextlib.contextmanager
def _faked_ipc(modules):
    connect = modules["awsiot.greengrasscoreipc"].connect
    with patch("utils.ipc_client.get_ipc_client", connect), \
            patch("utils.ipc_client.reset_ipc_client", lambda: None), \
            patch.dict(sys.modules, modules):
        yield


class TestDelegation:
    def test_default_publisher_delegates_with_encoded_payload_and_retain(self, monkeypatch):
        calls = []

        def fake(topic, payload, qos, *, retain=False):
            calls.append((topic, payload, qos, retain))

        monkeypatch.setattr(output_bindings, "publish_via_ipc", fake)
        _default_greengrass_publisher(_TOPIC, "héllo", 1, retain=True)
        _default_greengrass_publisher(_TOPIC, "plain", 0)
        assert calls == [
            (_TOPIC, "héllo".encode("utf-8"), 1, True),
            (_TOPIC, b"plain", 0, False),
        ]

    def test_default_publisher_keeps_its_signature(self):
        signature = inspect.signature(_default_greengrass_publisher)
        assert list(signature.parameters) == ["topic", "payload", "qos", "retain"]
        assert signature.parameters["retain"].kind is inspect.Parameter.KEYWORD_ONLY
        assert signature.parameters["retain"].default is False

    def test_publish_via_ipc_signature(self):
        signature = inspect.signature(publish_via_ipc)
        assert list(signature.parameters) == ["topic", "payload", "qos", "retain"]
        assert signature.parameters["retain"].kind is inspect.Parameter.KEYWORD_ONLY
        assert signature.parameters["retain"].default is False


class TestPublishViaIpc:
    def test_bytes_payload_and_qos_mapping(self):
        modules, state = _fake_awsiot(with_retain_field=True)
        with _faked_ipc(modules):
            publish_via_ipc(_TOPIC, b"\x00raw", 2)
        request = state["request"]
        assert request.topic_name == _TOPIC
        assert request.payload == b"\x00raw"
        assert request.qos == 1  # clamped to AT_LEAST_ONCE
        assert request.retain is None
        assert request.assigned == ["topic_name", "payload", "qos"]
        assert state["operations"] == 1

    def test_qos_zero_is_at_most_once(self):
        modules, state = _fake_awsiot(with_retain_field=True)
        with _faked_ipc(modules):
            publish_via_ipc(_TOPIC, b"x", 0)
        assert state["request"].qos == 0

    def test_retain_true_sets_the_request_field(self):
        modules, state = _fake_awsiot(with_retain_field=True)
        with _faked_ipc(modules):
            publish_via_ipc(_TOPIC, b"x", 1, retain=True)
        request = state["request"]
        assert request.retain is True
        assert request.assigned == ["topic_name", "payload", "qos", "retain"]

    def test_retain_on_sdk_without_field_fails_before_any_ipc_call(self):
        modules, state = _fake_awsiot(with_retain_field=False)
        with _faked_ipc(modules):
            with pytest.raises(RuntimeError) as excinfo:
                publish_via_ipc(_TOPIC, b"x", 0, retain=True)
        assert "awsiotsdk" in str(excinfo.value)
        assert _TOPIC in str(excinfo.value)
        assert state["connects"] == 0
        assert state["operations"] == 0
        assert state["request"] is None

    def test_denial_is_a_runtime_error_from_unauthorized(self):
        modules, _ = _fake_awsiot(
            with_retain_field=True,
            raise_error=lambda model: model.UnauthorizedError("UnauthorizedError"),
        )
        with _faked_ipc(modules):
            with pytest.raises(RuntimeError) as plain:
                publish_via_ipc(_TOPIC, b"x", 0)
        text = str(plain.value)
        assert _TOPIC in text
        assert "aws.greengrass.ipc.mqttproxy accessControl" in text
        assert "iot:RetainPublish" not in text
        assert isinstance(
            plain.value.__cause__,
            modules["awsiot.greengrasscoreipc.model"].UnauthorizedError,
        )

    def test_retained_denial_names_retain_publish(self):
        modules, _ = _fake_awsiot(
            with_retain_field=True,
            raise_error=lambda model: model.UnauthorizedError("UnauthorizedError"),
        )
        with _faked_ipc(modules):
            with pytest.raises(RuntimeError) as retained:
                publish_via_ipc(_TOPIC, b"x", 0, retain=True)
        assert "iot:RetainPublish" in str(retained.value)

    def test_other_errors_propagate_unchanged(self):
        modules, _ = _fake_awsiot(
            with_retain_field=True,
            raise_error=lambda model: model.ServiceError("spooler full"),
        )
        with _faked_ipc(modules):
            with pytest.raises(modules["awsiot.greengrasscoreipc.model"].ServiceError):
                publish_via_ipc(_TOPIC, b"x", 1)
