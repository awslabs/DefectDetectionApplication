# -*- coding: utf-8 -*-
"""Bug-condition exploration suite (task 1) for
vllm-multi-arch-publish-conflict.

**Property 1: Bug Condition — one create per distinct per-JetPack identity.**

Every test here asserts the FIXED expected behavior, so on the UNFIXED
tree they are EXPECTED TO FAIL (case 5 is the single exception: it
documents `F(X)`, the currently-absent IAM grant, and PASSES until task
3.7 inverts it). Each failure is the counterexample confirming one of the
four hypothesized causes:

- **Case 1 — duplicate identity** (`isBugCondition_1`): `publish_component`
  keeps ONE component name and ONE component version for the whole target
  loop of a vLLM record, so publishing a record packaged for both
  `jetson-xavier-jp6` and `jetson-xavier-jp7` issues the identical
  `(ComponentName, ComponentVersion)` twice and the second create raises
  `ConflictException` -> HTTP 502, `failed_step: greengrass_registration`
  (1.1, 1.2, 1.3).
- **Case 2 — rollback denied** (`isBugCondition_2`): the atomicity gate
  calls `delete_component` for every version it created but catches and
  only warns on failure, and the portal Lambda role does not grant
  `greengrass:DeleteComponent`, so the first target's version survives as
  a permanent orphan (1.4, 1.5).
- **Case 3 — wedged retry** (`isBugCondition_3`):
  `next_vllm_component_version()` reads only the record's own publish
  history — which a failed attempt deliberately leaves null — so it
  re-derives `1.0.0` and conflicts with the orphan on the FIRST target
  (1.6, 1.7, 1.9).
- **Case 4 — JP7 recipe mis-stamping** (`isBugCondition_4`):
  `jetson-xavier-jp7` is a value of `packaging.VLLM_ARCH_TO_TARGET` but a
  key of neither `TARGET_TO_LOCAL_SERVER` nor `TARGET_TO_PLATFORM`, so
  `TARGET_TO_PLATFORM.get(target, 'amd64')` stamps an aarch64 Thor
  component `amd64` and `resolve_local_server_component` hands back the
  amd64 LocalServer instead of failing closed. The counterexample is the
  mis-stamped recipe, not an exception — the publish SUCCEEDS
  (1.10, 1.11, 1.12, 1.13, 1.14).
- **Case 5 — IAM grant**: source-level assertion that
  `greengrass:DeleteComponent` is present in the combined Greengrass
  statement in `edge-cv-portal/infrastructure/lib/compute-stack.ts`.
  On the unfixed tree this documented F(X) (the action was ABSENT, so the
  rollback was always denied); task 3.7 added the grant and inverted the
  assertion, and task 4.7 encodes it as Property 3.
- **Case 6 — edge case**: a base name whose per-target suffixed form
  exceeds the Greengrass 128-character component-name limit must fail
  closed with a `PublishError` and no create; today the create is issued
  anyway.

Harness: the moto-backed `aws_stack` fixture from conftest plus a
training-jobs and models table, following
`test_vllm_publish_fit_gate.py` / `test_vllm_publish_writeback.py`.
greengrassv2 (which moto does not implement) is a fake client that
behaves like the service: `create_component_version` raises
`ConflictException` on a repeated `(ComponentName, ComponentVersion)`,
`get_paginator` serves `list_components` / `list_component_versions` from
its own state, and `delete_component` raises `AccessDeniedException`
exactly when `compute-stack.ts` does not grant
`greengrass:DeleteComponent` — so case 2 tracks the real authorization
state of the portal role rather than hard-coding it.

The weight-estimation seam is patched to return None (`fit_check.status
== 'unverified'`), which never blocks a publish (3.4), so these tests
exercise the registration path only.

Run (this suite needs the tests-directory conftest for the moto stack, so
it is run WITHOUT `--noconftest`):
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
      edge-cv-portal/backend/tests/test_vllm_multi_arch_publish_exploration.py \
      -q -p no:cacheprovider

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.9, 1.10,
1.11, 1.12, 1.13, 1.14**
"""
import importlib.util
import json
import os
import re
import sys
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-vllm-multi-arch"
MODELS_TABLE_NAME = "test-models-vllm-multi-arch"

ACCOUNT_ID = "123456789012"

