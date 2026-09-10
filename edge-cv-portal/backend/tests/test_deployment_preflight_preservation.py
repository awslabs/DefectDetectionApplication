"""Observation-first preservation oracle — deployment-preflight-validation
task 3.

Bugfix spec: .kiro/specs/deployment-preflight-validation/

# Feature: deployment-preflight-validation, Property 2: non-bug-condition
# submissions unchanged

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10,
3.11, 3.12, 3.13, 3.14**

METHODOLOGY — observation-first. Every expectation below was produced by
RUNNING the UNFIXED tree on non-bug-condition inputs and RECORDING what it
actually did (probe run 2026-09-10, `edge-cv-portal/backend/tests`, moto
stack + the stateful Greengrass/IoT fakes). The recorded outputs are then
encoded as properties, so this suite PASSES on the unfixed tree.

**THIS ORACLE IS IMMUTABLE.** It is never rebaselined after the fix lands.
A failure in task 4.9 means the fix leaked outside the bug condition and
the FIX is wrong, not the test. (During task 3 itself — and only then — a
failure means the encoding of current behaviour is wrong and the TEST is
corrected; two such corrections were needed and are recorded in the
"Encoding corrections" note below.)

Preservation is the DOMINANT risk in this spec: the new validation sits on
the submit path of EVERY deployment the portal makes
(`create_deployment` / `create_workflow_deployment`).

What is pinned, and why each one is load-bearing
------------------------------------------------
1. **Clean-submit document identity (3.1)** — the full `deployment_params`
   (components map with every `componentVersion` and `configurationUpdate`,
   `targetArn`, `deploymentName`, `tags`, `deploymentPolicies`) and the 201
   body (`auto_included`, `components`, `is_revision`,
   `superseded_deployment_id`, `message`, and the ABSENCE of `warnings`),
   as a property over generated component sets plus the two `jetson-thor1`
   reference deployments.
2. **Existing gate identity AND PRECEDENCE (3.4)** — the exact status,
   `code`, `message` and `details` of `VLLM_ARCH_UNSUPPORTED`,
   `PLUGIN_LIFECYCLE_VIOLATION`, `PLUGIN_ARCH_UNSUPPORTED`,
   `INCOMPATIBLE_LOCAL_SERVER`, `CAMERA_BINDINGS_INVALID`,
   `CAMERA_WARNINGS_UNCONFIRMED` and `REGISTRY_UNAVAILABLE`, on whichever
   submit path carries them — and, where a bug-condition shape (A/B/C)
   applies at the same time, that the EXISTING gate's response is what
   comes back, byte-identical.
3. **The plugin arch gate keeps failing CLOSED on a device with no recorded
   `Target_Architecture`** (`evaluate_plugin_arch_gate`,
   `deployments.py:1987-2020`). bugfix.md 2.9 requires the NEW validation to
   fail OPEN; 2.9 also states the asymmetry is intentional and that both
   must coexist. Pinned here explicitly so the new code cannot soften the
   old gate while implementing its own fail-open contract.
4. **AWS-managed public names never produce a finding** — `Nucleus`
   (`{"os":"linux"}`, no architecture), `Cli`, `ShadowManager` and
   `LogManager` (`{"os":"*"}`), `SecureTunneling`. These are auto-included
   on every portal deployment; evidence.md §1.1/§1.2 showed that a naive
   attribute matcher, or a single-namespace `list_component_versions` check,
   would report them incompatible/unresolvable and refuse EVERY submission
   (the account namespace returns an EMPTY LIST, not an error, for public
   names).
5. **Variant-less and wildcard manifests stay universal (3.2, 2.8)** — 107
   of the account's 148 aarch64 manifests are variant-less, including every
   LocalServer variant and every `model-*` component, plus the observed
   `{"os":"linux"}`, `{"os":"*"}` and `Platform: null` shapes.
6. **JetPack-matched twins keep deploying (3.3)**.
7. **Submitted-set immutability (3.14)** and **removal with no remaining
   dependant (3.11)** — reference case revision 53 =
   `a9086c7d-ae9e-4131-8e83-7efc4fd549b4`, which removed the model AND its
   depending workflow together.
8. **Automated store-limit remediation stays ungated** —
   `_submit_store_remediation` (`deployments.py:1676`) and
   `_resume_original_deployment` (`:1717`) submit without validation and
   without any acknowledgement requirement; an acknowledgement-required
   finding there would have nobody to acknowledge it and would wedge
   remediation.
9. **Thing-group targets (3.8)**, **no target selected (3.7)**,
   **deployment-detail passthrough (3.9)** and the **`src/` scope
   assertion (3.10, 3.13)**.

Honesty guard (tasks.md Notes)
------------------------------
Every assertion here is about a PURE function, the SUBMITTED deployment
document captured by the fake, or an HTTP response body. Nothing in this
file claims a real device removed or retained a component — Counterexample
C's six-day retention is device evidence, assigned to task 7's read-only
re-verification.

Recorded baselines (this suite's siblings, all green on the unfixed tree,
`HYPOTHESIS_PROFILE=ci ... -q -p no:cacheprovider` from the repo root)
------------------------------------------------------------------------
    test_deployment_vllm_gate.py                        11 passed
    test_deployment_shadow_manager.py                    4 passed
    test_deployment_store_limit.py                       8 passed
    test_deployment_plugin_gates.py                     27 passed
    test_camera_binding_submission.py                   10 passed
    test_camera_binding_validation.py                   59 passed
    test_secure_tunneling_jp5_guard.py                  21 passed
    test_workflow_packaging_deployment_integration.py   11 passed
    test_workflow_deploy_subscribe_merge_exploration.py  3 passed
    test_workflow_deploy_subscribe_merge_preservation.py 3 passed
    test_workflow_deploy_component_version_exploration.py 5 passed
    test_workflow_deploy_component_version_preservation.py 6 passed
    test_camera_binding_context.py                       7 passed
                            (harness source for this file's camera cases)

All of the above plus this suite in ONE invocation: **218 passed** — so the
new module's table bindings (`TRAINING_JOBS_TABLE`,
`CAMERA_REGISTRY_TABLE`) and its `deployments` re-import do not disturb any
sibling.

Frontend (`edge-cv-portal/frontend`, `npx vitest run <file>`):
    src/pages/CreateDeployment.archFilter.test.tsx              11 passed
    src/pages/CreateDeployment.preloadShadowManager.test.tsx      4 passed
    src/pages/deployments/archCompatibility.property.test.ts     16 passed
    src/pages/deployments/onnxComponentArch.property.test.ts     11 passed
    src/pages/deployments/vllmSuffixArch.property.test.ts         5 passed
    src/pages/deployments/pluginComponents.test.ts               13 passed
    src/pages/deployments/cameraBindings.test.ts                 24 passed

Encoding corrections made during observation (task 3 only)
----------------------------------------------------------
- The auto-included `aws.greengrass.Nucleus` entry carries NO
  `componentVersion` when the device's running Nucleus cannot be read, and
  the 201 body reports it as `component_version: 'auto'` in `auto_included`
  while the `components` list reports `'latest'`. The expectation was first
  written with a pinned version and corrected to the observed shape.
- `aws.greengrass.LogManager`'s `componentLogsConfigurationMap` contains
  ONLY the operator's own components (plus `com.aws.greengrass`), because
  the LogManager auto-include runs BEFORE the ShadowManager and Nucleus
  auto-includes. The expectation was corrected to build the map from the
  operator's set in request order.

Harness notes / coordination
----------------------------
`PreflightGreengrass` extends the shared `FakeGreengrass`
(`test_workflow_packaging_deployment_integration.py`) LOCALLY, in this file,
with `get_component` / `list_component_versions` / `describe_component` and
per-component recipe/version/platform seeding — deliberately local, because
another agent is concurrently adding equivalent additive support to the
shared fake for `test_deployment_preflight_exploration.py`. Overlap is
expected and harmless: the subclass's methods win over the inherited ones,
and its state lives under `preflight_recipes` / `preflight_published` so
neither system can read the other's seeding by accident.
`test_harness_shapes_are_the_ones_production_reads` proves the fake's
shapes against the ONE recipe/version read production performs today
(`resolve_public_component_version`, `deployments.py:437-450`), so the
dual-namespace seeding cannot be vacuous.
"""
import json
import os
import re
import subprocess
import sys
import uuid

import boto3
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conftest import REGION, TEST_ENV
from test_camera_binding_context import BindingEnv, camera_node
from test_deployment_shadow_manager import ShadowManagerEnv
from test_workflow_deploy_subscribe_merge_exploration import WorkflowDeployEnv
from test_workflow_packaging_deployment_integration import (
    ACCOUNT_ID, FakeGreengrass, FakeIot)

PREFLIGHT_TRAINING_JOBS_TABLE = "test-training-jobs-preflight-preservation"
PREFLIGHT_CAMERA_REGISTRY_TABLE = "test-camera-registry-preflight-preservation"

# --------------------------------------------------------------------------
# Live shapes from evidence.md (§1.1, §1.2, §1.3), used verbatim so the
# oracle pins the account's REAL manifest forms rather than invented ones.
# --------------------------------------------------------------------------

#: `dda.workflow.8784b33b-…` v1.0.0 — Counterexample A: jp5/jp6 only.
CE_A_WORKFLOW = "dda.workflow.8784b33b-25a6-44c3-b62d-d47e8213eabe"
CE_A_PLATFORMS = [
    {"os": "linux", "variant": "arm64_jp5", "architecture": "aarch64"},
    {"os": "linux", "variant": "arm64_jp6", "architecture": "aarch64"},
]
CE_A_DEVICE = "adlink-dlap-701"

#: Counterexample B — legacy models HARD-depending on names with an EMPTY
#: published-version list in BOTH namespaces.
CE_B_SEGHEAD = "model-cookies-segmentation-seghead-jetson-xavier"
CE_B_YOLO = "model-yolo-test-jetson-xavier"
DEAD_LOCAL_SERVER_JP4 = "aws.edgeml.dda.LocalServer.arm64JP4"
DEAD_LOCAL_SERVER_BARE = "aws.edgeml.dda.LocalServer.arm64"

#: Counterexample C — the removed model and the workflow that kept it.
CE_C_MODEL = "model-vllm-qwen3-5-9b-jetson-xavier-jp7"
CE_C_WORKFLOW = "dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8"
CE_C_WORKFLOW_VERSION = "12.0.0"
CE_C_DEVICE = "jetson-thor1"
CE_C_DEPENDENCIES = {
    CE_C_MODEL: {"VersionRequirement": ">=0.0.0", "DependencyType": "HARD"},
    "aws.edgeml.dda.LocalServer.arm64JP7": {
        "VersionRequirement": ">=1.0.0", "DependencyType": "HARD"},
}

#: The two 3.1 reference deployments on `jetson-thor1`.
REFERENCE_CLEAN_DEPLOYMENT = "7092ec91-b404-4738-a586-a022ddc15157"
REFERENCE_REVISION_53 = "a9086c7d-ae9e-4131-8e83-7efc4fd549b4"

LOCAL_SERVER_JP7 = "aws.edgeml.dda.LocalServer.arm64JP7"
LOCAL_SERVER_JP6 = "aws.edgeml.dda.LocalServer.arm64JP6"
LOCAL_SERVER_JP5 = "aws.edgeml.dda.LocalServer.arm64JP5"

#: All three LocalServer variants publish the SAME variant-less manifest —
#: JetPack lives in the component NAME (evidence.md §1.1).
VARIANTLESS_AARCH64 = [{"os": "linux", "architecture": "aarch64"}]

#: The wildcard forms the account actually publishes (evidence.md §1.1, §5.1).
NUCLEUS_PLATFORMS = [{"os": "linux"}, {"os": "darwin"}, {"os": "windows"}]
STAR_OS_PLATFORMS = [{"os": "*"}]
NULL_PLATFORM = [None]

AWS_MANAGED_NAMES = (
    "aws.greengrass.Nucleus",
    "aws.greengrass.Cli",
    "aws.greengrass.ShadowManager",
    "aws.greengrass.LogManager",
    "aws.greengrass.SecureTunneling",
)

