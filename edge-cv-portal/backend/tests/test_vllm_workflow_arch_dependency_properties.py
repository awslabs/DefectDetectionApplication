"""Portal-leg property tests for vllm-model-reload-after-backend-restart.

**Feature: vllm-model-reload-after-backend-restart, Property 2:
Preservation — Everything Outside the Bug Condition Is Unchanged
(portal leg, Requirement 3.9)**

This file carries the PRESERVATION property (task 2, written BEFORE the
fix, passing on the unfixed tree) AND, since task 4.6, the fix-check
property for the 2.6 per-architecture vLLM resolution:

**Property 5: Fix Checking — Workflow Packaging Emits Only
Platform-Suffixed vLLM Model Dependencies (Requirement 2.6)** — the
``TestVllmSuffixedResolutionFixCheck`` section at the bottom: _for any_
generated vLLM record shape (modern with per-JetPack ``components`` AND
plural ``published_components``; intermediate with exactly one evidence
source; legacy unsuffixed-only) × _any_ non-empty selected architecture
set, resolution either yields only platform-suffixed names covering the
selection or fails closed with a PackagingError naming the model and
the uncovered architecture(s); the unsuffixed base name NEVER appears
in any resolved value or emitted dependency; multi-arch selections
resolving to multiple distinct suffixed names follow the existing
Defect F omission-with-warning discipline.

Since task 4.7 the file also carries the portal-leg INTEGRATION pass
(``TestVllmPackagingIntegrationPass`` at the bottom, per the
test_vllm_multi_arch_publish_integration.py convention): registry
snapshot → resolution → dependency emission → recipe assembly through
the moto stack, asserting the final ComponentDependencies block
contains only suffixed vLLM entries.

Observation-first: the reference algorithms below were transcribed from
the UNFIXED ``workflow_packaging.py`` (2026-08-17, branch
spec/jetpack7-support). Requirement 3.9's domain is every packaging
input that references NO vLLM model: vision-only records (plural
``published_components``), plugin-only inputs, and model-free inputs.
For all of those, fixed ``resolve_model_components`` +
``model_component_dependencies`` output must deep-equal these recorded
references: vision per-target resolution, the Defect F single-variant
omission, the Defect G fail-closed coverage error, ``dda.plugin.*``
pinning, and the LocalServer single-variant discipline — all unchanged.

--------------------------------------------------------------------------
RECORDED at task 2 (2026-08-17, UNFIXED tree) — the ONE conscious
pinned-suite casualty, tests/test_workflow_packaging_vllm_resolution_
preservation.py (edge-deploy-reliability Property 14). The legs below
pin the exact singular short-circuit contract that requirement 2.6
forbids; task 3.6 repoints EXACTLY these legs to the 2.6 contract and
nothing else:

1. TestSingularVllmResolutionPreserved.
   test_singular_records_resolve_to_todays_exact_output —
   ``assert resolved == expected`` with
   ``expected[model_name] = published`` (the seeded singular map):
   "resolves ... to exactly ``{model_name: published map}``", i.e. the
   singular-map verbatim resolution for ANY arch selection. 2.6 instead
   requires per-arch suffixed resolution (a SET of suffixed names) or a
   fail-closed error.
2. TestDependencyEmissionPreserved.
   test_model_dependencies_for_resolved_singular_records_stable —
   ``assert out == expected`` with
   ``expected = {component_name: {'VersionRequirement': '>=0.0.0',
   'DependencyType': 'HARD'}}`` where component_name is the UNSUFFIXED
   ``model-vllm-{slug}`` base name: the base-name dependency emission
   2.6 forbids ("SHALL NEVER emit the unsuffixed base component name").
3. TestGenuinelyUnpublishedGatePreserved — the empty-singular
   parametrizations ``singular-map-without-component-name``
   (``{"published_component": {}}``),
   ``singular-map-with-empty-component-name``
   (``{"published_component": {"component_name": ""}}``) and
   ``empty-plural-list-no-singular`` pin
   ``UNPUBLISHED_MESSAGE = "no published Greengrass component"`` for
   shapes the fixed resolution may reclassify under the 2.6 fail-closed
   naming (model + uncovered architecture). Possibly repointed at 3.6.

Legs OUTSIDE the singular-resolution contract that must keep passing
UNMODIFIED after 3.6: TestNoRecordGatePreserved (the "no record in the
Use_Case model registry" error), the ``no-published-fields-at-all``
unpublished shape, and TestDependencyEmissionPreserved's plugin /
LocalServer golden checks.
--------------------------------------------------------------------------

Harness: conftest ``aws_stack`` (session-scoped moto) plus a
training-jobs Model_Registry table with the production
``usecase-training-index`` GSI shape (the resolution-preservation
suite's fixture pattern); each Hypothesis example seeds records under a
fresh usecase_id. Hypothesis profiles are conftest-registered
(``portal-fast``/``ci``) — no hardcoded ``max_examples``. The floor env
vars are cleared before the module import so the LocalServer goldens
resolve to the recorded scalar default "1.0.0".

Run from edge-cv-portal/backend WITH conftest:
    python3 -m pytest tests/test_vllm_workflow_arch_dependency_properties.py \
        -q -p no:cacheprovider
"""
import os
import sys
import uuid
from types import SimpleNamespace
from unittest import mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-vllm-arch-dependency-training-jobs"

