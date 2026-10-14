# -*- coding: utf-8 -*-
"""Unit tests for vllm-multi-arch-publish-conflict (task 5.1, design
Testing Strategy).

Example-based unit coverage of the fix's helpers, complementing the
property suites in test_vllm_multi_arch_publish_properties.py:

1. `derive_vllm_component_name` unchanged, and per-target name
   composition for each target in the closed suffix vocabulary
2. `validate_greengrass_component_name`: accepts valid names, raises
   `PublishError` on over-length and illegal characters (2.6)
3. `VLLM_TARGET_TO_ARCH` / `VLLM_TARGET_SUFFIX_TO_ARCH` mirror
   `packaging.VLLM_ARCH_TO_TARGET` exactly (totality both ways)
4. Target-map totality both ways, plus every producible Jetson target
   mapping to platform `aarch64` and an `arm64JP{N}` LocalServer variant
   — the test that would have caught defect 4 when `jp7-vllm-enablement`
   task 6.2 landed (2.17, 2.18)
5. A synthetic target injected into `packaging.VLLM_ARCH_TO_TARGET` but
   absent from either module map makes `resolve_target_platform` (and
   therefore recipe generation) raise `PublishError` with no create, and
   does NOT default to `amd64` (2.19)
6. `existing_component_versions`: paging, no-such-component -> set(),
   ClientError -> warn + set(); `next_vllm_component_version`: `1.0.0`
   when nothing exists, `N+1.0.0` over the highest major across all
   names, unaffected by record history (2.9, 2.10)
7. `resolve_local_server_component('jetson-xavier-jp7', 'aarch64')` ==
   the arm64JP7 variant, with platform `aarch64` (2.17, 2.18)
8. `split_vllm_component_name` / `vllm_component_architectures`:
   suffixed, unsuffixed, unknown-suffix names; per-component entry hit,
   suffix fallback, out-of-set suffix -> [], legacy record-wide path
   (2.11, 2.12)
9. Publish write-back shape (`components` list, base name retained,
   `published_components` entries carrying `[arch]`) and rollback
   (attempted per created ARN; a raising delete does not propagate)
   (2.5, 2.8)

(The TS `splitVllmComponentName` twin is covered by the task 4.8
frontend suite — backend only here.)

Harness: self-contained (module-scoped moto stack, fake greengrassv2)
so it runs with `--noconftest`, mirroring
test_vllm_multi_arch_publish_properties.py.

Run:
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \\
      edge-cv-portal/backend/tests/test_vllm_multi_arch_publish_units.py \\
      -q -p no:cacheprovider --noconftest

**Validates: Requirements 2.5, 2.6, 2.8, 2.9, 2.10, 2.11, 2.12, 2.17,
2.18, 2.19**
"""
import importlib.util
import json
import logging
import os
import re
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

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-vllm-units"
MODELS_TABLE_NAME = "test-models-vllm-units"
USECASES_TABLE_NAME = "test-usecases-vllm-units"
USER_ROLES_TABLE_NAME = "test-user-roles-vllm-units"
AUDIT_LOG_TABLE_NAME = "test-audit-log-vllm-units"
DEVICES_TABLE_NAME = "test-devices-vllm-units"

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

#: LocalServer variant every vLLM Target_Architecture must depend on.
LOCAL_SERVER_FOR_ARCH = {
    "arm64_jp5": "aws.edgeml.dda.LocalServer.arm64JP5",
    "arm64_jp6": "aws.edgeml.dda.LocalServer.arm64JP6",
    "arm64_jp7": "aws.edgeml.dda.LocalServer.arm64JP7",
}