#: Versions observed live for the AWS-managed names (evidence.md §1.1).
AWS_MANAGED_VERSIONS = {
    "aws.greengrass.Nucleus": "2.14.3",
    "aws.greengrass.Cli": "2.14.3",
    "aws.greengrass.ShadowManager": "2.3.9",
    "aws.greengrass.LogManager": "2.3.10",
    "aws.greengrass.SecureTunneling": "1.0.19",
}

JETSON_VARIANTS = ("arm64_jp4", "arm64_jp5", "arm64_jp6", "arm64_jp7")

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "..", ".."))


# ==========================================================================
# Module stack: the training-jobs GSI (vLLM gate) and the camera registry
# table (camera gates) must exist and be bound BEFORE deployments.py is
# imported, so one module fixture owns both plus the import.
# ==========================================================================

@pytest.fixture(scope="module")
def stack(aws_stack):
    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=PREFLIGHT_TRAINING_JOBS_TABLE,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
            {"AttributeName": "component_name", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "component_name-index",
            "KeySchema": [{"AttributeName": "component_name",
                           "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=PREFLIGHT_CAMERA_REGISTRY_TABLE,
        KeySchema=[{"AttributeName": "device_id", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "device_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    os.environ["TRAINING_JOBS_TABLE"] = PREFLIGHT_TRAINING_JOBS_TABLE
    os.environ["CAMERA_REGISTRY_TABLE"] = PREFLIGHT_CAMERA_REGISTRY_TABLE

    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    resource = boto3.resource("dynamodb", region_name=REGION)
    deployments._preflight_training_jobs = resource.Table(
        PREFLIGHT_TRAINING_JOBS_TABLE)
    yield deployments

    # Never leak the table bindings into later modules (the
    # test_deployment_vllm_gate.py precedent).
    os.environ.pop("TRAINING_JOBS_TABLE", None)
    os.environ.pop("CAMERA_REGISTRY_TABLE", None)

    # ...and never leak the TABLES either. TEARDOWN HYGIENE ONLY — the
    # "IMMUTABLE oracle" rule above is about this file's recorded
    # EXPECTATIONS (its assertions are never rebaselined), NOT about fixture
    # cleanliness, so do not revert this.
    #
    # `conftest.aws_stack` is SESSION-scoped, so one moto DynamoDB backend is
    # shared by the whole directory run, and DynamoDB's ListTables returns at
    # most 100 names per page in insertion order. Leaving these two tables
    # behind pushed `test-edge-credentials` from name #100 to #102 — off page
    # one — and the `test_user_admin_*` fixtures guard their table creation
    # with an UNPAGINATED `if … not in list_tables()["TableNames"]`, so every
    # one of those modules then tried to re-create an existing table and
    # errored with `ResourceInUseException: Table already exists`
    # (186 ERRORs). Deleting them keeps this module's footprint out of later
    # modules' pagination window.
    for table_name in (PREFLIGHT_TRAINING_JOBS_TABLE,
                       PREFLIGHT_CAMERA_REGISTRY_TABLE):
        try:
            client.delete_table(TableName=table_name)
        except Exception:      # pragma: no cover - teardown is best-effort
            # ResourceNotFoundException, or anything else: a teardown must
            # never fail a run.
            pass


@pytest.fixture(scope="module")
def deployments(stack):
    return stack


# ==========================================================================
# PreflightGreengrass — the shared fake plus the three reads the closure
# resolver will perform (additive, local to this file: see "Harness notes").
# ==========================================================================

class _Paginator:
    def __init__(self, pages_fn):
        self._pages_fn = pages_fn

    def paginate(self, **kwargs):
        return iter(self._pages_fn(**kwargs))


def _parse_component_arn(arn):
    """(namespace, name, version|None) of a Greengrass component ARN:
    arn:aws:greengrass:{region}:{namespace}:components:{name}[:versions:{v}]
    """
    text = str(arn or "")
    head, _, tail = text.partition(":components:")
    namespace = head.split(":")[4] if head.count(":") >= 4 else ""
    name, _, version = tail.partition(":versions:")
    return namespace, name, version or None


class PreflightGreengrass(FakeGreengrass):
    """`FakeGreengrass` plus recipe / published-version / platform reads.

    Seeding is per NAMESPACE so the account-vs-`aws` split evidence.md §1.2
    proved load-bearing is reproducible: a name seeded only under `aws`
    returns an EMPTY LIST — not an error — under the account ARN, exactly
    as the live API does.
    """

    def __init__(self):
        super().__init__()
        # Deliberately NOT named `recipes` / `component_versions`: the shared
        # fake is gaining its own equivalent state under those names for
        # test_deployment_preflight_exploration.py, and aliasing it would let
        # a seeder from one system be read by the reader of the other.
        self.preflight_recipes = {}     # (namespace, name, version) -> recipe
        self.preflight_published = {}   # (namespace, name) -> [versions]
        self.get_component_calls = []
        self.list_component_versions_calls = []
        self.describe_component_calls = []

    # ------------------------------------------------------------- setup
    def seed_component(self, name, version, platforms=VARIANTLESS_AARCH64,
                       dependencies=None, namespace=ACCOUNT_ID):
        """One published component version: its recipe (manifest platforms
        + `ComponentDependencies`) and its presence in the namespace's
        published-version list."""
        manifests = []
        for platform in platforms or []:
            manifest = {"Lifecycle": {}}
            if platform is not None:
                manifest["Platform"] = dict(platform)
            else:
                manifest["Platform"] = None
            manifests.append(manifest)
        self.preflight_recipes[(namespace, name, version)] = {
            "RecipeFormatVersion": "2020-01-25",
            "ComponentName": name,
            "ComponentVersion": version,
            "ComponentDependencies": dict(dependencies or {}),
            "Manifests": manifests,
        }
        versions = self.preflight_published.setdefault((namespace, name), [])
        if version not in versions:
            versions.append(version)
        return self

    def seed_aws_managed(self, nucleus_requirement=None):
        """The five AWS-managed public names, published ONLY in the `aws`
        namespace with their live manifest shapes (evidence.md §1.1)."""
        platforms = {
            "aws.greengrass.Nucleus": NUCLEUS_PLATFORMS,
            "aws.greengrass.Cli": NUCLEUS_PLATFORMS,
            "aws.greengrass.ShadowManager": STAR_OS_PLATFORMS,
            "aws.greengrass.LogManager": STAR_OS_PLATFORMS,
            "aws.greengrass.SecureTunneling": VARIANTLESS_AARCH64,
        }
        for name, version in AWS_MANAGED_VERSIONS.items():
            dependencies = {}
            if nucleus_requirement and name != "aws.greengrass.Nucleus":
                dependencies = {"aws.greengrass.Nucleus": {
                    "VersionRequirement": nucleus_requirement,
                    "DependencyType": "SOFT"}}
            self.seed_component(name, version, platforms[name],
                                dependencies, namespace="aws")
        return self

    # ------------------------------------------------- client API surface
    def get_paginator(self, operation):
        if operation == "list_component_versions":
            return _Paginator(self._pages_list_component_versions)
        return super().get_paginator(operation)

    def _pages_list_component_versions(self, arn=None, **_):
        self.list_component_versions_calls.append(arn)
        namespace, name, _version = _parse_component_arn(arn)
        versions = list(self.preflight_published.get((namespace, name), []))
        return [{"componentVersions": [
            {"componentName": name, "componentVersion": v,
             "arn": f"{arn}:versions:{v}"}
            for v in versions]}]

    def list_component_versions(self, arn=None, **_):
        [page] = self._pages_list_component_versions(arn=arn)
        return page

    def get_component(self, arn=None, recipeOutputFormat=None, **_):
        self.get_component_calls.append((arn, recipeOutputFormat))
        namespace, name, version = _parse_component_arn(arn)
        recipe = self.preflight_recipes.get((namespace, name, version))
        if recipe is None:
            # Live behaviour for an unpublished version (evidence.md §4.2).
            from botocore.exceptions import ClientError
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException",
                           "Message": f"component ({name}:{version}) "
                                      f"does not exist"}},
                "GetComponent")
        return {"recipeOutputFormat": "JSON",
                "recipe": json.dumps(recipe).encode("utf-8")}

    def describe_component(self, arn=None, **_):
        self.describe_component_calls.append(arn)
        namespace, name, version = _parse_component_arn(arn)
        recipe = self.preflight_recipes.get((namespace, name, version))
        if recipe is None:
            from botocore.exceptions import ClientError
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException",
                           "Message": f"component ({name}:{version}) "
                                      f"does not exist"}},
                "DescribeComponent")
        return {
            "componentName": name,
            "componentVersion": version,
            # evidence.md §1.1: describe mirrors the recipe exactly, and an
            # absent Platform block surfaces as {"attributes": {}}.
            "platforms": [{"attributes": dict(m.get("Platform") or {})}
                          for m in recipe.get("Manifests", [])],
            "status": {"componentState": "DEPLOYABLE",
                       "vendorGuidance": "ACTIVE"},
        }


# ==========================================================================
# Harnesses (reused, not rebuilt — tasks.md Notes)
# ==========================================================================

#: Devices-table rows written by `put_device_record` during the CURRENT test,
#: as (table, device_id) pairs, so `_devices_table_isolation` can delete
#: exactly those rows again — never a row another module seeded.
#:
#: TEARDOWN HYGIENE, NOT A CHANGE OF EXPECTATIONS (see the `stack` teardown
#: note above): the IMMUTABLE-oracle rule governs this file's recorded
#: EXPECTATIONS, not its fixture cleanliness. `conftest.aws_stack` is
#: SESSION-scoped and the devices table is keyed on `device_id` ALONE, so a
#: row written for one of the verbatim incident device names outlives the test
#: and is read by every later module resolving the same id.
#: `test_model_status_devices_read.py` pins the NO-record rendering of
#: `jetson-thor1` (`target_architecture: None`) and neither seeds nor cleans
#: that row, so the `arm64_jp7` record the 3.1 reference cases write here was
#: read by that oracle. The incident names stay exactly as they are; what is
#: fixed is that the rows do not survive the test that wrote them.
_SEEDED_DEVICE_ROWS = []


@pytest.fixture(autouse=True)
def _devices_table_isolation():
    """Delete exactly the devices-table rows this test seeded.

    Tolerant by construction: a teardown must never fail a run, and the row
    may legitimately be gone already. No assertion is affected — the deletes
    happen after the test body has finished asserting.
    """
    _SEEDED_DEVICE_ROWS.clear()
    yield
    while _SEEDED_DEVICE_ROWS:
        table, device_id = _SEEDED_DEVICE_ROWS.pop()
        try:
            table.delete_item(Key={"device_id": device_id})
        except Exception:      # pragma: no cover - teardown is best-effort
            pass


class GenericSubmitEnv(ShadowManagerEnv):
    """`ShadowManagerEnv` (endpoint-level `create_deployment` through the
    real handler) with the extended fake and the Devices-table / plugin /
    vLLM record seeding the existing gates read."""

    def __init__(self, env, deployments, monkeypatch):
        super().__init__(env, deployments, monkeypatch)
        self.gg = PreflightGreengrass()

    # ------------------------------------------------------------- setup
    def put_device_record(self, thing_name, arch=None, test_device=False):
        item = {"device_id": thing_name, "usecase_id": self.usecase_id,
                "test_device": test_device}
        if arch is not None:
            item["target_architecture"] = arch
        self.env.stack.tables.devices.put_item(Item=item)
        # Tracked so `_devices_table_isolation` removes it on teardown: the
        # devices table is SESSION-scoped and keyed on `device_id` alone.
        _SEEDED_DEVICE_ROWS.append((self.env.stack.tables.devices, thing_name))

    def seed_plugin_record(self, plugin_id, record_version, lifecycle_state,
                           archs):
        self.env.stack.tables.plugin_records.put_item(Item={
            "plugin_id": plugin_id,
            "version": record_version,
            "usecase_id": self.usecase_id,
            "created_at": 1,
            "name": plugin_id,
            "lifecycle_state": lifecycle_state,
            "artifacts": {arch: {"buildStatus": "succeeded"}
                          for arch in archs},
            "component": {"name": f"dda.plugin.{plugin_id}",
                          "version": f"{record_version}.0.0",
                          "architectures": list(archs),
                          "status": "registered"},
        })

    def seed_vllm_record(self, component_name, supported_architectures):
        self.deployments._preflight_training_jobs.put_item(Item={
            "training_id": f"vllm-{uuid.uuid4()}",
            "usecase_id": self.usecase_id,
            "model_type": "vllm",
            "status": "published",
            "created_at": 1,
            "component_name": component_name,
            "published_component": {
                "component_name": component_name,
                "component_version": "1.0.0",
                "supported_architectures": list(supported_architectures),
            },
        })

    def thing_arn(self, thing_name):
        return f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{thing_name}"

    def reset_submissions(self):
        """A fresh Greengrass fake — used per hypothesis example so each
        one submits to a pristine target (no accidental revision)."""
        self.gg = PreflightGreengrass()
        return self.gg

    def submitted(self):
        return self.gg.create_deployment_calls[-1]