#: The env vars min_local_server_version_for reads at module import time —
#: cleared before the fixture import so the recorded goldens apply.
_FLOOR_ENV_VARS = ("WORKFLOW_MIN_LOCAL_SERVER_VERSION",
                   "WORKFLOW_MIN_LOCAL_SERVER_VERSIONS",
                   "DDA_LOCAL_SERVER_VERSION")

#: Recorded default LocalServer floor (no floor env vars configured).
DEFAULT_FLOOR = "1.0.0"

#: Recorded arch -> accepted published_components target ids on the
#: UNFIXED tree (workflow_packaging.publish_targets_for_arch: the
#: ARCH_TO_PUBLISH_TARGET primary plus arm64_jp7's extra ONNX id) — the
#: real field vocabulary the strategies generate over.
ARCH_ACCEPTED_TARGETS = {
    "arm64_jp4": ("jetson-xavier",),
    "arm64_jp5": ("jetson-xavier-jp5",),
    "arm64_jp6": ("jetson-xavier-jp6",),
    "arm64_jp7": ("jetson-xavier-jp7", "onnx-jetson-xavier-jp7"),
    "x86_64": ("x86_64-cpu",),
    "x86_64_nvidia": ("x86_64-cuda",),
}

ARCHS = tuple(sorted(ARCH_ACCEPTED_TARGETS))

#: Recorded fragment of the Defect G fail-closed coverage error (2.19).
COVERAGE_MESSAGE = "no published Greengrass component for the selected architecture"


# --------------------------------------------------------------------------
# Fixture (the resolution-preservation suite's pattern)
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging_env(aws_stack):
    """The training-jobs Model_Registry table (production GSI shape) plus
    a freshly imported workflow_packaging bound to it inside moto."""
    import boto3

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME
    saved_floors = {name: os.environ.pop(name, None)
                    for name in _FLOOR_ENV_VARS}

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-training-index",
            "KeySchema": [{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )

    for module_name in ("workflow_packaging", "node_catalog_resolution",
                        "model_registry_snapshot"):
        sys.modules.pop(module_name, None)
    import workflow_packaging

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        packaging=workflow_packaging,
        training_table=resource.Table(TRAINING_JOBS_TABLE_NAME),
    )
    os.environ.pop("TRAINING_JOBS_TABLE", None)
    for name, value in saved_floors.items():
        if value is not None:
            os.environ[name] = value
    sys.modules.pop("workflow_packaging", None)


def fresh_usecase_id():
    return f"uc-vllm-arch-dep-preservation-{uuid.uuid4()}"


def seed_vision_record(training_table, usecase_id, model_name, entries):
    """A training-jobs record in the VISION plural shape
    greengrass_publish.py writes: ``published_components`` list, no
    singular vLLM map (Requirement 3.9's domain)."""
    training_table.put_item(Item={
        "training_id": f"tr-{uuid.uuid4()}",
        "usecase_id": usecase_id,
        "model_name": model_name,
        "model_type": "object_detection",
        "created_at": 1,
        "published_components": entries,
    })


# --------------------------------------------------------------------------
# Recorded reference algorithms (transcribed from the UNFIXED
# workflow_packaging.py, 2026-08-17)
# --------------------------------------------------------------------------

def expected_vision_resolution(entries, archs):
    """The UNFIXED plural-shape resolution: valid published entries whose
    target lies in the union of the selected archs' accepted target ids
    contribute their component names; the resolved value is that SET."""
    valid = [entry for entry in entries
             if isinstance(entry, dict)
             and entry.get("status") == "published"
             and isinstance(entry.get("component_name"), str)
             and entry.get("component_name")]
    accepted_union = {target for arch in archs
                      for target in ARCH_ACCEPTED_TARGETS[arch]}
    return {entry["component_name"] for entry in valid
            if entry.get("target") in accepted_union}


def expected_model_dependencies(resolved):
    """The UNFIXED emission: one UNPINNED HARD entry per distinct
    resolved name; models with multiple distinct names OMITTED (Defect F
    single-variant discipline); distinct models deduping to one name."""
    components = set()
    for value in resolved.values():
        names = {value["component_name"]} if isinstance(value, dict) \
            else set(value)
        if not names or len(names) > 1:
            continue
        components.add(next(iter(names)))
    return {name: {"VersionRequirement": ">=0.0.0",
                   "DependencyType": "HARD"}
            for name in sorted(components)}