def _load_module(path, name):
    """Load a functions/*.py module under a distinct module name (inside
    the moto mock, so module-level boto3 clients bind to the fake AWS)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fake Greengrass clients (moto has no greengrassv2)
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
    and ``delete_component`` succeeds unless ``delete_authorized=False``
    (which mirrors the pre-fix denied rollback)."""

    def __init__(self, existing=None, delete_authorized=True,
                 fail_describe_from=None):
        # component name -> set of registered version strings
        self.state = {name: set(versions)
                      for name, versions in (existing or {}).items()}
        self.attempts = []        # (name, version) creates attempted
        self.created = []         # accepted recipes
        self.created_arns = []    # ARNs of accepted creates, in order
        self.deleted = []         # rollback attempts
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
        if failing:
            return {"status": {"componentState": "FAILED",
                               "message": "simulated registration failure"}}
        return {"status": {"componentState": "DEPLOYABLE", "message": ""}}

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


class PagedGreengrass:
    """Listing-only fake whose list_components / list_component_versions
    results span SEVERAL pages, so existing_component_versions' paging is
    observable (2.9)."""

    def __init__(self, component_pages, version_pages):
        self.component_pages = component_pages  # [[name, ...], ...]
        self.version_pages = version_pages      # {name: [[version, ...], ...]}

    def get_paginator(self, operation):
        outer = self

        class _Paginator:
            def paginate(self, **kwargs):
                if operation == "list_components":
                    for page in outer.component_pages:
                        yield {"components": [
                            {"componentName": name,
                             "arn": component_arn(name)}
                            for name in page]}
                elif operation == "list_component_versions":
                    name = name_from_arn(kwargs["arn"])
                    for page in outer.version_pages.get(name, []):
                        yield {"componentVersions": [
                            {"componentVersion": version,
                             "arn": component_arn(name, version)}
                            for version in page]}
                else:  # pragma: no cover
                    raise AssertionError(
                        f"unexpected paginator: {operation}")

        return _Paginator()


class ErroringGreengrass:
    """Listing fake that raises ClientError from the requested operation,
    so the warn-and-degrade path is observable (2.9)."""

    def __init__(self, fail_operation, component_name):
        self.fail_operation = fail_operation
        self.component_name = component_name

    def get_paginator(self, operation):
        outer = self

        class _Paginator:
            def paginate(self, **kwargs):
                if operation == outer.fail_operation:
                    raise ClientError(
                        {"Error": {"Code": "AccessDeniedException",
                                   "Message": "listing denied"}},
                        operation)
                if operation == "list_components":
                    yield {"components": [
                        {"componentName": outer.component_name,
                         "arn": component_arn(outer.component_name)}]}
                else:  # pragma: no cover - never reached in these tests
                    yield {"componentVersions": []}

        return _Paginator()


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
        publish = _load_module(_PUBLISH_PATH, "portal_gg_publish_units")
        packaging = _load_module(_PACKAGING_PATH, "portal_packaging_units")
        import deployments

        resource = boto3.resource("dynamodb", region_name=REGION)
        usecase_id = f"uc-{uuid.uuid4()}"
        resource.Table(USECASES_TABLE_NAME).put_item(Item={
            "usecase_id": usecase_id,
            "name": "vLLM Multi-Arch Unit-Test Use Case",
            "account_id": ACCOUNT_ID,
            "s3_bucket": "test-vllm-units-bucket",
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
            f"s3://test-vllm-units-bucket/model_artifacts/model-abc/"
            f"abc_{target}_greengrass_model_component.zip"),
    }


def seed_vllm_record(stack, model_name, archs, extra=None, extra_targets=()):
    """A packaged vLLM_Model_Record as packaging.py leaves it — one
    packaged_components entry per requested architecture (plus any raw
    extra targets)."""
    arch_to_target = stack.packaging.VLLM_ARCH_TO_TARGET
    training_id = f"vllm-{uuid.uuid4()}"
    entries = [{**packaged_entry(arch_to_target[arch]),
                "supported_architectures": [arch]} for arch in archs]
    entries += [packaged_entry(target) for target in extra_targets]
    item = {
        "training_id": training_id,
        "usecase_id": stack.usecase_id,
        "model_name": model_name,
        "model_type": "vllm",
        "source": "vllm",
        "status": "Completed",
        "publish_eligible": True,
        "model_source": {"huggingface_model_id": "example/example-model"},
        "packaged_components": entries,
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    }
    if extra:
        item.update(extra)
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


