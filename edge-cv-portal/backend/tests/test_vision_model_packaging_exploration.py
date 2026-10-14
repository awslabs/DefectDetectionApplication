# -*- coding: utf-8 -*-
"""Bug-condition exploration test (Task 1) for vision-model-packaging-regression.

Property 1: Bug Condition — Published vision models resolve and package.

**These tests assert the FIXED expected behavior (2.1–2.6), so they are
EXPECTED TO FAIL on the unfixed tree.** The failure is the counterexample
confirming the defect (bug conditions 1.1, 1.2): the Defect C
`resolve_model_components` reads only the SINGULAR ``published_component``
registry attribute — the shape only the vLLM publish path writes. Vision /
ONNX publishes (greengrass_publish.py) write ``published_components``
(PLURAL): a per-target list of ``{component_name, target,
component_version, status}`` entries. So every published vision model hits
the fail-closed gate with "has no published Greengrass component; publish
the model before packaging workflows that use it".

Verified incident: workflow 6075bf76 v3 referencing model ``yolo_test``
failed packaging with exactly that error (Lambda log 2026-08-04T04:38Z);
the dda-portal-training-jobs item (training 6a43ff2b) carries
``published_component: null`` but ``published_components`` with a
``status: published`` entry for ``model-yolo-test-jetson-xavier-jp5``.

The SAME tests are re-run in task 3.2 against the fixed
workflow_packaging.py, where they must PASS:

- (a) a vision record published for jp5 + archs ['arm64_jp5'] resolves
  (no PackagingError) and emits exactly ONE HARD unpinned model entry on
  the jp5 component name;
- (b) a jp5-only record + archs ['arm64_jp6'] fails closed with a
  PackagingError naming the model and the uncovered arch/target (2.6,
  superseded by edge-deploy-reliability Defect G 2.19 — fail closed on
  uncovered architectures);
- (c) jp5 AND jp6 entries under DIFFERENT names + archs spanning both
  emit ZERO model entries (divergence omission, Defect F discipline — 2.4).

Signature note: the fixed ``resolve_model_components`` gains the selected
archs. The ``resolve`` wrapper below calls the fixed 3-arg signature and
falls back to the unfixed 2-arg one on TypeError, so on the unfixed tree
each test fails on the PackagingError (the bug), never on the signature
difference.

Harness: the moto ``aws_stack`` fixture from conftest plus a training-jobs
Model_Registry table with the production ``usecase-training-index`` GSI
(the test_workflow_packaging_recipe_preservation.py pattern);
workflow_packaging is imported inside the moto mock because its
module-level boto3 clients demand it.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6 (bug-condition
side: 1.1, 1.2)**
"""
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = (
    "test-vision-model-packaging-regression-training-jobs")

MODEL_NAME = "yolo_test"
JP5_TARGET = "jetson-xavier-jp5"
JP6_TARGET = "jetson-xavier-jp6"
JP5_COMPONENT = "model-yolo-test-jetson-xavier-jp5"
JP6_COMPONENT = "model-yolo-test-jetson-xavier-jp6"

#: The one HARD unpinned entry shape model_component_dependencies emits
#: (unchanged golden contract from the Defect C design).
HARD_UNPINNED = {"VersionRequirement": ">=0.0.0", "DependencyType": "HARD"}


@pytest.fixture(scope="module")
def packaging_env(aws_stack):
    """The training-jobs Model_Registry table (production GSI shape) plus
    a freshly imported workflow_packaging bound to it inside moto."""
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


def seed_vision_record(training_table, usecase_id, published_entries,
                       model_name=MODEL_NAME):
    """A training-jobs record shaped like the LIVE yolo_test item
    (dda-portal-training-jobs, training 6a43ff2b): ``published_component``
    (singular) null, ``published_components`` (plural) the per-target
    vision publish list written by greengrass_publish.py."""
    training_table.put_item(Item={
        "training_id": f"tr-{uuid.uuid4()}",
        "usecase_id": usecase_id,
        "model_name": model_name,
        "model_type": "object_detection",
        "created_at": 1,
        "published_component": None,
        "published_components": published_entries,
    })


def published_entry(component_name, target, status="published",
                    component_version="6.0.0"):
    return {
        "component_name": component_name,
        "target": target,
        "component_version": component_version,
        "status": status,
    }


def resolve(packaging, model_names, usecase_id, archs):
    """Call the FIXED resolve_model_components(model_names, usecase_id,
    archs); on the unfixed 2-arg signature fall back so the exploration
    failure lands on the PackagingError (the bug), not a TypeError."""
    try:
        return packaging.resolve_model_components(model_names, usecase_id,
                                                  archs)
    except TypeError:
        return packaging.resolve_model_components(model_names, usecase_id)


