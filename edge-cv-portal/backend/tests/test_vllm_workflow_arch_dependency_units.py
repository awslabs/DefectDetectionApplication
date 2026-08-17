"""Portal-leg unit tests for vllm-model-reload-after-backend-restart
task 4.6 (design fix-check case 7 + design Unit Tests).

Two subjects, both exercised DIRECTLY (no moto tables needed — the
functions under test take plain records / a service-shaped client):

1. ``_model_arch_contradiction_guard`` (design Decision 6) — the
   defense-in-depth arch-contradiction guard: a resolved model
   component's own recipe HARD-depending on a LocalServer variant
   serving NONE of the selected architectures refuses packaging with a
   PackagingError naming the model component, the contradicting
   variant, and the target architecture(s); a matching variant passes;
   every read-side failure FAILS OPEN (warn and proceed): no LocalServer
   dependency, component not found, ``get_component`` raising, a
   missing ``ComponentDependencies`` block, malformed recipe JSON.

2. ``_resolve_vllm_components`` (design Decision 5) — the
   per-architecture vLLM resolution: modern (both evidence sources),
   intermediate (``components``-only / plural-only), and legacy
   (unsuffixed-only) record shapes; malformed entries (non-dict, blank
   names, base-name echoes) skipped; secondary-source target matching
   uses PRIMARY publish-target ids only (the vision-only
   ``onnx-jetson-xavier-jp7`` extra acceptance NEVER counts as
   arm64_jp7 coverage).

Harness: conftest ``aws_stack`` (session-scoped moto) so the module's
import-time boto3 clients bind to the mock; ``workflow_packaging``
imported fresh. The Greengrass fake is service-shaped for exactly the
guard's read traffic (``get_paginator('list_components')`` +
``get_component(recipeOutputFormat='JSON')``), the
test_vllm_multi_arch_publish_units.py convention.

Run from edge-cv-portal/backend WITH conftest:
    python3 -m pytest tests/test_vllm_workflow_arch_dependency_units.py \
        -q -p no:cacheprovider
"""
import json
import sys
from unittest import mock

import pytest

from conftest import REGION

JP6_VARIANT = "aws.edgeml.dda.LocalServer.arm64JP6"
JP7_VARIANT = "aws.edgeml.dda.LocalServer.arm64JP7"

#: The vision-only extra jp7 publish-target id the vLLM secondary source
#: must NEVER accept (Decision 5: primary ids only).
JP7_EXTRA_TARGET = "onnx-jetson-xavier-jp7"

#: Recorded 2.6 remediation fragments (task 3.6 OUTCOME).
LEGACY_REMEDIATION = "this record predates per-JetPack vLLM components"
NON_LEGACY_REMEDIATION = ("re-publish the model for every selected "
                          "architecture before packaging workflows")


# --------------------------------------------------------------------------
# Fixture: workflow_packaging freshly imported inside the moto session
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging(aws_stack):
    for module_name in ("workflow_packaging", "node_catalog_resolution",
                        "model_registry_snapshot"):
        sys.modules.pop(module_name, None)
    import workflow_packaging
    yield workflow_packaging
    sys.modules.pop("workflow_packaging", None)


# --------------------------------------------------------------------------
# Service-shaped Greengrass fake (the guard's exact read traffic)
# --------------------------------------------------------------------------

def component_arn(name, version="1.0.0"):
    return (f"arn:aws:greengrass:{REGION}:123456789012:components:"
            f"{name}:versions:{version}")


