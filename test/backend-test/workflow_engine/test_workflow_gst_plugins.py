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
"""Unit tests for manifest plugin checksum verification and the extended
plugin scan path (custom-node-designer Requirements 10.6, 11.4).

Covers the pure verification decision (``verify_plugin_checksums``),
checksum-key path resolution (inline vs Plugin_Component-installed),
the run-scoped loader skipping mismatched plugins before the registry
scan, and discovery registering a workflow with a mismatched plugin as
invalid with the file identified.
"""
import hashlib
import json
import os
import sys
from unittest.mock import patch

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    RUNNING_VERSION,
    VALID_MANIFEST,
    write_artifact_set,
)

from workflow_engine import gst_plugins
from workflow_engine.discovery import (
    STATUS_INVALID,
    STATUS_REGISTERED,
    scan_workflow_root,
    validate_artifact_set,
)

COMPONENT_NAME = "dda.plugin.plg-1"
COMPONENT_VERSION = "2.0.0"

GOOD_BYTES = b"good plugin bytes"
GOOD_SHA = hashlib.sha256(GOOD_BYTES).hexdigest()


def manifest_with(plugin_checksums=None, plugin_components=None):
    manifest = dict(VALID_MANIFEST)
    manifest["pluginChecksums"] = dict(plugin_checksums or {})
    manifest["pluginComponents"] = dict(plugin_components or {})
    return manifest


class TestVerifyPluginChecksums:
    """The pure decision: (manifest, file-bytes lookup) -> verified/failures."""

    def test_no_checksums_verifies_vacuously(self):
        outcome = gst_plugins.verify_plugin_checksums(
            dict(VALID_MANIFEST), lambda key: None
        )
        assert outcome.ok
        assert outcome.verified == ()
        assert outcome.failures == ()

    def test_matching_bytes_verify(self):
        manifest = manifest_with({f"{COMPONENT_NAME}/a.so": GOOD_SHA})
        outcome = gst_plugins.verify_plugin_checksums(
            manifest, lambda key: GOOD_BYTES
        )
        assert outcome.ok
        assert outcome.verified == (f"{COMPONENT_NAME}/a.so",)

    def test_mismatched_bytes_fail_identifying_the_file(self):
        key = f"{COMPONENT_NAME}/a.so"
        manifest = manifest_with({key: GOOD_SHA})
        outcome = gst_plugins.verify_plugin_checksums(
            manifest, lambda k: b"tampered bytes"
        )
        assert not outcome.ok
        ((failed_key, reason),) = outcome.failures
        assert failed_key == key
        assert "mismatch" in reason
        assert GOOD_SHA in reason  # the recorded checksum is reported

    def test_missing_file_fails_identifying_the_file(self):
        key = "plugins/x86_64/gone.so"
        manifest = manifest_with({key: GOOD_SHA})
        outcome = gst_plugins.verify_plugin_checksums(manifest, lambda k: None)
        assert not outcome.ok
        ((failed_key, reason),) = outcome.failures
        assert failed_key == key
        assert "missing" in reason

    def test_mixed_entries_split_between_verified_and_failures(self):
        contents = {"ok.so": GOOD_BYTES, "bad.so": b"tampered"}
        manifest = manifest_with(
            {"ok.so": GOOD_SHA, "bad.so": GOOD_SHA, "gone.so": GOOD_SHA}
        )
        outcome = gst_plugins.verify_plugin_checksums(
            manifest, lambda key: contents.get(key)
        )
        assert outcome.verified == ("ok.so",)
        assert sorted(key for key, _ in outcome.failures) == [
            "bad.so",
            "gone.so",
        ]

    def test_non_object_checksums_fail(self):
        manifest = dict(VALID_MANIFEST)
        manifest["pluginChecksums"] = ["not", "a", "dict"]
        outcome = gst_plugins.verify_plugin_checksums(manifest, lambda k: None)
        assert not outcome.ok

    def test_non_string_checksum_fails(self):
        manifest = manifest_with({"a.so": 12345})
        outcome = gst_plugins.verify_plugin_checksums(
            manifest, lambda k: GOOD_BYTES
        )
        assert not outcome.ok


