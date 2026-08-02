#!/usr/bin/env python3
"""
Unit tests for the Plugin_Component listing extension of components.py
(custom-node-designer task 10.8, Requirement 16.2).

Covers:
- target_architectures_from_platforms: the pure inverse of
  plugin_components.platform_for, over both the recipe Manifest 'Platform'
  shape and the describe_component API shape;
- list_components (scope PRIVATE) recognizing dda.plugin.* components,
  joining them with the backing Plugin_Record via the registry tags, and
  returning name, version, Lifecycle_State, and supported
  Target_Architectures;
- non-plugin portal components staying untouched, and a missing
  Plugin_Record degrading to lifecycle_state None.

Runs the real components.py handler against the moto-backed stack from
conftest.py. moto does not implement greengrassv2 or the Resource Groups
Tagging API for Greengrass, so those two Use_Case-account clients are
MagicMocks (the test_plugin_components.py pattern); DynamoDB (use cases +
Plugin_Records) is real moto.
"""
import json
import sys
import uuid
from unittest.mock import MagicMock

import pytest

from conftest import TEST_ENV

ACCOUNT = "123456789012"
REGION = "us-east-1"

ALL_ARCHS = ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6")


@pytest.fixture(scope="module")
def components_module(aws_stack):
    """Import components.py inside the moto mock so its module-level
    shared_utils import (and boto3 clients) are intercepted."""
    sys.modules.pop("components", None)
    import components

    return components


@pytest.fixture(scope="module")
def plugin_components_module(aws_stack):
    """plugin_components.platform_for, for the inverse-derivation tests."""
    for name in ("plugin_components", "workflow_packaging"):
        sys.modules.pop(name, None)
    import plugin_components

    return plugin_components


def component_base_arn(name):
    return f"arn:aws:greengrass:{REGION}:{ACCOUNT}:components:{name}"


class TestTargetArchitectureDerivation:
    """Pure derivation of supported Target_Architectures (16.2)."""

    def test_inverse_of_platform_for_over_every_architecture(
            self, components_module, plugin_components_module):
        """Each arch's recipe platform block derives back to that arch."""
        for arch in ALL_ARCHS:
            platform = plugin_components_module.platform_for(arch)
            assert components_module.target_architectures_from_platforms(
                [platform]) == [arch]

    def test_amd64_flavors_split_by_nvidia_runtime(self, components_module):
        derived = components_module.target_architectures_from_platforms([
            {"os": "linux", "architecture": "amd64", "runtime": "nvidia"},
            {"os": "linux", "architecture": "amd64"},
        ])
        assert derived == ["x86_64_nvidia", "x86_64"]

    def test_describe_component_attributes_shape(self, components_module):
        """The API exposes each manifest's platform under 'attributes'."""
        platforms = [
            {"name": "linux-aarch64", "attributes": {
                "os": "linux", "architecture": "aarch64",
                "variant": "arm64_jp5"}},
            {"name": "linux-amd64", "attributes": {
                "os": "linux", "architecture": "amd64",
                "runtime": "nvidia"}},
        ]
        assert components_module.target_architectures_from_platforms(
            platforms) == ["arm64_jp5", "x86_64_nvidia"]

    def test_unknown_and_empty_platforms_derive_nothing(self, components_module):
        assert components_module.target_architectures_from_platforms([]) == []
        assert components_module.target_architectures_from_platforms(None) == []
        assert components_module.target_architectures_from_platforms(
            [{"os": "windows", "architecture": "x86"}, "not-a-dict"]) == []

    def test_plugin_version_from_component_version(self, components_module):
        assert components_module.plugin_version_from_component_version("3.0.0") == 3
        assert components_module.plugin_version_from_component_version("12.0.0") == 12
        assert components_module.plugin_version_from_component_version(None) is None
        assert components_module.plugin_version_from_component_version("bad") is None


