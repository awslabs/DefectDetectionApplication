#!/usr/bin/env python3
"""
Packaging and deployment unit-test fixtures
(custom-node-designer task 10.9, Requirements 16.1, 16.2, 16.6).

Explicit example fixtures over the pure packaging / deployment functions,
completing the coverage audit of the neighbouring suites:

- amd64 manifest ordering and attribute matching with the x86_64 and
  x86_64_nvidia flavors present and absent (x86_64 only, nvidia only,
  both, neither + arm) in BOTH recipe builders —
  plugin_components.build_plugin_recipe and workflow_packaging.build_recipe
  (16.1). test_plugin_components.py and
  test_workflow_packaging_custom_plugins.py already cover "both present"
  ordering and plain-x86_64-only; the nvidia-only and arm-only combos are
  pinned here, plus the ordering-helper agreement between the two modules.
- Packaging rejection messages identifying the Custom_Node_Type, the
  missing architecture, and the lifecycle state in the human-readable
  message (pure workflow_packaging.custom_plugin_gate_findings; the API
  envelope variants live in test_workflow_packaging_custom_plugins.py
  TestPackagingGates).
- Deployment gate rejection entries per device: the exact
  {pluginComponent, version, device, deviceArch} shape of
  deployments.evaluate_plugin_arch_gate, and both amd64 flavors matching
  distinctly when both manifests are present (16.6; single-flavor
  no-fallback cases live in test_deployment_plugin_gates.py
  TestArchGatePure).
- Plugin_Component listing fields (16.2): the listing's architecture
  derivation round-trips the recipes build_plugin_recipe actually
  produces, and the component-version inverse; the full listing
  field set is integration-tested in test_components_plugin_listing.py
  TestPluginComponentListing.

All tests are pure over module functions; aws_stack is only used so the
modules import inside the moto mock (their module-level boto3 clients).
"""
import sys

import pytest


@pytest.fixture(scope="module")
def plugin_components_module(aws_stack):
    """plugin_components inside the moto mock (module-level clients)."""
    for name in ("plugin_components", "workflow_packaging"):
        sys.modules.pop(name, None)
    import plugin_components

    return plugin_components