class TestResolveChecksumPath:
    def test_component_key_resolves_under_the_install_root(self, tmp_path):
        manifest = manifest_with(
            plugin_components={COMPONENT_NAME: COMPONENT_VERSION}
        )
        path = gst_plugins.resolve_checksum_path(
            f"{COMPONENT_NAME}/blur.so",
            manifest,
            str(tmp_path),
            plugins_root="/aws_dda/plugins",
        )
        # dda.plugin.plg-1 at component version 2.0.0 installs to
        # /aws_dda/plugins/plg-1/2/{arch}/ (plugin_components.py layout)
        assert path == f"/aws_dda/plugins/plg-1/2/{DEVICE_ARCH}/blur.so"

    def test_inline_relative_key_resolves_under_the_artifact_dir(self, tmp_path):
        manifest = manifest_with()
        path = gst_plugins.resolve_checksum_path(
            f"plugins/{DEVICE_ARCH}/curated.so", manifest, str(tmp_path)
        )
        assert path == os.path.join(
            str(tmp_path), "plugins", DEVICE_ARCH, "curated.so"
        )

    def test_bare_file_name_resolves_under_the_inline_arch_dir(self, tmp_path):
        manifest = manifest_with()
        path = gst_plugins.resolve_checksum_path(
            "curated.so", manifest, str(tmp_path)
        )
        assert path == os.path.join(
            str(tmp_path), "plugins", DEVICE_ARCH, "curated.so"
        )

    def test_plugin_scan_dirs_cover_inline_and_component_roots(self, tmp_path):
        manifest = manifest_with(
            plugin_components={COMPONENT_NAME: COMPONENT_VERSION}
        )
        dirs = gst_plugins.plugin_scan_dirs(
            manifest, str(tmp_path), plugins_root="/aws_dda/plugins"
        )
        assert dirs == [
            os.path.join(str(tmp_path), "plugins", DEVICE_ARCH),
            f"/aws_dda/plugins/plg-1/2/{DEVICE_ARCH}",
        ]


def _write(path, data=GOOD_BYTES):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


class TestWorkflowPluginPath:
    """The run-scoped loader: verify before scan, skip mismatches."""

    def test_component_install_root_joins_the_scan_path(self, tmp_path):
        artifact = tmp_path / "artifact"
        plugins_root = tmp_path / "device-plugins"
        inline_dir = artifact / "plugins" / DEVICE_ARCH
        _write(str(inline_dir / "curated.so"))
        component_file = _write(
            str(plugins_root / "plg-1" / "2" / DEVICE_ARCH / "custom.so")
        )
        manifest = manifest_with(
            {f"{COMPONENT_NAME}/custom.so": GOOD_SHA},
            {COMPONENT_NAME: COMPONENT_VERSION},
        )
        with patch.object(
            gst_plugins, "_scan_registry", return_value=True
        ) as scan:
            with gst_plugins.workflow_plugin_path(
                str(inline_dir),
                manifest=manifest,
                artifact_path=str(artifact),
                plugins_root=str(plugins_root),
            ) as applied:
                assert applied
                env = os.environ[gst_plugins.GST_PLUGIN_PATH_ENV]
                assert str(inline_dir) in env
                assert os.path.dirname(component_file) in env
        scanned = [call.args[0] for call in scan.call_args_list]
        assert str(inline_dir) in scanned
        assert os.path.dirname(component_file) in scanned

    def test_mismatched_plugin_is_skipped_before_the_scan(self, tmp_path):
        artifact = tmp_path / "artifact"
        plugins_root = tmp_path / "device-plugins"
        inline_dir = artifact / "plugins" / DEVICE_ARCH
        _write(str(inline_dir / "curated.so"))
        component_dir = plugins_root / "plg-1" / "2" / DEVICE_ARCH
        _write(str(component_dir / "custom.so"), b"tampered bytes")
        manifest = manifest_with(
            {f"{COMPONENT_NAME}/custom.so": GOOD_SHA},
            {COMPONENT_NAME: COMPONENT_VERSION},
        )
        with patch.object(
            gst_plugins, "_scan_registry", return_value=True
        ) as scan:
            with gst_plugins.workflow_plugin_path(
                str(inline_dir),
                manifest=manifest,
                artifact_path=str(artifact),
                plugins_root=str(plugins_root),
            ) as applied:
                # The verified inline dir still applies; the mismatched
                # component plugin is never scanned (fail closed, 10.6).
                assert applied
                env = os.environ[gst_plugins.GST_PLUGIN_PATH_ENV]
                assert str(component_dir) not in env
        scanned = [call.args[0] for call in scan.call_args_list]
        assert scanned == [str(inline_dir)]

    def test_without_manifest_behavior_is_unchanged(self, tmp_path):
        # Bundled plugins / Pipeline_Configuration execution unaffected:
        # the pre-existing single-directory contract holds exactly.
        inline_dir = tmp_path / "plugins" / DEVICE_ARCH
        _write(str(inline_dir / "curated.so"))
        before = os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV)
        with patch.object(gst_plugins, "_scan_registry", return_value=True):
            with gst_plugins.workflow_plugin_path(str(inline_dir)) as applied:
                assert applied
                env = os.environ[gst_plugins.GST_PLUGIN_PATH_ENV]
                assert env.split(":")[0] == str(inline_dir)
        assert os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV) == before

    def test_env_is_restored_after_a_skip(self, tmp_path):
        artifact = tmp_path / "artifact"
        inline_dir = artifact / "plugins" / DEVICE_ARCH
        _write(str(inline_dir / "bad.so"), b"tampered")
        manifest = manifest_with({f"plugins/{DEVICE_ARCH}/bad.so": GOOD_SHA})
        before = os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV)
        with patch.object(gst_plugins, "_scan_registry", return_value=True) as scan:
            with gst_plugins.workflow_plugin_path(
                str(inline_dir),
                manifest=manifest,
                artifact_path=str(artifact),
            ) as applied:
                assert not applied  # the only directory was skipped
        assert scan.call_count == 0
        assert os.environ.get(gst_plugins.GST_PLUGIN_PATH_ENV) == before


