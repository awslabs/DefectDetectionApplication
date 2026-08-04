"""Preservation property tests (Task 2) for vision-model-packaging-regression.

**Property 2: Preservation — vLLM resolution, fail-closed gates, and
non-model paths unchanged**

Observed on the UNFIXED `workflow_packaging.py` and encoded so the same
assertions hold BEFORE and AFTER the fix (the fix adds an ``archs``
parameter to ``resolve_model_components``; a signature-tolerant wrapper
below calls the new three-arg form first and falls back to the old
two-arg form on TypeError):

(a) singular vLLM-shape records (``published_component: {component_name:
    'model-vllm-<x>'}``) resolve and emit exactly today's entries —
    ``{'VersionRequirement': '>=0.0.0', 'DependencyType': 'HARD'}`` per
    distinct component — property over generated names (Hypothesis);
(b) a record with NEITHER publish shape raises the exact existing
    "has no published Greengrass component" PackagingError; a missing
    record raises the exact "no record in the Use_Case model registry"
    PackagingError;
(c) an empty model list resolves to {} (and emits {});
(d) TRAINING_JOBS_TABLE unset skips with the existing warning (empty
    result);
(e) plugin_component_dependencies and local_server_component_dependencies
    (Defect F single-variant rule) are byte-identical for sample inputs —
    these functions are untouched by this fix.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

Harness: the established moto-backed packaging stack (conftest
``aws_stack``) plus the training-jobs Model_Registry table with the
production ``usecase-training-index`` GSI shape (the Defect C exploration
test pattern). Hypothesis runs under the repo's ``portal-fast`` profile.
"""
import logging
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-vision-preservation-training-jobs"

# ---------------------------------------------------------------------------
# Reference expectations, restated from the unfixed module's observed
# behavior (not imported from the implementation, so the assertions cannot
# silently drift with a behavior change).
# ---------------------------------------------------------------------------

MODEL_DEPENDENCY_ENTRY = {"VersionRequirement": ">=0.0.0",
                          "DependencyType": "HARD"}

NO_PUBLISHED_COMPONENT_MESSAGE = (
    "Model '{name}' referenced by the workflow has no published Greengrass "
    "component; publish the model before packaging workflows that use it")

NO_REGISTRY_RECORD_MESSAGE = (
    "Model '{name}' referenced by the workflow has no record in the "
    "Use_Case model registry; it may have been removed since the workflow "
    "was validated")

VALID_ARCHS = ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5",
               "arm64_jp6")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging_env(aws_stack):
    """The training-jobs Model_Registry table (production GSI shape) plus a
    freshly imported workflow_packaging bound to it inside moto (Defect C
    exploration-test pattern)."""
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

    # Re-import so the module binds the table name above and the
    # moto-intercepted clients.
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


def resolve(packaging, model_names, usecase_id, archs):
    """Signature-tolerant call: the fix adds an ``archs`` parameter to
    resolve_model_components; before the fix it takes two arguments. Try the
    new form first, fall back on the old-arity TypeError."""
    try:
        return packaging.resolve_model_components(model_names, usecase_id,
                                                  archs)
    except TypeError:
        return packaging.resolve_model_components(model_names, usecase_id)


def seed_vllm_record(training_table, usecase_id, model_name, component_name):
    """A training-jobs record in the singular vLLM publish shape."""
    training_table.put_item(Item={
        "training_id": f"tr-{uuid.uuid4()}",
        "usecase_id": usecase_id,
        "model_name": model_name,
        "model_type": "vllm",
        "created_at": 1,
        "published_component": {
            "component_name": component_name,
            "component_version": "1.0.0",
            "runtime": "vllm",
            "supported_architectures": ["arm64_jp6"],
        },
    })


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_name_alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-"
_model_names = st.text(alphabet=_name_alphabet, min_size=1, max_size=24)

# 1-4 distinct model names, each becoming a vLLM-shape record.
model_name_lists = st.lists(_model_names, min_size=1, max_size=4,
                            unique=True)