# ===========================================================================
# 1. derive_vllm_component_name and per-target name composition
# ===========================================================================

def test_derive_vllm_component_name_is_unchanged_base_name(stack):
    """`derive_vllm_component_name` still returns
    `model-vllm-{safe_model_name}` verbatim — the base the per-target
    suffixing appends to.

    # Validates: Requirements 2.11 (Base_Component_Name is the GSI key)
    """
    module = stack.publish
    for model_name, expected_safe in (
            ("Qwen3-VL-8B-Instruct", "qwen3-vl-8b-instruct"),
            ("My Model", "my-model")):
        assert module._safe_model_name(model_name) == expected_safe
        assert module.derive_vllm_component_name(model_name) == \
            f"model-vllm-{expected_safe}"


def test_per_target_name_composition_for_closed_suffix_vocabulary(stack):
    """For every target in the closed suffix vocabulary, the
    Per_JetPack_Component name is `base + '-' + target_suffix` with
    `target_suffix = target.replace('_', '-')` (the vision convention),
    and it round-trips through the deployment-side splitter.

    # Validates: Requirements 2.11, 2.12
    """
    module = stack.publish
    deployments = stack.deployments
    base = module.derive_vllm_component_name("Qwen3-VL-8B-Instruct")
    for target, arch in sorted(module.VLLM_TARGET_TO_ARCH.items()):
        suffix = target.replace("_", "-")
        # The packaging targets are already dash-safe; the suffix IS the
        # target, exactly the transform vision components use.
        assert suffix == target
        name = f"{base}-{suffix}"
        assert name == f"model-vllm-qwen3-vl-8b-instruct-{target}"
        assert deployments.split_vllm_component_name(name) == (base, arch)


# ===========================================================================
# 2. validate_greengrass_component_name (2.6)
# ===========================================================================

def test_validate_component_name_accepts_valid_names(stack):
    """Valid names — including every real per-JetPack vLLM name and
    vision-style names — pass without raising.

    # Validates: Requirements 2.6
    """
    module = stack.publish
    valid = [
        "model-vllm-qwen3-vl-8b-instruct-jetson-xavier-jp7",
        "model-vllm-qwen3-vl-8b-instruct-jetson-xavier-jp6",
        "model-defect-classifier-x86-64-cuda",
        "a" * module.GREENGRASS_COMPONENT_NAME_MAX,  # exactly at the limit
        "A.b_c-9",
    ]
    for name in valid:
        assert module.validate_greengrass_component_name(name) is None


def test_validate_component_name_rejects_over_length(stack):
    """A name one character past the Greengrass 128-character limit
    raises PublishError naming the limit.

    # Validates: Requirements 2.6
    """
    module = stack.publish
    over = "m" * (module.GREENGRASS_COMPONENT_NAME_MAX + 1)
    with pytest.raises(module.PublishError) as excinfo:
        module.validate_greengrass_component_name(over)
    assert str(module.GREENGRASS_COMPONENT_NAME_MAX) in str(excinfo.value)


def test_validate_component_name_rejects_illegal_characters(stack):
    """Characters outside `^[a-zA-Z0-9._-]+$` (and the empty name)
    raise PublishError.

    # Validates: Requirements 2.6
    """
    module = stack.publish
    for bad in ("model vllm x", "model/vllm", "model:vllm", "modèl-vllm", ""):
        with pytest.raises(module.PublishError):
            module.validate_greengrass_component_name(bad)


# ===========================================================================
# 3 + 4. Map mirroring and totality — the defect-4 regression tests
# ===========================================================================

