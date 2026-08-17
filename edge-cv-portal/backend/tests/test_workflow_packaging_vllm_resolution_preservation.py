"""Preservation property tests (Task 16) for edge-deploy-reliability.

**Feature: edge-deploy-reliability, Property 14: Preservation — vLLM
resolution and other gates unchanged (Defect G)**

**Validates: Requirements 2.21, 3.17, 3.18, 3.19**

Observation-first (observed on the G-UNFIXED `workflow_packaging.py`,
recorded here as the golden contract):

- `resolve_model_components` resolves a training-jobs record carrying a
  singular ``published_component`` map with a non-empty ``component_name``
  (the shape greengrass_publish.py writes for vLLM records,
  ``model-vllm-...``) to exactly ``{model_name: published map}`` — the
  full singular map, byte-identical to the seeded record (2.21 / 3.17).
- A referenced model with NO training-jobs record at all raises
  PackagingError with the existing message: "... has no record in the
  Use_Case model registry; it may have been removed since the workflow
  was validated" (3.18).
- A genuinely unpublished record — the record exists but has no singular
  ``component_name`` AND no plural ``published_components`` entries —
  raises PackagingError with the existing message: "... has no published
  Greengrass component; publish the model before packaging workflows
  that use it" (the misleading-for-vision message survives ONLY for this
  genuinely-unpublished shape after the fix).
- `model_component_dependencies` over singular-resolved records emits one
  UNPINNED HARD entry (``{'VersionRequirement': '>=0.0.0',
  'DependencyType': 'HARD'}``) per distinct ``component_name`` (3.17).
- `plugin_component_dependencies` and `local_server_component_dependencies`
  outputs for sample inputs are stable (3.19) — light golden checks only;
  test_workflow_packaging_localserver_preservation.py (Property 12) pins
  the LocalServer function deeply and is not duplicated here.

These tests are written BEFORE the Defect G fix (task 17.1) and must PASS
on the G-unfixed tree AND keep passing after the fix: every assertion here
is about vLLM-shape (singular) records, absent records, or genuinely
unpublished records — never a record carrying plural ``published_components``
entries (isBugCondition_G, task 15 / Property 13's domain).

CONSCIOUS REPOINT (vllm-model-reload-after-backend-restart task 3.6):
requirement 2.6 of that bugfix supersedes the edge-deploy-reliability
2.21/3.17 contract for vLLM-shape (singular) records — the verbatim
singular short-circuit IS defect 1.6 (the arch-agnostic base name emitted
as a HARD dependency dragged the JP6-era artifact and
LocalServer.arm64JP6 onto the JP7 Thor). Exactly the two legs recorded at
that spec's task 2 are repointed to the 2.6 contract, and NOTHING else:

- ``TestSingularVllmResolutionPreserved.test_singular_records_resolve_to_
  todays_exact_output`` (singular-map verbatim resolution for ANY arch
  selection) is now ``test_singular_records_fail_closed_per_architecture``:
  a legacy record carrying only the unsuffixed base name FAILS CLOSED for
  ANY arch selection, naming the model and every uncovered architecture
  with the legacy-record remediation.
- ``TestDependencyEmissionPreserved.test_model_dependencies_for_resolved_
  singular_records_stable`` (base-name dependency emission) is now
  ``test_model_dependencies_for_resolved_vllm_records_suffixed_only``:
  records carrying platform-suffixed per-JetPack ``components`` evidence
  resolve to the suffixed names and emission is one UNPINNED HARD entry
  per distinct SUFFIXED name — the unsuffixed base name never appears.

Every other leg — the no-record gate, ALL genuinely-unpublished shapes
(including the empty-singular parametrizations, which still fall through
to the unchanged unpublished gate), and the plugin/LocalServer goldens —
passes UNMODIFIED.

The fix changes `resolve_model_components`' signature to accept the
selected archs (design Fix Implementation §8). The `resolve()` helper below
inspects the live signature and passes archs only when the function accepts
them, so these tests are valid both pre- and post-fix.

Harness: conftest `aws_stack` (session-scoped moto) plus a training-jobs
Model_Registry table with the production `usecase-training-index` GSI shape
(the test_workflow_packaging_dependencies_exploration seeding pattern);
each Hypothesis example seeds records under a fresh usecase_id, so examples
are isolated by the GSI hash key. The floor env vars are cleared before the
module import so `min_local_server_version_for` resolves to the recorded
scalar default "1.0.0" (the localserver-preservation golden discipline).
"""
import inspect
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-defect-g-preservation-training-jobs"