class ListingEnv:
    """Facade for exercising list_components with fake Use_Case-account
    Greengrass / Tagging clients over real moto DynamoDB."""

    def __init__(self, stack, components, monkeypatch):
        self.stack = stack
        self.components = components
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.user = {"user_id": f"user-{uuid.uuid4()}"}

        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Components Listing Test Use Case",
            "account_id": ACCOUNT,
            "region": REGION,
            "cross_account_role_arn":
                f"arn:aws:iam::{ACCOUNT}:role/DDAPortalAccessRole",
            "external_id": "test-external-id",
        })

        monkeypatch.setattr(components, "check_user_access",
                            lambda user_id, usecase_id, *a, **k: True)
        monkeypatch.setattr(components, "assume_cross_account_role",
                            lambda role_arn, external_id: {"AccessKeyId": "x"})

        # Fake Use_Case-account clients (moto lacks greengrassv2, and its
        # tagging API does not serve Greengrass components).
        self.tagging = MagicMock(name="resourcegroupstaggingapi")
        self.greengrass = MagicMock(name="greengrassv2")
        self._tagged = []          # ResourceTagMappingList entries
        self._versions = {}        # base arn -> [component versions]
        self._details = {}         # describe arn -> response dict
        self.tagging.get_resources.side_effect = lambda **kw: {
            "ResourceTagMappingList": list(self._tagged)}
        self.greengrass.list_component_versions.side_effect = lambda **kw: {
            "componentVersions": [{"componentVersion": v}
                                  for v in self._versions.get(kw["arn"], [])]}
        self.greengrass.describe_component.side_effect = \
            lambda arn: self._details[arn]

        def fake_client(service, credentials, region=None):
            return {"resourcegroupstaggingapi": self.tagging,
                    "greengrassv2": self.greengrass}[service]
        monkeypatch.setattr(components, "create_boto3_client", fake_client)

    # ------------------------------------------------------------- setup
    def add_component(self, name, versions, tags, platforms):
        """Register a tagged portal component with the fake clients."""
        base_arn = component_base_arn(name)
        latest = versions[-1]
        self._tagged.append({
            "ResourceARN": f"{base_arn}:versions:{latest}",
            "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
        })
        self._versions[base_arn] = list(versions)
        self._details[f"{base_arn}:versions:{latest}"] = {
            "componentName": name,
            "description": f"{name} description",
            "platforms": platforms,
            "creationTimestamp": "2025-01-01T00:00:00+00:00",
        }
        return base_arn

    def put_plugin_record(self, plugin_id, version, lifecycle_state):
        self.stack.tables.plugin_records.put_item(Item={
            "plugin_id": plugin_id,
            "version": version,
            "usecase_id": self.usecase_id,
            "name": f"Plugin {plugin_id}",
            "created_at": 1,
            "lifecycle_state": lifecycle_state,
        })

    def plugin_tags(self, plugin_id, plugin_version):
        return {
            "dda-portal:managed": "true",
            "dda-portal:usecase-id": self.usecase_id,
            "dda-portal:plugin-id": plugin_id,
            "dda-portal:plugin-version": str(plugin_version),
        }

    # ------------------------------------------------------------ invoke
    def list_components(self):
        response = self.components.list_components(
            self.user, {"usecase_id": self.usecase_id, "scope": "PRIVATE"},
            {"Content-Type": "application/json"})
        assert response["statusCode"] == 200, response["body"]
        return {c["component_name"]: c
                for c in json.loads(response["body"])["components"]}


@pytest.fixture
def lenv(aws_stack, components_module, monkeypatch):
    return ListingEnv(aws_stack, components_module, monkeypatch)


class TestPluginComponentListing:
    """list_components joins dda.plugin.* with the Plugin_Record (16.2)."""

    def test_plugin_component_listed_with_lifecycle_and_architectures(self, lenv):
        plugin_id = f"plg-{uuid.uuid4().hex[:8]}"
        lenv.put_plugin_record(plugin_id, 3, "test")
        lenv.add_component(
            f"dda.plugin.{plugin_id}", ["1.0.0", "3.0.0"],
            lenv.plugin_tags(plugin_id, 3),
            platforms=[
                {"name": "linux-amd64", "attributes": {
                    "os": "linux", "architecture": "amd64",
                    "runtime": "nvidia"}},
                {"name": "linux-aarch64", "attributes": {
                    "os": "linux", "architecture": "aarch64",
                    "variant": "arm64_jp5"}},
            ])

        entry = lenv.list_components()[f"dda.plugin.{plugin_id}"]

        assert entry["is_plugin_component"] is True
        assert entry["plugin_id"] == plugin_id
        assert entry["plugin_version"] == 3
        assert entry["latest_version"]["componentVersion"] == "3.0.0"
        assert entry["lifecycle_state"] == "test"
        assert entry["supported_architectures"] == [
            "x86_64_nvidia", "arm64_jp5"]

    def test_latest_version_wins_over_stale_version_tag(self, lenv):
        """A stale version-level tag never pins the backing record: the
        Plugin_Record version comes from the resolved latest component
        version's major part."""
        plugin_id = f"plg-{uuid.uuid4().hex[:8]}"
        lenv.put_plugin_record(plugin_id, 1, "dev")
        lenv.put_plugin_record(plugin_id, 2, "prod")
        # Tags name version 1, but version 2.0.0 is the registry's latest.
        lenv.add_component(
            f"dda.plugin.{plugin_id}", ["1.0.0", "2.0.0"],
            lenv.plugin_tags(plugin_id, 1),
            platforms=[{"name": "linux-amd64", "attributes": {
                "os": "linux", "architecture": "amd64"}}])

        entry = lenv.list_components()[f"dda.plugin.{plugin_id}"]

        assert entry["plugin_version"] == 2
        assert entry["lifecycle_state"] == "prod"
        assert entry["supported_architectures"] == ["x86_64"]

    def test_missing_plugin_record_degrades_to_none(self, lenv):
        plugin_id = f"plg-{uuid.uuid4().hex[:8]}"
        lenv.add_component(
            f"dda.plugin.{plugin_id}", ["1.0.0"],
            lenv.plugin_tags(plugin_id, 1),
            platforms=[{"name": "linux-amd64", "attributes": {
                "os": "linux", "architecture": "amd64"}}])

        entry = lenv.list_components()[f"dda.plugin.{plugin_id}"]

        assert entry["is_plugin_component"] is True
        assert entry["lifecycle_state"] is None
        assert entry["supported_architectures"] == ["x86_64"]

    def test_non_plugin_components_are_untouched(self, lenv):
        """Ordinary portal-managed components carry none of the plugin
        fields (existing listing behavior preserved)."""
        lenv.add_component(
            "com.example.model", ["1.0.115"],
            {"dda-portal:managed": "true",
             "dda-portal:model-name": "defect-detector"},
            platforms=[{"name": "linux-amd64", "attributes": {
                "os": "linux", "architecture": "amd64"}}])

        entry = lenv.list_components()["com.example.model"]

        assert "is_plugin_component" not in entry
        assert "lifecycle_state" not in entry
        assert "supported_architectures" not in entry
        assert entry["model_name"] == "defect-detector"
        assert entry["latest_version"]["componentVersion"] == "1.0.115"