def test_target_arch_maps_mirror_packaging_map_exactly(stack):
    """`VLLM_TARGET_TO_ARCH` (greengrass_publish.py) and
    `VLLM_TARGET_SUFFIX_TO_ARCH` (deployments.py) are BOTH the exact
    inverse of `packaging.VLLM_ARCH_TO_TARGET` — totality both ways, so
    neither mirrored map can drift from the packaging source of truth.

    # Validates: Requirements 2.17, 2.19
    """
    packaging_map = stack.packaging.VLLM_ARCH_TO_TARGET
    inverse = {target: arch for arch, target in packaging_map.items()}
    assert len(inverse) == len(packaging_map), (
        "packaging.VLLM_ARCH_TO_TARGET targets must be distinct")
    assert stack.publish.VLLM_TARGET_TO_ARCH == inverse
    assert stack.deployments.VLLM_TARGET_SUFFIX_TO_ARCH == inverse


def test_every_producible_jetson_target_maps_to_aarch64_and_jp_variant(stack):
    """Every target producible from `packaging.VLLM_ARCH_TO_TARGET` is a
    key of BOTH `TARGET_TO_LOCAL_SERVER` and `TARGET_TO_PLATFORM`, maps
    to platform `aarch64`, and resolves to the `arm64JP{N}` LocalServer
    variant whose major matches its architecture. THIS is the test that
    would have caught defect 4 the day `jp7-vllm-enablement` task 6.2
    updated the packaging map without the two publish-side maps.

    # Validates: Requirements 2.17, 2.18
    """
    module = stack.publish
    packaging_map = stack.packaging.VLLM_ARCH_TO_TARGET
    assert packaging_map, "packaging must produce at least one vLLM target"
    for arch, target in sorted(packaging_map.items()):
        match = re.match(r"^arm64_jp(\d+)$", arch)
        assert match, f"unexpected vLLM architecture shape: {arch}"
        jp_major = match.group(1)
        # Totality against BOTH module-level maps (defect 4's gap).
        assert target in module.TARGET_TO_LOCAL_SERVER, (
            f"producible target {target} missing from TARGET_TO_LOCAL_SERVER")
        assert target in module.TARGET_TO_PLATFORM, (
            f"producible target {target} missing from TARGET_TO_PLATFORM")
        # ...and to the matching platform + JetPack-tagged variant.
        assert module.TARGET_TO_PLATFORM[target] == "aarch64"
        expected_variant = f"aws.edgeml.dda.LocalServer.arm64JP{jp_major}"
        assert module.TARGET_TO_LOCAL_SERVER[target] == expected_variant
        assert module.resolve_target_platform(target) == "aarch64"
        assert module.resolve_local_server_component(
            target, "aarch64") == expected_variant
        assert module.resolve_vllm_target_architecture(target) == arch


def test_resolve_local_server_component_jp7(stack):
    """The defect-4 pair directly: `jetson-xavier-jp7` resolves to
    platform `aarch64` and the arm64JP7 LocalServer variant.

    # Validates: Requirements 2.17, 2.18
    """
    module = stack.publish
    assert module.resolve_target_platform("jetson-xavier-jp7") == "aarch64"
    assert module.resolve_local_server_component(
        "jetson-xavier-jp7", "aarch64") == \
        "aws.edgeml.dda.LocalServer.arm64JP7"


# ===========================================================================
# 5. Synthetic producible target absent from either module map (2.19)
# ===========================================================================

