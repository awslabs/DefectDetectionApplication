# -*- coding: utf-8 -*-
"""Bug-condition exploration test (Task 15, case 9) for edge-deploy-reliability.

Property 13: Bug Condition — Published vision models resolve and package
deployably (Defect G, `isBugCondition_G`).

**These tests assert the FIXED (post-G) resolution/emission behavior, so they
are EXPECTED TO FAIL on the G-UNFIXED tree.** The failure is the
counterexample confirming the defect: `resolve_model_components` reads only
the `published_component` (SINGULAR) registry field — the shape
`greengrass_publish.py` writes for vLLM models — while vision publishes write
per-target `published_components` (PLURAL list of `{component_name, target,
component_version, status, platform, component_arn}`) and no singular field.
A fully published vision model is therefore rejected with the misleading
PackagingError "no published Greengrass component; publish the model before
packaging" — and re-publishing can never help, because the vision publish
flow only ever writes the plural field (1.18).

Verified incident: the training-jobs record for vision model 'yolo_test'
carries published_components entries for targets jetson-xavier-jp5 and
jetson-xavier-jp6 (both status 'published', component_version 6.0.0) and NO
singular published_component; packaging the workflow for arm64_jp5 +
arm64_jp6 failed in the portal packaging dialog with the misleading
"no published Greengrass component" error.

The SAME tests are re-run in task 17.2 against the fixed
`workflow_packaging.py`, where they must PASS:

- (a) a plural-shape record whose published entries cover every selected
  architecture resolves WITHOUT PackagingError (2.18);
- (b) model dependency emission for the resolved record emits ZERO entries
  when the per-target component names differ (the Defect F deployability
  discipline applied to model components, 2.20) and exactly ONE unpinned
  HARD entry when the covered entries collapse to a single name (2.20);
- (c) a selected architecture with NO published entry raises PackagingError
  naming the model AND the uncovered architecture/target — an accurate
  message, unlike today's misleading one (2.19).

Harness: the established moto-backed packaging stack (conftest `aws_stack`)
plus the training-jobs Model_Registry table with the production
`usecase-training-index` GSI shape, seeded with plural-shape vision records
(test_workflow_packaging_dependencies_exploration.py pattern; the module is
imported inside the moto mock because its module-level boto3 clients demand
it). The fixed `resolve_model_components` gains the selected archs (design
Fix Implementation §8); the resolver helper below tolerates both the current
and the fixed signature so the same test runs on the unfixed and fixed trees.

Validates: Requirements 1.17, 1.18, 1.19
"""
import inspect
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-defect-g-training-jobs"

INCIDENT_MODEL_NAME = "yolo_test"
INCIDENT_JP5_COMPONENT = "model-yolo-test-jetson-xavier-jp5"
INCIDENT_JP6_COMPONENT = "model-yolo-test-jetson-xavier-jp6"
INCIDENT_ARCHS = ["arm64_jp5", "arm64_jp6"]

# arch -> publish target, per greengrass_publish.py's TARGET_TO_LOCAL_SERVER
# naming discipline (hardcoded as an independent oracle, not read back from
# the module under test).
TARGET_OF = {
    "arm64_jp5": "jetson-xavier-jp5",
    "arm64_jp6": "jetson-xavier-jp6",
}


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging_env(aws_stack):
    """The training-jobs Model_Registry table (production GSI shape) plus a
    freshly imported workflow_packaging bound to it inside moto."""
    import boto3

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME

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

    # Re-import so the module binds the table name above and moto-intercepted
    # clients (test_workflow_packaging_dependencies_exploration pattern).
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
    sys.modules.pop("workflow_packaging", None)