# --------------------------------------------------------------------------
# Hypothesis strategies (real field vocabulary, never arbitrary JSON)
# --------------------------------------------------------------------------

_slugs = st.uuids().map(str)

_arch_selections = st.sets(st.sampled_from(ARCHS), min_size=1).map(sorted)


@st.composite
def _vision_workloads(draw):
    """A vision-only workload: an arch selection plus 1..3 plural-shape
    records, each COVERING every selected arch (one published entry per
    arch on a drawn accepted target), in one of two naming modes —
    'single' (one shared component name → the dependency is emitted) or
    'per-target' (target-embedded names → divergent selections trip the
    Defect F omission) — plus noise entries the resolution must ignore
    (non-published statuses, blank names, unaccepted targets)."""
    archs = draw(_arch_selections)
    records = {}
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        slug = draw(_slugs)
        model_name = f"vision-model-{slug}"
        mode = draw(st.sampled_from(("single", "per-target")))
        version = "{}.0.0".format(draw(st.integers(1, 9)))
        entries = []
        for arch in archs:
            target = draw(st.sampled_from(ARCH_ACCEPTED_TARGETS[arch]))
            component_name = (f"model-onnx-{slug}" if mode == "single"
                              else f"model-onnx-{slug}-{target}")
            entries.append({
                "component_name": component_name,
                "target": target,
                "component_version": version,
                "status": "published",
            })
        # Noise the unfixed resolution provably ignores.
        if draw(st.booleans()):
            entries.append({
                "component_name": f"model-onnx-{slug}-pending",
                "target": ARCH_ACCEPTED_TARGETS[archs[0]][0],
                "component_version": version,
                "status": "pending",
            })
        if draw(st.booleans()):
            entries.append({
                "component_name": "",
                "target": ARCH_ACCEPTED_TARGETS[archs[0]][0],
                "component_version": version,
                "status": "published",
            })
        records[model_name] = entries
    return archs, records


@st.composite
def _uncovered_workloads(draw):
    """A record covering a strict, non-empty subset of the selected
    archs — the Defect G fail-closed domain (2.19 must keep raising the
    recorded coverage error naming the model AND the uncovered archs)."""
    archs = draw(st.sets(st.sampled_from(ARCHS), min_size=2).map(sorted))
    covered = draw(st.sets(st.sampled_from(archs), min_size=1,
                           max_size=len(archs) - 1).map(sorted))
    slug = draw(_slugs)
    model_name = f"vision-model-{slug}"
    entries = [{
        "component_name": f"model-onnx-{slug}",
        "target": draw(st.sampled_from(ARCH_ACCEPTED_TARGETS[arch])),
        "component_version": "1.0.0",
        "status": "published",
    } for arch in covered]
    uncovered = [arch for arch in archs if arch not in covered]
    return archs, model_name, entries, uncovered


#: Plugin dependency-record maps over the real shape
#: load_custom_plugin_records produces: resolvable records carry
#: plugin_id + version; unresolvable dependencies map to None.
_plugin_maps = st.dictionaries(
    keys=_slugs.map(lambda s: f"custom:uc/node-{s}"),
    values=st.one_of(
        st.none(),
        st.builds(
            lambda pid, version: {"plugin_id": f"plg-{pid}",
                                  "version": version},
            _slugs, st.integers(min_value=1, max_value=9),
        ),
    ),
    max_size=4,
)


# --------------------------------------------------------------------------
# (a) Vision per-target resolution + Defect F emission identity (3.9)
# --------------------------------------------------------------------------

