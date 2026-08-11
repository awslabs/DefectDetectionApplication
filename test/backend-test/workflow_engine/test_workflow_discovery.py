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
"""Unit tests for workflow artifact discovery/validation (Requirement 9.1, 13.3)."""
import os
from unittest.mock import patch

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    RUNNING_VERSION,
    VALID_MANIFEST,
    write_artifact_set,
)

from workflow_engine import discovery, environment
from workflow_engine.discovery import (
    STATUS_INVALID,
    STATUS_REGISTERED,
    scan_workflow_root,
    validate_artifact_set,
)


def _validate(root, workflow_id="wf-1", version="3"):
    (artifact_set,) = [
        s
        for s in scan_workflow_root(str(root))
        if s.workflow_id == workflow_id and s.version == version
    ]
    return validate_artifact_set(
        artifact_set, device_arch=DEVICE_ARCH, running_version=RUNNING_VERSION
    )


class TestScanWorkflowRoot:
    def test_missing_root_yields_nothing(self, tmp_path):
        # Devices that never received a Workflow_Component (13.6)
        assert scan_workflow_root(str(tmp_path / "does-not-exist")) == []

    def test_empty_root_yields_nothing(self, tmp_path):
        assert scan_workflow_root(str(tmp_path)) == []

    def test_finds_workflow_version_directories(self, tmp_path):
        write_artifact_set(tmp_path, "wf-a", "1")
        write_artifact_set(tmp_path, "wf-a", "2")
        write_artifact_set(tmp_path, "wf-b", "7")
        # stray file at each level is ignored
        (tmp_path / "notes.txt").write_text("x")
        (tmp_path / "wf-a" / "junk").write_text("x")

        found = scan_workflow_root(str(tmp_path))
        assert [(s.workflow_id, s.version) for s in found] == [
            ("wf-a", "1"),
            ("wf-a", "2"),
            ("wf-b", "7"),
        ]
        for artifact_set in found:
            assert os.path.isdir(artifact_set.path)


class TestValidateArtifactSet:
    def test_valid_set_registers(self, tmp_path):
        write_artifact_set(tmp_path)
        outcome = _validate(tmp_path)
        assert outcome.status == STATUS_REGISTERED
        assert outcome.arch == DEVICE_ARCH
        assert outcome.reason is None
        assert outcome.manifest["workflowId"] == "wf-1"
        assert outcome.compiled_document["segments"]

    def test_missing_required_file_is_invalid(self, tmp_path):
        for missing in ("manifest.json", "workflow.json", "compiled_pipeline.json"):
            root = tmp_path / missing.replace(".", "-")
            write_artifact_set(root, omit=(missing,))
            outcome = _validate(root)
            assert outcome.status == STATUS_INVALID
            assert missing in outcome.reason

    def test_malformed_manifest_json_is_invalid(self, tmp_path):
        write_artifact_set(tmp_path, raw_manifest="{not json")
        outcome = _validate(tmp_path)
        assert outcome.status == STATUS_INVALID
        assert "manifest.json" in outcome.reason

    def test_malformed_compiled_document_is_invalid(self, tmp_path):
        write_artifact_set(tmp_path, compiled={"schemaVersion": 1})  # no segments
        outcome = _validate(tmp_path)
        assert outcome.status == STATUS_INVALID
        assert "compiled_pipeline.json" in outcome.reason

    def test_wrong_architecture_is_invalid(self, tmp_path):
        manifest = dict(VALID_MANIFEST, targetArch="arm64_jp5")
        write_artifact_set(tmp_path, manifest=manifest)
        outcome = _validate(tmp_path)
        assert outcome.status == STATUS_INVALID
        assert "arm64_jp5" in outcome.reason
        assert DEVICE_ARCH in outcome.reason
        assert outcome.arch == "arm64_jp5"

    def test_min_local_server_version_above_running_is_invalid(self, tmp_path):
        manifest = dict(VALID_MANIFEST, minLocalServerVersion="9.9.9")
        write_artifact_set(tmp_path, manifest=manifest)
        outcome = _validate(tmp_path)
        assert outcome.status == STATUS_INVALID
        assert "9.9.9" in outcome.reason
        assert RUNNING_VERSION in outcome.reason

    def test_min_local_server_version_equal_to_running_is_valid(self, tmp_path):
        manifest = dict(VALID_MANIFEST, minLocalServerVersion=RUNNING_VERSION)
        write_artifact_set(tmp_path, manifest=manifest)
        assert _validate(tmp_path).status == STATUS_REGISTERED

    def test_unparseable_min_version_is_invalid(self, tmp_path):
        manifest = dict(VALID_MANIFEST, minLocalServerVersion="latest")
        write_artifact_set(tmp_path, manifest=manifest)
        outcome = _validate(tmp_path)
        assert outcome.status == STATUS_INVALID
        assert "latest" in outcome.reason

    # --- Variant-aware minimum (minLocalServerVersions map) --------------
    # LocalServer variants version on independent, non-comparable lineages,
    # so the per-arch map takes precedence over the scalar for this device's
    # arch. This is the JP6 fix: a low per-arch floor unblocks a device whose
    # own-lineage version sits below an arm64-derived scalar minimum.

    def test_per_arch_map_overrides_scalar_and_registers(self, tmp_path):
        # Scalar would block (9.9.9 > running 1.2.0), but the map gives this
        # device's arch a satisfiable floor -> the map wins and it registers.
        manifest = dict(
            VALID_MANIFEST,
            minLocalServerVersion="9.9.9",
            minLocalServerVersions={DEVICE_ARCH: "1.0.0", "arm64_jp6": "1.0.0"},
        )
        write_artifact_set(tmp_path, manifest=manifest)
        assert _validate(tmp_path).status == STATUS_REGISTERED

    def test_per_arch_map_overrides_scalar_and_invalidates(self, tmp_path):
        # Scalar would pass (1.0.0), but the map raises this arch's floor
        # above the running version -> the map wins and it is invalid.
        manifest = dict(
            VALID_MANIFEST,
            minLocalServerVersion="1.0.0",
            minLocalServerVersions={DEVICE_ARCH: "9.9.9"},
        )
        write_artifact_set(tmp_path, manifest=manifest)
        outcome = _validate(tmp_path)
        assert outcome.status == STATUS_INVALID
        assert "9.9.9" in outcome.reason
        assert RUNNING_VERSION in outcome.reason

    def test_per_arch_map_without_this_arch_falls_back_to_scalar(self, tmp_path):
        # Map present but no entry for this device's arch -> scalar applies.
        manifest = dict(
            VALID_MANIFEST,
            minLocalServerVersion="9.9.9",
            minLocalServerVersions={"arm64_jp6": "1.0.0"},
        )
        write_artifact_set(tmp_path, manifest=manifest)
        outcome = _validate(tmp_path)
        assert outcome.status == STATUS_INVALID
        assert "9.9.9" in outcome.reason


