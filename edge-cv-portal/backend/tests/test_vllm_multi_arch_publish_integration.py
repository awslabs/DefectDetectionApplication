# -*- coding: utf-8 -*-
"""Backend integration tests for vllm-multi-arch-publish-conflict
(task 5.2, design Testing Strategy).

Moto + fake Greengrass end-to-end coverage of the fixed publish and the
deployment gate:

1. **Two-target publish succeeds end to end**: a vLLM record packaged
   for `arm64_jp6` AND `arm64_jp7` publishes TWO DEPLOYABLE components
   (one per JetPack), returns 200 with two `published_components`
   entries, and writes the `published_component.components` list plus
   the Models-table record and `published = True` (2.3, 2.5, 3.14).
2. **Atomicity on a forced second-target failure**: when the second
   target fails registration, BOTH created component versions are rolled
   back, NO publish state is written onto the record, and the handler
   returns the retryable 502 with `failed_step: greengrass_registration`
   (3.13).
3. **Deploy-gate round trip**: publish, then run
   `check_vllm_deployment_gate` (the real gate entry point in
   deployments.py, fed by `collect_vllm_component_manifests` over the
   moto-backed GSI) for an `arm64_jp7` device against both per-JetPack
   component names — the JP7 component passes and the JP6 component is
   rejected with reason `ARCH_UNSUPPORTED` (2.12, 2.14).

Harness: self-contained (module-scoped moto stack, fake greengrassv2)
so it runs with `--noconftest`, mirroring
test_vllm_multi_arch_publish_properties.py.

Run:
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \\
      edge-cv-portal/backend/tests/test_vllm_multi_arch_publish_integration.py \\
      -q -p no:cacheprovider --noconftest

**Validates: Requirements 2.3, 2.5, 2.12, 2.14, 3.13, 3.14**
"""
import importlib.util
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_FUNCTIONS = os.path.join(_BACKEND, "functions")
_SHARED_LAYER = os.path.join(_BACKEND, "layers", "shared", "python")
_PUBLISH_PATH = os.path.join(_FUNCTIONS, "greengrass_publish.py")
_PACKAGING_PATH = os.path.join(_FUNCTIONS, "packaging.py")

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-vllm-integ"
MODELS_TABLE_NAME = "test-models-vllm-integ"
USECASES_TABLE_NAME = "test-usecases-vllm-integ"
USER_ROLES_TABLE_NAME = "test-user-roles-vllm-integ"
AUDIT_LOG_TABLE_NAME = "test-audit-log-vllm-integ"
DEVICES_TABLE_NAME = "test-devices-vllm-integ"

TEST_ENV = {
    # Fake credentials so boto3 can never reach real AWS.
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": REGION,
    "AWS_REGION": REGION,
    "TRAINING_JOBS_TABLE": TRAINING_JOBS_TABLE_NAME,
    "MODELS_TABLE": MODELS_TABLE_NAME,
    "USECASES_TABLE": USECASES_TABLE_NAME,
    "USER_ROLES_TABLE": USER_ROLES_TABLE_NAME,
    "AUDIT_LOG_TABLE": AUDIT_LOG_TABLE_NAME,
    "DEVICES_TABLE": DEVICES_TABLE_NAME,
}