def test_synthetic_producible_target_fails_closed_and_never_defaults_amd64(
        stack):
    """A synthetic target injected into `packaging.VLLM_ARCH_TO_TARGET`
    but absent from BOTH `TARGET_TO_LOCAL_SERVER` and `TARGET_TO_PLATFORM`
    makes `resolve_target_platform` — and therefore recipe generation —
    raise `PublishError`, reaches no `create_component_version` call, and
    does NOT default to `amd64`. This is the fail-closed guard that
    replaces the old `TARGET_TO_PLATFORM.get(target, 'amd64')` default.

    # Validates: Requirements 2.19
    """
    module = stack.publish
    synthetic_arch = "arm64_jp99"
    synthetic_target = "jetson-orin-jp99"

    with pytest.MonkeyPatch.context() as mp:
        # Producible from the packaging map, absent from either module map
        # — exactly isBugCondition_4's shape for the NEXT target added.
        mp.setitem(stack.packaging.VLLM_ARCH_TO_TARGET, synthetic_arch,
                   synthetic_target)
        assert synthetic_target in \
            stack.packaging.VLLM_ARCH_TO_TARGET.values()
        assert synthetic_target not in module.TARGET_TO_PLATFORM
        assert synthetic_target not in module.TARGET_TO_LOCAL_SERVER

        # Direct: the resolver raises rather than returning 'amd64'.
        with pytest.raises(module.PublishError) as excinfo:
            module.resolve_target_platform(synthetic_target)
        assert synthetic_target in str(excinfo.value)

        # End to end: a record packaged for the synthetic target reaches
        # no create; the recorded failed entry carries no defaulted
        # platform.
        record = seed_vllm_record(
            stack, f"Synthetic Target {uuid.uuid4().hex[:8]}", archs=(),
            extra_targets=(synthetic_target,))
        gg = FakeGreengrass()
        status, body = run_vllm_publish(stack, record, gg)

    assert status == 502, body
    assert body["failed_step"] == "greengrass_registration"
    assert gg.attempts == [], (
        f"an unmapped target must never reach create_component_version: "
        f"{gg.attempts}")
    (entry,) = body["published_components"]
    assert entry["target"] == synthetic_target
    assert entry["status"] == "failed"
    assert synthetic_target in entry["error"]
    assert entry["platform"] != "amd64", (
        "an unmapped target must NOT default to platform amd64")
    assert entry["platform"] is None


# ===========================================================================
# 6. existing_component_versions / next_vllm_component_version (2.9, 2.10)
# ===========================================================================

def test_existing_component_versions_collects_across_pages(stack):
    """Both listings page: the component ARN is found on a later
    list_components page and the version set is the union over every
    list_component_versions page.

    # Validates: Requirements 2.9
    """
    module = stack.publish
    name = "model-vllm-paged-jetson-xavier-jp7"
    gg = PagedGreengrass(
        component_pages=[["model-vllm-other"], [name]],
        version_pages={name: [["1.0.0", "2.0.0"], ["7.0.0"]]})
    assert module.existing_component_versions(gg, name) == \
        {"1.0.0", "2.0.0", "7.0.0"}


def test_existing_component_versions_no_such_component_is_empty_set(stack):
    """A component with no registered version — the name never appears in
    list_components — yields set(), the first-publish baseline.

    # Validates: Requirements 2.9
    """
    module = stack.publish
    gg = FakeGreengrass(existing={"model-vllm-other": {"3.0.0"}})
    assert module.existing_component_versions(
        gg, "model-vllm-never-published-jetson-xavier-jp6") == set()


def test_existing_component_versions_client_error_warns_and_degrades(
        stack, caplog):
    """A ClientError from either listing step warns and degrades to
    set() — identical degradation to the workflow packager, so a
    transient listing failure never blocks a publish.

    # Validates: Requirements 2.9
    """
    module = stack.publish
    name = "model-vllm-erroring-jetson-xavier-jp6"
    for fail_operation in ("list_components", "list_component_versions"):
        gg = ErroringGreengrass(fail_operation, name)
        with caplog.at_level(logging.WARNING):
            caplog.clear()
            assert module.existing_component_versions(gg, name) == set()
        assert any(record.levelno >= logging.WARNING
                   for record in caplog.records), (
            f"a {fail_operation} ClientError must be logged as a warning")


def test_next_vllm_component_version_is_1_0_0_when_nothing_exists(stack):
    """`1.0.0` when no version exists for any of the names.

    # Validates: Requirements 2.10
    """
    module = stack.publish
    gg = FakeGreengrass()
    names = ["model-vllm-fresh-jetson-xavier-jp6",
             "model-vllm-fresh-jetson-xavier-jp7"]
    assert module.next_vllm_component_version(gg, names) == "1.0.0"
    assert module.next_major_from_versions(set()) == "1.0.0"