class TestEnvironment:
    def test_parse_version(self):
        assert environment.parse_version("1.2.3") == (1, 2, 3)
        assert environment.parse_version("1.2.3-beta+42") == (1, 2, 3)
        assert environment.parse_version("latest") is None
        assert environment.parse_version(None) is None

    def test_is_version_compatible(self):
        assert environment.is_version_compatible("1.0.0", "1.2.0")
        assert environment.is_version_compatible("1.2.0", "1.2.0")
        assert not environment.is_version_compatible("1.2.1", "1.2.0")
        # unknown running version cannot be proven incompatible
        assert environment.is_version_compatible("1.0.0", None)

    def test_device_arch_x86(self):
        with patch("platform.machine", return_value="x86_64"):
            assert environment.device_arch() == "x86_64"

    def test_device_arch_jetpack_variants(self):
        cases = {
            ".../aws.edgeml.dda.LocalServer.arm64JP7/1.0.0/x-aarch64": "arm64_jp7",
            ".../aws.edgeml.dda.LocalServer.arm64JP6/1.0.0/x-aarch64": "arm64_jp6",
            ".../aws.edgeml.dda.LocalServer.arm64JP5/1.0.0/x-aarch64": "arm64_jp5",
            ".../aws.edgeml.dda.LocalServer.arm64/1.0.0/x-aarch64": "arm64_jp4",
        }
        for path, expected in cases.items():
            with patch("platform.machine", return_value="aarch64"), patch.dict(
                os.environ, {"LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": path}
            ):
                assert environment.device_arch() == expected

    def test_local_server_version_from_component_path(self):
        path = (
            "/aws_dda/greengrass/v2/packages/artifacts-unarchived/"
            "aws.edgeml.dda.LocalServer.arm64/1.0.5/"
            "aws.edgeml.dda.LocalServer.arm64-aarch64"
        )
        with patch.dict(
            os.environ, {"LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH": path}
        ):
            assert environment.local_server_version() == "1.0.5"

    def test_local_server_version_unknown(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH", None)
            assert environment.local_server_version() is None