#: The env vars min_local_server_version_for reads at module import time —
#: cleared before the fixture import so the recorded goldens apply.
_FLOOR_ENV_VARS = ("WORKFLOW_MIN_LOCAL_SERVER_VERSION",
                   "WORKFLOW_MIN_LOCAL_SERVER_VERSIONS",
                   "DDA_LOCAL_SERVER_VERSION")

#: Golden fragments of the two fail-closed messages observed on the
#: G-unfixed resolve_model_components (must survive the fix verbatim).
NO_RECORD_MESSAGE = "no record in the Use_Case model registry"
UNPUBLISHED_MESSAGE = "no published Greengrass component"

ARCHS = ("arm64_jp4", "arm64_jp5", "arm64_jp6", "x86_64", "x86_64_nvidia")

#: Recorded default floor (no floor env vars configured).
DEFAULT_FLOOR = "1.0.0"


# --------------------------------------------------------------------------
# Fixture
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging_env(aws_stack):
    """The training-jobs Model_Registry table (production GSI shape) plus a
    freshly imported workflow_packaging bound to it inside moto."""
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

    # Re-import so the module binds the table name above and
    # moto-intercepted clients (dependencies-exploration pattern).
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


# --------------------------------------------------------------------------
# Signature-agnostic resolution seam (survives the task 17 signature change)
# --------------------------------------------------------------------------

def resolve(packaging, model_names, usecase_id, archs=("arm64_jp6",)):
    """Call resolve_model_components pre- or post-fix.

    The Defect G fix gives the function the selected archs (design Fix
    Implementation §8: ``resolve_model_components(model_names, usecase,
    archs)``). Inspect the live signature: pass archs when the function
    accepts them (by name, or as a third positional parameter), otherwise
    call today's two-argument form.
    """
    function = packaging.resolve_model_components
    parameters = inspect.signature(function).parameters
    if "archs" in parameters:
        return function(model_names, usecase_id, archs=list(archs))
    positional = [p for p in parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY,
                                p.POSITIONAL_OR_KEYWORD)]
    if len(positional) >= 3:
        return function(model_names, usecase_id, list(archs))
    return function(model_names, usecase_id)


def fresh_usecase_id():
    return f"uc-defect-g-preservation-{uuid.uuid4()}"


def seed_vllm_record(training_table, usecase_id, model_name, published):
    """A training-jobs record in the vLLM shape greengrass_publish.py
    writes: singular ``published_component`` map, no plural field."""
    training_table.put_item(Item={
        "training_id": f"tr-{uuid.uuid4()}",
        "usecase_id": usecase_id,
        "model_name": model_name,
        "model_type": "vllm",
        "created_at": 1,
        "published_component": published,
    })


# --------------------------------------------------------------------------
# Hypothesis strategies
# --------------------------------------------------------------------------

_slugs = st.uuids().map(str)

#: model name -> (component slug, published version): the vLLM registry
#: shape domain. Names and component names are independently generated.
vllm_registry_shapes = st.dictionaries(
    keys=_slugs.map(lambda s: f"vllm-model-{s}"),
    values=st.tuples(
        _slugs.map(lambda s: f"model-vllm-{s}"),
        st.integers(min_value=1, max_value=9).map(lambda n: f"{n}.0.0"),
    ),
    min_size=1,
    max_size=3,
)