arch_lists = st.lists(st.sampled_from(VALID_ARCHS), min_size=1,
                      max_size=len(VALID_ARCHS), unique=True)


# ---------------------------------------------------------------------------
# (a) vLLM-shape resolution + emission unchanged (Requirement 3.1)
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(model_names=model_name_lists, archs=arch_lists)
def test_vllm_shape_records_resolve_and_emit_todays_entries(
        packaging_env, model_names, archs):
    """**Property 2: Preservation — vLLM resolution unchanged**

    For any generated model names carrying the singular vLLM publish shape,
    resolution succeeds and model_component_dependencies emits exactly one
    unpinned HARD entry per component, byte-identical to today's output.

    **Validates: Requirements 3.1**
    """
    usecase_id = f"uc-{uuid.uuid4()}"
    expected = {}
    for name in model_names:
        component_name = f"model-vllm-{name}"
        seed_vllm_record(packaging_env.training_table, usecase_id, name,
                         component_name)
        expected[component_name] = dict(MODEL_DEPENDENCY_ENTRY)

    resolved = resolve(packaging_env.packaging, model_names, usecase_id,
                       archs)

    assert set(resolved) == set(model_names)
    emitted = packaging_env.packaging.model_component_dependencies(resolved)
    assert emitted == expected


# ---------------------------------------------------------------------------
# (b) Fail-closed gates unchanged (Requirements 3.4 and the 2.5 message)
# ---------------------------------------------------------------------------

def test_record_with_neither_publish_shape_fails_closed(packaging_env):
    """A registry record with neither publish shape (no singular
    published_component, no published_components list) raises the exact
    existing PackagingError message.

    **Validates: Requirements 3.4 (existing gate wording preserved: 2.5)**
    """
    usecase_id = f"uc-{uuid.uuid4()}"
    name = "unpublished-model"
    packaging_env.training_table.put_item(Item={
        "training_id": f"tr-{uuid.uuid4()}",
        "usecase_id": usecase_id,
        "model_name": name,
        "model_type": "anomaly_detection",
        "created_at": 1,
    })

    with pytest.raises(packaging_env.packaging.PackagingError) as excinfo:
        resolve(packaging_env.packaging, [name], usecase_id, ["arm64_jp5"])

    assert excinfo.value.message == \
        NO_PUBLISHED_COMPONENT_MESSAGE.format(name=name)
    assert excinfo.value.artifact == f"models/{name}"


def test_record_with_null_published_component_fails_closed(packaging_env):
    """`published_component: None` (the explicit-null spelling live records
    carry) is the same neither-shape gate."""
    usecase_id = f"uc-{uuid.uuid4()}"
    name = "null-published-model"
    packaging_env.training_table.put_item(Item={
        "training_id": f"tr-{uuid.uuid4()}",
        "usecase_id": usecase_id,
        "model_name": name,
        "model_type": "anomaly_detection",
        "created_at": 1,
        "published_component": None,
    })

    with pytest.raises(packaging_env.packaging.PackagingError) as excinfo:
        resolve(packaging_env.packaging, [name], usecase_id, ["arm64_jp5"])

    assert excinfo.value.message == \
        NO_PUBLISHED_COMPONENT_MESSAGE.format(name=name)


def test_missing_registry_record_fails_closed(packaging_env):
    """A referenced model with no registry record at all raises the exact
    existing "no record in the Use_Case model registry" PackagingError.

    **Validates: Requirements 3.4**
    """
    usecase_id = f"uc-{uuid.uuid4()}"
    name = "never-registered-model"

    with pytest.raises(packaging_env.packaging.PackagingError) as excinfo:
        resolve(packaging_env.packaging, [name], usecase_id, ["arm64_jp5"])

    assert excinfo.value.message == \
        NO_REGISTRY_RECORD_MESSAGE.format(name=name)
    assert excinfo.value.artifact == f"models/{name}"


# ---------------------------------------------------------------------------
# (c) Empty model list (Requirement 3.2)
# ---------------------------------------------------------------------------