@pytest.fixture(scope="module")
def packaging(aws_stack):
    """workflow_packaging inside the moto mock."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """deployments inside the moto mock."""
    for name in ("deployments", "workflow_guards"):
        sys.modules.pop(name, None)
    import deployments

    return deployments


@pytest.fixture(scope="module")
def components_module(aws_stack):
    """components inside the moto mock."""
    sys.modules.pop("components", None)
    import components

    return components


def platforms_of(recipe):
    return [m["Platform"] for m in recipe["Manifests"]]


# ==========================================================================
# 16.1 — amd64 manifest ordering / attribute matching:
# plugin_components.build_plugin_recipe
# ==========================================================================

class TestPluginRecipeAmd64Matrix:
    """Plugin_Component recipes across the x86_64 / x86_64_nvidia matrix."""

    def build(self, module, archs):
        return module.build_plugin_recipe(
            "plg-m", 4, "acct-bucket", {a: "p.so" for a in archs})

    def test_x86_64_only(self, plugin_components_module):
        """Plain flavor alone: one amd64 manifest, no runtime attribute, so
        attribute-less amd64 devices match it."""
        recipe = self.build(plugin_components_module, ["x86_64"])
        assert platforms_of(recipe) == [
            {"os": "linux", "architecture": "amd64"}]

    def test_x86_64_nvidia_only(self, plugin_components_module):
        """NVIDIA flavor alone still carries 'runtime: nvidia' so an
        attribute-less amd64 device can never match it (no fallback)."""
        recipe = self.build(plugin_components_module, ["x86_64_nvidia"])
        assert platforms_of(recipe) == [
            {"os": "linux", "architecture": "amd64", "runtime": "nvidia"}]

    def test_both_flavors_nvidia_ordered_first(self, plugin_components_module):
        """Both flavors: the more specific nvidia manifest precedes the
        attribute-less plain manifest."""
        recipe = self.build(plugin_components_module,
                            ["x86_64", "x86_64_nvidia"])
        assert platforms_of(recipe) == [
            {"os": "linux", "architecture": "amd64", "runtime": "nvidia"},
            {"os": "linux", "architecture": "amd64"},
        ]

    def test_neither_flavor_arm_only(self, plugin_components_module):
        """No amd64 flavor built: no amd64 manifest exists at all; the
        JetPack manifest always carries its 'variant' attribute."""
        recipe = self.build(plugin_components_module, ["arm64_jp5"])
        assert platforms_of(recipe) == [
            {"os": "linux", "architecture": "aarch64", "variant": "arm64_jp5"}]

    def test_full_matrix_ordering_with_arm(self, plugin_components_module):
        """arm + both amd64 flavors: sorted order except plain x86_64
        re-ordered directly after x86_64_nvidia."""
        recipe = self.build(plugin_components_module,
                            ["x86_64", "arm64_jp5", "x86_64_nvidia"])
        assert platforms_of(recipe) == [
            {"os": "linux", "architecture": "aarch64", "variant": "arm64_jp5"},
            {"os": "linux", "architecture": "amd64", "runtime": "nvidia"},
            {"os": "linux", "architecture": "amd64"},
        ]


# ==========================================================================
# 16.1 — amd64 manifest ordering / attribute matching:
# workflow_packaging.build_recipe
# ==========================================================================

class TestWorkflowRecipeAmd64Matrix:
    """Workflow_Component recipes across the same amd64 flavor matrix."""

    def build(self, module, archs):
        final_keys = {
            a: f"workflows/components/wf-m/2/{a}/workflow-{a}.zip"
            for a in archs}
        return module.build_recipe("wf-m", 2, "acct-bucket", final_keys)

    def test_x86_64_only(self, packaging):
        recipe = self.build(packaging, ["x86_64"])
        assert platforms_of(recipe) == [
            {"os": "linux", "architecture": "amd64"}]

    def test_x86_64_nvidia_only(self, packaging):
        """nvidia-only workflow package still carries 'runtime: nvidia'
        (no fallback for plain amd64 devices)."""
        recipe = self.build(packaging, ["x86_64_nvidia"])
        assert platforms_of(recipe) == [
            {"os": "linux", "architecture": "amd64", "runtime": "nvidia"}]

    def test_both_flavors_nvidia_ordered_first(self, packaging):
        recipe = self.build(packaging, ["x86_64", "x86_64_nvidia"])
        assert platforms_of(recipe) == [
            {"os": "linux", "architecture": "amd64", "runtime": "nvidia"},
            {"os": "linux", "architecture": "amd64"},
        ]

    def test_neither_flavor_arm_only(self, packaging):
        """No amd64 flavor selected: no amd64 manifest; a single arm arch
        needs no 'variant' disambiguation in workflow recipes."""
        recipe = self.build(packaging, ["arm64_jp5"])
        assert platforms_of(recipe) == [
            {"os": "linux", "architecture": "aarch64"}]

    def test_full_matrix_ordering_with_arm(self, packaging):
        recipe = self.build(packaging,
                            ["x86_64", "arm64_jp5", "x86_64_nvidia"])
        architectures = [p["architecture"] for p in platforms_of(recipe)]
        runtimes = [p.get("runtime") for p in platforms_of(recipe)]
        assert architectures == ["aarch64", "amd64", "amd64"]
        assert runtimes == [None, "nvidia", None]

    def test_ordering_helpers_agree_across_modules(
            self, packaging, plugin_components_module):
        """Both packagers order the amd64 flavors identically, so a device
        matches the same flavor of a workflow and its plugin dependencies."""
        combos = (
            ["x86_64"],
            ["x86_64_nvidia"],
            ["x86_64", "x86_64_nvidia"],
            ["arm64_jp5"],
            ["x86_64", "arm64_jp5", "x86_64_nvidia"],
            ["x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6"],
        )
        for archs in combos:
            assert (packaging.recipe_manifest_order(archs)
                    == plugin_components_module.manifest_arch_order(archs)), archs


# ==========================================================================
# Packaging rejection messages identify node / arch / state
# (custom_plugin_gate_findings; API envelope in
# test_workflow_packaging_custom_plugins.py TestPackagingGates)
# ==========================================================================

def make_record(plugin_id="plg-1", version=2, state="test",
                archs=("x86_64",), component_status="registered"):
    """A minimal backing Plugin_Record item as the gates consume it."""
    return {
        "plugin_id": plugin_id,
        "version": version,
        "lifecycle_state": state,
        "artifacts": {
            arch: {"buildStatus": "succeeded",
                   "s3Key": f"workflow-plugins/custom/uc/{arch}/p.so",
                   "checksum": "aa" * 32, "signature": "c2ln"}
            for arch in archs
        },
        "component": {"status": component_status},
    }


class TestPackagingRejectionMessages:
    DEP = "custom:blur-regions"
    INDEX = {DEP: {"node_type_id": "nt-blur", "plugin_id": "plg-1",
                   "plugin_version": 2}}

    def test_dev_state_message_names_node_and_state(self, packaging):
        """Lifecycle rejection: message identifies the Custom_Node_Type and
        the offending Lifecycle_State (11.3 wording carried by the 409)."""
        record = make_record(state="dev")
        [finding] = packaging.custom_plugin_gate_findings(
            {"x86_64": [self.DEP]}, self.INDEX, {self.DEP: record})

        assert finding["code"] == "PLUGIN_LIFECYCLE_VIOLATION"
        assert finding["node_type_id"] == "nt-blur"
        assert finding["lifecycle_state"] == "dev"
        assert "'nt-blur'" in finding["message"]
        assert "'dev'" in finding["message"]
        assert "'plg-1' v2" in finding["message"]

    def test_missing_arch_message_names_node_and_arch(self, packaging):
        """Artifact rejection: message identifies the Custom_Node_Type and
        the missing Target_Architecture (11.2 wording)."""
        record = make_record(archs=("x86_64",))
        [finding] = packaging.custom_plugin_gate_findings(
            {"x86_64": [self.DEP], "arm64_jp5": [self.DEP]},
            self.INDEX, {self.DEP: record})

        assert finding["code"] == "PLUGIN_ARTIFACT_MISSING"
        assert finding["node_type_id"] == "nt-blur"
        assert finding["arch"] == "arm64_jp5"
        assert "'nt-blur'" in finding["message"]
        assert "'arm64_jp5'" in finding["message"]

    def test_missing_component_message_names_node_and_component(self, packaging):
        """Unregistered Plugin_Component rejects packaging naming the node
        and the dda.plugin.* component (16.4 dependency prerequisite)."""
        record = make_record(component_status="failed")
        [finding] = packaging.custom_plugin_gate_findings(
            {"x86_64": [self.DEP]}, self.INDEX, {self.DEP: record})

        assert finding["code"] == "PLUGIN_COMPONENT_MISSING"
        assert finding["node_type_id"] == "nt-blur"
        assert "'nt-blur'" in finding["message"]
        assert "dda.plugin.plg-1" in finding["message"]

    def test_clean_record_produces_no_findings(self, packaging):
        record = make_record(archs=("x86_64", "arm64_jp5"))
        findings = packaging.custom_plugin_gate_findings(
            {"x86_64": [self.DEP], "arm64_jp5": [self.DEP]},
            self.INDEX, {self.DEP: record})
        assert findings == []


# ==========================================================================
# 16.6 — deployment gate rejection entries per device
# ==========================================================================

class TestDeploymentArchGateFixtures:
    """The per-device rejection shape of evaluate_plugin_arch_gate; the
    single-flavor no-fallback cases live in test_deployment_plugin_gates.py."""

    def test_rejection_entry_shape_per_device(self, deployments):
        """Every offending pair carries exactly pluginComponent, version,
        device, and deviceArch (the PLUGIN_ARCH_UNSUPPORTED details)."""
        offending = deployments.evaluate_plugin_arch_gate(
            {"dda.plugin.p1": {"version": "2.0.0",
                               "architectures": ["arm64_jp5"]}},
            {"line-a": "x86_64", "line-b": "x86_64_nvidia"})

        assert offending == [
            {"pluginComponent": "dda.plugin.p1", "version": "2.0.0",
             "device": "line-a", "deviceArch": "x86_64"},
            {"pluginComponent": "dda.plugin.p1", "version": "2.0.0",
             "device": "line-b", "deviceArch": "x86_64_nvidia"},
        ]
        for entry in offending:
            assert set(entry) == {"pluginComponent", "version",
                                  "device", "deviceArch"}

    def test_both_amd64_flavors_present_match_both_device_kinds(self, deployments):
        """A component packaged with both amd64 flavors deploys to plain
        x86_64 and x86_64_nvidia devices alike (matched distinctly)."""
        offending = deployments.evaluate_plugin_arch_gate(
            {"dda.plugin.p1": {"version": "1.0.0",
                               "architectures": ["x86_64", "x86_64_nvidia"]}},
            {"d-plain": "x86_64", "d-nvidia": "x86_64_nvidia"})
        assert offending == []


# ==========================================================================
# 16.2 — Plugin_Component listing fields
# ==========================================================================

class TestListingFieldDerivationRoundTrip:
    """The deployment-screen listing derives its fields from exactly what
    build_plugin_recipe publishes (integration in
    test_components_plugin_listing.py TestPluginComponentListing)."""

    @pytest.mark.parametrize("archs", [
        ["x86_64"],
        ["x86_64_nvidia"],
        ["x86_64", "x86_64_nvidia"],
        ["arm64_jp5"],
        ["x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6"],
    ], ids=lambda a: "+".join(a))
    def test_supported_architectures_round_trip_the_recipe(
            self, plugin_components_module, components_module, archs):
        """target_architectures_from_platforms over a freshly built recipe's
        manifests recovers the packaged Target_Architectures exactly."""
        recipe = plugin_components_module.build_plugin_recipe(
            "plg-rt", 7, "b", {a: "p.so" for a in archs})
        derived = components_module.target_architectures_from_platforms(
            [m["Platform"] for m in recipe["Manifests"]])
        assert sorted(derived) == sorted(archs)

    def test_listing_version_inverts_recipe_component_version(
            self, plugin_components_module, components_module):
        """The listing's plugin_version is the inverse of the packaged
        ComponentVersion for the Plugin_Record versions the recipe emits."""
        for plugin_version in (1, 7, 12):
            component_version = plugin_components_module.component_version_for(
                plugin_version)
            assert components_module.plugin_version_from_component_version(
                component_version) == plugin_version

    def test_listing_name_matches_recipe_component_name(
            self, plugin_components_module, components_module):
        """The recipe's ComponentName is what the listing recognizes as a
        Plugin_Component (name field on the deployment screen)."""
        name = plugin_components_module.component_name_for("plg-rt")
        assert name == "dda.plugin.plg-rt"
        assert components_module.is_plugin_component(name) is True
        assert components_module.is_plugin_component("com.example.model") is False