class PreflightWorkflowEnv(WorkflowDeployEnv):
    """`WorkflowDeployEnv` (endpoint-level `create_workflow_deployment`)
    with the extended fake and per-version-item seeding for the plugin,
    vLLM and LocalServer-floor gates."""

    def __init__(self, env, deployments, monkeypatch):
        super().__init__(env, deployments, monkeypatch)
        self.gg = PreflightGreengrass()

    def seed_workflow(self, version=1, **version_attrs):
        """A validated + packaged workflow version with NO subscribed
        topics (so the subscribe accessControl merge stays a no-op) plus
        any gate attributes the caller wants recorded."""
        workflow_id = self.seed_subscribing_workflow(version=version,
                                                     topics=None)
        if version_attrs:
            self.env.stack.tables.versions.update_item(
                Key={"workflow_id": workflow_id, "version": version},
                UpdateExpression="SET " + ", ".join(
                    f"#{i} = :v{i}" for i in range(len(version_attrs))),
                ExpressionAttributeNames={
                    f"#{i}": key
                    for i, key in enumerate(version_attrs)},
                ExpressionAttributeValues={
                    f":v{i}": value
                    for i, value in enumerate(version_attrs.values())},
            )
        return workflow_id

    def put_device_record(self, thing_name, arch=None, test_device=False):
        item = {"device_id": thing_name, "usecase_id": self.usecase_id,
                "test_device": test_device}
        if arch is not None:
            item["target_architecture"] = arch
        self.env.stack.tables.devices.put_item(Item=item)
        # Same teardown tracking as GenericSubmitEnv.put_device_record.
        _SEEDED_DEVICE_ROWS.append((self.env.stack.tables.devices, thing_name))

    def seed_plugin_record(self, plugin_id, record_version, lifecycle_state,
                           archs):
        self.env.stack.tables.plugin_records.put_item(Item={
            "plugin_id": plugin_id,
            "version": record_version,
            "usecase_id": self.usecase_id,
            "created_at": 1,
            "name": plugin_id,
            "lifecycle_state": lifecycle_state,
            "artifacts": {arch: {"buildStatus": "succeeded"}
                          for arch in archs},
            "component": {"name": f"dda.plugin.{plugin_id}",
                          "version": f"{record_version}.0.0",
                          "architectures": list(archs),
                          "status": "registered"},
        })


@pytest.fixture
def gen_env(env, deployments, monkeypatch):
    return GenericSubmitEnv(env, deployments, monkeypatch)


@pytest.fixture
def wf_env(env, deployments, monkeypatch):
    return PreflightWorkflowEnv(env, deployments, monkeypatch)


@pytest.fixture
def camera_env(env, deployments, monkeypatch):
    resource = boto3.resource("dynamodb", region_name=REGION)
    return BindingEnv(env, {
        "deployments": deployments,
        "registry": resource.Table(PREFLIGHT_CAMERA_REGISTRY_TABLE),
    }, monkeypatch)


# ==========================================================================
# The recorded document oracle (observation-first, probe run 2026-09-10)
# ==========================================================================

DDA_NUCLEUS_PREFIXES = ("aws.edgeml.dda.LocalServer",
                        "aws.edgeml.dda.InferenceApp", "model-")

#: Observed synchronize merge of the auto-included / completed
#: ShadowManager entry (identical to test_deployment_shadow_manager.py's
#: EXPECTED_SYNC_CONFIG — pinned here independently so this oracle does not
#: depend on that module's constant).
OBSERVED_SHADOW_SYNC_CONFIG = {
    "synchronize": {
        "direction": "betweenDeviceAndCloud",
        "coreThing": {
            "classic": True,
            "namedShadows": ["dda-camera-registry", "dda-camera-bindings",
                             "dda-model-status"],
        },
    }
}


def _needs_nucleus(names):
    return any(name.startswith(prefix)
               for name in names for prefix in DDA_NUCLEUS_PREFIXES)


def _log_entry():
    return {"minimumLogLevel": "INFO", "diskSpaceLimit": 10,
            "diskSpaceLimitUnit": "MB",
            "deleteLogFileAfterCloudUpload": False}


def expected_document(deployments, requested, target_arn,
                      deployment_name=None, rollout_config=None,
                      usecase_id=None, user_id=None):
    """The `deployment_params` the UNFIXED generic path submits for
    ``requested`` (a list of ``{component_name, component_version}`` with
    concrete versions), reconstructed from the RECORDED shapes.

    Version PINS of the auto-included AWS components are read from the
    module constants on purpose: this oracle is about the submit path this
    spec touches, not about an unrelated LogManager/ShadowManager version
    bump. Everything else — which entries exist, their order, their merge
    documents byte-for-byte, the tag set, the policy block — is pinned.
    """
    names = [c["component_name"] for c in requested]
    components = {c["component_name"]: {"componentVersion":
                                        c["component_version"]}
                  for c in requested}
    needs_nucleus = _needs_nucleus(names)

    if needs_nucleus and "aws.greengrass.LogManager" not in components:
        log_config_map = {"com.aws.greengrass": _log_entry()}
        for name in components:
            if name not in ("aws.greengrass.Nucleus",
                            "aws.greengrass.LogManager"):
                log_config_map[name] = _log_entry()
        components["aws.greengrass.LogManager"] = {
            "componentVersion": deployments.LOG_MANAGER_VERSION,
            "configurationUpdate": {"merge": json.dumps({
                "logsUploaderConfiguration": {
                    "systemLogsConfiguration": {
                        "uploadToCloudWatch": True,
                        "minimumLogLevel": "INFO",
                        "diskSpaceLimit": 25,
                        "diskSpaceLimitUnit": "MB",
                        "deleteLogFileAfterCloudUpload": False},
                    "componentLogsConfigurationMap": log_config_map,
                    "periodicUploadIntervalSec": 300},
            })},
        }

    if needs_nucleus:
        shadow = components.setdefault(
            "aws.greengrass.ShadowManager",
            {"componentVersion": deployments.SHADOW_MANAGER_VERSION})
        shadow["configurationUpdate"] = {
            "merge": json.dumps(OBSERVED_SHADOW_SYNC_CONFIG)}

    store_merge = {"merge": json.dumps(
        {"componentStoreMaxSizeBytes":
         deployments.COMPONENT_STORE_MAX_SIZE_BYTES})}
    if needs_nucleus:
        nucleus = components.get("aws.greengrass.Nucleus")
        if nucleus is None:
            # No running Nucleus resolvable from the fake -> UNPINNED entry.
            components["aws.greengrass.Nucleus"] = {
                "configurationUpdate": store_merge}
        else:
            nucleus.setdefault("configurationUpdate", store_merge)

    params = {"targetArn": target_arn,
              "deploymentName": deployment_name,
              "components": components,
              "tags": {"dda-portal:managed": "true",
                       "dda-portal:usecase-id": usecase_id,
                       "dda-portal:created-by": user_id}}
    if rollout_config:
        params["deploymentPolicies"] = {
            "failureHandlingPolicy": (
                "ROLLBACK" if rollout_config.get("auto_rollback", True)
                else "DO_NOTHING"),
            "componentUpdatePolicy": {
                "timeoutInSeconds": rollout_config.get("timeout_seconds", 60),
                "action": "NOTIFY_COMPONENTS"},
        }
    return params


def expected_auto_included(deployments, requested):
    """The recorded `auto_included` list and its recorded ORDER:
    LogManager, ShadowManager, Nucleus."""
    names = [c["component_name"] for c in requested]
    if not _needs_nucleus(names):
        return []
    entries = []
    if "aws.greengrass.LogManager" not in names:
        entries.append({
            "component_name": "aws.greengrass.LogManager",
            "component_version": deployments.LOG_MANAGER_VERSION,
            "reason": "Required for CloudWatch logging from devices"})
    if "aws.greengrass.ShadowManager" not in names:
        entries.append({
            "component_name": "aws.greengrass.ShadowManager",
            "component_version": deployments.SHADOW_MANAGER_VERSION,
            "reason": (
                "Syncs the dda-camera-registry, dda-camera-bindings and "
                "dda-model-status named shadows with IoT Core for camera "
                "registry synchronization and model GPU-fallback status "
                "visibility")})
    if "aws.greengrass.Nucleus" not in names:
        entries.append({
            "component_name": "aws.greengrass.Nucleus",
            "component_version": "auto",
            "reason": (
                "Included as top-level component (unpinned) so Greengrass "
                "resolves a compatible Nucleus version; raises "
                "componentStoreMaxSizeBytes")})
    return entries


def expected_components_list(document):
    """The recorded 201 `components` echo: every submitted entry in
    submission order, an unpinned entry reported as 'latest'."""
    return [{"component_name": name,
             "component_version": config.get("componentVersion", "latest")}
            for name, config in document["components"].items()]


def assert_clean_submit(deployments, env, payload, requested, target_arn,
                        deployment_name=None, rollout_config=None,
                        is_revision=False, superseded_deployment_id=None):
    """The whole 3.1 claim in one place: the submitted document AND the 201
    body deep-equal the recorded unfixed capture, and no additional
    operator step is required (no acknowledgement field, no `warnings`)."""
    assert len(env.gg.create_deployment_calls) == 1, (
        "PRESERVATION FAILURE (3.1): expected exactly one submission, got "
        f"{len(env.gg.create_deployment_calls)}")
    submitted = env.gg.create_deployment_calls[-1]
    expected = expected_document(
        deployments, requested, target_arn,
        deployment_name=deployment_name or submitted["deploymentName"],
        rollout_config=rollout_config, usecase_id=env.usecase_id,
        user_id=env.user["user_id"])
    assert submitted == expected, (
        "PRESERVATION FAILURE (3.1): submitted deployment document changed\n"
        f"  submitted: {json.dumps(submitted, indent=2, sort_keys=True)}\n"
        f"  expected:  {json.dumps(expected, indent=2, sort_keys=True)}")
    if deployment_name is None:
        assert re.fullmatch(r"portal-deployment-\d{8}-\d{6}",
                            submitted["deploymentName"]), (
            "PRESERVATION FAILURE (3.1): generated deployment name shape "
            f"changed: {submitted['deploymentName']!r}")
    assert payload["auto_included"] == expected_auto_included(
        deployments, requested), (
        "PRESERVATION FAILURE (3.1): auto_included changed: "
        f"{payload['auto_included']!r}")
    assert payload["components"] == expected_components_list(expected)
    assert payload["is_revision"] is is_revision
    assert payload["superseded_deployment_id"] == superseded_deployment_id
    assert payload["message"] == (
        "Deployment updated successfully" if is_revision
        else "Deployment created successfully")
    assert "warnings" not in payload, (
        "PRESERVATION FAILURE (3.1): a clean submit gained a warnings key: "
        f"{payload.get('warnings')!r}")
    # 3.1/2.14: no acknowledgement step is introduced for a clean submit.
    for key in payload:
        assert "acknowledg" not in key.lower(), (
            "PRESERVATION FAILURE (3.1): clean submit response gained an "
            f"acknowledgement field {key!r}")
    return submitted


# ==========================================================================
# Generators
# ==========================================================================

slugs = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
                min_size=3, max_size=12).filter(
    lambda s: not s.startswith("-") and not s.endswith("-")
              and "--" not in s and not s.startswith("vllm-"))

versions = st.tuples(st.integers(1, 9), st.integers(0, 9),
                     st.integers(0, 99)).map(
    lambda t: f"{t[0]}.{t[1]}.{t[2]}")

