# -*- coding: utf-8 -*-
"""Property suite for vllm-multi-arch-publish-conflict: the task-2
preservation properties (Property 2, written and passing on the UNFIXED
tree) plus the task-4 fix-checking properties (Correctness Properties 1,
3, 4, 5, 6, 7, 8, written against the FIXED tree).

**Property 2: Preservation — non-bug inputs are behaviorally identical.**

Every property here was OBSERVED on the UNFIXED tree first and then
encoded, so the suite PASSES before the fix and must keep passing after
it (tasks 3.9 / 8.x). It pins the five behaviors design.md's Preservation
Requirements enumerate:

1. **Vision publish** (3.1, 3.2) — per-target `f"{name}-{suffix}"`
   component names, the caller-supplied component version, a write-back
   of ONLY `published_components` + `updated_at`, no atomicity gate and
   no rollback, and a fail-closed `PublishError` recorded per
   unresolvable target with no `create_component_version` for it.
2. **Legacy unsuffixed vLLM resolution** (3.4, 3.5) — a `model-vllm-*`
   name carrying no known target suffix resolves to its record with ONE
   GSI query on `component_name-index`, and
   `vllm_component_architectures(record)` returns the record-wide set,
   which keeps every device it was deployable on deployable.
3. **Triton identity** (3.3) — the generated vLLM recipe's Startup and
   Shutdown scripts pass `--model_name = _safe_model_name(model_name)`
   independently of the component name (which the fix suffixes), and
   `derive_vllm_component_name` returns `model-vllm-{safe}` verbatim.
4. **Gate semantics** (3.6, 3.7, 3.8, 3.9) — exact-name matching with no
   fallback, fail closed on a null device architecture and on an empty
   supported set, `arm64_jp4` -> `JP4_UNSUPPORTED` with the JetPack-4
   message, and exactly one entry per (component, device) miss.
5. **Target-map resolution baseline** (3.18, 3.19; design Preservation
   test case 6) — the exact `(LocalServer variant, platform)` pair every
   currently mapped target resolves to today, and a genuinely unknown
   aarch64 target still raising `PublishError`.

Harness: this suite is self-contained so it can run with `--noconftest`
(the run command in the task) — it starts its own module-scoped moto
stack, puts the shared layer and the functions directory on `sys.path`,
and loads `greengrass_publish.py` / `packaging.py` under distinct module
names inside the mock so their module-level boto3 clients bind to the
fake AWS. greengrassv2 (which moto does not implement) is a small fake.

Run:
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \\
      edge-cv-portal/backend/tests/test_vllm_multi_arch_publish_properties.py \\
      -q -p no:cacheprovider --noconftest

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9,
3.18, 3.19**
"""
import importlib.util
import json
import os
import re
import sys
import uuid
from functools import lru_cache
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_FUNCTIONS = os.path.join(_BACKEND, "functions")
_SHARED_LAYER = os.path.join(_BACKEND, "layers", "shared", "python")
_PUBLISH_PATH = os.path.join(_FUNCTIONS, "greengrass_publish.py")
_PACKAGING_PATH = os.path.join(_FUNCTIONS, "packaging.py")
_COMPUTE_STACK_PATH = os.path.abspath(os.path.join(
    _HERE, "..", "..", "infrastructure", "lib", "compute-stack.ts"))

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-vllm-props"
MODELS_TABLE_NAME = "test-models-vllm-props"
USECASES_TABLE_NAME = "test-usecases-vllm-props"
USER_ROLES_TABLE_NAME = "test-user-roles-vllm-props"
AUDIT_LOG_TABLE_NAME = "test-audit-log-vllm-props"
DEVICES_TABLE_NAME = "test-devices-vllm-props"

# Applied by the `stack` fixture BEFORE shared_utils / the handler modules
# are (re-)imported — all of them read table names at module import time —
# and restored on teardown so nothing leaks into other suites.
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

#: The `(LocalServer variant, manifest platform)` pair every currently
#: mapped compile target resolves to TODAY, recorded from the unfixed
#: `greengrass_publish.py` maps (design Preservation test case 6; 3.18).
BASELINE_TARGET_RESOLUTION = {
    "x86_64-cpu": ("aws.edgeml.dda.LocalServer.amd64", "amd64"),
    "x86_64-cuda": ("aws.edgeml.dda.LocalServer.amd64", "amd64"),
    "jetson-xavier": ("aws.edgeml.dda.LocalServer.arm64JP4", "aarch64"),
    "jetson-xavier-jp5": ("aws.edgeml.dda.LocalServer.arm64JP5", "aarch64"),
    "jetson-xavier-jp6": ("aws.edgeml.dda.LocalServer.arm64JP6", "aarch64"),
    "arm64-cpu": ("aws.edgeml.dda.LocalServer.arm64JP4", "aarch64"),
}

#: Every Target_Architecture the deployment gate can see, plus the JP4
#: value that is never in a vLLM supported set (3.8).
ALL_DEVICE_ARCHS = ("arm64_jp4", "arm64_jp5", "arm64_jp6", "arm64_jp7",
                    "x86_64", "x86_64_nvidia")
VLLM_ARCHS = ("arm64_jp5", "arm64_jp6", "arm64_jp7")

JP4_MESSAGE = "JetPack 4 does not support vLLM inference"

#: LocalServer variant every vLLM architecture MUST depend on, and the
#: `(variant, platform)` pair every mapped target resolves to on the FIXED
#: tree (the baseline map plus the jetson-xavier-jp7 entries the fix added;
#: 2.17, 2.18, 3.18) — used by the fix-checking properties (task 4).
LOCAL_SERVER_FOR_ARCH = {
    "arm64_jp5": "aws.edgeml.dda.LocalServer.arm64JP5",
    "arm64_jp6": "aws.edgeml.dda.LocalServer.arm64JP6",
    "arm64_jp7": "aws.edgeml.dda.LocalServer.arm64JP7",
}
FIXED_TARGET_RESOLUTION = dict(
    BASELINE_TARGET_RESOLUTION,
    **{"jetson-xavier-jp7": ("aws.edgeml.dda.LocalServer.arm64JP7",
                             "aarch64")})