def test_empty_model_list_resolves_and_emits_nothing(packaging_env):
    """No model references -> resolution {} and no emitted model entries.

    **Validates: Requirements 3.2**
    """
    resolved = resolve(packaging_env.packaging, [], f"uc-{uuid.uuid4()}",
                       ["arm64_jp5"])
    assert resolved == {}
    assert packaging_env.packaging.model_component_dependencies(resolved) == {}


# ---------------------------------------------------------------------------
# (d) TRAINING_JOBS_TABLE unset -> skip with warning (Requirement 3.3)
# ---------------------------------------------------------------------------

def test_training_jobs_table_unset_skips_with_warning(packaging_env,
                                                      monkeypatch, caplog):
    """With no TRAINING_JOBS_TABLE configured, model dependencies are
    skipped (empty result) with the existing warning.

    **Validates: Requirements 3.3**
    """
    monkeypatch.setattr(packaging_env.packaging, "TRAINING_JOBS_TABLE", None)
    with caplog.at_level(logging.WARNING):
        resolved = resolve(packaging_env.packaging, ["some-model"],
                           f"uc-{uuid.uuid4()}", ["arm64_jp5"])
    assert resolved == {}
    assert "TRAINING_JOBS_TABLE not configured" in caplog.text


# ---------------------------------------------------------------------------
# (e) Non-model dependency paths untouched (Requirement 3.2, Defect F rule)
# ---------------------------------------------------------------------------

def test_plugin_component_dependencies_golden(packaging_env):
    """plugin_component_dependencies output is byte-identical for a sample
    dep_records map (pinned per-plugin entries, None records skipped).

    **Validates: Requirements 3.2**
    """
    dep_records = {
        "custom:uc/alpha": {"plugin_id": "plug-a", "version": 2,
                            "lifecycle_state": "test"},
        "custom:uc/beta": None,
        "custom:uc/gamma": {"plugin_id": "plug-g", "version": 10,
                            "lifecycle_state": "prod"},
    }
    assert packaging_env.packaging.plugin_component_dependencies(
        dep_records) == {
        "dda.plugin.plug-a": {"VersionRequirement": ">=2.0.0 <3.0.0",
                              "DependencyType": "HARD"},
        "dda.plugin.plug-g": {"VersionRequirement": ">=10.0.0 <11.0.0",
                              "DependencyType": "HARD"},
    }


def test_local_server_dependencies_single_variant_golden(packaging_env):
    """local_server_component_dependencies emits today's single-variant
    entries: one HARD entry at the arch's minimum-version floor for a single
    arch, and one amd64 entry for the x86_64 + x86_64_nvidia pair (both run
    the one amd64 build).

    **Validates: Requirements 3.2 (Defect F single-variant rule)**
    """
    packaging = packaging_env.packaging

    assert packaging.local_server_component_dependencies(["arm64_jp5"]) == {
        "aws.edgeml.dda.LocalServer.arm64JP5": {
            "VersionRequirement":
                ">=" + packaging.min_local_server_version_for("arm64_jp5"),
            "DependencyType": "HARD",
        },
    }

    amd64_floor = max(packaging.min_local_server_version_for("x86_64"),
                      packaging.min_local_server_version_for("x86_64_nvidia"))
    assert packaging.local_server_component_dependencies(
        ["x86_64", "x86_64_nvidia"]) == {
        "aws.edgeml.dda.LocalServer.amd64": {
            "VersionRequirement": ">=" + amd64_floor,
            "DependencyType": "HARD",
        },
    }


def test_local_server_dependencies_multi_variant_omitted_golden(
        packaging_env):
    """Multiple distinct LocalServer variants -> {} (the Defect F
    deployability omission), and an unknown arch still fails closed.

    **Validates: Requirements 3.2 (Defect F single-variant rule)**
    """
    packaging = packaging_env.packaging

    assert packaging.local_server_component_dependencies(
        ["arm64_jp5", "arm64_jp6"]) == {}

    with pytest.raises(packaging.PackagingError):
        packaging.local_server_component_dependencies(["riscv"])
