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
import os

from local_server_base_test_case import LocalServerBaseTestCase
from unittest.mock import patch, MagicMock


def _mock_ipc_client_with_components(components):
    """Build a MagicMock ipc_client whose list-components call returns `components`."""
    mock_ipc_client = MagicMock()
    mock_operation = MagicMock()
    mock_ipc_client.new_list_components.return_value = mock_operation
    mock_future = MagicMock()
    mock_operation.get_response.return_value = mock_future
    mock_response = MagicMock()
    mock_response.components = components
    mock_future.result.return_value = mock_response
    return mock_ipc_client


def _component(name, version):
    # component_name is a real MagicMock kwarg name, so set attributes explicitly.
    comp = MagicMock()
    comp.component_name = name
    comp.version = version
    return comp


class TestGetLocalServerComponentVersion(LocalServerBaseTestCase):
    """Unit tests for endpoints.system.get_local_server_component_version.

    The core regression: multiple LocalServer variants (.arm64, .arm64JP5,
    .amd64) can be installed on one core device, all sharing the
    "aws.edgeml.dda.LocalServer" prefix. The version reported must be the
    variant this backend is actually running (derived from
    LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH), not just the first prefix match.
    """

    JP5_PATH = "/greengrass/v2/work/aws.edgeml.dda.LocalServer.arm64JP5-aarch64"

    # Realistic full Greengrass artifact paths, which embed the deployed version.
    JP5_FULL_PATH = (
        "/greengrass/v2/packages/artifacts-unarchived/"
        "aws.edgeml.dda.LocalServer.arm64JP5/1.0.5/"
        "aws.edgeml.dda.LocalServer.arm64JP5-aarch64"
    )
    ARM64_FULL_PATH = (
        "/greengrass/v2/packages/artifacts-unarchived/"
        "aws.edgeml.dda.LocalServer.arm64/1.0.5/"
        "aws.edgeml.dda.LocalServer.arm64-aarch64"
    )

    def test_version_read_from_artifact_path_ignores_leftover_component(self):
        from endpoints import system

        # The nucleus still knows a leftover arm64 1.0.108, but the running
        # component's own artifact path says 1.0.5 — the path must win.
        components = [
            _component("aws.edgeml.dda.LocalServer.arm64", "1.0.108"),
            _component("aws.edgeml.dda.LocalServer.arm64JP5", "1.0.108"),
        ]
        with patch.dict(os.environ, {"LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": self.JP5_FULL_PATH}), \
                patch("endpoints.system.ipc_client", _mock_ipc_client_with_components(components)):
            version = system.get_local_server_component_version()

        self.assertEqual(version, "1.0.5")

    def test_version_from_path_does_not_call_ipc(self):
        from endpoints import system

        mock_ipc = _mock_ipc_client_with_components([])
        with patch.dict(os.environ, {"LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": self.ARM64_FULL_PATH}), \
                patch("endpoints.system.ipc_client", mock_ipc):
            version = system.get_local_server_component_version()

        self.assertEqual(version, "1.0.5")
        # Path resolution is ground truth; IPC must not be consulted.
        mock_ipc.new_list_components.assert_not_called()

    def test_falls_back_to_ipc_when_path_has_no_version(self):
        from endpoints import system

        # No version segment in the path -> must fall back to IPC exact match.
        components = [
            _component("aws.edgeml.dda.LocalServer.arm64", "1.0.108"),
            _component("aws.edgeml.dda.LocalServer.arm64JP5", "1.0.4"),
        ]
        with patch.dict(os.environ, {"LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": self.JP5_PATH}), \
                patch("endpoints.system.ipc_client", _mock_ipc_client_with_components(components)):
            version = system.get_local_server_component_version()

        self.assertEqual(version, "1.0.4")

    def test_returns_running_variant_version_when_multiple_variants_present(self):
        from endpoints import system

        components = [
            _component("aws.edgeml.dda.LocalServer.arm64", "1.0.108"),
            _component("aws.edgeml.dda.LocalServer.arm64JP5", "1.0.4"),
        ]
        with patch.dict(os.environ, {"LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": self.JP5_PATH}), \
                patch("endpoints.system.ipc_client", _mock_ipc_client_with_components(components)):
            version = system.get_local_server_component_version()

        # Must report the JP5 variant we are running, not the leftover arm64 (1.0.108).
        self.assertEqual(version, "1.0.4")

    def test_exact_match_is_order_independent(self):
        from endpoints import system

        # Same data, JP5 listed first — result must still be the JP5 version.
        components = [
            _component("aws.edgeml.dda.LocalServer.arm64JP5", "1.0.4"),
            _component("aws.edgeml.dda.LocalServer.arm64", "1.0.108"),
        ]
        with patch.dict(os.environ, {"LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": self.JP5_PATH}), \
                patch("endpoints.system.ipc_client", _mock_ipc_client_with_components(components)):
            version = system.get_local_server_component_version()

        self.assertEqual(version, "1.0.4")

    def test_amd64_path_resolves_amd64_variant(self):
        from endpoints import system

        amd64_path = "/greengrass/v2/work/aws.edgeml.dda.LocalServer.amd64-x86_64"
        components = [
            _component("aws.edgeml.dda.LocalServer.arm64", "1.0.108"),
            _component("aws.edgeml.dda.LocalServer.amd64", "1.0.9"),
        ]
        with patch.dict(os.environ, {"LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": amd64_path}), \
                patch("endpoints.system.ipc_client", _mock_ipc_client_with_components(components)):
            version = system.get_local_server_component_version()

        self.assertEqual(version, "1.0.9")

    def test_falls_back_to_first_prefix_match_when_name_undeterminable(self):
        from endpoints import system

        # A path whose final segment is not a LocalServer component name forces
        # the fallback to the previous first-prefix-match behavior.
        components = [
            _component("aws.edgeml.dda.LocalServer.arm64", "1.0.108"),
            _component("aws.edgeml.dda.LocalServer.arm64JP5", "1.0.4"),
        ]
        with patch.dict(os.environ, {"LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": "comp_decomp_path_test"}), \
                patch("endpoints.system.ipc_client", _mock_ipc_client_with_components(components)):
            version = system.get_local_server_component_version()

        self.assertEqual(version, "1.0.108")

    def test_returns_not_found_when_no_localserver_component(self):
        from endpoints import system

        components = [
            _component("model-1", "1.0.0"),
            _component("aws.iot.lookoutvision.EdgeAgent", "1.2.3"),
        ]
        with patch.dict(os.environ, {"LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": self.JP5_PATH}), \
                patch("endpoints.system.ipc_client", _mock_ipc_client_with_components(components)):
            version = system.get_local_server_component_version()

        self.assertEqual(version, "NOT_FOUND")

    def test_returns_not_found_on_ipc_error(self):
        from endpoints import system

        failing_ipc = MagicMock()
        failing_ipc.new_list_components.side_effect = RuntimeError("ipc down")
        with patch.dict(os.environ, {"LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": self.JP5_PATH}), \
                patch("endpoints.system.ipc_client", failing_ipc):
            version = system.get_local_server_component_version()

        self.assertEqual(version, "NOT_FOUND")