#: Any non-empty arch selection — post-fix, the archs argument must not
#: perturb the singular (vLLM) resolution path (2.21).
arch_selections = st.sets(st.sampled_from(ARCHS), min_size=1).map(sorted)


def published_map(component_name, component_version):
    """The singular published_component map greengrass_publish.py writes
    for vLLM records (string/list values only, so the DynamoDB round-trip
    is byte-identical)."""
    return {
        "component_name": component_name,
        "component_version": component_version,
        "runtime": "vllm",
        "supported_architectures": ["arm64_jp6"],
    }


# --------------------------------------------------------------------------
# (a) vLLM-shape (singular) records fail closed per architecture
# (REPOINTED to vllm-model-reload-after-backend-restart 2.6, task 3.6 —
# supersedes the 2.21/3.17 verbatim-resolution contract for these records)
# --------------------------------------------------------------------------

class TestSingularVllmResolutionPreserved:

    @settings(max_examples=20, deadline=None)
    @given(shapes=vllm_registry_shapes, archs=arch_selections)
    def test_singular_records_fail_closed_per_architecture(
            self, packaging_env, shapes, archs):
        """**Feature: vllm-model-reload-after-backend-restart, Property 5:
        workflow packaging emits only platform-suffixed vLLM model
        dependencies** (conscious repoint of the Property 14 verbatim-
        resolution leg — requirement 2.6 forbids the singular short-circuit
        this leg used to pin).

        For ANY set of LEGACY vLLM-shape records (singular
        published_component carrying ONLY the unsuffixed model-vllm-* base
        component_name — no per-JetPack ``components``, no plural entries)
        and ANY arch selection, resolution FAILS CLOSED with a
        PackagingError naming the model and EVERY uncovered (i.e. every
        selected) architecture, carrying the legacy-record remediation —
        the base name is never resolved, so it can never be emitted.

        Validates: Requirements 2.6
        """
        usecase_id = fresh_usecase_id()
        for model_name, (component_name, version) in shapes.items():
            seed_vllm_record(packaging_env.training_table, usecase_id,
                             model_name, published_map(component_name,
                                                       version))

        with pytest.raises(packaging_env.packaging.PackagingError) as info:
            resolve(packaging_env.packaging, sorted(shapes),
                    usecase_id, archs=archs)
        message = info.value.message
        # Resolution processes models in sorted order and fails closed on
        # the FIRST legacy record, naming it.
        first_model = sorted(shapes)[0]
        assert first_model in message, (
            "2.6 REGRESSION: the fail-closed error must name the model; "
            "got: {!r}".format(message))
        for arch in archs:
            assert arch in message, (
                "2.6 REGRESSION: the fail-closed error must name every "
                "uncovered architecture ({} missing); got: {!r}"
                .format(arch, message))
        assert "predates per-JetPack vLLM components" in message, (
            "2.6 REGRESSION: the legacy-record remediation text is gone; "
            "got: {!r}".format(message))


# --------------------------------------------------------------------------
# (b) A referenced model with NO registry record keeps the existing
# "no record" error (Requirement 3.18)
# --------------------------------------------------------------------------

class TestNoRecordGatePreserved:

    @settings(max_examples=10, deadline=None)
    @given(slug=_slugs)
    def test_missing_record_raises_existing_no_record_error(
            self, packaging_env, slug):
        """**Feature: edge-deploy-reliability, Property 14: Preservation —
        vLLM resolution and other gates unchanged**

        For ANY model name with no training-jobs record in the Use_Case
        registry, resolution fails closed with the existing "no record in
        the Use_Case model registry" PackagingError naming the model.

        Validates: Requirements 3.18
        """
        model_name = f"ghost-model-{slug}"
        usecase_id = fresh_usecase_id()  # nothing seeded under it

        with pytest.raises(packaging_env.packaging.PackagingError) as info:
            resolve(packaging_env.packaging, [model_name], usecase_id)
        assert NO_RECORD_MESSAGE in info.value.message, (
            "PRESERVATION REGRESSION (Property 14/3.18): the no-record "
            "gate no longer raises the existing message; got: {!r}"
            .format(info.value.message))
        assert model_name in info.value.message, (
            "the no-record error must name the model; got: {!r}"
            .format(info.value.message))