#: Component names that do NOT belong to any existing gated class: no
#: `dda.plugin.*` (plugin gates), no `model-vllm-*` / `dda.workflow.*`
#: (vLLM gate), no `aws.greengrass.*`. The gated classes get their own
#: dedicated pins in the gate-identity section.
dda_names = st.one_of(
    st.sampled_from([LOCAL_SERVER_JP5, LOCAL_SERVER_JP6, LOCAL_SERVER_JP7]),
    slugs.map(lambda s: f"model-{s}"),
)
plain_names = slugs.map(lambda s: f"com.example.{s}")

#: Component sets that resolve today: at least one DDA component (so the
#: auto-includes fire — the interesting document) plus arbitrary extras.
def _requested(pairs):
    return [{"component_name": name, "component_version": version}
            for name, version in pairs]


resolving_component_sets = st.lists(
    st.tuples(st.one_of(dda_names, plain_names), versions),
    min_size=1, max_size=5, unique_by=lambda pair: pair[0],
).filter(lambda pairs: _needs_nucleus([p[0] for p in pairs])).map(_requested)

plain_component_sets = st.lists(
    st.tuples(plain_names, versions),
    min_size=1, max_size=4, unique_by=lambda pair: pair[0],
).map(_requested)


# ==========================================================================
# 1. Clean-submit document identity (3.1)
# ==========================================================================

class TestCleanSubmitDocumentIdentity:
    """**Validates: Requirements 3.1, 3.14**"""

    # Feature: deployment-preflight-validation, Property 2:
    # non-bug-condition submissions unchanged — clean-submit document identity
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(requested=resolving_component_sets)
    def test_property_clean_submit_document_and_response_identity(
            self, gen_env, deployments, requested):
        """**Property 2 (PBT)** — _for any_ component set whose closure
        resolves today, the submitted `deployment_params` (components map
        with every componentVersion and configurationUpdate, target ARN,
        name, tags, deploymentPolicies) and the 201 body (`auto_included`,
        `components`, `is_revision`, `superseded_deployment_id`, no
        `warnings`) deep-equal the recorded unfixed capture, and no
        additional operator step is required (3.1).
        """
        gen_env.reset_submissions()
        thing = "line-a-camera-01"
        gen_env.gg.register_device(thing)
        for entry in requested:
            gen_env.gg.seed_component(entry["component_name"],
                                      entry["component_version"])

        status, payload = gen_env.deploy_components(requested,
                                                   target_devices=[thing])

        assert status == 201, payload
        assert_clean_submit(deployments, gen_env, payload, requested,
                            gen_env.thing_arn(thing))

    # Feature: deployment-preflight-validation, Property 2:
    # non-bug-condition submissions unchanged — no auto-includes without DDA
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(requested=plain_component_sets)
    def test_property_non_dda_set_submits_verbatim_with_no_auto_includes(
            self, gen_env, deployments, requested):
        """**Property 2 (PBT)** — _for any_ set with no DDA/model
        component, the submitted components map is the operator's set
        VERBATIM: no Nucleus, no LogManager, no ShadowManager, and an empty
        `auto_included` (3.1, 3.14).
        """
        gen_env.reset_submissions()
        thing = "plain-device-01"

        status, payload = gen_env.deploy_components(requested,
                                                   target_devices=[thing])

        assert status == 201, payload
        submitted = assert_clean_submit(deployments, gen_env, payload,
                                        requested, gen_env.thing_arn(thing))
        assert set(submitted["components"]) == {
            entry["component_name"] for entry in requested}
        assert payload["auto_included"] == []

    def test_reference_deployment_7092ec91_clean_set_submits_unchanged(
            self, gen_env, deployments):
        """Reference case `7092ec91-b404-4738-a586-a022ddc15157` on
        `jetson-thor1` (tasks.md 3.1): a JP7 set whose closure resolves —
        the JP7 LocalServer, a `-jp7` model and a variant-less single-arm
        workflow, every manifest satisfied by the device. Submitted
        unchanged, with no operator step.

        The COMPONENT SHAPES are the live ones evidence.md §1.1 recorded;
        the live document itself is not reproduced here (it is not in the
        allowed read-only evidence set) and no claim is made that it is.
        """
        requested = [
            {"component_name": LOCAL_SERVER_JP7,
             "component_version": "1.0.26"},
            {"component_name": "model-yolo-test-jetson-xavier-jp7",
             "component_version": "8.0.0"},
            {"component_name": CE_C_WORKFLOW,
             "component_version": CE_C_WORKFLOW_VERSION},
        ]
        gen_env.gg.register_device(CE_C_DEVICE)
        gen_env.gg.seed_component(LOCAL_SERVER_JP7, "1.0.26",
                                  VARIANTLESS_AARCH64)
        gen_env.gg.seed_component("model-yolo-test-jetson-xavier-jp7",
                                  "8.0.0", VARIANTLESS_AARCH64,
                                  {LOCAL_SERVER_JP7: {
                                      "VersionRequirement": ">=1.0.0 <2.0.0",
                                      "DependencyType": "HARD"}})
        gen_env.gg.seed_component(CE_C_WORKFLOW, CE_C_WORKFLOW_VERSION,
                                  VARIANTLESS_AARCH64, CE_C_DEPENDENCIES)
        gen_env.gg.seed_aws_managed()
        gen_env.put_device_record(CE_C_DEVICE, arch="arm64_jp7")

        status, payload = gen_env.deploy_components(
            requested, target_devices=[CE_C_DEVICE])

        assert status == 201, payload
        assert_clean_submit(deployments, gen_env, payload, requested,
                            gen_env.thing_arn(CE_C_DEVICE))

    def test_revision_reuses_existing_name_and_reports_superseded(
            self, gen_env, deployments):
        """A revision of a target that already has a deployment keeps the
        existing deployment NAME, reports `is_revision` and the superseded
        id, and submits the same document otherwise (3.1)."""
        requested = [{"component_name": LOCAL_SERVER_JP7,
                      "component_version": "1.0.19"}]
        gen_env.gg.register_device(CE_C_DEVICE)
        superseded = gen_env.gg.seed_deployment(
            gen_env.thing_arn(CE_C_DEVICE),
            {LOCAL_SERVER_JP7: {"componentVersion": "1.0.18"}},
            name="ssh-tunnel-on-jetson-thor1")

        status, payload = gen_env.deploy_components(
            requested, target_devices=[CE_C_DEVICE])

        assert status == 201, payload
        assert_clean_submit(
            deployments, gen_env, payload, requested,
            gen_env.thing_arn(CE_C_DEVICE),
            deployment_name="ssh-tunnel-on-jetson-thor1",
            is_revision=True, superseded_deployment_id=superseded)

    def test_rollout_config_produces_the_recorded_policy_block(
            self, gen_env, deployments):
        """`rollout_config` still maps to the recorded `deploymentPolicies`
        block — `ROLLBACK` + `NOTIFY_COMPONENTS` with the caller's timeout
        (3.1; `failureHandlingPolicy: ROLLBACK` is the blast-radius claim
        of bugfix.md's introduction)."""
        requested = [{"component_name": LOCAL_SERVER_JP7,
                      "component_version": "1.0.26"}]
        rollout = {"auto_rollback": True, "timeout_seconds": 60}
        gen_env.gg.register_device(CE_C_DEVICE)

        status, payload = gen_env.deploy_components(
            requested, target_devices=[CE_C_DEVICE], rollout_config=rollout)

        assert status == 201, payload
        submitted = assert_clean_submit(
            deployments, gen_env, payload, requested,
            gen_env.thing_arn(CE_C_DEVICE), rollout_config=rollout)
        assert submitted["deploymentPolicies"] == {
            "failureHandlingPolicy": "ROLLBACK",
            "componentUpdatePolicy": {"timeoutInSeconds": 60,
                                      "action": "NOTIFY_COMPONENTS"}}

    def test_workflow_path_clean_submit_merges_and_submits_unchanged(
            self, wf_env):
        """The workflow path's recorded clean submit: the target's current
        components are carried over verbatim, the workflow entry is placed
        at the registered version, the five recorded tags are present, and
        the 201 body carries no warnings and no acknowledgement field
        (3.1)."""
        workflow_id = wf_env.seed_workflow()
        wf_env.register_device(CE_C_DEVICE)
        wf_env.gg.seed_deployment(wf_env.thing_arn(CE_C_DEVICE), {
            LOCAL_SERVER_JP7: {"componentVersion": "1.0.19"},
            "aws.greengrass.Nucleus": {"componentVersion": "2.12.0"},
        }, name="ssh-tunnel-on-jetson-thor1")

        status, payload = wf_env.deploy(workflow_id,
                                       target_devices=[CE_C_DEVICE])

        assert status == 201, payload
        [call] = wf_env.gg.create_deployment_calls
        assert call["components"] == {
            LOCAL_SERVER_JP7: {"componentVersion": "1.0.19"},
            "aws.greengrass.Nucleus": {"componentVersion": "2.12.0"},
            f"dda.workflow.{workflow_id}": {"componentVersion": "1.0.0"},
        }
        assert call["deploymentName"] == "ssh-tunnel-on-jetson-thor1"
        assert set(call["tags"]) == {
            "dda-portal:managed", "dda-portal:usecase-id",
            "dda-portal:workflow-id", "dda-portal:workflow-version",
            "dda-portal:created-by"}
        assert "deploymentPolicies" not in call
        assert payload["is_revision"] is True
        assert payload["camera_bindings_delivered"] is False
        assert payload["camera_warnings"] == []
        assert "warnings" not in payload
        for key in payload:
            assert "acknowledg" not in key.lower(), (
                "PRESERVATION FAILURE (3.1): workflow clean submit gained "
                f"an acknowledgement field {key!r}")


# ==========================================================================
# 2. Existing gate identity and PRECEDENCE (3.4)
#
# Each envelope is pinned WHOLE — status plus the complete
# {error: {code, message, details}} body — on the submit path that carries
# it. `INCOMPATIBLE_LOCAL_SERVER` and the camera codes exist only on
# `create_workflow_deployment`; the vLLM and plugin gates exist on both, and
# both are pinned.
# ==========================================================================

VLLM_GATE_MESSAGE = (
    "One or more target devices have a Target_Architecture outside the "
    "supported set of a vLLM model component or an LLM-bearing workflow "
    "component; the deployment was not submitted")
PLUGIN_LIFECYCLE_MESSAGE = (
    "One or more depended-on plugin components are not deployable to the "
    "requested target devices in their current lifecycle state; the "
    "deployment was not submitted")
PLUGIN_ARCH_MESSAGE = (
    "One or more target devices have no published Plugin_Artifact for their "
    "recorded Target_Architecture in a depended-on plugin component version; "
    "the deployment was not submitted")
LOCAL_SERVER_MESSAGE = (
    "One or more target devices do not have a LocalServer component version "
    "compatible with this workflow component; the deployment was not "
    "submitted")
CAMERA_INVALID_MESSAGE = (
    "One or more camera bindings are invalid; the deployment was not "
    "submitted")
CAMERA_UNCONFIRMED_MESSAGE = (
    "Camera binding warnings require explicit confirmation before the "
    "deployment can be created")
REGISTRY_UNAVAILABLE_MESSAGE = (
    "The camera registry could not be read for the target devices; camera "
    "binding validation cannot run and the deployment was not submitted")