class TestVisionResolutionIdentityPreserved:

    @settings(deadline=None)
    @given(workload=_vision_workloads())
    def test_vision_only_resolution_and_emission_match_reference(
            self, packaging_env, workload):
        """**Feature: vllm-model-reload-after-backend-restart,
        Property 2: Preservation — portal 3.9 identity (vision leg)**

        *For any* vision-only workload (plural published_components
        records covering the selected archs, single or per-target
        naming, plus ignorable noise), ``resolve_model_components``
        deep-equals the recorded per-target reference (a SET of
        component names per model) and ``model_component_dependencies``
        deep-equals the recorded emission — one UNPINNED HARD entry per
        single-name model, divergent multi-name models OMITTED (the
        Defect F single-variant discipline).

        Validates: Requirements 3.9
        """
        archs, records = workload
        usecase_id = fresh_usecase_id()
        for model_name, entries in records.items():
            seed_vision_record(packaging_env.training_table, usecase_id,
                               model_name, entries)

        resolved = packaging_env.packaging.resolve_model_components(
            sorted(records), usecase_id, list(archs))
        expected = {
            model_name: expected_vision_resolution(entries, archs)
            for model_name, entries in records.items()}
        assert resolved == expected, (
            "PRESERVATION REGRESSION (Property 2 / 3.9): vision "
            "per-target resolution diverged for archs {}: got {!r}, "
            "expected {!r}".format(archs, resolved, expected))

        out = packaging_env.packaging.model_component_dependencies(resolved)
        expected_out = expected_model_dependencies(expected)
        assert out == expected_out, (
            "PRESERVATION REGRESSION (Property 2 / 3.9): vision "
            "dependency emission diverged: got {!r}, expected {!r}"
            .format(out, expected_out))

    @settings(deadline=None)
    @given(workload=_uncovered_workloads())
    def test_uncovered_arch_keeps_failing_closed_with_coverage_error(
            self, packaging_env, workload):
        """**Feature: vllm-model-reload-after-backend-restart,
        Property 2: Preservation — portal 3.9 identity (Defect G leg)**

        *For any* vision record covering a strict subset of the
        selected archs, resolution keeps failing closed with the
        recorded coverage error naming the model AND every uncovered
        architecture (the Defect G / 2.19 semantics, unchanged).

        Validates: Requirements 3.9
        """
        archs, model_name, entries, uncovered = workload
        usecase_id = fresh_usecase_id()
        seed_vision_record(packaging_env.training_table, usecase_id,
                           model_name, entries)

        with pytest.raises(
                packaging_env.packaging.PackagingError) as info:
            packaging_env.packaging.resolve_model_components(
                [model_name], usecase_id, list(archs))
        message = info.value.message
        assert COVERAGE_MESSAGE in message, (
            "PRESERVATION REGRESSION (Property 2 / 3.9): the Defect G "
            "coverage error changed; got: {!r}".format(message))
        assert model_name in message
        for arch in uncovered:
            assert arch in message, (
                "the coverage error must name uncovered arch '{}'; "
                "got: {!r}".format(arch, message))


# --------------------------------------------------------------------------
# (b) Model-free identity (3.9)
# --------------------------------------------------------------------------

class TestModelFreeIdentityPreserved:

    def test_model_free_input_resolves_and_emits_nothing(
            self, packaging_env):
        """**Feature: vllm-model-reload-after-backend-restart,
        Property 2: Preservation — portal 3.9 identity (model-free leg)**

        A workflow referencing no models resolves to {} and emits no
        model dependency entries — unchanged.

        Validates: Requirements 3.9
        """
        resolved = packaging_env.packaging.resolve_model_components(
            [], fresh_usecase_id(), ["arm64_jp6"])
        assert resolved == {}
        assert packaging_env.packaging.model_component_dependencies(
            {}) == {}


# --------------------------------------------------------------------------
# (c) dda.plugin.* pinning identity (3.9)
# --------------------------------------------------------------------------

class TestPluginPinningIdentityPreserved:

    @settings(deadline=None)
    @given(dep_records=_plugin_maps)
    def test_plugin_dependency_pinning_matches_reference(
            self, packaging_env, dep_records):
        """**Feature: vllm-model-reload-after-backend-restart,
        Property 2: Preservation — portal 3.9 identity (plugin leg)**

        *For any* custom-plugin dependency map (resolvable records plus
        None gaps), ``plugin_component_dependencies`` deep-equals the
        recorded pinning: one HARD ``dda.plugin.{plugin_id}`` entry per
        resolvable record with the ``>={v}.0.0 <{v+1}.0.0`` requirement,
        None entries skipped.

        Validates: Requirements 3.9
        """
        out = packaging_env.packaging.plugin_component_dependencies(
            dep_records)

        expected = {}
        for dep in sorted(dep_records):
            record = dep_records[dep]
            if not record:
                continue
            version = int(record["version"])
            expected[f"dda.plugin.{record['plugin_id']}"] = {
                "VersionRequirement":
                    f">={version}.0.0 <{version + 1}.0.0",
                "DependencyType": "HARD",
            }
        assert out == expected, (
            "PRESERVATION REGRESSION (Property 2 / 3.9): plugin "
            "dependency pinning diverged: got {!r}, expected {!r}"
            .format(out, expected))


# --------------------------------------------------------------------------
# (d) LocalServer single-variant discipline — light goldens (3.9)
# --------------------------------------------------------------------------

class TestLocalServerDisciplinePreserved:
    """Light golden checks only: test_workflow_packaging_localserver_
    preservation.py pins the function deeply and stays green untouched
    (recorded in the task-2 baselines)."""

    def test_single_variant_sample_unchanged(self, packaging_env):
        """Validates: Requirements 3.9"""
        function = \
            packaging_env.packaging.local_server_component_dependencies
        assert function(["arm64_jp6"]) == {
            "aws.edgeml.dda.LocalServer.arm64JP6": {
                "VersionRequirement": ">=" + DEFAULT_FLOOR,
                "DependencyType": "HARD",
            },
        }

    def test_multi_variant_omission_sample_unchanged(self, packaging_env):
        """Validates: Requirements 3.9 (the Defect F recipe-global
        omission for multi-variant selections)."""
        function = \
            packaging_env.packaging.local_server_component_dependencies
        assert function(["arm64_jp5", "arm64_jp6"]) == {}