# --------------------------------------------------------------------------
# (c) A genuinely unpublished record keeps the existing "no published
# Greengrass component" error (Requirement 3.17 boundary; design §8 item 4)
# --------------------------------------------------------------------------

#: Genuinely-unpublished record shapes: no singular component_name AND no
#: plural published_components entries. NEVER a plural-shape record — that
#: is isBugCondition_G, task 15 / Property 13's domain.
UNPUBLISHED_SHAPES = [
    pytest.param({}, id="no-published-fields-at-all"),
    pytest.param({"published_component": {}},
                 id="singular-map-without-component-name"),
    pytest.param({"published_component": {"component_name": ""}},
                 id="singular-map-with-empty-component-name"),
    pytest.param({"published_components": []},
                 id="empty-plural-list-no-singular"),
]


class TestGenuinelyUnpublishedGatePreserved:

    @pytest.mark.parametrize("extra_fields", UNPUBLISHED_SHAPES)
    def test_unpublished_record_raises_existing_unpublished_error(
            self, packaging_env, extra_fields):
        """**Feature: edge-deploy-reliability, Property 14: Preservation —
        vLLM resolution and other gates unchanged**

        A record that exists but carries no singular component_name and no
        plural published_components entries fails closed with the existing
        "no published Greengrass component" PackagingError naming the model
        (the message survives the fix ONLY for this genuinely-unpublished
        shape).

        Validates: Requirements 3.17
        """
        model_name = f"unpublished-model-{uuid.uuid4()}"
        usecase_id = fresh_usecase_id()
        item = {
            "training_id": f"tr-{uuid.uuid4()}",
            "usecase_id": usecase_id,
            "model_name": model_name,
            "model_type": "anomaly_detection",
            "created_at": 1,
        }
        item.update(extra_fields)
        packaging_env.training_table.put_item(Item=item)

        with pytest.raises(packaging_env.packaging.PackagingError) as info:
            resolve(packaging_env.packaging, [model_name], usecase_id)
        assert UNPUBLISHED_MESSAGE in info.value.message, (
            "PRESERVATION REGRESSION (Property 14/3.17): the genuinely-"
            "unpublished gate no longer raises the existing message; "
            "got: {!r}".format(info.value.message))
        assert model_name in info.value.message, (
            "the unpublished error must name the model; got: {!r}"
            .format(info.value.message))


# --------------------------------------------------------------------------
# (d) Dependency emission over singular-resolved records is stable, and the
# plugin/LocalServer emission functions are untouched (Requirements 3.17,
# 3.19)
# --------------------------------------------------------------------------

#: The one arch/target pair the repointed emission leg resolves for —
#: resolve()'s default arch selection and its primary publish-target id.
EMISSION_ARCH = "arm64_jp6"
EMISSION_TARGET = "jetson-xavier-jp6"


def published_map_with_per_jetpack(component_name, component_version):
    """The MODERN singular published_component map: the unsuffixed base
    name (kept as the component_name-index GSI key for legacy readers)
    PLUS a platform-suffixed per-JetPack ``components`` entry covering
    EMISSION_ARCH — the greengrass_publish.py write-back shape since the
    multi-arch publish fix."""
    suffixed = f"{component_name}-{EMISSION_TARGET}"
    published = published_map(component_name, component_version)
    published["components"] = [{
        "component_name": suffixed,
        "component_version": component_version,
        "target": EMISSION_TARGET,
        "architecture": EMISSION_ARCH,
        "supported_architectures": [EMISSION_ARCH],
    }]
    return published


