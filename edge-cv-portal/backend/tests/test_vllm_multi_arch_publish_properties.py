# -*- coding: utf-8 -*-
"""Preservation property suite (task 2) for
vllm-multi-arch-publish-conflict.

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
import sys
import uuid
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_FUNCTIONS = os.path.join(_BACKEND, "functions")
_SHARED_LAYER = os.path.join(_BACKEND, "layers", "shared", "python")
_PUBLISH_PATH = os.path.join(_FUNCTIONS, "greengrass_publish.py")
_PACKAGING_PATH = os.path.join(_FUNCTIONS, "packaging.py")

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

def component_arn(name, version):
    return (f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:components:"
            f"{name}:versions:{version}")


class FakeGreengrass:
    """Accepts every create and reports DEPLOYABLE, so the only publish
    outcomes these properties observe come from the handler's own
    decisions (naming, versioning, fail-closed resolution)."""

    def __init__(self):
        self.attempts = []        # (name, version) creates attempted
        self.created = []         # accepted recipes
        self.deleted = []         # rollback attempts

    def create_component_version(self, inlineRecipe, tags=None):
        recipe = json.loads(inlineRecipe)
        name = recipe["ComponentName"]
        version = recipe["ComponentVersion"]
        self.attempts.append((name, version))
        self.created.append(recipe)
        return {"arn": component_arn(name, version)}

    def describe_component(self, arn):
        return {"status": {"componentState": "DEPLOYABLE", "message": ""}}

    def delete_component(self, arn):
        self.deleted.append(arn)

    def recipe_for(self, name):
        for recipe in self.created:
            if recipe["ComponentName"] == name:
                return recipe
        return None


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