# ==========================================================================
# Property 5: Fix Checking — Workflow Packaging Emits Only
# Platform-Suffixed vLLM Model Dependencies (task 4.6, design fix-check
# case 6). Everything below exercises the FIXED per-architecture vLLM
# resolution (_resolve_vllm_components, Decision 5) end to end through
# resolve_model_components + model_component_dependencies.
# ==========================================================================

#: Primary publish-target id per arch — the ONLY ids the vLLM secondary
#: source (plural published_components) matches on (Decision 5: the
#: vision-only ARCH_TO_EXTRA_PUBLISH_TARGETS acceptance does NOT apply).
ARCH_PRIMARY_TARGET = {arch: targets[0]
                       for arch, targets in ARCH_ACCEPTED_TARGETS.items()}

#: The one extra (vision-only) target id — used as noise the vLLM
#: resolution must NEVER accept as arm64_jp7 coverage.
JP7_EXTRA_TARGET = "onnx-jetson-xavier-jp7"

#: Recorded fragment of the 2.6 legacy-record remediation text.
LEGACY_REMEDIATION = ("this record predates per-JetPack vLLM components")


def _all_platform_suffixes():
    """Every '-{target}' suffix a platform-suffixed vLLM component name
    may legitimately carry (the primary publish-target vocabulary)."""
    return {f"-{target}" for target in ARCH_PRIMARY_TARGET.values()}


def _component_arn(name, version="1.0.0"):
    return (f"arn:aws:greengrass:{REGION}:123456789012:components:"
            f"{name}:versions:{version}")


def seed_vllm_record(training_table, usecase_id, model_name, base_name,
                     published_component, published_components=None):
    """A training-jobs record in the vLLM singular shape the publish
    path writes (base component_name kept as the GSI key for legacy
    readers, per-JetPack evidence in the drawn sources)."""
    item = {
        "training_id": f"tr-{uuid.uuid4()}",
        "usecase_id": usecase_id,
        "model_name": model_name,
        "model_type": "vllm",
        "source": "vllm",
        "created_at": 1,
        "component_name": base_name,
        "published": True,
        "published_component": published_component,
    }
    if published_components is not None:
        item["published_components"] = published_components
    training_table.put_item(Item=item)


@st.composite
def _vllm_workloads(draw):
    """One vLLM record over the real field vocabulary: an arch selection,
    a drawn covered subset (possibly empty = the legacy degenerate), a
    record shape ('modern' = both evidence sources; 'components-only' /
    'plural-only' = intermediate single-source), per-covered-arch
    platform-suffixed entries (per-target names, the greengrass_publish
    write-back naming), plus noise the resolution must ignore: non-dict
    entries, blank names, base-name echoes, non-published statuses, and
    the vision-only onnx-jetson-xavier-jp7 target."""
    archs = draw(_arch_selections)
    slug = draw(_slugs)
    base_name = f"model-vllm-{slug}"
    model_name = f"vllm-model-{slug}"
    shape = draw(st.sampled_from(("modern", "components-only",
                                  "plural-only")))
    covered = draw(st.sets(st.sampled_from(archs),
                           max_size=len(archs)).map(sorted))

    components = []
    plural = []
    names_by_arch = {}
    for arch in covered:
        target = ARCH_PRIMARY_TARGET[arch]
        name = f"{base_name}-{target}"
        names_by_arch[arch] = {name}
        source = (draw(st.sampled_from(("primary", "secondary", "both")))
                  if shape == "modern"
                  else ("primary" if shape == "components-only"
                        else "secondary"))
        if source in ("primary", "both"):
            components.append({
                "component_name": name,
                "component_version": "1.0.0",
                "target": target,
                "architecture": arch,
                "supported_architectures": [arch],
                "component_arn": _component_arn(name),
            })
        if source in ("secondary", "both"):
            plural.append({
                "component_name": name,
                "component_version": "1.0.0",
                "target": target,
                "status": "published",
                "component_arn": _component_arn(name),
            })

    # --- noise the fixed resolution must provably ignore -------------
    noise_arch = archs[0]
    noise_target = ARCH_PRIMARY_TARGET[noise_arch]
    if draw(st.booleans()):  # blank component_name (skipped)
        components.append({"component_name": "", "architecture": noise_arch,
                           "target": noise_target,
                           "component_version": "1.0.0"})
    if draw(st.booleans()):  # base-name echo: NEVER coverage, NEVER a value
        components.append({"component_name": base_name,
                           "architecture": noise_arch,
                           "target": noise_target,
                           "component_version": "1.0.0"})
    if draw(st.booleans()):  # non-dict entry (skipped)
        components.append("corrupt-entry")
    if draw(st.booleans()):  # non-published plural entry (skipped)
        plural.append({"component_name": f"{base_name}-{noise_target}",
                       "target": noise_target, "status": "pending",
                       "component_version": "1.0.0"})
    if draw(st.booleans()):  # plural base-name echo (skipped)
        plural.append({"component_name": base_name, "target": noise_target,
                       "status": "published", "component_version": "1.0.0"})
    if "arm64_jp7" in archs and draw(st.booleans()):
        # The vision-only extra jp7 target: published + suffixed, but it
        # must NEVER count as arm64_jp7 coverage (primary ids only).
        plural.append({"component_name": f"{base_name}-{JP7_EXTRA_TARGET}",
                       "target": JP7_EXTRA_TARGET, "status": "published",
                       "component_version": "1.0.0"})

    published_component = {
        "component_name": base_name,
        "component_version": "1.0.0",
        "runtime": "vllm",
    }
    if components:
        published_component["components"] = components
    return SimpleNamespace(
        archs=archs, model_name=model_name, base_name=base_name,
        published_component=published_component,
        published_components=plural or None,
        names_by_arch=names_by_arch)


