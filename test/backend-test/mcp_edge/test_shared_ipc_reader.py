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
"""The shared component-configuration reader of ``local_auth.config``.

Feature: mcp-servers (design §6 ``local_auth/config.py``, M4; tasks.md 10.1;
Requirements 12.2, 12.3). The local-login poller and the MCP configuration
poller read through one ``read_component_config_via_ipc``: one
``DefectDetectionConfig`` instance and one IPC connection per process, and
never two concurrent operations on it.
"""
import sys
import threading
import time
import types

import pytest

from local_auth import config as local_auth_config


class _FakeReader:
    instances = 0

    def __init__(self, ipc_client):
        type(self).instances += 1
        self.ipc_client = ipc_client
        self.result = {"LocalLoginEnabled": "true", "McpServerEnabled": "false"}
        self.active = 0
        self.max_active = 0
        self.error = None
        self._guard = threading.Lock()

    def get_local_server_component_name(self):
        return "aws.edgeml.dda.LocalServer"

    def get_component_config(self, name):
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.01)
            if self.error is not None:
                raise self.error
            return self.result
        finally:
            with self._guard:
                self.active -= 1


@pytest.fixture
def fake_ipc(monkeypatch):
    """Fake Greengrass modules for the lazy imports; a fresh reader cache."""
    clients = []

    def get_ipc_client():
        clients.append(object())
        return clients[-1]

    _FakeReader.instances = 0
    ipc_module = types.ModuleType("utils.ipc_client")
    ipc_module.get_ipc_client = get_ipc_client
    ddc_module = types.ModuleType("defect_detection_config.defect_detection_config")
    ddc_module.DefectDetectionConfig = _FakeReader
    monkeypatch.setitem(sys.modules, "utils.ipc_client", ipc_module)
    monkeypatch.setitem(
        sys.modules, "defect_detection_config.defect_detection_config", ddc_module)
    monkeypatch.setattr(local_auth_config, "_IPC_CONFIG_READER", None)
    return types.SimpleNamespace(clients=clients)


def _reader():
    return local_auth_config._IPC_CONFIG_READER


def test_returns_the_component_configuration(fake_ipc):
    assert local_auth_config.read_component_config_via_ipc() == {
        "LocalLoginEnabled": "true", "McpServerEnabled": "false"}


def test_non_dict_result_is_empty(fake_ipc):
    local_auth_config.read_component_config_via_ipc()
    for value in (None, "LocalLoginEnabled", ["LocalLoginEnabled"], 7):
        _reader().result = value
        assert local_auth_config.read_component_config_via_ipc() == {}


def test_local_login_reader_delegates(fake_ipc):
    assert local_auth_config._read_local_login_enabled_via_ipc() == "true"
    _reader().result = {"McpServerEnabled": "true"}
    assert (local_auth_config._read_local_login_enabled_via_ipc()
            is local_auth_config.MISSING)
    _reader().result = None
    assert (local_auth_config._read_local_login_enabled_via_ipc()
            is local_auth_config.MISSING)
    _reader().result = {"LocalLoginEnabled": None}
    assert local_auth_config._read_local_login_enabled_via_ipc() is None


def test_one_reader_and_one_connection_per_process(fake_ipc):
    for _ in range(5):
        local_auth_config.read_component_config_via_ipc()
        local_auth_config._read_local_login_enabled_via_ipc()
    assert _FakeReader.instances == 1
    assert len(fake_ipc.clients) == 1
    assert _reader().ipc_client is fake_ipc.clients[0]


def test_ipc_failure_raises(fake_ipc):
    local_auth_config.read_component_config_via_ipc()
    _reader().error = RuntimeError("ipc down")
    with pytest.raises(RuntimeError):
        local_auth_config.read_component_config_via_ipc()
    with pytest.raises(RuntimeError):
        local_auth_config._read_local_login_enabled_via_ipc()


def test_failed_read_keeps_local_login_disabled(fake_ipc):
    local_auth_config.read_component_config_via_ipc()
    _reader().error = RuntimeError("ipc down")
    login = local_auth_config.LocalLoginConfig(
        read_value=local_auth_config._read_local_login_enabled_via_ipc)
    assert login.refresh() is False


def test_concurrent_pollers_never_overlap(fake_ipc):
    local_auth_config.read_component_config_via_ipc()
    errors = []

    def poll(function):
        try:
            for _ in range(5):
                function()
        except Exception as error:  # noqa: BLE001 - reported below
            errors.append(error)

    threads = [threading.Thread(target=poll, args=(function,))
               for function in (local_auth_config.read_component_config_via_ipc,
                                local_auth_config._read_local_login_enabled_via_ipc)
               for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert _reader().max_active == 1
    assert _FakeReader.instances == 1