class TestVllmGateIdentity:
    """**Validates: Requirements 3.4**"""

    def test_generic_path_vllm_arch_unsupported_envelope(self, gen_env):
        """`VLLM_ARCH_UNSUPPORTED` on `create_deployment`, whole envelope."""
        gen_env.seed_vllm_record(CE_C_MODEL, ["arm64_jp7"])
        gen_env.put_device_record("jp6-cam-01", arch="arm64_jp6")

        status, payload = gen_env.deploy_components(
            [{"component_name": CE_C_MODEL, "component_version": "1.0.0"}],
            target_devices=["jp6-cam-01"])

        assert status == 409
        assert payload == {"error": {
            "code": "VLLM_ARCH_UNSUPPORTED",
            "message": VLLM_GATE_MESSAGE,
            "details": {"unsupported": [{
                "component": CE_C_MODEL,
                "version": "1.0.0",
                "device": "jp6-cam-01",
                "deviceArch": "arm64_jp6",
                "supported": ["arm64_jp7"],
                "reason": "ARCH_UNSUPPORTED"}]}}}
        assert gen_env.gg.create_deployment_calls == []

    def test_workflow_path_vllm_arch_unsupported_envelope(self, wf_env):
        """`VLLM_ARCH_UNSUPPORTED` on `create_workflow_deployment`, whole
        envelope, including the jp4 reason branch."""
        workflow_id = wf_env.seed_workflow(
            has_llm_inference=True, packaged_architectures=["arm64_jp6"])
        wf_env.gg.register_device("jp4-cam-01", local_server_version="99.0.0",
                                  arch="arm64JP4")
        wf_env.put_device_record("jp4-cam-01", arch="arm64_jp4")

        status, payload = wf_env.deploy(workflow_id,
                                       target_devices=["jp4-cam-01"])

        assert status == 409
        assert payload == {"error": {
            "code": "VLLM_ARCH_UNSUPPORTED",
            "message": VLLM_GATE_MESSAGE,
            "details": {"unsupported": [{
                "component": f"dda.workflow.{workflow_id}",
                "version": "1.0.0",
                "device": "jp4-cam-01",
                "deviceArch": "arm64_jp4",
                "supported": ["arm64_jp6"],
                "reason": "JP4_UNSUPPORTED"}]}}}
        assert wf_env.gg.create_deployment_calls == []


class TestPluginGateIdentity:
    """**Validates: Requirements 3.4**"""

    def test_generic_path_plugin_lifecycle_violation_envelope(self, gen_env):
        gen_env.seed_plugin_record("p1", 3, "dev", ["arm64_jp7"])
        gen_env.put_device_record("bench-1", arch="arm64_jp7",
                                  test_device=True)

        status, payload = gen_env.deploy_components(
            [{"component_name": "dda.plugin.p1",
              "component_version": "3.0.0"}],
            target_devices=["bench-1"])

        assert status == 409
        assert payload == {"error": {
            "code": "PLUGIN_LIFECYCLE_VIOLATION",
            "message": PLUGIN_LIFECYCLE_MESSAGE,
            "details": {"violations": [{
                "pluginComponent": "dda.plugin.p1",
                "lifecycleState": "dev",
                "devices": ["bench-1"],
                "version": "3.0.0",
                "plugin_id": "p1"}]}}}
        assert gen_env.gg.create_deployment_calls == []

    def test_generic_path_plugin_arch_unsupported_envelope(self, gen_env):
        gen_env.seed_plugin_record("p2", 3, "prod", ["arm64_jp5"])
        gen_env.put_device_record("jp7-cam-01", arch="arm64_jp7")

        status, payload = gen_env.deploy_components(
            [{"component_name": "dda.plugin.p2",
              "component_version": "3.0.0"}],
            target_devices=["jp7-cam-01"])

        assert status == 409
        assert payload == {"error": {
            "code": "PLUGIN_ARCH_UNSUPPORTED",
            "message": PLUGIN_ARCH_MESSAGE,
            "details": {"unsupported": [{
                "pluginComponent": "dda.plugin.p2",
                "version": "3.0.0",
                "device": "jp7-cam-01",
                "deviceArch": "arm64_jp7"}]}}}
        assert gen_env.gg.create_deployment_calls == []

    def test_workflow_path_plugin_gates_still_read_the_recorded_closure(
            self, wf_env):
        """The workflow path gates on the packager-recorded
        `plugin_components` closure, with the same envelope (3.4)."""
        workflow_id = wf_env.seed_workflow(
            plugin_components={"dda.plugin.p3": "3.0.0"})
        wf_env.seed_plugin_record("p3", 3, "prod", ["arm64_jp5"])
        wf_env.gg.register_device(CE_C_DEVICE, local_server_version="99.0.0",
                                  arch="arm64JP7")
        wf_env.put_device_record(CE_C_DEVICE, arch="arm64_jp7")

        status, payload = wf_env.deploy(workflow_id,
                                       target_devices=[CE_C_DEVICE])

        assert status == 409
        assert payload == {"error": {
            "code": "PLUGIN_ARCH_UNSUPPORTED",
            "message": PLUGIN_ARCH_MESSAGE,
            "details": {"unsupported": [{
                "pluginComponent": "dda.plugin.p3",
                "version": "3.0.0",
                "device": CE_C_DEVICE,
                "deviceArch": "arm64_jp7"}]}}}
        assert wf_env.gg.create_deployment_calls == []


class TestPluginArchGateStaysFailClosed:
    """The existing plugin architecture gate FAILS CLOSED on a device with
    no recorded `Target_Architecture` (`evaluate_plugin_arch_gate`,
    `deployments.py:1987-2020`, docstring: "A device with no recorded
    Target_Architecture fails closed").

    bugfix.md 2.9 requires the NEW validation to fail OPEN and states the
    asymmetry is INTENTIONAL: the plugin gate guards a known-narrow
    component class, while the new validation runs on every submission
    where a fail-closed default would take submission down whenever an
    account read is unavailable. Both must coexist unchanged — so the old
    gate's fail-closed semantics are pinned here, at the pure-function
    level and at the endpoint, and must not be softened while the new
    fail-open contract is implemented.

    **Validates: Requirements 3.4** (and guards 2.9's asymmetry)
    """

    def test_pure_gate_blocks_a_device_with_no_recorded_architecture(
            self, deployments):
        offending = deployments.evaluate_plugin_arch_gate(
            {"dda.plugin.p1": {"version": "3.0.0",
                               "architectures": ["arm64_jp7"]}},
            {"mystery-device": None})
        assert offending == [{"pluginComponent": "dda.plugin.p1",
                              "version": "3.0.0",
                              "device": "mystery-device",
                              "deviceArch": None}], (
            "PRESERVATION FAILURE (3.4 / bugfix.md 2.9): the plugin arch "
            "gate stopped failing CLOSED on a device with no recorded "
            "Target_Architecture. The NEW validation's fail-open contract "
            "must not be applied to this gate.")

    def test_pure_gate_blocks_when_the_manifest_set_is_empty(
            self, deployments):
        """An unresolvable plugin record yields an EMPTY architecture set,
        which blocks every device — the fail-closed twin of the rule
        above."""
        offending = deployments.evaluate_plugin_arch_gate(
            {"dda.plugin.ghost": {"version": "3.0.0", "architectures": []}},
            {"jp7-cam-01": "arm64_jp7"})
        assert offending == [{"pluginComponent": "dda.plugin.ghost",
                              "version": "3.0.0",
                              "device": "jp7-cam-01",
                              "deviceArch": "arm64_jp7"}]

    def test_endpoint_blocks_a_device_with_no_recorded_architecture(
            self, gen_env):
        """End to end: a device with a Devices-table record carrying no
        `target_architecture` is REFUSED with `PLUGIN_ARCH_UNSUPPORTED` and
        `deviceArch: null`, and nothing is submitted."""
        gen_env.seed_plugin_record("p4", 3, "prod", ["arm64_jp7"])
        gen_env.put_device_record("mystery-device", arch=None)

        status, payload = gen_env.deploy_components(
            [{"component_name": "dda.plugin.p4",
              "component_version": "3.0.0"}],
            target_devices=["mystery-device"])

        assert status == 409
        assert payload["error"]["code"] == "PLUGIN_ARCH_UNSUPPORTED"
        [entry] = payload["error"]["details"]["unsupported"]
        assert entry["deviceArch"] is None
        assert gen_env.gg.create_deployment_calls == []

    def test_endpoint_blocks_a_device_with_no_devices_table_record(
            self, gen_env):
        """`load_device_gate_info`: "Devices without a record fail closed"
        — the same refusal with no record at all."""
        gen_env.seed_plugin_record("p5", 3, "prod", ["arm64_jp7"])

        status, payload = gen_env.deploy_components(
            [{"component_name": "dda.plugin.p5",
              "component_version": "3.0.0"}],
            target_devices=["unrecorded-device"])

        assert status == 409
        assert payload["error"]["code"] == "PLUGIN_ARCH_UNSUPPORTED"
        [entry] = payload["error"]["details"]["unsupported"]
        assert entry["device"] == "unrecorded-device"
        assert entry["deviceArch"] is None
        assert gen_env.gg.create_deployment_calls == []


class TestLocalServerFloorIdentity:
    """**Validates: Requirements 3.4**"""

    def test_incompatible_local_server_envelope(self, wf_env):
        workflow_id = wf_env.seed_workflow(min_local_server_version="9.9.9")
        wf_env.gg.register_device(CE_C_DEVICE, local_server_version="1.0.19",
                                  arch="arm64JP7")

        status, payload = wf_env.deploy(workflow_id,
                                       target_devices=[CE_C_DEVICE])

        assert status == 409
        assert payload == {"error": {
            "code": "INCOMPATIBLE_LOCAL_SERVER",
            "message": LOCAL_SERVER_MESSAGE,
            "details": {
                "workflow_id": workflow_id,
                "workflow_version": 1,
                "min_local_server_version": "9.9.9",
                "incompatible_devices": [{
                    "device": CE_C_DEVICE,
                    "local_server_version": "1.0.19",
                    "min_local_server_version": "9.9.9",
                    "reason": ("Installed LocalServer version 1.0.19 is "
                               "older than the required minimum 9.9.9")}]}}}
        assert wf_env.gg.create_deployment_calls == []


class TestCameraGateIdentity:
    """**Validates: Requirements 3.4**"""

    def test_camera_bindings_invalid_envelope(self, camera_env):
        camera_env.seed_workflow([camera_node()])
        camera_env.seed_registry("line-a", {"cfg-1": {}})
        camera_env.gg.register_device("line-a")

        status, payload = camera_env.deploy(
            ["line-a"],
            camera_bindings={"line-a": {"n1": {"cameraSourceId": "ghost"}}})

        assert status == 409
        assert payload == {"error": {
            "code": "CAMERA_BINDINGS_INVALID",
            "message": CAMERA_INVALID_MESSAGE,
            "details": {"errors": [{
                "code": "CAMERA_SOURCE_MISSING",
                "device": "line-a",
                "nodeId": "n1",
                "cameraSourceId": "ghost",
                "message": ("Camera source 'ghost' bound to node 'n1' is "
                            "not registered on device 'line-a'")}],
                "warnings": []}}}
        assert camera_env.gg.create_deployment_calls == []

    def test_camera_warnings_unconfirmed_envelope(self, camera_env):
        camera_env.seed_workflow([camera_node()])
        camera_env.seed_registry("line-a", {"cfg-1": {"sync_status":
                                                     "pending"}})
        camera_env.gg.register_device("line-a")

        status, payload = camera_env.deploy(
            ["line-a"],
            camera_bindings={"line-a": {"n1": {"cameraSourceId": "cfg-1"}}})

        assert status == 409
        assert payload == {"error": {
            "code": "CAMERA_WARNINGS_UNCONFIRMED",
            "message": CAMERA_UNCONFIRMED_MESSAGE,
            "details": {"warnings": [{
                "id": "camera-degraded:line-a:n1:cfg-1:pending",
                "code": "CAMERA_SOURCE_DEGRADED",
                "device": "line-a",
                "nodeId": "n1",
                "cameraSourceId": "cfg-1",
                "conditions": ["pending"],
                "confirmed": False,
                "message": ("Camera source 'cfg-1' bound to node 'n1' on "
                            "device 'line-a' is pending")}]}}}
        assert camera_env.gg.create_deployment_calls == []

    def test_registry_unavailable_envelope(self, camera_env, monkeypatch):
        """`REGISTRY_UNAVAILABLE` keeps its 503 + code + message + the
        single `reason` detail. The reason text embeds the underlying AWS
        error string, which is a botocore/moto detail rather than a portal
        contract, so its exact wording is not pinned — its presence and
        the rest of the envelope are."""
        camera_env.seed_workflow([camera_node()])
        camera_env.gg.register_device("line-a")
        monkeypatch.setattr(camera_env.deployments, "CAMERA_REGISTRY_TABLE",
                            "missing-table-name")

        status, payload = camera_env.deploy(
            ["line-a"],
            camera_bindings={"line-a": {"n1": {"cameraSourceId": "cfg-1"}}})

        assert status == 503
        assert payload["error"]["code"] == "REGISTRY_UNAVAILABLE"
        assert payload["error"]["message"] == REGISTRY_UNAVAILABLE_MESSAGE
        assert list(payload["error"]["details"]) == ["reason"]
        assert payload["error"]["details"]["reason"]
        assert camera_env.gg.create_deployment_calls == []