_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS = os.path.abspath(os.path.join(_HERE, "..", "functions"))
_PUBLISH_PATH = os.path.join(_FUNCTIONS, "greengrass_publish.py")
_PACKAGING_PATH = os.path.join(_FUNCTIONS, "packaging.py")
_COMPUTE_STACK_PATH = os.path.abspath(os.path.join(
    _HERE, "..", "..", "infrastructure", "lib", "compute-stack.ts"))

#: LocalServer variant every vLLM architecture MUST depend on (2.4, 2.18).
LOCAL_SERVER_FOR_ARCH = {
    "arm64_jp5": "aws.edgeml.dda.LocalServer.arm64JP5",
    "arm64_jp6": "aws.edgeml.dda.LocalServer.arm64JP6",
    "arm64_jp7": "aws.edgeml.dda.LocalServer.arm64JP7",
}
#: Every Jetson vLLM target is aarch64 (2.17, 2.18).
PLATFORM_FOR_ARCH = {arch: "aarch64" for arch in LOCAL_SERVER_FOR_ARCH}

GREENGRASS_COMPONENT_NAME_MAX = 128


def _load_module(path, name):
    """Load a functions/*.py module under a distinct module name (inside
    the moto mock, so module-level boto3 clients bind to the fake AWS)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# compute-stack.ts source inspection (cases 2 and 5)
# ---------------------------------------------------------------------------

def combined_greengrass_actions():
    """The action list of the combined per-service Greengrass statement —
    the one scoped to the components / coreDevices / deployments resource
    ARNs in compute-stack.ts."""
    with open(_COMPUTE_STACK_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    match = re.search(
        r"actions:\s*\[(?P<actions>[^\]]*)\]\s*,\s*resources:\s*\[\s*"
        r"'arn:aws:greengrass:\*:\*:components:\*'",
        source, re.S)
    assert match, (
        "could not locate the combined Greengrass policy statement in "
        f"{_COMPUTE_STACK_PATH}")
    return re.findall(r"'(greengrass:[A-Za-z]+)'", match.group("actions"))


def iam_grants_delete_component():
    return "greengrass:DeleteComponent" in combined_greengrass_actions()


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
        self.fake.paginated.append((self.operation, kwargs))
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
    """Behaves like the greengrassv2 service for the publish path.

    - ``create_component_version`` raises ``ConflictException`` when the
      ``(ComponentName, ComponentVersion)`` pair already exists, with the
      service's own message.
    - ``delete_component`` raises ``AccessDeniedException`` unless the
      portal role actually grants ``greengrass:DeleteComponent``
      (read from compute-stack.ts), mirroring the unauthorized rollback.
    - ``get_paginator`` serves ``list_components`` /
      ``list_component_versions`` from its own state, so cloud-side
      version derivation can observe pre-existing orphans.
    """

    def __init__(self, existing=None, delete_authorized=None,
                 fail_describe_from=None):
        # component name -> set of registered version strings
        self.state = {name: set(versions)
                      for name, versions in (existing or {}).items()}
        self.attempts = []        # every (name, version) create attempted
        self.created = []         # parsed recipes that were accepted
        self.created_arns = []    # ARNs of accepted creates, in order
        self.delete_attempts = []
        self.paginated = []
        self.delete_authorized = (iam_grants_delete_component()
                                  if delete_authorized is None
                                  else delete_authorized)
        # Index (into created_arns) from which describe_component reports a
        # non-DEPLOYABLE state, used to force the atomicity gate in case 2.
        self.fail_describe_from = fail_describe_from

    # -- service surface -------------------------------------------------
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
                   and self.created_arns.index(arn) >= self.fail_describe_from)
        if failing:
            return {"status": {"componentState": "FAILED",
                               "message": "simulated registration failure"}}
        return {"status": {"componentState": "DEPLOYABLE", "message": ""}}

    def delete_component(self, arn):
        self.delete_attempts.append(arn)
        if not self.delete_authorized:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException",
                           "Message": (
                               "User is not authorized to perform: "
                               "greengrass:DeleteComponent")}},
                "DeleteComponent")
        name = name_from_arn(arn)
        self.state.get(name, set()).discard(version_from_arn(arn))

    def get_paginator(self, operation):
        return _FakePaginator(self, operation)

    # -- assertions helpers ----------------------------------------------
    def surviving_versions(self, names):
        return {name: sorted(self.state.get(name, ()))
                for name in names if self.state.get(name)}


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pub_env(aws_stack):
    """Training-jobs + models tables, the real greengrass_publish module,
    and packaging (for VLLM_ARCH_TO_TARGET) loaded inside the mock."""
    import boto3

    mp = pytest.MonkeyPatch()
    mp.setenv("TRAINING_JOBS_TABLE", TRAINING_JOBS_TABLE_NAME)
    mp.setenv("MODELS_TABLE", MODELS_TABLE_NAME)

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=MODELS_TABLE_NAME,
        KeySchema=[{"AttributeName": "model_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "model_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    module = _load_module(_PUBLISH_PATH,
                          "portal_gg_publish_multi_arch_exploration")
    packaging = _load_module(_PACKAGING_PATH,
                             "portal_packaging_multi_arch_exploration")

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=module,
        packaging=packaging,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
        models=resource.Table(MODELS_TABLE_NAME),
        usecases=aws_stack.tables.usecases,
        user_roles=aws_stack.tables.user_roles,
    )
    mp.undo()


@pytest.fixture
def seeded(pub_env, monkeypatch):
    """Fresh Use_Case + DataScientist, no polling sleeps, and an
    unverifiable weight estimate so the fit gate never blocks (3.4)."""
    monkeypatch.setattr(pub_env.module.time, "sleep", lambda s: None)
    monkeypatch.setattr(pub_env.module, "estimate_weights",
                        lambda record, s3_head=None, hf_fetch=None: None)
    usecase_id = f"uc-{uuid.uuid4()}"
    pub_env.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "vLLM Multi-Arch Publish Use Case",
        "account_id": ACCOUNT_ID,
        "s3_bucket": "test-vllm-usecase-bucket",
    })
    user_id = f"user-{uuid.uuid4()}"
    pub_env.user_roles.put_item(Item={
        "user_id": user_id,
        "usecase_id": usecase_id,
        "role": "DataScientist",
    })
    return SimpleNamespace(usecase_id=usecase_id, user_id=user_id)


def use_greengrass(pub_env, monkeypatch, gg):
    monkeypatch.setattr(pub_env.module, "get_usecase_client",
                        lambda service, usecase, **kw: gg)
    return gg


def packaged_entry(target, arch):
    return {
        "target": target,
        "status": "packaged",
        "component_package_s3": (
            f"s3://test-vllm-usecase-bucket/model_artifacts/model-abc/"
            f"abc_{target}_greengrass_model_component.zip"),
        "supported_architectures": [arch],
    }


def seed_vllm_record(pub_env, seeded, model_name="Qwen3-VL-8B-Instruct",
                     archs=("arm64_jp6", "arm64_jp7")):
    """A packaged vLLM_Model_Record as packaging.py leaves it, with one
    packaged_components entry per requested architecture."""
    arch_to_target = pub_env.packaging.VLLM_ARCH_TO_TARGET
    training_id = str(uuid.uuid4())
    item = {
        "training_id": training_id,
        "usecase_id": seeded.usecase_id,
        "model_name": model_name,
        "model_type": "vllm",
        "source": "vllm",
        "status": "Completed",
        "publish_eligible": True,
        "model_source": {"huggingface_model_id": "Qwen/Qwen3-VL-8B-Instruct"},
        "packaged_components": [
            packaged_entry(arch_to_target[arch], arch) for arch in archs
        ],
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    }
    pub_env.training_jobs.put_item(Item=item)
    return item


def publish_event(training_id, user_id):
    return {
        "httpMethod": "POST",
        "path": f"/api/v1/training/{training_id}/publish",
        "pathParameters": {"id": training_id},
        "body": json.dumps({
            # Required by the shared request shape; the vLLM branch derives
            # the convention name/version itself.
            "component_name": "model-caller-chosen",
            "component_version": "9.0.0",
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


def publish(pub_env, seeded, record):
    response = pub_env.module.publish_component(
        publish_event(record["training_id"], seeded.user_id), None)
    return response["statusCode"], json.loads(response["body"])


def base_component_name(pub_env, model_name):
    return pub_env.module.derive_vllm_component_name(model_name)


def per_jetpack_name(pub_env, model_name, arch):
    """The expected Per_JetPack_Component name: the base name suffixed with
    the packaging target, exactly the vision convention (2.1)."""
    target = pub_env.packaging.VLLM_ARCH_TO_TARGET[arch]
    return f"{base_component_name(pub_env, model_name)}-{target}"


def major(version):
    match = re.match(r"^(\d+)\.", str(version))
    assert match, f"not an N.x.y version: {version!r}"
    return int(match.group(1))


def recipe_for(gg, component_name):
    for recipe in gg.created:
        if recipe["ComponentName"] == component_name:
            return recipe
    return None


# ---------------------------------------------------------------------------
# Case 1 — duplicate component identity (isBugCondition_1)
# ---------------------------------------------------------------------------

def test_case_1_one_create_per_distinct_per_jetpack_identity(
        pub_env, seeded, monkeypatch):
    """isBugCondition_1 holds (a vLLM record with |archs| > 1), so the
    publish must issue exactly one create per architecture with distinct
    (name, version) identities and per-JetPack names.

    Counterexample on the unfixed tree: two creates carrying the identical
    `model-vllm-qwen3-vl-8b-instruct:1.0.0`, the second raising
    ConflictException -> 502 / failed_step greengrass_registration.

    **Validates: Requirements 1.1, 1.2, 1.3 (expected behavior 2.1-2.4)**
    """
    record = seed_vllm_record(pub_env, seeded)
    gg = use_greengrass(pub_env, monkeypatch, FakeGreengrass())
    archs = pub_env.module.vllm_supported_architectures()
    assert len(archs) > 1, "isBugCondition_1 requires more than one arch"

    status, body = publish(pub_env, seeded, record)

    assert status == 200, (
        f"vLLM publish failed: {body.get('error')} "
        f"(failed_step={body.get('failed_step')}); create attempts="
        f"{gg.attempts}")

    # Exactly one create per supported architecture, all identities distinct.
    assert len(gg.attempts) == len(archs), (
        f"expected {len(archs)} create_component_version calls (one per "
        f"architecture), got {gg.attempts}")
    assert len(set(gg.attempts)) == len(gg.attempts), (
        f"duplicate (ComponentName, ComponentVersion) identity requested: "
        f"{gg.attempts}")

    expected_names = {per_jetpack_name(pub_env, record["model_name"], arch)
                      for arch in archs}
    assert {name for name, _ in gg.attempts} == expected_names

    # Each per-JetPack component advertises exactly its own architecture and
    # depends HARD on that architecture's LocalServer variant.
    for arch in archs:
        name = per_jetpack_name(pub_env, record["model_name"], arch)
        recipe = recipe_for(gg, name)
        assert recipe is not None, f"no recipe created for {name}"
        default_config = recipe["ComponentConfiguration"][
            "DefaultConfiguration"]
        assert default_config["supported_architectures"] == [arch]
        local_server = LOCAL_SERVER_FOR_ARCH[arch]
        assert local_server in recipe["ComponentDependencies"], (
            f"{name} must depend on {local_server}, got "
            f"{sorted(recipe['ComponentDependencies'])}")
        assert recipe["ComponentDependencies"][local_server][
            "DependencyType"] == "HARD"
        assert recipe["Manifests"][0]["Platform"]["architecture"] == \
            PLATFORM_FOR_ARCH[arch]


# ---------------------------------------------------------------------------
# Case 2 — rollback denied, orphan survives (isBugCondition_2)
# ---------------------------------------------------------------------------

def test_case_2_failed_attempt_leaves_no_surviving_component_version(
        pub_env, seeded, monkeypatch):
    """isBugCondition_2 holds (>= 1 version created, >= 1 target failed):
    the rollback must actually remove every version the attempt created.

    Counterexample on the unfixed tree: delete_component IS attempted, the
    AccessDeniedException (the portal role has no greengrass:DeleteComponent)
    is swallowed as a warning, and the JP6 version survives in the fake's
    state as a permanent orphan.

    **Validates: Requirements 1.4, 1.5 (expected behavior 2.7, 2.8)**
    """
    record = seed_vllm_record(pub_env, seeded)
    # Force the atomicity gate: the SECOND accepted component version never
    # becomes DEPLOYABLE. On the unfixed tree the second create conflicts
    # before that, which trips the same gate with the first ARN created.
    gg = use_greengrass(pub_env, monkeypatch,
                        FakeGreengrass(fail_describe_from=1))

    status, body = publish(pub_env, seeded, record)

    # The attempt fails either way — that is the bug condition, not the bug.
    assert status == 502, body
    assert body["failed_step"] == "greengrass_registration"
    assert gg.created_arns, "bug condition requires >= 1 created version"

    # Rollback must be attempted for every ARN created during the attempt...
    for arn in gg.created_arns:
        assert arn in gg.delete_attempts, (
            f"no delete_component attempted for {arn}")

    # ...and must leave nothing behind.
    survivors = gg.surviving_versions(
        {name for name, _ in gg.attempts})
    assert survivors == {}, (
        f"component versions survived the failed attempt (orphans): "
        f"{survivors}; delete_authorized={gg.delete_authorized}")


# ---------------------------------------------------------------------------
# Case 3 — wedged retry (isBugCondition_3)
# ---------------------------------------------------------------------------

def test_case_3_retry_derives_version_above_every_existing_version(
        pub_env, seeded, monkeypatch):
    """isBugCondition_3 holds (a cloud-side version the record's null
    history cannot see): the derived version must be strictly above every
    version that actually exists in Greengrass for the names published.

    Counterexample on the unfixed tree: next_vllm_component_version() reads
    only the (null) record history and re-derives 1.0.0, which conflicts
    with the orphan on the FIRST target -> 502.

    **Validates: Requirements 1.6, 1.7, 1.9 (expected behavior 2.9, 2.10)**
    """
    record = seed_vllm_record(pub_env, seeded)
    archs = pub_env.module.vllm_supported_architectures()
    base = base_component_name(pub_env, record["model_name"])
    # Orphans left by an earlier failed attempt whose rollback was denied:
    # the legacy unsuffixed name AND both per-JetPack names at 1.0.0.
    existing = {base: {"1.0.0"}}
    for arch in archs:
        existing[per_jetpack_name(pub_env, record["model_name"], arch)] = \
            {"1.0.0"}
    gg = use_greengrass(pub_env, monkeypatch,
                        FakeGreengrass(existing=existing))
    # The record itself has no publish history (a failed attempt writes none).
    assert "published_component" not in record
    assert "published_components" not in record

    status, body = publish(pub_env, seeded, record)

    assert status == 200, (
        f"retry still wedged: {body.get('error')}; create attempts="
        f"{gg.attempts}")
    derived = body["component_version"]
    assert re.match(r"^\d+\.0\.0$", derived), derived
    for name, _ in gg.attempts:
        for version in existing.get(name, ()):
            assert major(derived) > major(version), (
                f"derived {derived} does not dominate existing {version} "
                f"of {name}")


# ---------------------------------------------------------------------------
# Case 4 — JP7 target unmapped, recipe mis-stamped (isBugCondition_4)
# ---------------------------------------------------------------------------

def test_case_4_jp7_target_is_mapped_in_both_module_maps(pub_env):
    """isBugCondition_4 must NOT hold for any producible vLLM target: every
    value of packaging.VLLM_ARCH_TO_TARGET must be a key of BOTH
    TARGET_TO_LOCAL_SERVER and TARGET_TO_PLATFORM.

    Counterexample on the unfixed tree: `jetson-xavier-jp7` is in
    values(packaging.VLLM_ARCH_TO_TARGET) but in NEITHER map, so
    isBugCondition_4('jetson-xavier-jp7') holds and
    TARGET_TO_PLATFORM.get(target, 'amd64') stamps it amd64.

    **Validates: Requirements 1.10, 1.11 (expected behavior 2.17, 2.19)**
    """
    module = pub_env.module
    producible = set(pub_env.packaging.VLLM_ARCH_TO_TARGET.values())
    assert "jetson-xavier-jp7" in producible

    def is_bug_condition_4(target):
        return (target in producible
                and (target not in module.TARGET_TO_LOCAL_SERVER
                     or target not in module.TARGET_TO_PLATFORM))

    unmapped = sorted(t for t in producible if is_bug_condition_4(t))
    assert unmapped == [], (
        f"isBugCondition_4 holds for {unmapped}: producible vLLM target(s) "
        f"absent from TARGET_TO_LOCAL_SERVER "
        f"({sorted(module.TARGET_TO_LOCAL_SERVER)}) or TARGET_TO_PLATFORM "
        f"({sorted(module.TARGET_TO_PLATFORM)})")


def test_case_4_jp7_recipe_is_stamped_aarch64_with_arm64jp7_dependency(
        pub_env, seeded, monkeypatch):
    """The JP7 component's recipe must carry manifest platform aarch64 and a
    HARD dependency on aws.edgeml.dda.LocalServer.arm64JP7.

    Counterexample on the unfixed tree: the publish SUCCEEDS (nothing
    surfaces the error) and the recipe is stamped `amd64` with a HARD
    dependency on aws.edgeml.dda.LocalServer.amd64 — an aarch64 Thor device
    receives a component built for the wrong architecture pointing at the
    wrong LocalServer variant.

    **Validates: Requirements 1.12, 1.13, 1.14 (expected behavior 2.18)**
    """
    # JP7 alone, so the duplicate-identity defect cannot mask the stamping:
    # the single create succeeds on the unfixed tree too.
    record = seed_vllm_record(pub_env, seeded, archs=("arm64_jp7",))
    gg = use_greengrass(pub_env, monkeypatch, FakeGreengrass())

    status, body = publish(pub_env, seeded, record)
    assert status == 200, body
    assert len(gg.created) == 1, gg.attempts

    recipe = gg.created[0]
    assert recipe["Manifests"][0]["Platform"]["architecture"] == "aarch64", (
        f"JP7 recipe mis-stamped: "
        f"{recipe['Manifests'][0]['Platform']} for component "
        f"{recipe['ComponentName']}")
    local_server = LOCAL_SERVER_FOR_ARCH["arm64_jp7"]
    assert local_server in recipe["ComponentDependencies"], (
        f"JP7 recipe depends on {sorted(recipe['ComponentDependencies'])} "
        f"instead of {local_server}")
    assert recipe["ComponentDependencies"][local_server][
        "DependencyType"] == "HARD"


# ---------------------------------------------------------------------------
# Case 5 — IAM grant (inverted by task 3.7; asserts the action IS present)
# ---------------------------------------------------------------------------

def test_case_5_iam_policy_grants_delete_component():
    """The portal Lambda role's combined Greengrass statement grants
    `greengrass:DeleteComponent`, so the atomicity rollback in
    `greengrass_publish.py` can actually erase the component versions the
    failed publish created (2.7).

    On the unfixed tree this case documented F(X) — the action was absent
    and every rollback was denied (1.4). Task 3.7 added the grant to the
    EXISTING combined statement (no new statement, no resource change), so
    the assertion is inverted here; task 4.7 encodes it as Property 3.

    The grant stays scoped to the Greengrass components / coreDevices /
    deployments resource ARNs — never a wildcard resource (3.15).

    **Validates: Requirements 2.7, 2.8, 3.15**
    """
    actions = combined_greengrass_actions()
    assert "greengrass:CreateComponentVersion" in actions, actions
    assert "greengrass:DeleteComponent" in actions, (
        "greengrass:DeleteComponent is not granted on the portal Lambda "
        "role's combined Greengrass statement — the atomicity rollback "
        f"will be denied (actions: {actions})")


# ---------------------------------------------------------------------------
# Case 6 — edge case: over-long per-JetPack component name
# ---------------------------------------------------------------------------

def test_case_6_overlong_component_name_fails_closed_without_create(
        pub_env, seeded, monkeypatch):
    """A base name whose per-target suffixed form exceeds the Greengrass
    128-character component-name limit must fail closed (PublishError
    recorded as a failed target, vLLM 502) with NO create attempted.

    Counterexample on the unfixed tree: create_component_version is called
    anyway with an invalid name.

    **Validates: Requirement 1.3 edge case (expected behavior 2.6)**
    """
    target = pub_env.packaging.VLLM_ARCH_TO_TARGET["arm64_jp6"]
    suffix_len = len(target) + 1
    # Base name inside the limit, suffixed name outside it.
    model_name = ("Long-Model-Name" * 12)[:GREENGRASS_COMPONENT_NAME_MAX
                                          - len("model-vllm-") - 1]
    record = seed_vllm_record(pub_env, seeded, model_name=model_name,
                              archs=("arm64_jp6",))
    base = base_component_name(pub_env, model_name)
    assert len(base) <= GREENGRASS_COMPONENT_NAME_MAX
    assert len(base) + suffix_len > GREENGRASS_COMPONENT_NAME_MAX, (
        f"test setup: {len(base)} + {suffix_len} must exceed "
        f"{GREENGRASS_COMPONENT_NAME_MAX}")
    gg = use_greengrass(pub_env, monkeypatch, FakeGreengrass())

    status, body = publish(pub_env, seeded, record)

    assert gg.attempts == [], (
        f"create_component_version called with an over-long name: "
        f"{[(name, len(name)) for name, _ in gg.attempts]}")
    assert status == 502, body
    assert body["failed_step"] == "greengrass_registration"
    errors = " ".join(str(component.get("error", ""))
                      for component in body.get("published_components", []))
    assert re.search(r"128|length|too long|character", errors, re.I), (
        f"failure does not explain the component-name limit: {errors!r}")