def test_next_vllm_component_version_dominates_highest_major_across_names(
        stack):
    """`N+1.0.0` over the HIGHEST major across ALL the per-JetPack names
    being published — one shared version, strictly above everything that
    exists for any of them; unparseable version strings are ignored.

    # Validates: Requirements 2.9, 2.10
    """
    module = stack.publish
    jp6 = "model-vllm-orphaned-jetson-xavier-jp6"
    jp7 = "model-vllm-orphaned-jetson-xavier-jp7"
    gg = FakeGreengrass(existing={
        jp6: {"3.0.0", "1.0.0"},
        jp7: {"7.0.0", "not-a-version"},
        # Versions of a name NOT being published must not participate.
        "model-vllm-unrelated": {"40.0.0"},
    })
    assert module.next_vllm_component_version(gg, [jp6, jp7]) == "8.0.0"
    assert module.next_vllm_component_version(gg, [jp6]) == "4.0.0"


def test_next_vllm_component_version_ignores_record_history(stack):
    """The derivation is a function of the cloud-side state ONLY: the
    fixed `next_vllm_component_version(greengrass, component_names)`
    does not take (and cannot see) the record, so any publish history the
    record carries cannot influence the result — the wedged-retry defect
    (`1.0.0` forever from null history) is structurally impossible.

    # Validates: Requirements 2.9
    """
    module = stack.publish
    name = "model-vllm-history-blind-jetson-xavier-jp7"
    gg = FakeGreengrass(existing={name: {"5.0.0"}})
    # Whatever any record remembers, the same cloud state derives the
    # same version.
    assert module.next_vllm_component_version(gg, [name]) == "6.0.0"
    assert module.next_vllm_component_version(gg, [name]) == "6.0.0"
    # And an empty cloud state derives 1.0.0 even though a history-based
    # derivation over a stale record would have produced something else.
    assert module.next_vllm_component_version(FakeGreengrass(), [name]) == \
        "1.0.0"


# ===========================================================================
# 8. split_vllm_component_name / vllm_component_architectures (2.11, 2.12)
# ===========================================================================

def test_split_vllm_component_name_suffixed_unsuffixed_and_unknown(stack):
    """Suffixed names split into (base, arch); unsuffixed and
    unknown-suffix names return (name, None) — the closed vocabulary
    means a `-jetson-…` fragment outside it is NOT a target suffix.

    # Validates: Requirements 2.11
    """
    split = stack.deployments.split_vllm_component_name
    base = "model-vllm-qwen3-vl-8b-instruct"

    # Every suffix in the closed vocabulary.
    assert split(f"{base}-jetson-xavier-jp5") == (base, "arm64_jp5")
    assert split(f"{base}-jetson-xavier-jp6") == (base, "arm64_jp6")
    assert split(f"{base}-jetson-xavier-jp7") == (base, "arm64_jp7")

    # Unsuffixed legacy name.
    assert split(base) == (base, None)

    # Unknown suffixes: out-of-vocabulary JetPack, non-target fragments.
    assert split(f"{base}-jetson-xavier-jp4") == \
        (f"{base}-jetson-xavier-jp4", None)
    assert split(f"{base}-jetson-nano") == (f"{base}-jetson-nano", None)

    # A name that IS exactly a suffix marker must not split to an empty
    # base.
    assert split("-jetson-xavier-jp7") == ("-jetson-xavier-jp7", None)
    assert split(None) == ("", None)