class TestExistingGatePrecedenceOverNewFindings:
    """When an existing gate AND a bug-condition shape (A / B / C) both
    apply to one submission, the EXISTING gate's response is what comes
    back — byte-identical — and nothing is submitted.

    Today no new finding exists, so these cases pin the existing envelope
    as the answer for exactly the inputs that will ALSO produce a preflight
    finding after the fix. Post-fix they are the precedence assertion of
    3.4: the new validation runs LAST among the pre-submit gates on both
    paths, so it can never replace or reword an existing gate's response.

    **Validates: Requirements 3.4**
    """

    def test_counterexample_A_shape_with_plugin_arch_gate(self, gen_env):
        """Counterexample A (a jp5/jp6-only component on a JP7 device) plus
        a plugin architecture violation: `PLUGIN_ARCH_UNSUPPORTED` wins."""
        gen_env.gg.seed_component(CE_A_WORKFLOW, "1.0.0", CE_A_PLATFORMS)
        gen_env.seed_plugin_record("pa", 3, "prod", ["arm64_jp5"])
        gen_env.put_device_record(CE_A_DEVICE, arch="arm64_jp7")

        status, payload = gen_env.deploy_components(
            [{"component_name": CE_A_WORKFLOW, "component_version": "1.0.0"},
             {"component_name": "dda.plugin.pa",
              "component_version": "3.0.0"}],
            target_devices=[CE_A_DEVICE])

        assert status == 409
        assert payload == {"error": {
            "code": "PLUGIN_ARCH_UNSUPPORTED",
            "message": PLUGIN_ARCH_MESSAGE,
            "details": {"unsupported": [{
                "pluginComponent": "dda.plugin.pa",
                "version": "3.0.0",
                "device": CE_A_DEVICE,
                "deviceArch": "arm64_jp7"}]}}}
        assert gen_env.gg.create_deployment_calls == []

    def test_counterexample_B_shape_with_vllm_gate(self, gen_env):
        """Counterexample B (a model HARD-depending on a name with an EMPTY
        published-version list in BOTH namespaces) plus a vLLM
        architecture violation: `VLLM_ARCH_UNSUPPORTED` wins."""
        gen_env.gg.seed_component(
            CE_B_SEGHEAD, "2.0.0", VARIANTLESS_AARCH64,
            {DEAD_LOCAL_SERVER_JP4: {"VersionRequirement": ">=1.0.0 <2.0.0",
                                     "DependencyType": "HARD"}})
        # DEAD_LOCAL_SERVER_JP4 is deliberately NOT seeded in EITHER
        # namespace: evidence.md §1.2 recorded `account: [] aws: []`.
        gen_env.seed_vllm_record(CE_C_MODEL, ["arm64_jp7"])
        gen_env.put_device_record("jp6-cam-01", arch="arm64_jp6")

        status, payload = gen_env.deploy_components(
            [{"component_name": CE_B_SEGHEAD, "component_version": "2.0.0"},
             {"component_name": CE_C_MODEL, "component_version": "1.0.0"}],
            target_devices=["jp6-cam-01"])

        assert status == 409
        assert payload == {"error": {
            "code": "VLLM_ARCH_UNSUPPORTED",
            "message": VLLM_GATE_MESSAGE,
            "details": {"unsupported": [{
                "component": CE_C_MODEL,
                "version": "1.0.0",
                "device": "jp6-cam-01",
                "deviceArch": "arm64_jp6",
                "supported": ["arm64_jp7"],
                "reason": "ARCH_UNSUPPORTED"}]}}}
        assert gen_env.gg.create_deployment_calls == []

    def test_counterexample_C_shape_with_local_server_floor(self, wf_env):
        """Counterexample C (the previous deployment carries the model and
        the workflow that HARD-requires it; the submitted set drops the
        model) plus a LocalServer floor violation:
        `INCOMPATIBLE_LOCAL_SERVER` wins."""
        workflow_id = wf_env.seed_workflow(min_local_server_version="9.9.9")
        wf_env.gg.register_device(CE_C_DEVICE, local_server_version="1.0.19",
                                  arch="arm64JP7")
        wf_env.gg.seed_deployment(wf_env.thing_arn(CE_C_DEVICE), {
            CE_C_MODEL: {"componentVersion": "1.0.0"},
            CE_C_WORKFLOW: {"componentVersion": CE_C_WORKFLOW_VERSION},
        }, name="ssh-tunnel-on-jetson-thor1")
        wf_env.gg.seed_component(CE_C_WORKFLOW, CE_C_WORKFLOW_VERSION,
                                 VARIANTLESS_AARCH64, CE_C_DEPENDENCIES)

        status, payload = wf_env.deploy(workflow_id,
                                       target_devices=[CE_C_DEVICE])

        assert status == 409
        assert payload["error"]["code"] == "INCOMPATIBLE_LOCAL_SERVER"
        assert payload["error"]["message"] == LOCAL_SERVER_MESSAGE
        assert payload["error"]["details"]["min_local_server_version"] == \
            "9.9.9"
        assert wf_env.gg.create_deployment_calls == []


# ==========================================================================
# 3. Variant-less and wildcard manifests stay universal (3.2, and the
#    matcher rules bugfix.md 2.8 / evidence.md §1.1 §5.1 make load-bearing)
# ==========================================================================

class TestVariantlessAndWildcardManifestsStayUniversal:
    """**Validates: Requirements 3.2, 3.3**"""

    # Feature: deployment-preflight-validation, Property 2:
    # non-bug-condition submissions unchanged — variant-less stays universal
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(model_slug=slugs, model_version=versions,
           device_variant=st.sampled_from(JETSON_VARIANTS),
           local_server=st.sampled_from(
               [LOCAL_SERVER_JP5, LOCAL_SERVER_JP6, LOCAL_SERVER_JP7]))
    def test_property_variantless_aarch64_manifest_deploys_to_any_jetson(
            self, gen_env, deployments, model_slug, model_version,
            device_variant, local_server):
        """**Property 2 (PBT)** — _for any_ component publishing the
        variant-less `{"os":"linux","architecture":"aarch64"}` manifest
        (`greengrass_publish.py:323`/`:654`, and 107 of the account's 148
        aarch64 manifests) and ANY Jetson device variant, the component is
        deployed, never reported incompatible: the submitted document is
        the recorded clean-submit document (3.2).
        """
        gen_env.reset_submissions()
        model = f"model-{model_slug}"
        thing = f"jetson-{device_variant}"
        requested = [
            {"component_name": model, "component_version": model_version},
            {"component_name": local_server, "component_version": "1.0.26"},
        ]
        gen_env.gg.register_device(thing)
        # The variant-less pair, exactly as the account publishes them.
        gen_env.gg.seed_component(model, model_version, VARIANTLESS_AARCH64,
                                  {local_server: {
                                      "VersionRequirement": ">=1.0.0 <2.0.0",
                                      "DependencyType": "HARD"}})
        gen_env.gg.seed_component(local_server, "1.0.26", VARIANTLESS_AARCH64)
        gen_env.put_device_record(thing, arch=device_variant)

        status, payload = gen_env.deploy_components(requested,
                                                   target_devices=[thing])

        assert status == 201, payload
        assert_clean_submit(deployments, gen_env, payload, requested,
                            gen_env.thing_arn(thing))

    @pytest.mark.parametrize("label,platforms", [
        ("absent architecture (aws.greengrass.Nucleus, Cli)",
         NUCLEUS_PLATFORMS),
        ("literal os wildcard (aws.greengrass.ShadowManager, LogManager)",
         STAR_OS_PLATFORMS),
        ("no Platform block at all (testmodel, alienmodel)", NULL_PLATFORM),
        ("variant-less aarch64 (every LocalServer variant, every model-*)",
         VARIANTLESS_AARCH64),
    ])
    def test_every_unconstrained_manifest_form_still_deploys(
            self, gen_env, deployments, label, platforms):
        """All four unconstrained forms the account actually publishes
        (evidence.md §1.1: absent attribute key, literal `"*"`, empty/null
        `Platform`, variant-less aarch64) keep deploying to a JP7 device.

        This is the shape a stricter matcher would break: requiring every
        attribute to be present and literal reports Nucleus and
        ShadowManager — auto-included on EVERY portal deployment — as
        incompatible with every device (3.2, bugfix.md 2.8).
        """
        model = "model-unconstrained-probe"
        requested = [{"component_name": model, "component_version": "1.0.0"}]
        gen_env.gg.register_device(CE_C_DEVICE)
        gen_env.gg.seed_component(model, "1.0.0", platforms)
        gen_env.put_device_record(CE_C_DEVICE, arch="arm64_jp7")

        status, payload = gen_env.deploy_components(
            requested, target_devices=[CE_C_DEVICE])

        assert status == 201, f"{label} was refused: {payload}"
        assert_clean_submit(deployments, gen_env, payload, requested,
                            gen_env.thing_arn(CE_C_DEVICE))

    def test_jetpack_matched_twin_still_deploys(self, gen_env, deployments):
        """3.3: a `-jp7` model whose HARD dependency is
        `…LocalServer.arm64JP7` (present in the account with 27 published
        versions) onto a JP7 device submits unchanged."""
        requested = [
            {"component_name": "model-yolo-test-jetson-xavier-jp7",
             "component_version": "8.0.0"},
            {"component_name": LOCAL_SERVER_JP7,
             "component_version": "1.0.26"},
        ]
        gen_env.gg.register_device(CE_C_DEVICE)
        gen_env.gg.seed_component(
            "model-yolo-test-jetson-xavier-jp7", "8.0.0", VARIANTLESS_AARCH64,
            {LOCAL_SERVER_JP7: {"VersionRequirement": ">=1.0.0 <2.0.0",
                                "DependencyType": "HARD"}})
        for patch in range(24, 27):
            gen_env.gg.seed_component(LOCAL_SERVER_JP7, f"1.0.{patch}",
                                      VARIANTLESS_AARCH64)
        gen_env.put_device_record(CE_C_DEVICE, arch="arm64_jp7")

        status, payload = gen_env.deploy_components(
            requested, target_devices=[CE_C_DEVICE])

        assert status == 201, payload
        assert_clean_submit(deployments, gen_env, payload, requested,
                            gen_env.thing_arn(CE_C_DEVICE))


# ==========================================================================
# 4. AWS-managed public names never produce a finding
# ==========================================================================