def resolve_and_emit(packaging, model_names, usecase_id, archs):
    """Resolution feeding emission — the packaging handler's model
    dependency path. Fails the test with the bug's counterexample when the
    unfixed fail-closed gate misfires (isBugCondition: plural-only record
    with a status=='published' entry)."""
    try:
        resolved = resolve(packaging, model_names, usecase_id, archs)
    except packaging.PackagingError as exc:
        pytest.fail(
            "COUNTEREXAMPLE (bug condition 1.1/1.2): "
            "resolve_model_components raised PackagingError "
            "(artifact={!r}): {} — for a model whose registry record "
            "carries published_components (plural, vision shape) with a "
            "status=='published' entry. The live incident's error "
            "(workflow 6075bf76 v3, model yolo_test, Lambda 04:38Z): the "
            "resolver reads only the singular vLLM-shape "
            "published_component attribute.".format(
                exc.artifact, exc.message))
    return packaging.model_component_dependencies(resolved)


class TestPublishedVisionModelResolvesAndPackages:
    """Property 1 exploration — Validates: Requirements 2.1–2.6 (1.1, 1.2)."""

    def test_jp5_published_model_resolves_and_emits_one_hard_entry(
            self, packaging_env):
        """(a) The LIVE incident record shape: yolo_test published for
        jetson-xavier-jp5 only, archs ['arm64_jp5'] — resolution must not
        raise and emission is exactly one HARD unpinned entry on the jp5
        component name (2.2, 2.3).

        Validates: Requirements 2.2, 2.3 (bug condition 1.1, 1.2)
        """
        usecase_id = f"uc-{uuid.uuid4()}"
        seed_vision_record(packaging_env.training_table, usecase_id,
                           [published_entry(JP5_COMPONENT, JP5_TARGET)])

        dependencies = resolve_and_emit(
            packaging_env.packaging, [MODEL_NAME], usecase_id,
            ["arm64_jp5"])

        assert dependencies == {JP5_COMPONENT: HARD_UNPINNED}, (
            "COUNTEREXAMPLE: expected exactly one HARD unpinned model "
            "dependency on {!r}, got {!r}".format(JP5_COMPONENT,
                                                  dependencies))

    def test_target_mismatch_fails_closed_naming_model_and_uncovered_arch(
            self, packaging_env):
        """(b) Only-jp5-published record + archs ['arm64_jp6']: the selected
        architecture has NO published entry, so resolution fails closed with
        a PackagingError naming the model AND the uncovered
        architecture/target (2.6, superseded by edge-deploy-reliability
        Defect G 2.19 — silently packaging for an uncovered arch would
        produce a component that cannot work there).

        Validates: Requirements 2.6 (bug condition 1.1, 1.2)
        """
        usecase_id = f"uc-{uuid.uuid4()}"
        seed_vision_record(packaging_env.training_table, usecase_id,
                           [published_entry(JP5_COMPONENT, JP5_TARGET)])

        with pytest.raises(
                packaging_env.packaging.PackagingError) as excinfo:
            resolve(packaging_env.packaging, [MODEL_NAME], usecase_id,
                    ["arm64_jp6"])

        message = str(excinfo.value)
        assert MODEL_NAME in message, (
            "the fail-closed error must name the model; got {!r}"
            .format(message))
        assert ("arm64_jp6" in message or JP6_TARGET in message), (
            "COUNTEREXAMPLE: '{}' has a published entry only for {}; "
            "requesting ['arm64_jp6'] must raise a PackagingError naming "
            "the uncovered architecture 'arm64_jp6' (or its target '{}') "
            "— got the message {!r}".format(
                MODEL_NAME, JP5_TARGET, JP6_TARGET, message))

    def test_divergent_per_target_names_emit_zero_entries(
            self, packaging_env):
        """(c) Published entries for BOTH jp5 and jp6 under DIFFERENT
        component names, archs spanning both: resolution must not raise
        and emission omits the model entirely (divergence omission, the
        Defect F single-name discipline) (2.4).

        Validates: Requirements 2.4 (bug condition 1.1, 1.2)
        """
        usecase_id = f"uc-{uuid.uuid4()}"
        seed_vision_record(packaging_env.training_table, usecase_id, [
            published_entry(JP5_COMPONENT, JP5_TARGET),
            published_entry(JP6_COMPONENT, JP6_TARGET),
        ])

        dependencies = resolve_and_emit(
            packaging_env.packaging, [MODEL_NAME], usecase_id,
            ["arm64_jp5", "arm64_jp6"])

        assert dependencies == {}, (
            "COUNTEREXAMPLE: per-target component names diverge ({!r} vs "
            "{!r}) across the selected archs; the model's entries must be "
            "omitted (2.4, Defect F discipline), got {!r}".format(
                JP5_COMPONENT, JP6_COMPONENT, dependencies))