def test_vllm_component_architectures_per_component_entry_hit(stack):
    """Rule 1: a `published_component.components` entry whose
    `component_name` matches wins, returning that entry's OWN
    `supported_architectures`.

    # Validates: Requirements 2.12
    """
    deployments = stack.deployments
    base = "model-vllm-rules"
    record = {
        "training_id": "t-rules",
        "supported_architectures": ["arm64_jp6", "arm64_jp7"],
        "published_component": {
            "component_name": base,
            "supported_architectures": ["arm64_jp6", "arm64_jp7"],
            "components": [
                {"component_name": f"{base}-jetson-xavier-jp6",
                 "supported_architectures": ["arm64_jp6"]},
                {"component_name": f"{base}-jetson-xavier-jp7",
                 "supported_architectures": ["arm64_jp7"]},
            ],
        },
    }
    assert deployments.vllm_component_architectures(
        record, f"{base}-jetson-xavier-jp6") == ["arm64_jp6"]
    assert deployments.vllm_component_architectures(
        record, f"{base}-jetson-xavier-jp7") == ["arm64_jp7"]


def test_vllm_component_architectures_suffix_fallback(stack):
    """Rule 2: no matching `components` entry (e.g. a record published
    before the fix), but the name carries a known suffix whose arch is in
    the record-wide set -> `[arch]`.

    # Validates: Requirements 2.12
    """
    deployments = stack.deployments
    record = {
        "training_id": "t-fallback",
        "published_component": {
            "component_name": "model-vllm-fallback",
            "supported_architectures": ["arm64_jp6", "arm64_jp7"],
        },
    }
    assert deployments.vllm_component_architectures(
        record, "model-vllm-fallback-jetson-xavier-jp7") == ["arm64_jp7"]


def test_vllm_component_architectures_out_of_set_suffix_fails_closed(stack):
    """Rule 2 fail-closed: a known suffix whose arch is NOT in the
    record-wide set -> `[]` (the gate then rejects every device).

    # Validates: Requirements 2.12
    """
    deployments = stack.deployments
    record = {
        "training_id": "t-out-of-set",
        "published_component": {
            "component_name": "model-vllm-oos",
            "supported_architectures": ["arm64_jp6"],
        },
    }
    assert deployments.vllm_component_architectures(
        record, "model-vllm-oos-jetson-xavier-jp7") == []


def test_vllm_component_architectures_legacy_record_wide_path(stack):
    """Rule 3: no name given, or an unsuffixed legacy name -> the
    record-wide set, from `published_component.supported_architectures`
    or (older shape) the record itself; unresolvable records -> [].

    # Validates: Requirements 2.12
    """
    deployments = stack.deployments
    on_published = {
        "training_id": "t-legacy-1",
        "published_component": {
            "component_name": "model-vllm-legacy",
            "supported_architectures": ["arm64_jp6", "arm64_jp7"],
        },
    }
    on_record = {
        "training_id": "t-legacy-2",
        "supported_architectures": ["arm64_jp6"],
    }
    assert deployments.vllm_component_architectures(on_published) == \
        ["arm64_jp6", "arm64_jp7"]
    assert deployments.vllm_component_architectures(
        on_published, "model-vllm-legacy") == ["arm64_jp6", "arm64_jp7"]
    assert deployments.vllm_component_architectures(on_record) == \
        ["arm64_jp6"]
    assert deployments.vllm_component_architectures(None) == []


# ===========================================================================
# 9. Publish write-back shape and rollback behavior (2.5, 2.8)
# ===========================================================================