class FakeGreengrass:
    """``list_components`` paginator + ``get_component`` fake.

    ``components`` maps componentName -> versioned arn (or None for a
    listed component WITHOUT a latest version); ``recipes`` maps arn ->
    raw recipe blob (a str, as botocore would after ``.read()``);
    ``get_component_error`` forces the read-failure leg.
    """

    def __init__(self, components=None, recipes=None,
                 get_component_error=None):
        self.components = components or {}
        self.recipes = recipes or {}
        self.get_component_error = get_component_error
        self.get_component_calls = []

    def get_paginator(self, operation):
        assert operation == "list_components", operation
        outer = self

        class _Paginator:
            def paginate(self, scope):
                assert scope == "PRIVATE", scope
                yield {"components": [
                    {"componentName": name,
                     "latestVersion": {"arn": arn} if arn else {}}
                    for name, arn in outer.components.items()]}

        return _Paginator()

    def get_component(self, recipeOutputFormat, arn):
        assert recipeOutputFormat == "JSON", recipeOutputFormat
        self.get_component_calls.append(arn)
        if self.get_component_error is not None:
            raise self.get_component_error
        return {"recipe": self.recipes[arn]}


def greengrass_with_recipe(name, recipe):
    """A fake serving one component whose latest-version recipe is
    ``recipe`` (dict → JSON-encoded; str → raw malformed blob)."""
    arn = component_arn(name)
    blob = json.dumps(recipe) if isinstance(recipe, dict) else recipe
    return FakeGreengrass(components={name: arn}, recipes={arn: blob})


def recipe_with_dependencies(dependencies):
    return {
        "RecipeFormatVersion": "2020-01-25",
        "ComponentName": "irrelevant",
        "ComponentVersion": "1.0.0",
        "ComponentDependencies": dependencies,
    }


MODEL_JP6 = "model-vllm-unit-jetson-xavier-jp6"


# --------------------------------------------------------------------------
# Design fix-check case 7: the arch-contradiction guard
# --------------------------------------------------------------------------