def resolve_models(packaging, model_names, usecase_id, archs):
    """Call resolve_model_components against the FIXED contract (selected
    archs passed in, design Fix Implementation §8), tolerating the CURRENT
    two-argument signature so the identical test runs on the unfixed tree
    (where the plural-only record is rejected regardless of archs)."""
    parameters = inspect.signature(
        packaging.resolve_model_components).parameters
    positional = [p for p in parameters.values()
                  if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    if len(positional) >= 3 or any(
            name in parameters for name in ("archs", "architectures")):
        return packaging.resolve_model_components(
            model_names, usecase_id, archs)
    return packaging.resolve_model_components(model_names, usecase_id)


def published_entry(component_name, target, version="6.0.0",
                    status="published"):
    """One per-target published_components entry, the verified vision
    registry shape greengrass_publish.py writes."""
    return {
        "component_name": component_name,
        "target": target,
        "component_version": version,
        "status": status,
        "platform": "aarch64",
        "component_arn": (
            "arn:aws:greengrass:us-east-1:123456789012:"
            "components:{}:versions:{}".format(component_name, version)),
    }


def seed_vision_record(training_table, usecase_id, model_name, entries):
    """A vision Model_Registry record: per-target `published_components`
    (PLURAL list) and NO singular `published_component` field — the verified
    incident shape."""
    training_table.put_item(Item={
        "training_id": "tr-{}".format(uuid.uuid4()),
        "usecase_id": usecase_id,
        "model_name": model_name,
        "model_type": "object_detection",
        "created_at": 1,
        "published_components": entries,
    })


@pytest.fixture
def usecase_id():
    """A fresh usecase per test so GSI queries never cross-contaminate."""
    return "uc-{}".format(uuid.uuid4())


# --------------------------------------------------------------------------
# Exploration case 9: isBugCondition_G
# --------------------------------------------------------------------------

class TestVisionPluralPublishedComponentsResolution:
    """Exploration case 9 — plural-shape vision resolution (isBugCondition_G).

    Validates: Requirements 1.17, 1.18, 1.19
    """

    def test_plural_published_record_resolves_without_packaging_error(
            self, packaging_env, usecase_id):
        """(a) isBugCondition_G, the verified incident: vision model
        'yolo_test' carries published_components entries (status 'published',
        6.0.0) for jetson-xavier-jp5 AND jetson-xavier-jp6 — every selected
        architecture covered — and no singular published_component. The fixed
        resolve_model_components resolves it; the unfixed one raises the
        misleading PackagingError "no published Greengrass component".

        Validates: Requirements 1.17, 1.18 (expected behavior 2.18)
        """
        seed_vision_record(
            packaging_env.training_table, usecase_id, INCIDENT_MODEL_NAME,
            [published_entry(INCIDENT_JP5_COMPONENT, TARGET_OF["arm64_jp5"]),
             published_entry(INCIDENT_JP6_COMPONENT, TARGET_OF["arm64_jp6"])])

        try:
            resolved = resolve_models(
                packaging_env.packaging, [INCIDENT_MODEL_NAME], usecase_id,
                INCIDENT_ARCHS)
        except packaging_env.packaging.PackagingError as error:
            pytest.fail(
                "COUNTEREXAMPLE (Defect G): resolve_model_components(['{}'], "
                "usecase, archs={}) raised PackagingError {!r} for a vision "
                "record whose plural published_components cover BOTH selected "
                "architectures ({} @ {}, {} @ {}; status 'published', "
                "6.0.0) — the misleading 'publish the model' rejection from "
                "the portal packaging dialog; re-publishing cannot help "
                "because the vision publish flow only writes the plural "
                "field.".format(
                    INCIDENT_MODEL_NAME, INCIDENT_ARCHS, str(error),
                    INCIDENT_JP5_COMPONENT, TARGET_OF["arm64_jp5"],
                    INCIDENT_JP6_COMPONENT, TARGET_OF["arm64_jp6"]))

        assert INCIDENT_MODEL_NAME in resolved, (
            "resolution succeeded but returned no entry for '{}': {!r}"
            .format(INCIDENT_MODEL_NAME, resolved))

    def test_distinct_per_target_names_emit_zero_model_entries(
            self, packaging_env, usecase_id):
        """(b, multi-name half) the incident record's per-target component
        names DIFFER (…-jetson-xavier-jp5 vs …-jetson-xavier-jp6), so
        emitting one HARD entry per target into the recipe-GLOBAL
        ComponentDependencies would make a multi-arch package undeployable
        on any single device (the Defect F failure shape, 1.19). The fixed
        emission omits the model's entries entirely.

        Validates: Requirements 1.17, 1.19 (expected behavior 2.20)
        """
        seed_vision_record(
            packaging_env.training_table, usecase_id, INCIDENT_MODEL_NAME,
            [published_entry(INCIDENT_JP5_COMPONENT, TARGET_OF["arm64_jp5"]),
             published_entry(INCIDENT_JP6_COMPONENT, TARGET_OF["arm64_jp6"])])

        try:
            resolved = resolve_models(
                packaging_env.packaging, [INCIDENT_MODEL_NAME], usecase_id,
                INCIDENT_ARCHS)
        except packaging_env.packaging.PackagingError as error:
            pytest.fail(
                "COUNTEREXAMPLE (Defect G): resolution of the plural-shape "
                "'{}' record failed before emission could run: {!r}"
                .format(INCIDENT_MODEL_NAME, str(error)))

        dependencies = packaging_env.packaging.model_component_dependencies(
            resolved)

        assert dependencies == {}, (
            "COUNTEREXAMPLE (Defect G): the resolved '{}' record covers the "
            "selected archs with DISTINCT per-target component names ({} / "
            "{}), yet model_component_dependencies emitted {!r} into the "
            "recipe-global block — a multi-arch package carrying HARD deps "
            "on per-target model components is undeployable on any single "
            "device (the Defect F failure shape, for model components)."
            .format(INCIDENT_MODEL_NAME, INCIDENT_JP5_COMPONENT,
                    INCIDENT_JP6_COMPONENT, dependencies))

    def test_single_name_collapse_emits_one_unpinned_hard_entry(
            self, packaging_env, usecase_id):
        """(b, single-name half) a plural-shape record whose covered entries
        collapse to a SINGLE component name emits exactly one unpinned HARD
        entry — the Defect C emission behavior.

        Validates: Requirements 1.17 (expected behavior 2.20)
        """
        model_name = "yolo-single-name"
        component_name = "model-yolo-single-name"
        seed_vision_record(
            packaging_env.training_table, usecase_id, model_name,
            [published_entry(component_name, TARGET_OF["arm64_jp5"]),
             published_entry(component_name, TARGET_OF["arm64_jp6"])])

        try:
            resolved = resolve_models(
                packaging_env.packaging, [model_name], usecase_id,
                INCIDENT_ARCHS)
        except packaging_env.packaging.PackagingError as error:
            pytest.fail(
                "COUNTEREXAMPLE (Defect G): resolution of the plural-shape "
                "'{}' record (single component name '{}' across both "
                "targets) failed before emission could run: {!r}"
                .format(model_name, component_name, str(error)))

        dependencies = packaging_env.packaging.model_component_dependencies(
            resolved)

        assert dependencies == {component_name: {
            "VersionRequirement": ">=0.0.0",
            "DependencyType": "HARD",
        }}, (
            "COUNTEREXAMPLE (Defect G): the resolved '{}' record collapses "
            "to the single component name '{}', which must emit exactly ONE "
            "unpinned HARD entry; got {!r}"
            .format(model_name, component_name, dependencies))

    def test_uncovered_arch_raises_naming_model_and_architecture(
            self, packaging_env, usecase_id):
        """(c) a selected architecture with NO published entry fails closed
        with a PackagingError naming the model AND the uncovered
        architecture/target — an accurate message, unlike today's misleading
        'no published Greengrass component; publish the model'.

        Validates: Requirements 1.18 (expected behavior 2.19)
        """
        model_name = "yolo-jp6-only"
        seed_vision_record(
            packaging_env.training_table, usecase_id, model_name,
            [published_entry("model-yolo-jp6-only-jetson-xavier-jp6",
                             TARGET_OF["arm64_jp6"])])

        with pytest.raises(
                packaging_env.packaging.PackagingError) as excinfo:
            resolve_models(packaging_env.packaging, [model_name],
                           usecase_id, INCIDENT_ARCHS)

        message = str(excinfo.value)
        assert model_name in message, (
            "the fail-closed error must name the model; got {!r}"
            .format(message))
        assert ("arm64_jp5" in message
                or TARGET_OF["arm64_jp5"] in message), (
            "COUNTEREXAMPLE (Defect G): '{}' has a published entry only for "
            "{}; requesting {} must raise a PackagingError naming the "
            "uncovered architecture 'arm64_jp5' (or its target '{}') — got "
            "the message {!r} (today's misleading 'publish the model' text "
            "names neither, and re-publishing cannot fix a coverage gap)."
            .format(model_name, TARGET_OF["arm64_jp6"], INCIDENT_ARCHS,
                    TARGET_OF["arm64_jp5"], message))