#: Legacy-only workloads: the record's ONLY publish evidence is the
#: unsuffixed base name (the JP6-era incident record shape).
@st.composite
def _legacy_workloads(draw):
    archs = draw(_arch_selections)
    slug = draw(_slugs)
    return SimpleNamespace(
        archs=archs,
        model_name=f"vllm-model-{slug}",
        base_name=f"model-vllm-{slug}",
        published_component={
            "component_name": f"model-vllm-{slug}",
            "component_version": "1.0.0",
            "runtime": "vllm",
            "supported_architectures": ["arm64_jp6"],
        })


class TestVllmSuffixedResolutionFixCheck:
    """**Feature: vllm-model-reload-after-backend-restart, Property 5:
    Fix Checking — Workflow Packaging Emits Only Platform-Suffixed vLLM
    Model Dependencies** (design fix-check case 6)."""

    @settings(deadline=None)
    @given(workload=_vllm_workloads())
    def test_resolution_is_suffixed_and_covering_or_fails_closed(
            self, packaging_env, workload):
        """*For any* generated vLLM record shape (modern / intermediate /
        legacy-degenerate) × *any* non-empty selected architecture set:
        EITHER every selected arch has suffixed evidence and resolution
        yields exactly the platform-suffixed per-target names (all names
        suffixed, the selection covered), OR resolution fails closed
        with a PackagingError naming the model AND every uncovered
        architecture. The unsuffixed base name NEVER appears in any
        resolved value or emitted dependency; a multi-arch selection
        resolving to multiple distinct suffixed names is OMITTED from
        the emission with a warning naming the model (the existing
        Defect F discipline); a single resolved name is emitted as the
        one unpinned HARD entry.

        **Validates: Requirements 2.6**
        """
        usecase_id = fresh_usecase_id()
        seed_vllm_record(packaging_env.training_table, usecase_id,
                         workload.model_name, workload.base_name,
                         workload.published_component,
                         workload.published_components)
        uncovered = [arch for arch in workload.archs
                     if arch not in workload.names_by_arch]

        if uncovered:
            with pytest.raises(
                    packaging_env.packaging.PackagingError) as info:
                packaging_env.packaging.resolve_model_components(
                    [workload.model_name], usecase_id,
                    list(workload.archs))
            message = info.value.message
            assert workload.model_name in message, (
                "FIX-CHECK FAILURE (Property 5 / 2.6): the fail-closed "
                "error must name the model; got: {!r}".format(message))
            for arch in uncovered:
                assert arch in message, (
                    "FIX-CHECK FAILURE (Property 5 / 2.6): the "
                    "fail-closed error must name uncovered arch '{}'; "
                    "got: {!r}".format(arch, message))
            return

        resolved = packaging_env.packaging.resolve_model_components(
            [workload.model_name], usecase_id, list(workload.archs))
        expected_names = set()
        for names in workload.names_by_arch.values():
            expected_names.update(names)
        assert resolved == {workload.model_name: expected_names}, (
            "FIX-CHECK FAILURE (Property 5 / 2.6): resolution diverged "
            "from the per-arch suffixed evidence for archs {}: got {!r}, "
            "expected {!r}".format(workload.archs, resolved,
                                   expected_names))
        suffixes = _all_platform_suffixes()
        for name in resolved[workload.model_name]:
            assert name != workload.base_name and any(
                name.endswith(suffix) for suffix in suffixes), (
                "FIX-CHECK FAILURE (Property 5 / 2.6): resolved value "
                "{!r} is not platform-suffixed (base name: {!r})".format(
                    name, workload.base_name))

        with mock.patch.object(packaging_env.packaging.logger,
                               "warning") as warning:
            out = packaging_env.packaging.model_component_dependencies(
                resolved)
        assert workload.base_name not in out, (
            "FIX-CHECK FAILURE (Property 5 / 2.6): the unsuffixed base "
            "name {!r} was emitted as a dependency: {!r}".format(
                workload.base_name, out))
        if len(expected_names) == 1:
            only = next(iter(expected_names))
            assert out == {only: {"VersionRequirement": ">=0.0.0",
                                  "DependencyType": "HARD"}}, (
                "FIX-CHECK FAILURE (Property 5 / 2.6): single-name "
                "emission diverged: got {!r}".format(out))
        else:
            assert out == {}, (
                "FIX-CHECK FAILURE (Property 5 / 2.6, Defect F): a "
                "multi-name vLLM resolution must be omitted from the "
                "emission (a recipe-global HARD dep on several "
                "Per_JetPack components is undeployable); got {!r}"
                .format(out))
            assert any(workload.model_name in str(call)
                       for call in warning.call_args_list), (
                "FIX-CHECK FAILURE (Property 5 / 2.6, Defect F): the "
                "omission must warn naming the model")

    @settings(deadline=None)
    @given(workload=_legacy_workloads())
    def test_legacy_unsuffixed_only_records_always_fail_closed(
            self, packaging_env, workload):
        """*For any* non-empty architecture selection, a LEGACY record
        whose only publish evidence is the unsuffixed base name (the
        incident's JP6-era record shape) fails closed with a
        PackagingError naming the model, EVERY selected architecture,
        and the legacy-record remediation — the base name is never a
        fallback (defect 1.6's exact emission is unreachable).

        **Validates: Requirements 2.6**
        """
        usecase_id = fresh_usecase_id()
        seed_vllm_record(packaging_env.training_table, usecase_id,
                         workload.model_name, workload.base_name,
                         workload.published_component)

        with pytest.raises(
                packaging_env.packaging.PackagingError) as info:
            packaging_env.packaging.resolve_model_components(
                [workload.model_name], usecase_id, list(workload.archs))
        message = info.value.message
        assert workload.model_name in message
        for arch in workload.archs:
            assert arch in message, (
                "FIX-CHECK FAILURE (Property 5 / 2.6): the legacy "
                "fail-closed error must name selected arch '{}'; got: "
                "{!r}".format(arch, message))
        assert LEGACY_REMEDIATION in message, (
            "FIX-CHECK FAILURE (Property 5 / 2.6): the legacy record "
            "remediation text is missing; got: {!r}".format(message))