class TestDiscoveryChecksumValidation:
    """A mismatch registers the workflow as invalid with the file
    identified, through the existing validation/status path (10.6)."""

    def _validate(self, root, plugins_root):
        (artifact_set,) = scan_workflow_root(str(root))
        return validate_artifact_set(
            artifact_set,
            device_arch=DEVICE_ARCH,
            running_version=RUNNING_VERSION,
            plugins_root=str(plugins_root),
        )

    def test_verified_plugins_register(self, tmp_path):
        root = tmp_path / "workflows"
        plugins_root = tmp_path / "device-plugins"
        _write(str(plugins_root / "plg-1" / "2" / DEVICE_ARCH / "custom.so"))
        manifest = manifest_with(
            {f"{COMPONENT_NAME}/custom.so": GOOD_SHA},
            {COMPONENT_NAME: COMPONENT_VERSION},
        )
        version_dir = write_artifact_set(root, manifest=manifest)
        _write(
            os.path.join(version_dir, "plugins", DEVICE_ARCH, "curated.so")
        )
        outcome = self._validate(root, plugins_root)
        assert outcome.status == STATUS_REGISTERED

    def test_inline_checksum_entry_is_verified_too(self, tmp_path):
        root = tmp_path / "workflows"
        manifest = manifest_with(
            {f"plugins/{DEVICE_ARCH}/curated.so": GOOD_SHA}
        )
        version_dir = write_artifact_set(root, manifest=manifest)
        _write(
            os.path.join(version_dir, "plugins", DEVICE_ARCH, "curated.so")
        )
        outcome = self._validate(root, tmp_path / "device-plugins")
        assert outcome.status == STATUS_REGISTERED

    def test_mismatch_is_invalid_with_the_file_identified(self, tmp_path):
        root = tmp_path / "workflows"
        plugins_root = tmp_path / "device-plugins"
        _write(
            str(plugins_root / "plg-1" / "2" / DEVICE_ARCH / "custom.so"),
            b"tampered bytes",
        )
        manifest = manifest_with(
            {f"{COMPONENT_NAME}/custom.so": GOOD_SHA},
            {COMPONENT_NAME: COMPONENT_VERSION},
        )
        write_artifact_set(root, manifest=manifest)
        outcome = self._validate(root, plugins_root)
        assert outcome.status == STATUS_INVALID
        assert "checksum" in outcome.reason
        assert f"{COMPONENT_NAME}/custom.so" in outcome.reason

    def test_missing_component_file_is_invalid(self, tmp_path):
        root = tmp_path / "workflows"
        manifest = manifest_with(
            {f"{COMPONENT_NAME}/custom.so": GOOD_SHA},
            {COMPONENT_NAME: COMPONENT_VERSION},
        )
        write_artifact_set(root, manifest=manifest)
        outcome = self._validate(root, tmp_path / "device-plugins")
        assert outcome.status == STATUS_INVALID
        assert f"{COMPONENT_NAME}/custom.so" in outcome.reason

    def test_manifest_without_checksums_is_unaffected(self, tmp_path):
        root = tmp_path / "workflows"
        write_artifact_set(root)  # VALID_MANIFEST has no pluginChecksums
        outcome = self._validate(root, tmp_path / "device-plugins")
        assert outcome.status == STATUS_REGISTERED


class TestFactoryPreflightContainment:
    """The pipeline-factory preflight helpers are contained: without a
    usable GStreamer they disable the guard by reporting nothing, never
    raising (Requirement 13.7). ``gi`` is forced unavailable so the
    behavior is deterministic on machines that do have GStreamer."""

    def test_missing_factories_without_gstreamer_reports_nothing(self):
        with patch.dict(sys.modules, {"gi": None}):
            assert gst_plugins.missing_factories(
                ["resize_image", "fakesink"]
            ) == []

    def test_provided_elements_without_directories_is_empty(self, tmp_path):
        assert gst_plugins.provided_elements(str(tmp_path / "absent")) == []

    def test_provided_elements_without_gstreamer_is_empty(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "libgstcustom.so").write_bytes(b"\x7fELF")
        with patch.dict(sys.modules, {"gi": None}):
            assert gst_plugins.provided_elements(str(plugin_dir)) == []