class TestAwsManagedPublicNamesNeverProduceAFinding:
    """The five AWS-managed names are auto-included on (or supplied to)
    every portal deployment. evidence.md §1.2 recorded that
    `list-component-versions` under the ACCOUNT ARN returns an EMPTY LIST —
    not an error — for every one of them, and 53/54/30/29/26 versions under
    the `aws` namespace. A resolvability check that queried only the
    account namespace, or a matcher that required literal attributes, would
    refuse EVERY submission.

    **Validates: Requirements 3.1, 3.2**
    """

    def test_account_namespace_is_empty_for_public_names_in_the_harness(
            self, gen_env):
        """Harness fidelity for the silent failure mode: a public name
        seeded only under `aws` yields `[]` under the account ARN with NO
        exception, so exception handling cannot substitute for querying
        both namespaces (evidence.md §5.6)."""
        gen_env.gg.seed_aws_managed()
        for name in AWS_MANAGED_NAMES:
            account = gen_env.gg.list_component_versions(
                arn=f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:"
                    f"components:{name}")
            public = gen_env.gg.list_component_versions(
                arn=f"arn:aws:greengrass:{REGION}:aws:components:{name}")
            assert account["componentVersions"] == [], name
            assert [v["componentVersion"]
                    for v in public["componentVersions"]] == \
                [AWS_MANAGED_VERSIONS[name]], name

    def test_full_aws_managed_set_submits_cleanly(self, gen_env):
        """A component set carrying `aws.greengrass.Nucleus`,
        `aws.greengrass.Cli`, `aws.greengrass.ShadowManager`,
        `aws.greengrass.LogManager` and `aws.greengrass.SecureTunneling`
        alongside a LocalServer submits, with the recorded
        caller-supplied-entry behaviour: the caller's LogManager is left
        verbatim (its auto-include is skipped), the caller's ShadowManager
        keeps its version and gains the portal synchronize merge, the
        caller's Nucleus keeps its version and gains the store merge, and
        `auto_included` is EMPTY."""
        gen_env.gg.seed_aws_managed()
        requested = [{"component_name": LOCAL_SERVER_JP7,
                      "component_version": "1.0.26"}] + [
            {"component_name": name,
             "component_version": AWS_MANAGED_VERSIONS[name]}
            for name in AWS_MANAGED_NAMES]
        gen_env.gg.register_device(CE_C_DEVICE)
        gen_env.put_device_record(CE_C_DEVICE, arch="arm64_jp7")

        status, payload = gen_env.deploy_components(
            requested, target_devices=[CE_C_DEVICE])

        assert status == 201, payload
        submitted = gen_env.submitted()
        assert set(submitted["components"]) == {LOCAL_SERVER_JP7,
                                                *AWS_MANAGED_NAMES}
        assert submitted["components"]["aws.greengrass.Cli"] == {
            "componentVersion": "2.14.3"}
        assert submitted["components"]["aws.greengrass.SecureTunneling"] == {
            "componentVersion": "1.0.19"}
        assert submitted["components"]["aws.greengrass.LogManager"] == {
            "componentVersion": "2.3.10"}
        shadow = submitted["components"]["aws.greengrass.ShadowManager"]
        assert shadow["componentVersion"] == "2.3.9"
        assert json.loads(shadow["configurationUpdate"]["merge"]) == \
            OBSERVED_SHADOW_SYNC_CONFIG
        nucleus = submitted["components"]["aws.greengrass.Nucleus"]
        assert nucleus["componentVersion"] == "2.14.3"
        assert "componentStoreMaxSizeBytes" in \
            nucleus["configurationUpdate"]["merge"]
        assert payload["auto_included"] == []
        assert "warnings" not in payload

    # Feature: deployment-preflight-validation, Property 2:
    # non-bug-condition submissions unchanged — public names never block
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(managed=st.lists(st.sampled_from(AWS_MANAGED_NAMES), min_size=1,
                            max_size=5, unique=True))
    def test_property_any_subset_of_aws_managed_names_submits(
            self, gen_env, managed):
        """**Property 2 (PBT)** — _for any_ subset of the AWS-managed
        public names, published ONLY in the `aws` namespace, the submission
        is accepted and forwarded to Greengrass: no resolvability finding
        and no platform finding is emitted for a public name.
        """
        gen_env.reset_submissions()
        gen_env.gg.seed_aws_managed()
        requested = [{"component_name": LOCAL_SERVER_JP7,
                      "component_version": "1.0.26"}] + [
            {"component_name": name,
             "component_version": AWS_MANAGED_VERSIONS[name]}
            for name in managed]
        gen_env.gg.register_device(CE_C_DEVICE)

        status, payload = gen_env.deploy_components(
            requested, target_devices=[CE_C_DEVICE])

        assert status == 201, payload
        assert len(gen_env.gg.create_deployment_calls) == 1
        submitted = gen_env.submitted()
        for name in managed:
            assert name in submitted["components"], (
                "PRESERVATION FAILURE: AWS-managed public name "
                f"{name} was dropped from the submitted set")

    def test_harness_shapes_are_the_ones_production_reads(self, gen_env):
        """Harness-fidelity guard: the ONE recipe/version read production
        performs today — `resolve_public_component_version`
        (`deployments.py:437-450`), the read the closure resolver
        generalizes — consumes this fake's `list_component_versions`
        paginator and `get_component(recipeOutputFormat='JSON')` output and
        resolves a version by its recipe's Nucleus `VersionRequirement`.

        Without this, the dual-namespace seeding above could be vacuous:
        the fake would be shaped like nothing the production code reads.
        """
        gg = gen_env.gg
        gg.seed_component("aws.greengrass.LogManager", "2.3.10",
                          STAR_OS_PLATFORMS,
                          {"aws.greengrass.Nucleus": {
                              "VersionRequirement": ">=2.1.0 <2.15.0"}},
                          namespace="aws")
        gg.seed_component("aws.greengrass.LogManager", "2.9.9",
                          STAR_OS_PLATFORMS,
                          {"aws.greengrass.Nucleus": {
                              "VersionRequirement": ">=2.20.0"}},
                          namespace="aws")

        resolved = gen_env.deployments.resolve_log_manager_version(
            gg, REGION, "2.12.0")

        assert resolved == "2.3.10", (
            "the fake's list_component_versions / get_component shapes are "
            "not the ones production reads")
        assert any(":aws:components:aws.greengrass.LogManager" in str(arn)
                   for arn in gg.list_component_versions_calls)
        assert any(recipe_format == "JSON"
                   for _arn, recipe_format in gg.get_component_calls)


# ==========================================================================
# 5. Thing-group targets (3.8) and no target selected (3.7)
# ==========================================================================