#: The JetPack token grammar the frontend's inferComponentTargetArchs
#: matches (Property 7 / Requirement 2.6).
JP_TOKEN_RE = re.compile(r"(?:jp|jetpack)(4|5|6|7)(?![0-9])")

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
# compute-stack.ts source inspection (Property 3, task 4.7) — mirrors
# combined_greengrass_actions() in test_vllm_multi_arch_publish_exploration.py
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def combined_greengrass_statement():
    """(actions, resources) of the combined per-service Greengrass
    statement in compute-stack.ts — the one scoped to the components /
    coreDevices / deployments resource ARNs."""
    with open(_COMPUTE_STACK_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    match = re.search(
        r"actions:\s*\[(?P<actions>[^\]]*)\]\s*,\s*resources:\s*\["
        r"(?P<resources>\s*'arn:aws:greengrass:\*:\*:components:\*'[^\]]*)"
        r"\]",
        source, re.S)
    assert match, (
        "could not locate the combined Greengrass policy statement in "
        f"{_COMPUTE_STACK_PATH}")
    actions = tuple(re.findall(r"'(greengrass:[A-Za-z]+)'",
                               match.group("actions")))
    resources = tuple(re.findall(r"'([^']+)'", match.group("resources")))
    return actions, resources


def iam_grants_delete_component():
    return "greengrass:DeleteComponent" in combined_greengrass_statement()[0]


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

    Reports every accepted create DEPLOYABLE (unless
    ``fail_describe_from`` forces the atomicity gate for Property 3), so
    the only publish outcomes these properties observe come from the
    handler's own decisions (naming, versioning, fail-closed resolution).
    ``create_component_version`` raises ``ConflictException`` on a
    repeated ``(ComponentName, ComponentVersion)`` pair, ``get_paginator``
    serves ``list_components`` / ``list_component_versions`` from its own
    state (so cloud-side version derivation can observe pre-existing
    orphans), and ``delete_component`` succeeds exactly when the portal
    role in compute-stack.ts grants ``greengrass:DeleteComponent`` — the
    fake tracks the real authorization state rather than hard-coding it.
    """

    def __init__(self, existing=None, delete_authorized=None,
                 fail_describe_from=None):
        # component name -> set of registered version strings
        self.state = {name: set(versions)
                      for name, versions in (existing or {}).items()}
        self.attempts = []        # (name, version) creates attempted
        self.created = []         # accepted recipes
        self.created_arns = []    # ARNs of accepted creates, in order
        self.deleted = []         # rollback attempts
        self.paginated = []
        self.delete_authorized = (iam_grants_delete_component()
                                  if delete_authorized is None
                                  else delete_authorized)
        # Index (into created_arns) from which describe_component reports a
        # non-DEPLOYABLE state, forcing the atomicity gate (Property 3).
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

    def surviving_versions(self, names):
        return {name: sorted(self.state.get(name, ()))
                for name in names if self.state.get(name)}


class _CountingTable:
    """Table proxy that records every ``query`` call, so the "one GSI
    query for a legacy unsuffixed name" baseline is observable (3.4)."""

    def __init__(self, table, log):
        self._table = table
        self._log = log

    def query(self, **kwargs):
        self._log.append(kwargs)
        return self._table.query(**kwargs)

    def __getattr__(self, item):
        return getattr(self._table, item)


class _CountingResource:
    def __init__(self, resource, table_name, log):
        self._resource = resource
        self._table_name = table_name
        self._log = log

    def Table(self, name):
        table = self._resource.Table(name)
        if name == self._table_name:
            return _CountingTable(table, self._log)
        return table

    def __getattr__(self, item):
        return getattr(self._resource, item)


# ---------------------------------------------------------------------------
# Module-scoped moto stack (function-scoped fixtures are incompatible with
# Hypothesis, so every per-example seam is monkeypatched inside the tests)
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
        publish = _load_module(_PUBLISH_PATH, "portal_gg_publish_props")
        packaging = _load_module(_PACKAGING_PATH, "portal_packaging_props")
        import deployments

        resource = boto3.resource("dynamodb", region_name=REGION)
        usecase_id = f"uc-{uuid.uuid4()}"
        resource.Table(USECASES_TABLE_NAME).put_item(Item={
            "usecase_id": usecase_id,
            "name": "vLLM Multi-Arch Preservation Use Case",
            "account_id": ACCOUNT_ID,
            "s3_bucket": "test-vllm-props-bucket",
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
# Generators
# ---------------------------------------------------------------------------

_ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"

_slugs = st.text(alphabet=_ALNUM + "-", min_size=1, max_size=18).filter(
    lambda s: not s.startswith("-") and not s.endswith("-") and "--" not in s)

vision_component_names = _slugs.map(lambda s: f"model-{s}")
component_versions = st.integers(min_value=1, max_value=25).map(
    lambda n: f"{n}.0.0")
mapped_target_lists = st.lists(
    st.sampled_from(sorted(BASELINE_TARGET_RESOLUTION)),
    min_size=1, max_size=3, unique=True)
# Never collides with a mapped target, so it is "genuinely unknown" (3.19).
unknown_targets = _slugs.map(lambda s: f"unknown-target-{s}")

model_names = st.text(
    alphabet=_ALNUM + "ABCDEFGHIJKLMNOPQRSTUVWXYZ ._-/",
    min_size=1, max_size=30,
).filter(lambda name: name.strip() != "")

# Legacy vLLM component names: a `model-vllm-*` name carrying NO known
# target suffix, which is exactly the input class 3.4 / 3.5 protect.
_KNOWN_TARGET_SUFFIXES = ("jetson-xavier-jp5", "jetson-xavier-jp6",
                          "jetson-xavier-jp7")
legacy_model_names = model_names.filter(
    lambda name: not any(
        name.lower().replace(" ", "-").endswith(suffix)
        for suffix in _KNOWN_TARGET_SUFFIXES))

vllm_arch_sets = st.lists(st.sampled_from(VLLM_ARCHS), min_size=1,
                          max_size=3, unique=True).map(sorted)


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
            f"s3://test-vllm-props-bucket/model_artifacts/model-abc/"
            f"abc_{target}_greengrass_model_component.zip"),
    }


def seed_vision_record(stack, targets):
    """A packaged VISION (non-vLLM) model record — the input class for
    which none of the four bug conditions can hold (3.1, 3.2)."""
    training_id = str(uuid.uuid4())
    item = {
        "training_id": training_id,
        "usecase_id": stack.usecase_id,
        "model_name": "Defect Classifier",
        "model_type": "classification",
        "source": "sagemaker",
        "status": "Completed",
        "packaged_components": [packaged_entry(t) for t in targets],
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    }
    stack.training_jobs.put_item(Item=item)
    return item


def seed_vllm_record(stack, model_name, archs, extra=None,
                     extra_targets=()):
    """A packaged vLLM_Model_Record as packaging.py leaves it — one
    packaged_components entry per requested architecture (plus any raw
    extra targets), the input class of the fix-checking properties."""
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
    unverifiable weight estimate (fit gate never blocks, 3.4), and the
    fake Greengrass. The caller-supplied name/version in the body are
    required by the shared request shape; the vLLM branch derives its own.
    """
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
    """The expected Per_JetPack_Component name: the base name suffixed
    with the packaging target, exactly the vision convention (2.1)."""
    target = stack.packaging.VLLM_ARCH_TO_TARGET[arch]
    return f"{stack.publish.derive_vllm_component_name(model_name)}-{target}"


def unsupported_target_message(module, target):
    """The exact resolve_target_platform PublishError wording (2.19)."""
    supported = sorted(set(module.TARGET_TO_PLATFORM)
                       & set(module.TARGET_TO_LOCAL_SERVER))
    return (f"Unsupported compile target '{target}': it has no platform "
            f"and LocalServer mapping (TARGET_TO_PLATFORM / "
            f"TARGET_TO_LOCAL_SERVER) (supported: {supported})")


def major(version):
    match = re.match(r"^(\d+)\.", str(version))
    return int(match.group(1)) if match else None


# ===========================================================================
# Property 2a — Vision (non-vLLM) publish is unchanged
# ===========================================================================

@settings(deadline=None)
@given(component_name=vision_component_names,
       component_version=component_versions,
       targets=mapped_target_lists,
       unknown_target=unknown_targets)
def test_preservation_vision_publish_naming_version_writeback_and_fail_closed(
        stack, component_name, component_version, targets, unknown_target):
    """**Property 2: Preservation — vision publish is behaviorally
    identical.**

    For any non-vLLM record and target list, `publish_component` names
    each target's component `f"{component_name}-{target_suffix}"`, uses
    the caller-supplied component version, applies NO atomicity gate and
    NO rollback, writes back ONLY `published_components` + `updated_at`,
    and records a fail-closed `PublishError` for a target whose
    LocalServer variant cannot be resolved without ever attempting a
    create for it.

    The unresolvable target is constructed the only way it is reachable:
    an aarch64-platform target absent from `TARGET_TO_LOCAL_SERVER`
    (an unmapped target alone defaults to platform `amd64`, which the
    resolver satisfies with the single amd64 variant).

    # Validates: Requirements 3.1, 3.2
    """
    module = stack.publish
    all_targets = list(targets) + [unknown_target]
    record = seed_vision_record(stack, all_targets)
    before = stack.training_jobs.get_item(
        Key={"training_id": record["training_id"]})["Item"]
    gg = FakeGreengrass()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(module.time, "sleep", lambda seconds: None)
        mp.setattr(module, "get_usecase_client",
                   lambda service, usecase, **kwargs: gg)
        # Reachable fail-closed input: platform-mapped as aarch64 but with
        # no LocalServer variant (3.2, 3.19).
        mp.setitem(module.TARGET_TO_PLATFORM, unknown_target, "aarch64")
        response = module.publish_component(
            publish_event(record["training_id"], stack.user_id,
                          component_name, component_version), None)

    body = json.loads(response["body"])
    # A failing target does NOT fail a vision publish: no atomicity gate.
    assert response["statusCode"] == 200, body
    assert body["component_name"] == component_name
    assert body["component_version"] == component_version
    assert gg.deleted == [], (
        f"vision publish must never roll back created versions, deleted="
        f"{gg.deleted}")

    entries = {entry["component_name"]: entry
               for entry in body["published_components"]}

    # Per-target naming and the caller-supplied version, for every target.
    for target in all_targets:
        expected_name = f"{component_name}-{target.replace('_', '-')}"
        assert expected_name in entries, (
            f"target {target} must publish as {expected_name}, got "
            f"{sorted(entries)}")
        assert entries[expected_name]["component_version"] == \
            component_version

    # Resolvable targets: created with today's platform + LocalServer.
    for target in targets:
        name = f"{component_name}-{target.replace('_', '-')}"
        local_server, platform = BASELINE_TARGET_RESOLUTION[target]
        assert entries[name]["status"] == "published"
        assert entries[name]["platform"] == platform
        assert (name, component_version) in gg.attempts
        recipe = gg.recipe_for(name)
        assert recipe is not None
        assert recipe["ComponentVersion"] == component_version
        assert recipe["Manifests"][0]["Platform"]["architecture"] == platform
        assert local_server in recipe["ComponentDependencies"]
        assert recipe["ComponentDependencies"][local_server][
            "DependencyType"] == "HARD"

    # The unresolvable target fails closed with NO create attempted.
    failed_name = f"{component_name}-{unknown_target.replace('_', '-')}"
    assert entries[failed_name]["status"] == "failed"
    assert "LocalServer" in entries[failed_name]["error"]
    assert failed_name not in {name for name, _ in gg.attempts}, (
        f"a target with no resolvable LocalServer variant must never reach "
        f"create_component_version: {gg.attempts}")

    # Write-back shape: published_components + updated_at, nothing else.
    after = stack.training_jobs.get_item(
        Key={"training_id": record["training_id"]})["Item"]
    assert set(after) - set(before) == {"published_components"}, (
        f"vision write-back added {sorted(set(after) - set(before))}; only "
        f"published_components may be added")
    assert "published_component" not in after
    assert "published" not in after
    assert "component_name" not in after
    assert int(after["updated_at"]) >= int(before["updated_at"])
    for key in set(before) - {"updated_at", "published_components"}:
        assert after[key] == before[key], (
            f"vision write-back must not touch {key}")


# ===========================================================================
# Property 2b — Legacy unsuffixed vLLM resolution is unchanged
# ===========================================================================

@settings(deadline=None)
@given(model_name=legacy_model_names, archs=vllm_arch_sets,
       on_published_component=st.booleans())
def test_preservation_legacy_unsuffixed_vllm_resolution_and_record_wide_set(
        stack, model_name, archs, on_published_component):
    """**Property 2: Preservation — legacy unsuffixed vLLM components
    resolve exactly as before.**

    For any `model-vllm-*` name carrying no known target suffix,
    `load_vllm_model_record(name)` resolves to its record with a SINGLE
    query against the `component_name-index` GSI keyed on the exact name,
    `vllm_component_architectures(record)` returns the record-wide
    supported set, and the gate stays green for every device in that set
    (so an existing deployment remains revisable).

    # Validates: Requirements 3.4, 3.5
    """
    deployments = stack.deployments
    # The GSI key is the component name, and the moto table persists across
    # examples: a per-example token keeps each seeded record independently
    # resolvable (two records sharing one component name is the
    # newest-created_at tiebreak's input, not this property's).
    record_model_name = f"{model_name} {uuid.uuid4().hex[:8]}"
    component_name = stack.publish.derive_vllm_component_name(
        record_model_name)
    training_id = f"legacy-{uuid.uuid4()}"
    # Input-class precondition: the name carries NO known target suffix.
    assert not any(component_name.endswith(suffix)
                   for suffix in _KNOWN_TARGET_SUFFIXES)
    published_component = {
        "component_name": component_name,
        "component_version": "1.0.0",
        "runtime": "vllm",
    }
    item = {
        "training_id": training_id,
        "usecase_id": stack.usecase_id,
        "model_name": record_model_name,
        "model_type": "vllm",
        "source": "vllm",
        "status": "published",
        "created_at": 1_700_000_000_001,
        "component_name": component_name,
        "published_component": published_component,
    }
    # The record-wide set lives on published_component for records the
    # current publish wrote, and on the record itself for the older
    # shape; both are the legacy "record-wide" carrier (3.4).
    if on_published_component:
        published_component["supported_architectures"] = list(archs)
    else:
        item["supported_architectures"] = list(archs)
    stack.training_jobs.put_item(Item=item)

    queries = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(deployments, "dynamodb",
                   _CountingResource(stack.resource,
                                     TRAINING_JOBS_TABLE_NAME, queries))
        record = deployments.load_vllm_model_record(component_name)

    assert record is not None, (
        f"legacy unsuffixed name {component_name} must resolve to its record")
    assert record["training_id"] == training_id
    assert len(queries) == 1, (
        f"a legacy unsuffixed name must resolve in ONE GSI query, got "
        f"{len(queries)}")
    assert queries[0]["IndexName"] == COMPONENT_NAME_INDEX

    assert deployments.vllm_component_architectures(record) == list(archs), (
        "a legacy unsuffixed component must resolve to the record-wide "
        "supported set")

    # 3.5: still deployable on every device of that recorded set.
    manifests = {component_name: {"version": "1.0.0",
                                  "architectures": list(archs)}}
    for arch in archs:
        assert deployments.evaluate_vllm_arch_gate(
            manifests, {"thing-1": arch}) == []


# ===========================================================================
# Property 2c — Triton device-side model identity is unchanged
# ===========================================================================

@settings(deadline=None)
@given(model_name=model_names, arch=st.sampled_from(VLLM_ARCHS),
       suffix_component_name=st.booleans())
def test_preservation_triton_model_identity_and_base_component_name(
        stack, model_name, arch, suffix_component_name):
    """**Property 2: Preservation — the Triton model identity is
    untouched by the component rename.**

    For any model name, `derive_vllm_component_name` returns
    `model-vllm-{safe_model_name}` verbatim, and the generated recipe's
    Startup and Shutdown scripts pass
    `--model_name = _safe_model_name(model_name)` regardless of the
    component name they are generated for (the fix suffixes that name).

    # Validates: Requirement 3.3
    """
    module = stack.publish
    safe = module._safe_model_name(model_name)
    base_name = module.derive_vllm_component_name(model_name)
    assert base_name == f"model-vllm-{safe}", (
        f"derive_vllm_component_name must stay model-vllm-{{safe}}: "
        f"{base_name}")

    target = stack.packaging.VLLM_ARCH_TO_TARGET[arch]
    # Both the base name and the per-target suffixed name must leave the
    # device-side --model_name alone.
    component_name = (f"{base_name}-{target}" if suffix_component_name
                      else base_name)
    recipe = module.generate_vllm_component_recipe(
        component_name=component_name,
        component_version="1.0.0",
        friendly_name=model_name,
        # jp7 is unmapped on the unfixed tree, so the platform the publish
        # path derives is used rather than a hardcoded one.
        platform=module.TARGET_TO_PLATFORM.get(target, "amd64"),
        artifact_s3_uri="s3://test-vllm-props-bucket/repo.zip",
        repo_unarchived_path="repo",
        model_name=safe,
        target=target,
        supported_architectures=[arch],
    )

    lifecycle = recipe["Manifests"][0]["Lifecycle"]
    for phase in ("Startup", "Shutdown"):
        script = lifecycle[phase]["Script"]
        assert f"--model_name {safe} --component_name" in script, (
            f"{phase} must pass the sanitized model name {safe!r}: {script}")
    assert recipe["ComponentName"] == component_name


# ===========================================================================
# Property 2d — Architecture gate semantics are unchanged
# ===========================================================================

@settings(deadline=None)
@given(component_archs=st.lists(st.sampled_from(VLLM_ARCHS), min_size=1,
                                max_size=3, unique=True),
       device_archs=st.lists(st.sampled_from(ALL_DEVICE_ARCHS + (None,)),
                             min_size=1, max_size=4, unique=True))
def test_preservation_arch_gate_exact_fail_closed_and_one_entry_per_miss(
        stack, component_archs, device_archs):
    """**Property 2: Preservation — the architecture gate keeps its exact,
    fail-closed semantics.**

    Over (component arch, device arch) pairs: matching is by exact name
    with no cross-architecture fallback, a null device architecture and an
    empty supported set both fail closed for every device, `arm64_jp4`
    carries reason `JP4_UNSUPPORTED` (with the JetPack-4 message
    constant), and the gate returns exactly one entry per
    (component, device) miss.

    # Validates: Requirements 3.6, 3.7, 3.8, 3.9
    """
    deployments = stack.deployments
    assert deployments.VLLM_JP4_UNSUPPORTED_MESSAGE == JP4_MESSAGE
    assert deployments.VLLM_GATE_REASON_JP4 == "JP4_UNSUPPORTED"
    assert deployments.VLLM_GATE_REASON_ARCH == "ARCH_UNSUPPORTED"

    devices = {f"thing-{index}": arch
               for index, arch in enumerate(device_archs)}

    # One manifest per component architecture — exact-name matching with
    # no fallback means a device matches iff its arch IS the supported one.
    manifests = {f"model-vllm-m{index}": {"version": "1.0.0",
                                          "architectures": [arch]}
                 for index, arch in enumerate(component_archs)}
    findings = deployments.evaluate_vllm_arch_gate(manifests, devices)

    misses = [(name, device)
              for name, manifest in manifests.items()
              for device, arch in devices.items()
              if arch not in manifest["architectures"]]
    assert len(findings) == len(misses), (
        f"expected one entry per (component, device) miss ({len(misses)}), "
        f"got {findings}")
    assert {(f["component"], f["device"]) for f in findings} == set(misses)
    for finding in findings:
        device_arch = devices[finding["device"]]
        assert finding["deviceArch"] == device_arch
        assert finding["supported"] == sorted(
            manifests[finding["component"]]["architectures"])
        expected_reason = ("JP4_UNSUPPORTED" if device_arch == "arm64_jp4"
                           else "ARCH_UNSUPPORTED")
        assert finding["reason"] == expected_reason
        # arm64_jp4 never appears in a vLLM supported set (3.8).
        assert "arm64_jp4" not in finding["supported"]

    # Fail closed on a null device architecture (3.6).
    for name, manifest in manifests.items():
        assert deployments.evaluate_vllm_arch_gate(
            {name: manifest}, {"unknown-device": None}) != []

    # Fail closed on an empty supported set, for every device (3.7).
    empty = {"model-vllm-unresolvable": {"version": "1.0.0",
                                         "architectures": []}}
    empty_findings = deployments.evaluate_vllm_arch_gate(empty, devices)
    assert len(empty_findings) == len(devices)
    assert all(f["supported"] == [] for f in empty_findings)


# ===========================================================================
# Property 2e — Target-map resolution baseline (design Preservation case 6)
# ===========================================================================

@settings(deadline=None)
@given(target=st.sampled_from(sorted(BASELINE_TARGET_RESOLUTION)),
       unknown_target=unknown_targets)
def test_preservation_mapped_targets_resolve_as_today_and_unknown_fails_closed(
        stack, target, unknown_target):
    """**Property 2: Preservation — every already-mapped target resolves
    to exactly today's LocalServer variant and platform, and a genuinely
    unknown aarch64 target still fails closed.**

    # Validates: Requirements 3.18, 3.19
    """
    module = stack.publish
    local_server, platform = BASELINE_TARGET_RESOLUTION[target]

    assert module.TARGET_TO_LOCAL_SERVER[target] == local_server
    assert module.TARGET_TO_PLATFORM[target] == platform
    assert module.resolve_local_server_component(target, platform) == \
        local_server

    # The baseline is a subset assertion: the fix may ADD jetson-xavier-jp7
    # to both maps, but may not change or drop any existing entry.
    assert set(BASELINE_TARGET_RESOLUTION) <= set(module.TARGET_TO_LOCAL_SERVER)
    assert set(BASELINE_TARGET_RESOLUTION) <= set(module.TARGET_TO_PLATFORM)

    assert unknown_target not in module.TARGET_TO_LOCAL_SERVER
    with pytest.raises(module.PublishError):
        module.resolve_local_server_component(unknown_target, "aarch64")


# ===========================================================================
# Property 8 — Target maps are total and unmapped targets fail closed (4.1)
# ===========================================================================

@settings(deadline=None)
@given(arch=st.sampled_from(VLLM_ARCHS), unknown_target=unknown_targets)
def test_property8_target_maps_total_and_unmapped_targets_fail_closed(
        stack, arch, unknown_target):
    """**Property 8: Fix Checking — target maps are total and unmapped
    targets fail closed.**

    Over the full set of producible targets plus generated unmapped
    target names: every value of `packaging.VLLM_ARCH_TO_TARGET` is a key
    of BOTH `TARGET_TO_LOCAL_SERVER` and `TARGET_TO_PLATFORM` (totality in
    both directions); each generated recipe's manifest platform and HARD
    LocalServer dependency correspond to the same architecture (`aarch64`
    + `arm64JP{N}` for every Jetson target); every already-mapped target
    resolves to exactly today's variant and platform; and any target
    absent from either map raises `PublishError` with no
    `create_component_version` call — the platform never defaults to
    `amd64`.

    # Validates: Requirements 2.17, 2.18, 2.19, 3.18, 3.19
    """
    module = stack.publish
    packaging = stack.packaging

    # Totality in both directions against the packaging map (2.17).
    producible = set(packaging.VLLM_ARCH_TO_TARGET.values())
    for target in producible:
        assert target in module.TARGET_TO_LOCAL_SERVER, (
            f"producible vLLM target {target} absent from "
            f"TARGET_TO_LOCAL_SERVER ({sorted(module.TARGET_TO_LOCAL_SERVER)})")
        assert target in module.TARGET_TO_PLATFORM, (
            f"producible vLLM target {target} absent from "
            f"TARGET_TO_PLATFORM ({sorted(module.TARGET_TO_PLATFORM)})")
    assert set(module.VLLM_TARGET_TO_ARCH) == producible, (
        "VLLM_TARGET_TO_ARCH must mirror packaging.VLLM_ARCH_TO_TARGET")

    # Platform and LocalServer dependency always correspond to the SAME
    # architecture: aarch64 + arm64JP{N} for every Jetson target (2.18).
    for producible_target in producible:
        target_arch = module.VLLM_TARGET_TO_ARCH[producible_target]
        assert module.resolve_target_platform(producible_target) == "aarch64"
        assert module.resolve_local_server_component(
            producible_target, "aarch64") == LOCAL_SERVER_FOR_ARCH[target_arch]

    # The generated recipe carries that correspondence end to end.
    target = packaging.VLLM_ARCH_TO_TARGET[arch]
    recipe = module.generate_vllm_component_recipe(
        component_name=f"model-vllm-prop8-{target}",
        component_version="1.0.0",
        friendly_name="Prop8 Model",
        platform=module.resolve_target_platform(target),
        artifact_s3_uri="s3://test-vllm-props-bucket/repo.zip",
        repo_unarchived_path="repo",
        model_name="prop8-model",
        target=target,
        supported_architectures=[arch],
    )
    assert recipe["Manifests"][0]["Platform"] == {
        "os": "linux", "architecture": "aarch64"}
    local_server = LOCAL_SERVER_FOR_ARCH[arch]
    assert local_server in recipe["ComponentDependencies"], (
        f"{target} recipe depends on "
        f"{sorted(recipe['ComponentDependencies'])} instead of {local_server}")
    assert recipe["ComponentDependencies"][local_server][
        "DependencyType"] == "HARD"

    # Every already-mapped target resolves to exactly today's variant and
    # platform (3.18) — the baseline map plus the JP7 entries the fix added.
    for mapped_target, (variant, platform) in FIXED_TARGET_RESOLUTION.items():
        assert module.resolve_target_platform(mapped_target) == platform
        assert module.resolve_local_server_component(
            mapped_target, platform) == variant

    # An unmapped target fails closed with the exact resolver wording and
    # NEVER defaults to amd64 (2.19, 3.19).
    assert unknown_target not in module.TARGET_TO_PLATFORM
    assert unknown_target not in module.TARGET_TO_LOCAL_SERVER
    expected_message = unsupported_target_message(module, unknown_target)
    with pytest.raises(module.PublishError) as excinfo:
        module.resolve_target_platform(unknown_target)
    assert str(excinfo.value) == expected_message

    # End to end: a vLLM record packaged for an unmapped target reaches no
    # create_component_version call — the target is a recorded failed
    # target (and the vLLM atomicity gate fails the publish).
    record = seed_vllm_record(
        stack, f"Prop8 Unmapped {uuid.uuid4().hex[:8]}", archs=(),
        extra_targets=(unknown_target,))
    gg = FakeGreengrass()
    status, body = run_vllm_publish(stack, record, gg)

    assert status == 502, body
    assert body["failed_step"] == "greengrass_registration"
    assert gg.attempts == [], (
        f"an unmapped target must never reach create_component_version: "
        f"{gg.attempts}")
    (entry,) = body["published_components"]
    assert entry["target"] == unknown_target
    assert entry["status"] == "failed"
    assert entry["error"] == expected_message
    assert entry["platform"] is None, (
        f"unmapped target must not default to a platform, got "
        f"{entry['platform']!r}")


# ===========================================================================
# Property 1 — One create per distinct per-JetPack identity (4.2)
# ===========================================================================

@settings(deadline=None)
@given(model_name=model_names, archs=vllm_arch_sets)
def test_property1_one_create_per_distinct_per_jetpack_identity(
        stack, model_name, archs):
    """**Property 1: Fix Checking — one create per distinct per-JetPack
    identity.**

    For any vLLM record and any supported-architecture subset, the fixed
    `publish_component` issues exactly one `create_component_version` per
    architecture, all `(component_name, component_version)` pairs are
    distinct, the call for architecture `a` carries name
    `derive_vllm_component_name(record) + "-" + suffix(a)`, a recipe
    whose `DefaultConfiguration.supported_architectures` is exactly
    `[a]`, a HARD `ComponentDependencies` entry on `a`'s LocalServer
    variant, and manifest platform `linux/aarch64`.

    # Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.17, 2.18
    """
    record_model_name = f"{model_name} {uuid.uuid4().hex[:8]}"
    record = seed_vllm_record(stack, record_model_name, archs)
    gg = FakeGreengrass()

    status, body = run_vllm_publish(stack, record, gg)

    assert status == 200, (
        f"vLLM publish failed: {body.get('error')} "
        f"(failed_step={body.get('failed_step')}); attempts={gg.attempts}")

    # Exactly one create per supported architecture, all identities
    # distinct (2.1, 2.2).
    assert len(gg.attempts) == len(archs), (
        f"expected {len(archs)} create_component_version calls (one per "
        f"architecture), got {gg.attempts}")
    assert len(set(gg.attempts)) == len(gg.attempts), (
        f"duplicate (ComponentName, ComponentVersion) identity requested: "
        f"{gg.attempts}")

    expected_names = {per_jetpack_name(stack, record_model_name, arch)
                      for arch in archs}
    assert {name for name, _ in gg.attempts} == expected_names, (
        f"per-JetPack names must be base + '-' + target suffix: "
        f"{sorted(name for name, _ in gg.attempts)} != "
        f"{sorted(expected_names)}")

    # Each per-JetPack component advertises exactly its own architecture
    # and depends HARD on that architecture's LocalServer variant, with
    # manifest platform linux/aarch64 (2.3, 2.4, 2.17, 2.18).
    for arch in archs:
        name = per_jetpack_name(stack, record_model_name, arch)
        recipe = gg.recipe_for(name)
        assert recipe is not None, f"no recipe created for {name}"
        default_config = recipe["ComponentConfiguration"][
            "DefaultConfiguration"]
        assert default_config["supported_architectures"] == [arch], (
            f"{name} must advertise exactly [{arch}], got "
            f"{default_config['supported_architectures']}")
        local_server = LOCAL_SERVER_FOR_ARCH[arch]
        assert local_server in recipe["ComponentDependencies"], (
            f"{name} must depend on {local_server}, got "
            f"{sorted(recipe['ComponentDependencies'])}")
        assert recipe["ComponentDependencies"][local_server][
            "DependencyType"] == "HARD"
        assert recipe["Manifests"][0]["Platform"] == {
            "os": "linux", "architecture": "aarch64"}


# ===========================================================================
# Property 4 — Derived version dominates every existing version (4.3)
# ===========================================================================

_existing_version_strings = st.lists(
    st.one_of(
        st.tuples(st.integers(min_value=0, max_value=30),
                  st.integers(min_value=0, max_value=9),
                  st.integers(min_value=0, max_value=9)).map(
            lambda t: f"{t[0]}.{t[1]}.{t[2]}"),
        # Unparseable entries must be ignored, never crash (2.10).
        st.sampled_from(["not-a-version", "v2", ""]),
    ),
    max_size=4, unique=True)

# Arbitrary record publish histories the derivation must IGNORE (2.9): the
# shapes a previous successful publish (or older code) could have left.
_record_histories = st.one_of(
    st.none(),
    st.integers(min_value=1, max_value=40).map(lambda n: {
        "published_component": {
            "component_name": "model-vllm-stale",
            "component_version": f"{n}.0.0",
            "runtime": "vllm",
        },
        "published_components": [{
            "component_name": "model-vllm-stale-jetson-xavier-jp6",
            "component_version": f"{n}.0.0",
            "status": "published",
        }],
    }),
)


@settings(deadline=None)
@given(model_name=model_names, archs=vllm_arch_sets,
       per_name_versions=st.lists(_existing_version_strings, min_size=3,
                                  max_size=3),
       base_versions=_existing_version_strings,
       history=_record_histories)
def test_property4_derived_version_dominates_every_existing_version(
        stack, model_name, archs, per_name_versions, base_versions, history):
    """**Property 4: Fix Checking — the derived version dominates every
    existing version.**

    For any set of versions existing cloud-side for the per-JetPack names
    being published and any record history, the derived version matches
    `^\\d+\\.0\\.0$`, its major is strictly greater than every existing
    major, and the result is a function of the cloud-side state ONLY —
    independent of the record's `published_components` /
    `published_component` history.

    # Validates: Requirements 2.9, 2.10
    """
    module = stack.publish
    record_model_name = f"{model_name} {uuid.uuid4().hex[:8]}"
    record = seed_vllm_record(stack, record_model_name, archs,
                              extra=history)

    # Orphans left by earlier attempts: versions on the per-JetPack names
    # (which the derivation must dominate) and on the unsuffixed base name
    # (a legacy orphan, not among the names being published).
    names = [per_jetpack_name(stack, record_model_name, arch)
             for arch in archs]
    existing = {name: set(versions)
                for name, versions in zip(names, per_name_versions)
                if versions}
    base = module.derive_vllm_component_name(record_model_name)
    if base_versions:
        existing[base] = set(base_versions)
    gg = FakeGreengrass(existing=existing)

    status, body = run_vllm_publish(stack, record, gg)

    assert status == 200, (
        f"publish wedged by existing cloud-side versions: "
        f"{body.get('error')}; existing={existing}")
    derived = body["component_version"]
    assert re.match(r"^\d+\.0\.0$", derived), derived

    # Strict domination over every parseable existing major of every name
    # this publish registers (2.9).
    for name in names:
        for version in existing.get(name, ()):
            existing_major = major(version)
            if existing_major is not None:
                assert major(derived) > existing_major, (
                    f"derived {derived} does not dominate existing "
                    f"{version} of {name}")

    # The result is fully determined by the cloud-side state of the names
    # being published — so it is independent of the generated record
    # history (2.9) and of the base-name orphan.
    cloud_versions = set()
    for name in names:
        cloud_versions |= {version for version in existing.get(name, ())}
    assert derived == module.next_major_from_versions(cloud_versions), (
        f"derived {derived} is not the pure cloud-side derivation "
        f"{module.next_major_from_versions(cloud_versions)} over "
        f"{sorted(cloud_versions)} (history={history})")


# ===========================================================================
# Property 5 — Suffixed names round-trip to record and architecture (4.4)
# ===========================================================================

@settings(deadline=None)
@given(model_name=model_names, archs=vllm_arch_sets)
def test_property5_suffixed_names_round_trip_to_record_and_architecture(
        stack, model_name, archs):
    """**Property 5: Fix Checking — suffixed names round-trip to record
    and architecture.**

    For any published vLLM record and any supported architecture `a`, the
    per-JetPack name `derive_vllm_component_name(record) + "-" +
    suffix(a)` resolves through `load_vllm_model_record` (moto-backed
    `component_name-index` GSI) back to that record, and
    `vllm_component_architectures(record, name)` returns exactly `[a]` —
    matching the write-back the publish produced for that component.

    # Validates: Requirements 2.5, 2.11, 2.12
    """
    deployments = stack.deployments
    record_model_name = f"{model_name} {uuid.uuid4().hex[:8]}"
    record = seed_vllm_record(stack, record_model_name, archs)
    gg = FakeGreengrass()

    status, body = run_vllm_publish(stack, record, gg)
    assert status == 200, body

    base = stack.publish.derive_vllm_component_name(record_model_name)
    for arch in archs:
        name = per_jetpack_name(stack, record_model_name, arch)
        target = stack.packaging.VLLM_ARCH_TO_TARGET[arch]

        # The suffixed name resolves back to THE record through the GSI
        # (the top-level component_name stays the unsuffixed base, 2.11).
        resolved = deployments.load_vllm_model_record(name)
        assert resolved is not None, (
            f"per-JetPack name {name} did not resolve to its record")
        assert resolved["training_id"] == record["training_id"]
        assert resolved["component_name"] == base

        # ...and to exactly its OWN architecture (2.12)...
        assert deployments.vllm_component_architectures(
            resolved, name) == [arch], (
            f"{name} must resolve to exactly [{arch}], got "
            f"{deployments.vllm_component_architectures(resolved, name)}")

        # ...matching the write-back the publish produced (2.5).
        entries = [entry for entry
                   in resolved["published_component"]["components"]
                   if entry["component_name"] == name]
        assert len(entries) == 1, (
            f"expected one components entry for {name}, got "
            f"{resolved['published_component']['components']}")
        entry = entries[0]
        assert entry["supported_architectures"] == [arch]
        assert entry["architecture"] == arch
        assert entry["target"] == target
        assert entry["component_version"] == body["component_version"]

    # The record-wide legacy keys are all retained around the new list
    # (2.5): base name, shared version, record-wide union.
    published = deployments.load_vllm_model_record(base)
    assert published is not None
    assert published["training_id"] == record["training_id"]
    assert len(published["published_component"]["components"]) == len(archs)


# ===========================================================================
# Property 6 — The per-JetPack arch gate is exact and fail-closed (4.5)
# ===========================================================================

@settings(deadline=None)
@given(component_arch=st.sampled_from(VLLM_ARCHS),
       device_arch=st.sampled_from(ALL_DEVICE_ARCHS + (None,)))
def test_property6_per_jetpack_arch_gate_is_exact_and_fail_closed(
        stack, component_arch, device_arch):
    """**Property 6: Fix Checking — the per-JetPack arch gate is exact
    and fail-closed.**

    For any per-JetPack component whose supported set is `[a]` and any
    device architecture `b` (including None): no findings when `b == a`,
    at least one finding when `b != a` (with reason `JP4_UNSUPPORTED` and
    the JetPack-4 message for `arm64_jp4`), at least one finding on a
    null device architecture, and at least one finding on an empty
    supported set.

    # Validates: Requirements 2.13, 2.14, 3.6, 3.7, 3.8, 3.9
    """
    deployments = stack.deployments
    target = stack.packaging.VLLM_ARCH_TO_TARGET[component_arch]
    name = f"model-vllm-prop6-{target}"
    manifests = {name: {"version": "1.0.0",
                        "architectures": [component_arch]}}
    devices = {"thing-prop6": device_arch}

    findings = deployments.evaluate_vllm_arch_gate(manifests, devices)

    if device_arch == component_arch:
        # Exact match: the per-JetPack component deploys to its own
        # architecture with zero findings (2.13).
        assert findings == [], (
            f"gate must pass when device arch {device_arch} == component "
            f"arch {component_arch}: {findings}")
    else:
        # Any other architecture — the OTHER JetPack included — misses
        # (2.14); a null device arch fails closed (3.6).
        assert len(findings) >= 1, (
            f"gate must fail for device arch {device_arch!r} against "
            f"supported [{component_arch}]")
        finding = findings[0]
        assert finding["component"] == name
        assert finding["deviceArch"] == device_arch
        assert finding["supported"] == [component_arch]
        if device_arch == "arm64_jp4":
            # JetPack 4: reason JP4_UNSUPPORTED with the JetPack-4
            # message constant (3.8).
            assert finding["reason"] == deployments.VLLM_GATE_REASON_JP4
            assert deployments.VLLM_JP4_UNSUPPORTED_MESSAGE == JP4_MESSAGE
        else:
            assert finding["reason"] == deployments.VLLM_GATE_REASON_ARCH

    # A null device architecture yields at least one finding whatever the
    # component's architecture (3.6).
    assert deployments.evaluate_vllm_arch_gate(
        manifests, {"thing-null": None}) != []

    # An empty supported set yields at least one finding for every device
    # architecture (3.7).
    empty = {name: {"version": "1.0.0", "architectures": []}}
    empty_findings = deployments.evaluate_vllm_arch_gate(empty, devices)
    assert len(empty_findings) >= 1, (
        f"gate must fail closed on an empty supported set for device "
        f"arch {device_arch!r}")
    assert empty_findings[0]["supported"] == []


# ===========================================================================
# Property 7 — Derived names satisfy every downstream consumer (4.6)
# ===========================================================================

# Long enough that base + '-' + suffix crosses the 128-character Greengrass
# limit on some examples, so both the in-range invariants and the fail-closed
# branch are exercised (2.6).
arbitrary_model_names = st.text(
    alphabet=_ALNUM + "ABCDEFGHIJKLMNOPQRSTUVWXYZ ._-/",
    min_size=1, max_size=160,
).filter(lambda name: name.strip() != "")


@settings(deadline=None)
@given(model_name=arbitrary_model_names, arch=st.sampled_from(VLLM_ARCHS))
def test_property7_derived_names_satisfy_every_downstream_consumer(
        stack, model_name, arch):
    """**Property 7: Fix Checking — derived names satisfy every
    downstream consumer.**

    For any model name and any supported architecture, the derived
    per-JetPack component name starts with `model-` (backend publish
    validation) and `model-vllm-` (`isVllmModelComponent` /
    `VLLM_MODEL_COMPONENT_PREFIX`), contains a JetPack token matching
    `/(?:jp|jetpack)(4|5|6|7)(?![0-9])/` whose major equals that
    architecture's major (`inferComponentTargetArchs`), and matches the
    Greengrass charset `^[a-zA-Z0-9._-]+$`; a name above the
    128-character limit fails closed with `PublishError` and NO
    `create_component_version` call.

    # Validates: Requirements 2.6
    """
    module = stack.publish
    target = stack.packaging.VLLM_ARCH_TO_TARGET[arch]
    base = module.derive_vllm_component_name(model_name)
    name = f"{base}-{target}"

    # Backend publish validation and the frontend vLLM discriminator.
    assert name.startswith("model-")
    assert name.startswith("model-vllm-")
    assert module.validate_component_name(name)

    # Greengrass component-name charset.
    assert module.GREENGRASS_COMPONENT_NAME_RE.match(name), (
        f"derived name contains characters outside the Greengrass "
        f"charset: {name!r}")

    # The JetPack token the frontend's inferComponentTargetArchs matches
    # names this architecture's major.
    arch_major = int(arch.rsplit("jp", 1)[-1])
    token_majors = {int(m) for m in JP_TOKEN_RE.findall(name.lower())}
    assert arch_major in token_majors, (
        f"{name} carries no JetPack token for major {arch_major} "
        f"(tokens: {sorted(token_majors)})")
    # The trailing target suffix is the discriminating token.
    assert name.endswith(f"-{target}")

    if len(name) <= module.GREENGRASS_COMPONENT_NAME_MAX:
        # In-range names pass validation untouched.
        module.validate_greengrass_component_name(name)
    else:
        # Above 128 characters the publish fails closed with PublishError
        # and never reaches create_component_version (2.6).
        with pytest.raises(module.PublishError):
            module.validate_greengrass_component_name(name)

        record = seed_vllm_record(stack, model_name, (arch,))
        gg = FakeGreengrass()
        status, body = run_vllm_publish(stack, record, gg)

        assert status == 502, body
        assert body["failed_step"] == "greengrass_registration"
        assert gg.attempts == [], (
            f"an over-long component name must never reach "
            f"create_component_version: {gg.attempts}")
        (entry,) = body["published_components"]
        assert entry["status"] == "failed"
        assert str(module.GREENGRASS_COMPONENT_NAME_MAX) in entry["error"]


# ===========================================================================
# Property 3 — Rollback is authorized and attempted for every ARN (4.7)
# ===========================================================================

@settings(deadline=None)
@given(model_name=model_names, archs=vllm_arch_sets,
       fail_index=st.integers(min_value=0, max_value=2))
def test_property3_rollback_is_authorized_and_attempted_for_every_arn(
        stack, model_name, archs, fail_index):
    """**Property 3: Fix Checking — rollback is authorized and attempted
    for every ARN.**

    The portal Lambda policy grants `greengrass:DeleteComponent` on the
    Greengrass components resource ARN and on nothing wider; for any
    failed vLLM attempt that created >= 1 component version, a
    `delete_component` call is attempted for every ARN created during the
    attempt, the rollback raises nothing (the reported error stays the
    publish failure), and — with the grant in place — no version
    survives.

    # Validates: Requirements 2.7, 2.8, 3.13, 3.15
    """
    # Source-level policy check (mirrors the exploration suite's
    # combined_greengrass_actions): the action is in the combined
    # statement, scoped to exactly the three Greengrass resource ARNs the
    # portal operates on — never a wildcard, nothing wider (2.7, 3.15).
    actions, resources = combined_greengrass_statement()
    assert "greengrass:DeleteComponent" in actions, (
        f"greengrass:DeleteComponent is not granted on the portal Lambda "
        f"role's combined Greengrass statement (actions: {list(actions)})")
    assert "arn:aws:greengrass:*:*:components:*" in resources
    assert set(resources) == {
        "arn:aws:greengrass:*:*:components:*",
        "arn:aws:greengrass:*:*:coreDevices:*",
        "arn:aws:greengrass:*:*:deployments:*",
    }, (f"the DeleteComponent grant must stay confined to the Greengrass "
        f"components / coreDevices / deployments resource ARNs, got "
        f"{list(resources)}")
    assert "*" not in resources

    # Behavioral check: force the atomicity gate partway through the
    # attempt (the component at fail_index never becomes DEPLOYABLE), so
    # >= 1 version is created and >= 1 target fails — isBugCondition_2's
    # input class.
    record_model_name = f"{model_name} {uuid.uuid4().hex[:8]}"
    record = seed_vllm_record(stack, record_model_name, archs)
    gg = FakeGreengrass(fail_describe_from=fail_index % len(archs))

    status, body = run_vllm_publish(stack, record, gg)

    # The reported error stays the publish failure — the rollback raised
    # nothing into the response (3.13).
    assert status == 502, body
    assert body["failed_step"] == "greengrass_registration"
    assert body["error"].startswith("vLLM component publish failed"), (
        body["error"])
    assert "simulated registration failure" in body["error"]
    assert body.get("retryable") is True

    # A delete is attempted for every ARN created during the attempt
    # (2.8)...
    assert gg.created_arns, "the attempt must have created >= 1 version"
    for arn in gg.created_arns:
        assert arn in gg.deleted, (
            f"no delete_component attempted for {arn} (deleted: "
            f"{gg.deleted})")

    # ...and, with the grant in place, no component version survives the
    # failed attempt (the fake authorizes deletes exactly when
    # compute-stack.ts grants the action).
    assert gg.delete_authorized, (
        "the fake tracked compute-stack.ts as NOT granting "
        "greengrass:DeleteComponent")
    survivors = gg.surviving_versions({name for name, _ in gg.attempts})
    assert survivors == {}, (
        f"component versions survived the failed attempt (orphans): "
        f"{survivors}")