def test_publish_writeback_components_list_shape(stack):
    """A successful two-arch vLLM publish writes the extended
    `published_component` map: the top-level `component_name` (and the
    map's own `component_name`) stay the UNSUFFIXED base name, the
    `components` list carries exactly one entry per architecture with
    the documented keys and `supported_architectures == [arch]`, and
    each `published_components` entry carries `[arch]` alongside its
    per-target name.

    # Validates: Requirements 2.5
    """
    module = stack.publish
    archs = ["arm64_jp6", "arm64_jp7"]
    model_name = f"Writeback Shape {uuid.uuid4().hex[:8]}"
    record = seed_vllm_record(stack, model_name, archs)
    base = module.derive_vllm_component_name(model_name)
    gg = FakeGreengrass()

    status, body = run_vllm_publish(stack, record, gg)
    assert status == 200, body

    after = stack.training_jobs.get_item(
        Key={"training_id": record["training_id"]})["Item"]

    # Top-level component_name (the GSI key) is the unsuffixed base name.
    assert after["component_name"] == base
    published = after["published_component"]
    assert published["component_name"] == base
    assert published["component_version"] == body["component_version"]
    assert published["runtime"] == "vllm"
    assert sorted(published["supported_architectures"]) == archs

    # components: one entry per architecture, each with the documented
    # keys and its own single-arch set.
    entries = {entry["architecture"]: entry
               for entry in published["components"]}
    assert sorted(entries) == archs
    assert len(published["components"]) == len(archs)
    for arch in archs:
        target = stack.packaging.VLLM_ARCH_TO_TARGET[arch]
        entry = entries[arch]
        assert entry["component_name"] == f"{base}-{target}"
        assert entry["component_version"] == body["component_version"]
        assert entry["target"] == target
        assert entry["supported_architectures"] == [arch]
        assert entry["component_arn"] == component_arn(
            f"{base}-{target}", body["component_version"])

    # published_components entries carry [arch] alongside the per-target
    # name (2.5).
    for comp in after["published_components"]:
        assert comp["status"] == "published"
        expected_target = stack.packaging.VLLM_ARCH_TO_TARGET[
            comp["supported_architectures"][0]]
        assert comp["target"] == expected_target
        assert comp["component_name"] == f"{base}-{expected_target}"
        assert len(comp["supported_architectures"]) == 1


def test_rollback_attempts_every_created_arn_and_raising_delete_is_swallowed(
        stack):
    """A failed vLLM attempt attempts `delete_component` once per ARN
    created during the attempt, and a RAISING delete does not propagate:
    the handler still returns the retryable 502 whose error is the
    publish failure, not the cleanup failure.

    # Validates: Requirements 2.8
    """
    archs = ["arm64_jp6", "arm64_jp7"]
    model_name = f"Rollback Denied {uuid.uuid4().hex[:8]}"
    record = seed_vllm_record(stack, model_name, archs)
    # Second created component reports FAILED (forcing the atomicity
    # gate) AND every delete raises AccessDeniedException — the pre-fix
    # IAM shape, the worst case the rollback must survive.
    gg = FakeGreengrass(fail_describe_from=1, delete_authorized=False)

    status, body = run_vllm_publish(stack, record, gg)

    # The raising delete did not propagate: the handler returned the
    # publish failure as a response instead of crashing.
    assert status == 502, body
    assert body["failed_step"] == "greengrass_registration"
    assert body["retryable"] is True
    assert "simulated registration failure" in body["error"]
    assert "AccessDenied" not in body["error"], (
        "the reported error must stay the publish failure, not the "
        "cleanup failure")

    # One delete attempted per created ARN, in creation order.
    assert len(gg.created_arns) == len(archs)
    assert gg.deleted == gg.created_arns, (
        f"rollback must attempt every ARN created during the attempt: "
        f"created={gg.created_arns}, deleted={gg.deleted}")

    # And no publish state was written onto the record.
    after = stack.training_jobs.get_item(
        Key={"training_id": record["training_id"]})["Item"]
    assert "published_component" not in after
    assert "published" not in after
    assert "component_name" not in after


def test_rollback_deletes_succeed_when_authorized(stack):
    """The authorized-rollback counterpart: with `delete_component`
    allowed, a failed attempt leaves NO surviving component version in
    the fake's state.

    # Validates: Requirements 2.8
    """
    archs = ["arm64_jp6", "arm64_jp7"]
    model_name = f"Rollback Clean {uuid.uuid4().hex[:8]}"
    record = seed_vllm_record(stack, model_name, archs)
    gg = FakeGreengrass(fail_describe_from=1, delete_authorized=True)

    status, body = run_vllm_publish(stack, record, gg)

    assert status == 502, body
    assert gg.deleted == gg.created_arns
    survivors = {name: sorted(versions)
                 for name, versions in gg.state.items() if versions}
    assert survivors == {}, (
        f"an authorized rollback must erase every created version: "
        f"{survivors}")