class TestDependencyEmissionPreserved:

    @settings(max_examples=20, deadline=None)
    @given(shapes=vllm_registry_shapes)
    def test_model_dependencies_for_resolved_vllm_records_suffixed_only(
            self, packaging_env, shapes):
        """**Feature: vllm-model-reload-after-backend-restart, Property 5:
        workflow packaging emits only platform-suffixed vLLM model
        dependencies** (conscious repoint of the Property 14 base-name
        emission leg — requirement 2.6 forbids emitting the unsuffixed base
        name this leg used to pin).

        For ANY set of vLLM records carrying platform-suffixed per-JetPack
        ``components`` evidence, model_component_dependencies emits exactly
        one UNPINNED ('>=0.0.0') HARD entry per distinct SUFFIXED
        component name — end-to-end from real resolution output — and the
        unsuffixed base name NEVER appears.

        Validates: Requirements 2.6
        """
        usecase_id = fresh_usecase_id()
        for model_name, (component_name, version) in shapes.items():
            seed_vllm_record(packaging_env.training_table, usecase_id,
                             model_name,
                             published_map_with_per_jetpack(component_name,
                                                            version))

        resolved = resolve(packaging_env.packaging, sorted(shapes),
                           usecase_id)
        out = packaging_env.packaging.model_component_dependencies(resolved)

        expected = {f"{component_name}-{EMISSION_TARGET}":
                    {"VersionRequirement": ">=0.0.0",
                     "DependencyType": "HARD"}
                    for component_name, _ in shapes.values()}
        assert out == expected, (
            "2.6 REGRESSION: model dependency emission for vLLM records "
            "with per-JetPack evidence changed: got {!r}, expected {!r}"
            .format(out, expected))
        for component_name, _ in shapes.values():
            assert component_name not in out, (
                "2.6 REGRESSION: the unsuffixed base name {!r} was "
                "emitted as a dependency".format(component_name))

    def test_plugin_dependency_emission_sample_unchanged(self,
                                                         packaging_env):
        """**Feature: edge-deploy-reliability, Property 14: Preservation —
        vLLM resolution and other gates unchanged**

        Light golden check (3.19): plugin_component_dependencies for a
        sample input — one resolvable record, one None (skipped) — emits
        today's exact pinned HARD entry.

        Validates: Requirements 3.19
        """
        out = packaging_env.packaging.plugin_component_dependencies({
            "custom:uc/alpha": {"plugin_id": "plg-alpha", "version": 2},
            "custom:uc/ghost": None,
        })
        assert out == {
            "dda.plugin.plg-alpha": {
                "VersionRequirement": ">=2.0.0 <3.0.0",
                "DependencyType": "HARD",
            },
        }, ("PRESERVATION REGRESSION (Property 14/3.19): "
            "plugin_component_dependencies sample output changed: {!r}"
            .format(out))

    def test_local_server_dependency_emission_samples_unchanged(
            self, packaging_env):
        """**Feature: edge-deploy-reliability, Property 14: Preservation —
        vLLM resolution and other gates unchanged**

        Light golden checks (3.19): local_server_component_dependencies for
        sample inputs — a single arch and the amd64-flavor collapse — emits
        today's exact entries. (The function's deep contract, including the
        Defect F multi-variant omission, is pinned by
        test_workflow_packaging_localserver_preservation.py.)

        Validates: Requirements 3.19
        """
        function = packaging_env.packaging.local_server_component_dependencies
        assert function(["arm64_jp6"]) == {
            "aws.edgeml.dda.LocalServer.arm64JP6": {
                "VersionRequirement": ">=" + DEFAULT_FLOOR,
                "DependencyType": "HARD",
            },
        }, "PRESERVATION REGRESSION (Property 14/3.19): arm64_jp6 sample"
        assert function(["x86_64", "x86_64_nvidia"]) == {
            "aws.edgeml.dda.LocalServer.amd64": {
                "VersionRequirement": ">=" + DEFAULT_FLOOR,
                "DependencyType": "HARD",
            },
        }, "PRESERVATION REGRESSION (Property 14/3.19): amd64 collapse sample"