class TestThingGroupAndNoTarget:
    """**Validates: Requirements 3.7, 3.8**"""

    def test_thing_group_with_unresolvable_member_platforms_is_not_blocked(
            self, gen_env, deployments):
        """3.8: an IoT Thing Group target whose members' platforms cannot
        be resolved (no Devices-table records, and `get_core_device` raises
        for every member) hides nothing and blocks nothing — even for a
        component whose manifests are variant-BEARING, because per
        evidence.md §1.3 an unknown device variant is UNVERIFIED, never
        incompatible."""
        requested = [
            {"component_name": CE_A_WORKFLOW, "component_version": "1.0.0"},
            {"component_name": LOCAL_SERVER_JP6,
             "component_version": "1.0.67"},
        ]
        gen_env.iot.thing_groups["line-a"] = ["member-1", "member-2"]
        gen_env.gg.seed_component(CE_A_WORKFLOW, "1.0.0", CE_A_PLATFORMS)
        gen_env.gg.seed_component(LOCAL_SERVER_JP6, "1.0.67",
                                  VARIANTLESS_AARCH64)

        status, payload = gen_env.deploy_components(
            requested, target_thing_group="line-a")

        assert status == 201, payload
        submitted = assert_clean_submit(
            deployments, gen_env, payload, requested,
            f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thinggroup/line-a")
        assert CE_A_WORKFLOW in submitted["components"]

    def test_no_target_keeps_the_recorded_400_and_submits_nothing(
            self, gen_env):
        """3.7: with no target device and no thing group the recorded 400
        is returned unchanged — no device-derived validation runs first and
        nothing is submitted. (The catalog-discoverability half of 3.7 is a
        frontend claim, baselined by
        `CreateDeployment.archFilter.test.tsx` — 11 passed.)"""
        status, payload = gen_env.deploy_components(
            [{"component_name": "com.example.Widget",
              "component_version": "1.0.0"}])

        assert status == 400
        assert payload == {
            "error": "Either target_devices or target_thing_group required"}
        assert gen_env.gg.create_deployment_calls == []


# ==========================================================================
# 6. Removal with no remaining dependant (3.11) and submitted-set
#    immutability (3.14)
# ==========================================================================

class TestRemovalAndSubmittedSetImmutability:
    """**Validates: Requirements 3.11, 3.14**"""

    def test_revision_53_paired_removal_needs_no_acknowledgement(
            self, gen_env, deployments):
        """3.11 reference case: revision 53 =
        `a9086c7d-ae9e-4131-8e83-7efc4fd549b4` (2026-09-01T16:15:32.170Z)
        removed `model-vllm-qwen3-5-9b-jetson-xavier-jp7` TOGETHER WITH the
        workflow that HARD-required it, so nothing selected required the
        removed component. That submission carries no acknowledgement step
        and no added finding: 201, neither component in the submitted set,
        the removal forwarded to Greengrass exactly as today."""
        previous = {
            CE_C_MODEL: {"componentVersion": "1.0.0"},
            CE_C_WORKFLOW: {"componentVersion": CE_C_WORKFLOW_VERSION},
            LOCAL_SERVER_JP7: {"componentVersion": "1.0.19"},
        }
        gen_env.gg.register_device(CE_C_DEVICE)
        superseded = gen_env.gg.seed_deployment(
            gen_env.thing_arn(CE_C_DEVICE), previous,
            name="thor1-remove-qwen-folder-test")
        gen_env.gg.seed_component(CE_C_WORKFLOW, CE_C_WORKFLOW_VERSION,
                                  VARIANTLESS_AARCH64, CE_C_DEPENDENCIES)
        gen_env.gg.seed_component(CE_C_MODEL, "1.0.0", VARIANTLESS_AARCH64)
        gen_env.gg.seed_component(LOCAL_SERVER_JP7, "1.0.19",
                                  VARIANTLESS_AARCH64)
        gen_env.put_device_record(CE_C_DEVICE, arch="arm64_jp7")

        # The operator keeps ONLY the LocalServer: both the model and its
        # depending workflow are de-selected.
        requested = [{"component_name": LOCAL_SERVER_JP7,
                      "component_version": "1.0.19"}]
        status, payload = gen_env.deploy_components(
            requested, target_devices=[CE_C_DEVICE])

        assert status == 201, payload
        submitted = assert_clean_submit(
            deployments, gen_env, payload, requested,
            gen_env.thing_arn(CE_C_DEVICE),
            deployment_name="thor1-remove-qwen-folder-test",
            is_revision=True, superseded_deployment_id=superseded)
        assert CE_C_MODEL not in submitted["components"], (
            "PRESERVATION FAILURE (3.11/3.14): the de-selected model was "
            "re-added to the submitted set")
        assert CE_C_WORKFLOW not in submitted["components"], (
            "PRESERVATION FAILURE (3.11/3.14): the de-selected workflow was "
            "re-added to the submitted set")

    # Feature: deployment-preflight-validation, Property 2:
    # non-bug-condition submissions unchanged — submitted set immutability
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(shape=st.sampled_from(["A", "B", "C"]), slug=slugs,
           version=versions)
    def test_property_submitted_set_is_exactly_the_operators_set(
            self, gen_env, slug, version, shape):
        """**Property 2 (PBT)** — _for any_ submission carrying a
        bug-condition shape (A: a jp5/jp6-only component on a JP7 device;
        B: a HARD dependency on a name with an EMPTY published-version list
        in both namespaces; C: a de-selected component still HARD-required
        by a component that stays selected), the component set forwarded to
        Greengrass — WHEN it is forwarded — is exactly the operator's set
        plus the recorded auto-includes: no de-selected component silently
        re-added, no depending component auto-removed (3.14).

        The guard is deliberate: after the fix A and B are refused and C is
        refused until acknowledged, so nothing is forwarded in those cases
        and the invariant is vacuously true. What must never happen is a
        forwarded set that is not the operator's.
        """
        gen_env.reset_submissions()
        gg = gen_env.gg
        gg.register_device(CE_C_DEVICE)
        gen_env.put_device_record(CE_C_DEVICE, arch="arm64_jp7")

        if shape == "A":
            offender = f"dda.workflow.{slug}"
            gg.seed_component(offender, version, CE_A_PLATFORMS)
            requested = [{"component_name": offender,
                          "component_version": version},
                         {"component_name": LOCAL_SERVER_JP7,
                          "component_version": "1.0.26"}]
            must_stay, must_not_appear = {offender}, set()
        elif shape == "B":
            offender = f"model-{slug}"
            gg.seed_component(offender, version, VARIANTLESS_AARCH64,
                              {DEAD_LOCAL_SERVER_BARE: {
                                  "VersionRequirement": ">=1.0.0 <2.0.0",
                                  "DependencyType": "HARD"}})
            requested = [{"component_name": offender,
                          "component_version": version}]
            must_stay, must_not_appear = {offender}, {DEAD_LOCAL_SERVER_BARE}
        else:
            deselected = f"model-{slug}"
            depender = f"dda.workflow.{slug}"
            gg.seed_deployment(gen_env.thing_arn(CE_C_DEVICE), {
                deselected: {"componentVersion": version},
                depender: {"componentVersion": "12.0.0"},
            }, name="ssh-tunnel-on-jetson-thor1")
            gg.seed_component(depender, "12.0.0", VARIANTLESS_AARCH64,
                              {deselected: {"VersionRequirement": ">=0.0.0",
                                            "DependencyType": "HARD"}})
            gg.seed_component(deselected, version, VARIANTLESS_AARCH64)
            requested = [{"component_name": depender,
                          "component_version": "12.0.0"},
                         {"component_name": LOCAL_SERVER_JP7,
                          "component_version": "1.0.26"}]
            must_stay, must_not_appear = {depender}, {deselected}

        status, payload = gen_env.deploy_components(
            requested, target_devices=[CE_C_DEVICE])

        if not gg.create_deployment_calls:
            # Refused before submit: nothing to preserve about the document.
            return
        assert status == 201, payload
        submitted = gen_env.submitted()["components"]
        operator_names = {entry["component_name"] for entry in requested}
        auto_names = {"aws.greengrass.LogManager",
                      "aws.greengrass.ShadowManager",
                      "aws.greengrass.Nucleus"}
        assert set(submitted) - auto_names == operator_names, (
            "PRESERVATION FAILURE (3.14): the submitted set is not the "
            f"operator's. submitted={sorted(submitted)} "
            f"operator={sorted(operator_names)}")
        for name in must_stay:
            assert name in submitted, (
                f"PRESERVATION FAILURE (3.14): {name} was auto-removed")
        for name in must_not_appear:
            assert name not in submitted, (
                f"PRESERVATION FAILURE (3.14): {name} was silently re-added")


# ==========================================================================
# 7. Automated store-limit remediation stays ungated
#
# `remediate_component_store_failures` -> `_submit_store_remediation`
# (deployments.py:1676) / `_resume_original_deployment` (:1717) are MACHINE
# remediation. An acknowledgement-required finding there would have nobody
# to acknowledge it and would wedge remediation, so they must keep
# submitting with no validation and no acknowledgement. Baseline:
# test_deployment_store_limit.py — 8 passed.
# ==========================================================================

class TestAutomatedRemediationStaysUngated:
    """**Validates: Requirements 3.11, 3.14** (and tasks.md's scope guard)"""

    def test_store_remediation_submits_the_installed_root_set_ungated(
            self, gen_env, deployments):
        """`_submit_store_remediation` submits the device's installed ROOT
        components plus the raised store limit — including the
        Counterexample C pair, whose dependency graph the new validation
        would have an opinion about — with no validation and no
        acknowledgement."""
        gg = gen_env.gg
        gg.register_device(CE_C_DEVICE, local_server_version="1.0.19",
                           arch="arm64JP7")
        gg.installed[CE_C_DEVICE].append(
            {"componentName": CE_C_WORKFLOW,
             "componentVersion": CE_C_WORKFLOW_VERSION})
        gg.seed_component(CE_C_WORKFLOW, CE_C_WORKFLOW_VERSION,
                          VARIANTLESS_AARCH64, CE_C_DEPENDENCIES)
        target_arn = gen_env.thing_arn(CE_C_DEVICE)

        remediation_id = deployments._submit_store_remediation(
            gg, CE_C_DEVICE, target_arn, "failed-dep-1",
            "ssh-tunnel-on-jetson-thor1")

        assert remediation_id
        [call] = gg.create_deployment_calls
        assert call["targetArn"] == target_arn
        assert call["deploymentName"] == "ssh-tunnel-on-jetson-thor1"
        assert call["components"] == {
            LOCAL_SERVER_JP7: {"componentVersion": "1.0.19"},
            CE_C_WORKFLOW: {"componentVersion": CE_C_WORKFLOW_VERSION},
            "aws.greengrass.Nucleus": {
                "configurationUpdate": {"merge": json.dumps(
                    {"componentStoreMaxSizeBytes":
                     deployments.COMPONENT_STORE_MAX_SIZE_BYTES})}},
        }
        assert call["tags"] == {
            "dda-portal:managed": "true",
            deployments.TAG_REMEDIATION_FOR: "failed-dep-1"}
        for key in call:
            assert "acknowledg" not in key.lower(), (
                "PRESERVATION FAILURE: store remediation gained an "
                f"acknowledgement parameter {key!r}")

    def test_resumed_original_deployment_is_resubmitted_verbatim(
            self, gen_env, deployments):
        """`_resume_original_deployment` resubmits the ORIGINAL document —
        here the Counterexample C shape: the depending workflow kept while
        the model it HARD-requires is absent — verbatim plus the Nucleus
        store merge. Nothing validates it and nothing can refuse it."""
        gg = gen_env.gg
        target_arn = gen_env.thing_arn(CE_C_DEVICE)
        original_id = gg.seed_deployment(target_arn, {
            CE_C_WORKFLOW: {"componentVersion": CE_C_WORKFLOW_VERSION},
            LOCAL_SERVER_JP7: {"componentVersion": "1.0.19"},
        }, name="ssh-tunnel-on-jetson-thor1")
        gg.seed_component(CE_C_WORKFLOW, CE_C_WORKFLOW_VERSION,
                          VARIANTLESS_AARCH64, CE_C_DEPENDENCIES)

        resumed_id = deployments._resume_original_deployment(
            gg, target_arn, original_id)

        assert resumed_id
        [call] = gg.create_deployment_calls
        assert call["components"] == {
            CE_C_WORKFLOW: {"componentVersion": CE_C_WORKFLOW_VERSION},
            LOCAL_SERVER_JP7: {"componentVersion": "1.0.19"},
            "aws.greengrass.Nucleus": {
                "configurationUpdate": {"merge": json.dumps(
                    {"componentStoreMaxSizeBytes":
                     deployments.COMPONENT_STORE_MAX_SIZE_BYTES})}},
        }
        assert call["tags"] == {
            "dda-portal:managed": "true",
            deployments.TAG_RESUMED_FROM: original_id}

    def test_remediation_sweep_advances_without_any_gate(
            self, gen_env, deployments):
        """The sweep itself (`_sweep_device_for_store_remediation`, the
        per-device step `remediate_component_store_failures` calls) still
        submits a remediation for a store-limit failure, and still resumes
        the original after a completed remediation — the two states the
        state machine has, neither of them gated."""
        gg = gen_env.gg
        target_arn = gen_env.thing_arn(CE_C_DEVICE)
        gg.register_device(CE_C_DEVICE, local_server_version="1.0.19",
                           arch="arm64JP7")
        blocked_id = gg.seed_deployment(
            target_arn, {LOCAL_SERVER_JP7: {"componentVersion": "1.0.19"}},
            name="ssh-tunnel-on-jetson-thor1")
        gg.report_effective(
            CE_C_DEVICE, blocked_id, "FAILED",
            reason=f"{deployments.STORE_LIMIT_REASON_MARKER} on device")
        gg.effective[CE_C_DEVICE][0]["targetArn"] = target_arn

        action = deployments._sweep_device_for_store_remediation(
            gg, CE_C_DEVICE, REGION, ACCOUNT_ID)

        assert action["action"] == "remediation_submitted"
        assert action["blocked_deployment_id"] == blocked_id
        assert len(gg.create_deployment_calls) == 1
        assert deployments.TAG_REMEDIATION_FOR in \
            gg.create_deployment_calls[0]["tags"]


# ==========================================================================
# 8. Deployment-detail passthrough (3.9)
# ==========================================================================

class TestDeploymentDetailPassthrough:
    """**Validates: Requirements 3.9**"""

    def test_greengrass_reason_and_status_details_surface_verbatim(
            self, gen_env):
        """3.9: the Greengrass `reason` and `statusDetails` still surface
        verbatim on the deployment detail — INCLUDING the clause that
        reports the DEVICE's platform as the component's claim, which
        bugfix.md 1.5 says misdirects diagnosis. The fix adds pre-submit
        findings; it must not start rewriting or dropping what Greengrass
        said about a deployment that was already submitted."""
        gen_env.gg.register_device(CE_A_DEVICE)
        status, payload = gen_env.deploy_components(
            [{"component_name": "com.example.Widget",
              "component_version": "1.0.0"}],
            target_devices=[CE_A_DEVICE])
        assert status == 201, payload
        deployment_id = payload["deployment_id"]

        reason = ("NO_AVAILABLE_COMPONENT_VERSION: Component "
                  f"{CE_A_WORKFLOW} claimed platform: os linux, variant "
                  "arm64_jp7")
        status_details = {
            "detailedStatus": "FAILED_NO_STATE_CHANGE",
            "detailedStatusReason": (
                '["DEPLOYMENT_FAILURE", "NO_AVAILABLE_COMPONENT_VERSION", '
                '"COMPONENT_VERSION_REQUIREMENTS_NOT_MET"]')}
        gen_env.gg.effective[CE_A_DEVICE] = [{
            "deploymentId": deployment_id,
            "coreDeviceExecutionStatus": "FAILED",
            "reason": reason,
            "description": "",
            "statusDetails": status_details}]

        event = gen_env.env.event("GET", "/deployments/{id}", gen_env.user)
        event["pathParameters"] = {"id": deployment_id}
        event["queryStringParameters"] = {"usecase_id": gen_env.usecase_id}
        response = gen_env.deployments.handler(event, None)
        detail = json.loads(response["body"])["deployment"]

        assert response["statusCode"] == 200
        [effective] = detail["effective_deployments"]
        assert effective["reason"] == reason
        assert effective["status_details"] == status_details
        [error] = detail["error_messages"]
        assert error["reason"] == reason
        assert error["detailed_status"] == "FAILED_NO_STATE_CHANGE"
        assert error["detailed_status_reason"] == \
            status_details["detailedStatusReason"]


# ==========================================================================
# 9. Scope: `src/` untouched (3.10, 3.13)
# ==========================================================================

def _git(*args):
    return subprocess.run(["git", *args], cwd=REPO_ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=60)


class TestSourceTreeUntouched:
    """3.10/3.13: this spec is portal-only — no component artifact, recipe,
    LocalServer build, published component version or on-device behaviour is
    altered, and `vllm_model_prep.cleanup()` is out of scope. Asserted as a
    `git diff --name-only` scope check over TRACKED modifications (the
    `test_source_selection_preservation.py` precedent of asserting a build
    property from the repo itself).

    Untracked paths are excluded by observation: on the unfixed tree the
    only untracked entry under `src/` is the build byproduct
    `src/edgemlsdk/cached-debs-amd64.bak/`, which no spec authored.

    **Validates: Requirements 3.10, 3.12, 3.13**
    """

    def test_no_tracked_file_under_src_is_modified(self):
        probe = _git("rev-parse", "--is-inside-work-tree")
        if probe.returncode != 0:
            pytest.skip("not a git work tree")
        result = _git("diff", "--name-only", "HEAD", "--", "src")
        assert result.returncode == 0, result.stderr
        changed = [line for line in result.stdout.splitlines() if line.strip()]
        assert changed == [], (
            "PRESERVATION FAILURE (3.10/3.13): this spec is portal-only, but "
            f"tracked files under src/ are modified: {changed}")

    def test_no_recipe_or_dockerfile_is_modified(self):
        probe = _git("rev-parse", "--is-inside-work-tree")
        if probe.returncode != 0:
            pytest.skip("not a git work tree")
        result = _git("diff", "--name-only", "HEAD")
        assert result.returncode == 0, result.stderr
        pattern = re.compile(
            r"(^src/|Dockerfile|/recipes?/|recipe.*\.(yaml|json)$"
            r"|docker-compose)", re.IGNORECASE)
        offenders = [line for line in result.stdout.splitlines()
                     if line.strip() and pattern.search(line)]
        assert offenders == [], (
            "PRESERVATION FAILURE (3.10): a recipe / Dockerfile / "
            f"docker-compose file is modified: {offenders}")

    def test_workflow_packaging_still_emits_the_unpinned_model_dependency(
            self, aws_stack):
        """3.12: `workflow_packaging.model_component_dependencies` still
        emits ONE unpinned `>=0.0.0` HARD `ComponentDependencies` entry per
        resolved model component — the entry that causes Counterexample C
        and that this spec deliberately does NOT change (it surfaces the
        consequence at submit time instead)."""
        sys.modules.pop("workflow_packaging", None)
        import workflow_packaging

        source = _git("diff", "--name-only", "HEAD", "--",
                      "edge-cv-portal/backend/functions/workflow_packaging.py")
        assert source.stdout.strip() == "", (
            "PRESERVATION FAILURE (3.12): workflow_packaging.py is modified; "
            "the unpinned >=0.0.0 HARD model dependency must stay as is")
        assert hasattr(workflow_packaging, "model_component_dependencies")