class TestArchContradictionGuard:
    """**Feature: vllm-model-reload-after-backend-restart** — design
    fix-check case 7 (Decision 6 guard units)."""

    def test_contradiction_raises_naming_component_variant_and_arch(
            self, packaging):
        """A resolved model component whose recipe HARD-depends on the
        JP6 LocalServer variant while the workflow targets arm64_jp7
        raises PackagingError naming the model component, the
        contradicting variant, AND the target architecture.

        **Validates: Requirements 2.6**
        """
        greengrass = greengrass_with_recipe(
            MODEL_JP6, recipe_with_dependencies(
                {JP6_VARIANT: {"VersionRequirement": ">=1.0.0",
                               "DependencyType": "HARD"}}))
        with pytest.raises(packaging.PackagingError) as info:
            packaging._model_arch_contradiction_guard(
                greengrass, {"m": {MODEL_JP6}}, ["arm64_jp7"])
        message = info.value.message
        assert MODEL_JP6 in message
        assert JP6_VARIANT in message
        assert "arm64_jp7" in message

    def test_matching_variant_passes(self, packaging):
        """A recipe depending on the selected architecture's own
        LocalServer variant passes the guard silently.

        **Validates: Requirements 2.6**
        """
        greengrass = greengrass_with_recipe(
            MODEL_JP6, recipe_with_dependencies(
                {JP6_VARIANT: {"VersionRequirement": ">=1.0.0",
                               "DependencyType": "HARD"}}))
        with mock.patch.object(packaging.logger, "warning") as warning:
            packaging._model_arch_contradiction_guard(
                greengrass, {"m": {MODEL_JP6}}, ["arm64_jp6"])
        assert not warning.called
        assert greengrass.get_component_calls == [component_arn(MODEL_JP6)]

    def test_multiple_local_server_keys_all_matching_pass(self, packaging):
        """Recipe parsing collects EVERY aws.edgeml.dda.LocalServer.*
        key: a recipe naming both JP6 and JP7 variants passes when the
        selection covers both.

        **Validates: Requirements 2.6**
        """
        greengrass = greengrass_with_recipe(
            MODEL_JP6, recipe_with_dependencies({
                JP6_VARIANT: {"DependencyType": "HARD"},
                JP7_VARIANT: {"DependencyType": "HARD"},
                "dda.plugin.plg-x": {"DependencyType": "HARD"},
            }))
        packaging._model_arch_contradiction_guard(
            greengrass, {"m": {MODEL_JP6}}, ["arm64_jp6", "arm64_jp7"])

    def test_multiple_local_server_keys_one_contradicting_raises(
            self, packaging):
        """With multiple LocalServer keys, only the variant(s) serving
        none of the selected architectures are named as contradicting.

        **Validates: Requirements 2.6**
        """
        greengrass = greengrass_with_recipe(
            MODEL_JP6, recipe_with_dependencies({
                JP6_VARIANT: {"DependencyType": "HARD"},
                JP7_VARIANT: {"DependencyType": "HARD"},
            }))
        with pytest.raises(packaging.PackagingError) as info:
            packaging._model_arch_contradiction_guard(
                greengrass, {"m": {MODEL_JP6}}, ["arm64_jp6"])
        message = info.value.message
        assert JP7_VARIANT in message
        assert "arm64_jp6" in message

    def test_no_local_server_dependency_warns_and_proceeds(self, packaging):
        """A recipe naming no LocalServer dependency passes with a
        warning naming the component (fail-open: the guard is
        secondary).

        **Validates: Requirements 2.6**
        """
        greengrass = greengrass_with_recipe(
            MODEL_JP6, recipe_with_dependencies(
                {"dda.plugin.plg-x": {"DependencyType": "HARD"}}))
        with mock.patch.object(packaging.logger, "warning") as warning:
            packaging._model_arch_contradiction_guard(
                greengrass, {"m": {MODEL_JP6}}, ["arm64_jp7"])
        assert warning.called
        assert any(MODEL_JP6 in str(call)
                   for call in warning.call_args_list)

    def test_component_not_found_warns_and_proceeds(self, packaging):
        """A resolved component absent from the Use_Case account (or
        listed without a latest version) is skipped with a warning —
        never an error.

        **Validates: Requirements 2.6**
        """
        for greengrass in (
                FakeGreengrass(),  # not listed at all
                FakeGreengrass(components={MODEL_JP6: None})):  # no version
            with mock.patch.object(packaging.logger, "warning") as warning:
                packaging._model_arch_contradiction_guard(
                    greengrass, {"m": {MODEL_JP6}}, ["arm64_jp7"])
            assert warning.called
            assert greengrass.get_component_calls == []

    def test_get_component_raising_warns_and_proceeds(self, packaging):
        """ANY get_component failure (throttle, transient API error)
        warns and proceeds — the guard never makes packaging flakier
        than the primary per-arch resolution.

        **Validates: Requirements 2.6**
        """
        greengrass = FakeGreengrass(
            components={MODEL_JP6: component_arn(MODEL_JP6)},
            get_component_error=RuntimeError("ThrottlingException"))
        with mock.patch.object(packaging.logger, "warning") as warning:
            packaging._model_arch_contradiction_guard(
                greengrass, {"m": {MODEL_JP6}}, ["arm64_jp7"])
        assert warning.called
        assert any(MODEL_JP6 in str(call)
                   for call in warning.call_args_list)

    def test_missing_dependencies_block_warns_and_proceeds(self, packaging):
        """A recipe with NO ComponentDependencies block at all parses to
        the no-LocalServer-dependency case: warn and proceed.

        **Validates: Requirements 2.6**
        """
        recipe = recipe_with_dependencies({})
        del recipe["ComponentDependencies"]
        greengrass = greengrass_with_recipe(MODEL_JP6, recipe)
        with mock.patch.object(packaging.logger, "warning") as warning:
            packaging._model_arch_contradiction_guard(
                greengrass, {"m": {MODEL_JP6}}, ["arm64_jp7"])
        assert warning.called

    def test_malformed_recipe_json_warns_and_proceeds(self, packaging):
        """A recipe blob that is not valid JSON is a read failure: warn
        and proceed, never raise.

        **Validates: Requirements 2.6**
        """
        greengrass = greengrass_with_recipe(MODEL_JP6, "{not json")
        with mock.patch.object(packaging.logger, "warning") as warning:
            packaging._model_arch_contradiction_guard(
                greengrass, {"m": {MODEL_JP6}}, ["arm64_jp7"])
        assert warning.called
        assert any(MODEL_JP6 in str(call)
                   for call in warning.call_args_list)