for _path in (_SHARED_LAYER, _FUNCTIONS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

COMPONENT_NAME_INDEX = "component_name-index"


def _load_module(path, name):
    """Load a functions/*.py module under a distinct module name (inside
    the moto mock, so module-level boto3 clients bind to the fake AWS)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fake Greengrass client (moto has no greengrassv2)
# ---------------------------------------------------------------------------

def component_arn(name, version=None):
    arn = f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:components:{name}"
    return f"{arn}:versions:{version}" if version else arn


def name_from_arn(arn):
    return str(arn).split(":components:")[1].split(":")[0]


def version_from_arn(arn):
    return str(arn).split(":versions:")[1]


class _FakePaginator:
    def __init__(self, fake, operation):
        self.fake = fake
        self.operation = operation

    def paginate(self, **kwargs):
        if self.operation == "list_components":
            yield {"components": [
                {"componentName": name, "arn": component_arn(name)}
                for name in sorted(self.fake.state)
            ]}
        elif self.operation == "list_component_versions":
            name = name_from_arn(kwargs["arn"])
            yield {"componentVersions": [
                {"componentVersion": version,
                 "arn": component_arn(name, version)}
                for version in sorted(self.fake.state.get(name, ()))
            ]}
        else:  # pragma: no cover - unexpected paginator in the publish path
            raise AssertionError(f"unexpected paginator: {self.operation}")


class FakeGreengrass:
    """Service-shaped greengrassv2 fake for the publish path (same shape
    as the one in test_vllm_multi_arch_publish_properties.py).

    ``create_component_version`` raises ``ConflictException`` on a
    repeated ``(ComponentName, ComponentVersion)``, ``get_paginator``
    serves listings from its own state, ``describe_component`` reports
    DEPLOYABLE unless ``fail_describe_from`` forces the atomicity gate,
    and ``delete_component`` succeeds (the post-fix authorized-rollback
    IAM shape)."""

    def __init__(self, existing=None, delete_authorized=True,
                 fail_describe_from=None):
        # component name -> set of registered version strings
        self.state = {name: set(versions)
                      for name, versions in (existing or {}).items()}
        self.attempts = []        # (name, version) creates attempted
        self.created = []         # accepted recipes
        self.created_arns = []    # ARNs of accepted creates, in order
        self.deleted = []         # rollback attempts
        self.describe_states = {} # arn -> last componentState reported
        self.delete_authorized = delete_authorized
        self.fail_describe_from = fail_describe_from

    def create_component_version(self, inlineRecipe, tags=None):
        recipe = json.loads(inlineRecipe)
        name = recipe["ComponentName"]
        version = recipe["ComponentVersion"]
        self.attempts.append((name, version))
        if version in self.state.get(name, set()):
            raise ClientError(
                {"Error": {
                    "Code": "ConflictException",
                    "Message": (
                        f"Component [{name} : {version}] for account "
                        f"[{ACCOUNT_ID}] already exists and can't be "
                        f"created again with tags."),
                }},
                "CreateComponentVersion")
        self.state.setdefault(name, set()).add(version)
        self.created.append(recipe)
        arn = component_arn(name, version)
        self.created_arns.append(arn)
        return {"arn": arn}

    def describe_component(self, arn):
        failing = (self.fail_describe_from is not None
                   and arn in self.created_arns
                   and self.created_arns.index(arn) >=
                   self.fail_describe_from)
        state = "FAILED" if failing else "DEPLOYABLE"
        self.describe_states[arn] = state
        message = "simulated registration failure" if failing else ""
        return {"status": {"componentState": state, "message": message}}

    def delete_component(self, arn):
        self.deleted.append(arn)
        if not self.delete_authorized:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException",
                           "Message": (
                               "User is not authorized to perform: "
                               "greengrass:DeleteComponent")}},
                "DeleteComponent")
        self.state.get(name_from_arn(arn), set()).discard(
            version_from_arn(arn))

    def get_paginator(self, operation):
        return _FakePaginator(self, operation)

    def recipe_for(self, name):
        for recipe in self.created:
            if recipe["ComponentName"] == name:
                return recipe
        return None


# ---------------------------------------------------------------------------
# Module-scoped moto stack (mirrors test_vllm_multi_arch_publish_properties)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def stack():
    from moto import mock_aws

    saved_env = {key: os.environ.get(key) for key in TEST_ENV}
    os.environ.update(TEST_ENV)

    with mock_aws():
        import boto3

        client = boto3.client("dynamodb", region_name=REGION)
        client.create_table(
            TableName=TRAINING_JOBS_TABLE_NAME,
            KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "training_id", "AttributeType": "S"},
                {"AttributeName": "component_name", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": COMPONENT_NAME_INDEX,
                "KeySchema": [
                    {"AttributeName": "component_name", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        for table_name, key in ((MODELS_TABLE_NAME, "model_id"),
                                (USECASES_TABLE_NAME, "usecase_id"),
                                (DEVICES_TABLE_NAME, "device_id")):
            client.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        client.create_table(
            TableName=USER_ROLES_TABLE_NAME,
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "usecase_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "usecase_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName=AUDIT_LOG_TABLE_NAME,
            KeySchema=[
                {"AttributeName": "event_id", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "N"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        for module_name in ("shared_utils", "workflow_guards", "deployments"):
            sys.modules.pop(module_name, None)
        publish = _load_module(_PUBLISH_PATH, "portal_gg_publish_integ")
        packaging = _load_module(_PACKAGING_PATH, "portal_packaging_integ")
        import deployments

        resource = boto3.resource("dynamodb", region_name=REGION)
        usecase_id = f"uc-{uuid.uuid4()}"
        resource.Table(USECASES_TABLE_NAME).put_item(Item={
            "usecase_id": usecase_id,
            "name": "vLLM Multi-Arch Integration Use Case",
            "account_id": ACCOUNT_ID,
            "s3_bucket": "test-vllm-integ-bucket",
        })
        user_id = f"user-{uuid.uuid4()}"
        resource.Table(USER_ROLES_TABLE_NAME).put_item(Item={
            "user_id": user_id,
            "usecase_id": usecase_id,
            "role": "DataScientist",
        })

        yield SimpleNamespace(
            publish=publish,
            packaging=packaging,
            deployments=deployments,
            resource=resource,
            training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
            models=resource.Table(MODELS_TABLE_NAME),
            devices=resource.Table(DEVICES_TABLE_NAME),
            usecase_id=usecase_id,
            user_id=user_id,
        )

    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    for module_name in ("shared_utils", "workflow_guards", "deployments"):
        sys.modules.pop(module_name, None)


# ---------------------------------------------------------------------------
# Seed / drive helpers (same shapes as the property suite)
# ---------------------------------------------------------------------------

def publish_event(training_id, user_id, component_name, component_version):
    return {
        "httpMethod": "POST",
        "path": f"/api/v1/training/{training_id}/publish",
        "pathParameters": {"id": training_id},
        "body": json.dumps({
            "component_name": component_name,
            "component_version": component_version,
        }),
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": user_id,
                    "email": f"{user_id}@example.com",
                    "cognito:username": user_id,
                }
            }
        },
    }


def packaged_entry(target):
    return {
        "target": target,
        "status": "packaged",
        "component_package_s3": (
            f"s3://test-vllm-integ-bucket/model_artifacts/model-abc/"
            f"abc_{target}_greengrass_model_component.zip"),
    }


def seed_vllm_record(stack, model_name, archs):
    """A packaged vLLM_Model_Record as packaging.py leaves it — one
    packaged_components entry per requested architecture."""
    arch_to_target = stack.packaging.VLLM_ARCH_TO_TARGET
    training_id = f"vllm-{uuid.uuid4()}"
    item = {
        "training_id": training_id,
        "usecase_id": stack.usecase_id,
        "model_name": model_name,
        "model_type": "vllm",
        "source": "vllm",
        "status": "Completed",
        "publish_eligible": True,
        "model_source": {"huggingface_model_id": "example/example-model"},
        "packaged_components": [
            {**packaged_entry(arch_to_target[arch]),
             "supported_architectures": [arch]} for arch in archs],
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    }
    stack.training_jobs.put_item(Item=item)
    return item


def run_vllm_publish(stack, record, gg):
    """Drive publish_component for a vLLM record: no polling sleeps, an
    unverifiable weight estimate (fit gate never blocks), and the fake
    Greengrass client."""
    module = stack.publish
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(module.time, "sleep", lambda seconds: None)
        mp.setattr(module, "estimate_weights",
                   lambda job, s3_head=None, hf_fetch=None: None)
        mp.setattr(module, "get_usecase_client",
                   lambda service, usecase, **kwargs: gg)
        response = module.publish_component(
            publish_event(record["training_id"], stack.user_id,
                          "model-caller-chosen", "9.0.0"), None)
    return response["statusCode"], json.loads(response["body"])


def per_jetpack_name(stack, model_name, arch):
    target = stack.packaging.VLLM_ARCH_TO_TARGET[arch]
    return f"{stack.publish.derive_vllm_component_name(model_name)}-{target}"


def seed_device(stack, thing_name, target_architecture):
    stack.devices.put_item(Item={
        "device_id": thing_name,
        "usecase_id": stack.usecase_id,
        "target_architecture": target_architecture,
    })


def gate_response_body(response):
    """Parsed error envelope of a check_vllm_deployment_gate rejection."""
    assert response is not None
    return response["statusCode"], json.loads(response["body"])


# ===========================================================================
# 1. End-to-end two-target publish (2.3, 2.5, 3.14)
# ===========================================================================

def test_two_target_publish_creates_two_deployable_components(stack):
    """A vLLM record packaged for BOTH `arm64_jp6` and `arm64_jp7`
    publishes end to end: two DEPLOYABLE components (one per JetPack,
    distinct names, shared version), HTTP 200 with two
    `published_components` entries, the `published_component.components`
    list written onto the record, `published = True`, and the
    Models-table record created.

    # Validates: Requirements 2.3, 2.5, 3.14
    """
    archs = ["arm64_jp6", "arm64_jp7"]
    model_name = f"Qwen3-VL-8B-Instruct {uuid.uuid4().hex[:8]}"
    record = seed_vllm_record(stack, model_name, archs)
    base = stack.publish.derive_vllm_component_name(model_name)
    gg = FakeGreengrass()

    status, body = run_vllm_publish(stack, record, gg)

    # 200 with two published entries (2.3).
    assert status == 200, body
    assert body["component_name"] == base
    published_entries = [comp for comp in body["published_components"]
                         if comp["status"] == "published"]
    assert len(published_entries) == 2
    expected_names = {per_jetpack_name(stack, model_name, arch)
                      for arch in archs}
    assert {comp["component_name"] for comp in published_entries} == \
        expected_names

    # Two components created, each polled to DEPLOYABLE, distinct
    # identities sharing one version (2.3).
    assert len(gg.created_arns) == 2
    assert {name for name, _ in gg.attempts} == expected_names
    assert {version for _, version in gg.attempts} == \
        {body["component_version"]}
    for arn in gg.created_arns:
        assert gg.describe_states[arn] == "DEPLOYABLE"

    # The record carries the components list write-back (2.5).
    after = stack.training_jobs.get_item(
        Key={"training_id": record["training_id"]})["Item"]
    assert after["published"] is True
    assert after["component_name"] == base
    components = after["published_component"]["components"]
    assert {entry["component_name"] for entry in components} == \
        expected_names
    assert {entry["architecture"] for entry in components} == set(archs)

    # Models-table record + model_id = training_id-version (3.14).
    model_id = f"{record['training_id']}-{body['component_version']}"
    model_item = stack.models.get_item(Key={"model_id": model_id}).get("Item")
    assert model_item is not None, f"no Models-table record {model_id}"
    assert model_item["training_job_id"] == record["training_id"]
    assert sorted(model_item["component_arns"]) == sorted(
        stack.packaging.VLLM_ARCH_TO_TARGET[arch] for arch in archs)


# ===========================================================================
# 2. Atomicity: forced second-target failure (3.13)
# ===========================================================================

def test_second_target_failure_rolls_both_back_and_returns_retryable_502(
        stack):
    """When the SECOND target fails registration, the atomicity gate
    rolls back BOTH created component versions (nothing survives in the
    fake's state), writes NO publish state onto the record, and returns
    the retryable 502 with `failed_step: greengrass_registration`.

    # Validates: Requirements 3.13
    """
    archs = ["arm64_jp6", "arm64_jp7"]
    model_name = f"Atomicity Forced Failure {uuid.uuid4().hex[:8]}"
    record = seed_vllm_record(stack, model_name, archs)
    # The component created second reports FAILED; deletes are authorized
    # (the post-fix IAM shape).
    gg = FakeGreengrass(fail_describe_from=1)

    status, body = run_vllm_publish(stack, record, gg)

    # Retryable 502 with the failing step (3.13).
    assert status == 502, body
    assert body["failed_step"] == "greengrass_registration"
    assert body["retryable"] is True

    # Both creates happened, and BOTH were rolled back — the first
    # (successful) target's version included.
    assert len(gg.created_arns) == 2
    assert gg.deleted == gg.created_arns
    survivors = {name: sorted(versions)
                 for name, versions in gg.state.items() if versions}
    assert survivors == {}, (
        f"the atomicity rollback must erase every version created during "
        f"the attempt: {survivors}")

    # No publish state written: the record stays in its pre-publish,
    # retryable shape.
    after = stack.training_jobs.get_item(
        Key={"training_id": record["training_id"]})["Item"]
    assert "published_component" not in after
    assert "published_components" not in after
    assert "published" not in after
    assert "component_name" not in after

    # And no Models-table record either.
    model_id = f"{record['training_id']}-{body['component_version']}"
    assert "Item" not in stack.models.get_item(Key={"model_id": model_id})


# ===========================================================================
# 3. Deploy-gate round trip for an arm64_jp7 device (2.12, 2.14)
# ===========================================================================

def test_deploy_gate_round_trip_jp7_device_accepts_jp7_rejects_jp6(stack):
    """Publish a two-target record, then run the real pre-submit gate
    (`check_vllm_deployment_gate`, fed by
    `collect_vllm_component_manifests` over the moto-backed GSI) for an
    `arm64_jp7` device against both per-JetPack component names: the JP7
    component passes (gate returns None) and the JP6 component is
    rejected with reason `ARCH_UNSUPPORTED` in the 409
    `VLLM_ARCH_UNSUPPORTED` envelope.

    # Validates: Requirements 2.12, 2.14
    """
    deployments = stack.deployments
    archs = ["arm64_jp6", "arm64_jp7"]
    model_name = f"Gate Round Trip {uuid.uuid4().hex[:8]}"
    record = seed_vllm_record(stack, model_name, archs)
    gg = FakeGreengrass()

    status, body = run_vllm_publish(stack, record, gg)
    assert status == 200, body
    version = body["component_version"]

    jp6_name = per_jetpack_name(stack, model_name, "arm64_jp6")
    jp7_name = per_jetpack_name(stack, model_name, "arm64_jp7")

    thing_name = f"jetson-thor1-{uuid.uuid4().hex[:8]}"
    seed_device(stack, thing_name, "arm64_jp7")

    # Manifest resolution goes through the record write-back: each
    # per-JetPack component resolves to exactly its OWN architecture
    # (2.12).
    manifests = deployments.collect_vllm_component_manifests(
        {jp6_name: version, jp7_name: version})
    assert manifests[jp7_name]["architectures"] == ["arm64_jp7"]
    assert manifests[jp6_name]["architectures"] == ["arm64_jp6"]

    # The JP7 component passes for the JP7 device (2.14).
    assert deployments.check_vllm_deployment_gate(
        {jp7_name: manifests[jp7_name]}, [thing_name]) is None

    # The JP6 component is rejected with ARCH_UNSUPPORTED (2.14).
    rejection = deployments.check_vllm_deployment_gate(
        {jp6_name: manifests[jp6_name]}, [thing_name])
    status_code, envelope = gate_response_body(rejection)
    assert status_code == 409
    assert envelope["error"]["code"] == "VLLM_ARCH_UNSUPPORTED"
    (finding,) = envelope["error"]["details"]["unsupported"]
    assert finding["component"] == jp6_name
    assert finding["device"] == thing_name
    assert finding["deviceArch"] == "arm64_jp7"
    assert finding["supported"] == ["arm64_jp6"]
    assert finding["reason"] == "ARCH_UNSUPPORTED"

    # Both names together: the ONLY finding is the JP6 component's — the
    # JP7 component contributes none.
    rejection = deployments.check_vllm_deployment_gate(
        manifests, [thing_name])
    _, envelope = gate_response_body(rejection)
    findings = envelope["error"]["details"]["unsupported"]
    assert [f["component"] for f in findings] == [jp6_name]