# ==========================================================================
# Task 4.7 portal-leg integration pass (design Testing Strategy
# "Integration Tests"): registry snapshot → resolution → dependency
# emission → recipe assembly through the moto stack, asserting the FINAL
# ComponentDependencies block contains only platform-suffixed vLLM
# entries. A dedicated class in this file per the
# test_vllm_multi_arch_publish_integration.py convention (the moto
# Model_Registry fixture and record seeding above are reused verbatim).
#
# Honesty guard: the real integration tier is on-hardware/in-account —
# Session A (task 11) for the device leg and the post-deploy portal
# verification (task 12+) for real Greengrass recipes; this pass stops
# at the assembled recipe dict, exactly where publish-side moto coverage
# ends.
# ==========================================================================

import json as _json


class TestVllmPackagingIntegrationPass:
    """**Feature: vllm-model-reload-after-backend-restart** — task 4.7
    portal-leg integration: the full packaging pipeline slice from the
    Model_Registry snapshot to the assembled Workflow_Component recipe,
    over the moto-backed training-jobs table."""

    def _seed_modern_record(self, packaging_env, usecase_id, archs):
        """A modern vLLM record (per-JetPack ``components`` evidence,
        the multi-arch publish write-back shape) covering ``archs``."""
        slug = uuid.uuid4().hex[:12]
        base_name = f"model-vllm-{slug}"
        model_name = f"vllm-model-{slug}"
        components = []
        suffixed = {}
        for arch in archs:
            target = ARCH_PRIMARY_TARGET[arch]
            name = f"{base_name}-{target}"
            suffixed[arch] = name
            components.append({
                "component_name": name,
                "component_version": "1.0.0",
                "target": target,
                "architecture": arch,
                "supported_architectures": [arch],
                "component_arn": _component_arn(name),
            })
        seed_vllm_record(
            packaging_env.training_table, usecase_id, model_name,
            base_name,
            {"component_name": base_name, "component_version": "1.0.0",
             "runtime": "vllm", "components": components})
        return model_name, base_name, suffixed

    def _assemble_recipe(self, packaging, dependencies, archs):
        """Recipe assembly exactly as the packaging handler performs it:
        the three disjoint dependency namespaces merged, then
        ``build_recipe`` over one final artifact key per arch."""
        final_keys = {
            arch: f"workflows/uc/wf-integ/1/workflow-{arch}.zip"
            for arch in archs}
        return packaging.build_recipe(
            "wf-integ", 1, "test-bucket", final_keys,
            component_dependencies=dependencies)

    def test_single_arch_pass_recipe_carries_only_the_suffixed_vllm_entry(
            self, packaging_env):
        """Registry snapshot → per-arch resolution → dependency emission
        → recipe assembly for a single-architecture (arm64_jp7)
        workflow referencing a modern vLLM record: the FINAL
        ComponentDependencies block carries EXACTLY the platform-
        suffixed Per_JetPack_Component name as its one vLLM entry
        (unpinned HARD), the unsuffixed base name appears NOWHERE in the
        assembled recipe, and the non-vLLM namespaces (LocalServer)
        ride along untouched.

        **Validates: Requirements 2.6**
        """
        packaging = packaging_env.packaging
        archs = ["arm64_jp7"]
        usecase_id = fresh_usecase_id()
        model_name, base_name, suffixed = self._seed_modern_record(
            packaging_env, usecase_id, archs)
        expected_name = suffixed["arm64_jp7"]

        # Snapshot → resolution (over the moto-backed GSI).
        resolved = packaging.resolve_model_components(
            [model_name], usecase_id, archs)
        assert resolved == {model_name: {expected_name}}

        # Dependency emission, merged the handler's way (the three
        # namespaces are disjoint).
        dependencies = {
            **packaging.plugin_component_dependencies({}),
            **packaging.model_component_dependencies(resolved),
            **packaging.local_server_component_dependencies(archs),
        }

        # Recipe assembly.
        recipe = self._assemble_recipe(packaging, dependencies, archs)
        block = recipe["ComponentDependencies"]

        # The FINAL block's vLLM entries: exactly the one suffixed name.
        vllm_entries = {key for key in block
                        if key.startswith("model-vllm-")}
        assert vllm_entries == {expected_name}, (
            "INTEGRATION FAILURE (2.6): the final ComponentDependencies "
            "block must carry exactly the suffixed vLLM entry; got "
            "vLLM keys {!r}".format(sorted(vllm_entries)))
        assert block[expected_name] == {
            "VersionRequirement": ">=0.0.0", "DependencyType": "HARD"}
        assert base_name not in block

        # The unsuffixed base name appears NOWHERE in the whole recipe
        # as a standalone value (it is legitimately a PREFIX of the
        # suffixed name, so the check is for the exact JSON token).
        assert '"{}"'.format(base_name) not in _json.dumps(recipe), (
            "INTEGRATION FAILURE (2.6): the unsuffixed base name {!r} "
            "leaked into the assembled recipe".format(base_name))

        # The LocalServer edge rides along (single variant → emitted).
        assert "aws.edgeml.dda.LocalServer.arm64JP7" in block

    def test_multi_arch_pass_omits_divergent_vllm_entries_from_recipe(
            self, packaging_env):
        """The same pipeline slice for a two-architecture (arm64_jp6 +
        arm64_jp7) workflow: resolution yields BOTH suffixed names,
        emission omits the divergent set with a warning naming the
        model (the Defect F recipe-global discipline), and the final
        assembled recipe carries NO vLLM entry — and, above all, never
        the unsuffixed base name (defect 1.6's emission is unreachable
        end to end).

        **Validates: Requirements 2.6**
        """
        packaging = packaging_env.packaging
        archs = ["arm64_jp6", "arm64_jp7"]
        usecase_id = fresh_usecase_id()
        model_name, base_name, suffixed = self._seed_modern_record(
            packaging_env, usecase_id, archs)

        resolved = packaging.resolve_model_components(
            [model_name], usecase_id, archs)
        assert resolved == {model_name: set(suffixed.values())}

        with mock.patch.object(packaging.logger, "warning") as warning:
            dependencies = {
                **packaging.plugin_component_dependencies({}),
                **packaging.model_component_dependencies(resolved),
                **packaging.local_server_component_dependencies(archs),
            }
        assert any(model_name in str(call)
                   for call in warning.call_args_list), (
            "the Defect F omission must warn naming the model")

        recipe = self._assemble_recipe(packaging, dependencies, archs)

        # No vLLM entry survives into the final block (multi-variant
        # selections also omit the LocalServer edge, so the whole block
        # may legitimately be absent).
        block = recipe.get("ComponentDependencies") or {}
        assert not any(key.startswith("model-vllm-") for key in block), (
            "INTEGRATION FAILURE (2.6/Defect F): divergent per-JetPack "
            "vLLM names must be omitted from the final block; got "
            "{!r}".format(sorted(block)))
        assert '"{}"'.format(base_name) not in _json.dumps(recipe), (
            "INTEGRATION FAILURE (2.6): the unsuffixed base name {!r} "
            "leaked into the assembled recipe".format(base_name))