# --------------------------------------------------------------------------
# Design Unit Tests: _resolve_vllm_components (Decision 5)
# --------------------------------------------------------------------------

BASE = "model-vllm-unit"


def vllm_record(components=None, plural=None, base_name=BASE,
                model_name="unit-model"):
    """A vLLM-shape registry record over the real field vocabulary."""
    published_component = {
        "component_name": base_name,
        "component_version": "1.0.0",
        "runtime": "vllm",
    }
    if components is not None:
        published_component["components"] = components
    record = {
        "model_name": model_name,
        "model_type": "vllm",
        "published_component": published_component,
    }
    if plural is not None:
        record["published_components"] = plural
    return record


def per_jetpack_entry(arch, target, name=None):
    return {
        "component_name": name or f"{BASE}-{target}",
        "component_version": "1.0.0",
        "target": target,
        "architecture": arch,
        "supported_architectures": [arch],
    }


def plural_entry(target, name=None, status="published"):
    return {
        "component_name": name or f"{BASE}-{target}",
        "component_version": "1.0.0",
        "target": target,
        "status": status,
    }


class TestResolveVllmComponents:
    """**Feature: vllm-model-reload-after-backend-restart** — design
    Unit Tests for the Decision 5 per-architecture vLLM resolution."""

    def test_modern_record_resolves_per_arch_suffixed_names(
            self, packaging):
        """A modern record (per-JetPack ``components`` AND plural
        ``published_components``) covering arm64_jp6 through the primary
        source and arm64_jp7 through the secondary source resolves to
        exactly the two platform-suffixed names — never the base name.

        **Validates: Requirements 2.6**
        """
        record = vllm_record(
            components=[per_jetpack_entry("arm64_jp6",
                                          "jetson-xavier-jp6")],
            plural=[plural_entry("jetson-xavier-jp7")])
        resolved = packaging._resolve_vllm_components(
            record, ["arm64_jp6", "arm64_jp7"], "models/unit-model")
        assert resolved == {f"{BASE}-jetson-xavier-jp6",
                            f"{BASE}-jetson-xavier-jp7"}
        assert BASE not in resolved

    def test_components_only_intermediate_resolves(self, packaging):
        """An intermediate record with ONLY the per-JetPack
        ``components`` evidence resolves the selection from that single
        source.

        **Validates: Requirements 2.6**
        """
        record = vllm_record(
            components=[per_jetpack_entry("arm64_jp6",
                                          "jetson-xavier-jp6")])
        resolved = packaging._resolve_vllm_components(
            record, ["arm64_jp6"], "models/unit-model")
        assert resolved == {f"{BASE}-jetson-xavier-jp6"}

    def test_plural_only_intermediate_resolves(self, packaging):
        """An intermediate record with ONLY plural published entries
        resolves the selection through target-id matching.

        **Validates: Requirements 2.6**
        """
        record = vllm_record(plural=[plural_entry("jetson-xavier-jp6")])
        resolved = packaging._resolve_vllm_components(
            record, ["arm64_jp6"], "models/unit-model")
        assert resolved == {f"{BASE}-jetson-xavier-jp6"}

    def test_legacy_unsuffixed_only_record_fails_closed(self, packaging):
        """A legacy record whose only evidence is the unsuffixed base
        name fails closed for EVERY selected architecture, naming the
        model, the uncovered archs, and the legacy remediation — the
        base name is never a fallback.

        **Validates: Requirements 2.6**
        """
        record = vllm_record()  # base name only, no evidence sources
        with pytest.raises(packaging.PackagingError) as info:
            packaging._resolve_vllm_components(
                record, ["arm64_jp6", "arm64_jp7"], "models/unit-model")
        message = info.value.message
        assert "unit-model" in message
        assert "arm64_jp6" in message and "arm64_jp7" in message
        assert LEGACY_REMEDIATION in message

    def test_partially_covered_fails_closed_naming_uncovered_only(
            self, packaging):
        """A record with suffixed jp6 evidence but none for jp7 fails
        closed naming ONLY the uncovered arch, with the non-legacy
        remediation (the record does not predate per-JetPack
        components).

        **Validates: Requirements 2.6**
        """
        record = vllm_record(
            components=[per_jetpack_entry("arm64_jp6",
                                          "jetson-xavier-jp6")])
        with pytest.raises(packaging.PackagingError) as info:
            packaging._resolve_vllm_components(
                record, ["arm64_jp6", "arm64_jp7"], "models/unit-model")
        message = info.value.message
        assert "[arm64_jp7]" in message, (
            "exactly the uncovered arch must be named (the covered "
            f"arm64_jp6 must not); got: {message!r}")
        assert NON_LEGACY_REMEDIATION in message
        assert LEGACY_REMEDIATION not in message

    def test_malformed_entries_skipped(self, packaging):
        """Non-dict entries, blank component names, base-name echoes,
        and non-published plural entries are all skipped — resolution
        still succeeds from the valid entries alone.

        **Validates: Requirements 2.6**
        """
        record = vllm_record(
            components=[
                "corrupt-entry",                              # non-dict
                {"component_name": "", "architecture": "arm64_jp6",
                 "target": "jetson-xavier-jp6"},              # blank name
                {"component_name": BASE, "architecture": "arm64_jp6",
                 "target": "jetson-xavier-jp6"},              # base echo
                per_jetpack_entry("arm64_jp6", "jetson-xavier-jp6"),
            ],
            plural=[
                42,                                           # non-dict
                plural_entry("jetson-xavier-jp6", status="pending"),
                plural_entry("jetson-xavier-jp6", name=BASE),  # base echo
            ])
        resolved = packaging._resolve_vllm_components(
            record, ["arm64_jp6"], "models/unit-model")
        assert resolved == {f"{BASE}-jetson-xavier-jp6"}

    def test_malformed_only_evidence_fails_closed(self, packaging):
        """When EVERY entry is malformed (blank / base-name echoes),
        there is no usable suffixed evidence: fail closed.

        **Validates: Requirements 2.6**
        """
        record = vllm_record(
            components=[{"component_name": BASE,
                         "architecture": "arm64_jp6",
                         "target": "jetson-xavier-jp6"}],
            plural=[plural_entry("jetson-xavier-jp6", name=BASE)])
        with pytest.raises(packaging.PackagingError) as info:
            packaging._resolve_vllm_components(
                record, ["arm64_jp6"], "models/unit-model")
        assert "arm64_jp6" in info.value.message

    def test_secondary_source_rejects_extra_jp7_onnx_target(
            self, packaging):
        """A published, suffixed plural entry on the vision-only
        ``onnx-jetson-xavier-jp7`` target NEVER counts as arm64_jp7
        coverage — the vLLM secondary source matches PRIMARY
        publish-target ids only.

        **Validates: Requirements 2.6**
        """
        record = vllm_record(plural=[plural_entry(JP7_EXTRA_TARGET)])
        with pytest.raises(packaging.PackagingError) as info:
            packaging._resolve_vllm_components(
                record, ["arm64_jp7"], "models/unit-model")
        assert "arm64_jp7" in info.value.message

    def test_secondary_source_accepts_primary_jp7_target(self, packaging):
        """The PRIMARY jp7 id (``jetson-xavier-jp7``) is accepted —
        the rejection above is target-id-specific, not a jp7 blackout.

        **Validates: Requirements 2.6**
        """
        record = vllm_record(plural=[plural_entry("jetson-xavier-jp7")])
        resolved = packaging._resolve_vllm_components(
            record, ["arm64_jp7"], "models/unit-model")
        assert resolved == {f"{BASE}-jetson-xavier-jp7"}

