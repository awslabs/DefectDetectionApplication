"""Preservation property tests (Task 2) for edge-deploy-reliability.

Property 7: Preservation — Existing packaging output unchanged apart from
added dependencies.

**Validates: Requirements 3.3, 3.8**

Observation-first (observed on UNFIXED workflow_packaging.py and asserted
here as the golden contract): for any packaged workflow, every field of the
registered ``dda.workflow.*`` recipe EXCEPT ComponentDependencies is fully
determined by (workflow_id, workflow_version, component_version, bucket,
selected architectures) — RecipeFormatVersion, ComponentName/Version/Type,
Publisher, ComponentConfiguration, the per-arch Manifests (platform
attributes, one-shot Run lifecycle, artifact URIs) and the empty top-level
Lifecycle. And whenever the workflow uses Custom_Node_Type plugins, the
``dda.plugin.*`` ComponentDependencies entries emitted by
``plugin_component_dependencies`` (names, pinned VersionRequirements, HARD
type) appear byte-identical in the recipe.

The assertions are deliberately structured to hold BOTH pre-fix and
post-fix (the Defect C fix, task 3.3, adds model-* and LocalServer entries
to ComponentDependencies via the packaging handler and changes nothing
else): every non-ComponentDependencies field is compared against an
independently-computed expectation, and the plugin entries are compared as
an exact restriction (``{k: v for k in deps if k.startswith('dda.plugin.')}``)
— never asserting that ComponentDependencies is empty or has no other
entries.

Harness: the moto-backed packaging stack from conftest (``aws_stack`` +
``env``), the training-jobs Model_Registry table seeded with published
records (same shape as test_workflow_packaging_dependencies_exploration.py,
so the post-fix resolution path has real records), and the
CustomPluginPackagingEnv harness from test_workflow_packaging_custom_plugins
for the plugin-passthrough scenario. The pure ``build_recipe`` contract is
additionally property-tested with Hypothesis across random ids, versions,
arch subsets, and dda.plugin.* dependency dicts.
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION
from test_workflow_packaging_custom_plugins import (
    CustomPluginPackagingEnv,
)

TRAINING_JOBS_TABLE_NAME = (
    "test-edge-deploy-reliability-preservation-training-jobs")

LLM_MODEL_NAME = "opt125m-smoke"
LLM_MODEL_COMPONENT = "model-vllm-opt125m-smoke"
VISION_MODEL_NAME = "defect-model"
VISION_MODEL_COMPONENT = "model-defect-model"

PLUGIN_PREFIX = "dda.plugin."

#: Independently-recorded platform mapping (golden, not imported from the
#: module under test): workflow_core arch id -> Greengrass architecture.
ARCH_TO_GG_PLATFORM = {
    "x86_64": "amd64",
    "x86_64_nvidia": "amd64",
    "arm64_jp4": "aarch64",
    "arm64_jp5": "aarch64",
    "arm64_jp6": "aarch64",
}


# --------------------------------------------------------------------------
# The golden recipe contract (recorded from UNFIXED build_recipe output)
# --------------------------------------------------------------------------

def expected_manifest_order(archs):
    """Golden ordering: sorted, except plain x86_64 listed directly after
    x86_64_nvidia (both map to amd64; the nvidia manifest must match
    first)."""
    ordered = sorted(archs)
    if "x86_64" in ordered and "x86_64_nvidia" in ordered:
        ordered.remove("x86_64")
        ordered.insert(ordered.index("x86_64_nvidia") + 1, "x86_64")
    return ordered


def expected_recipe_modulo_dependencies(workflow_id, workflow_version,
                                        component_version, bucket, archs):
    """Every field of the registered recipe EXCEPT ComponentDependencies,
    computed independently of workflow_packaging.py — the stable contract
    observed on the unfixed tree."""
    install_dir = "/aws_dda/workflows/{}/{}".format(
        workflow_id, workflow_version)
    arm_archs = [a for a in archs if ARCH_TO_GG_PLATFORM[a] == "aarch64"]
    disambiguate_arm = len(arm_archs) > 1

    manifests = []
    for arch in expected_manifest_order(archs):
        platform = {"os": "linux", "architecture": ARCH_TO_GG_PLATFORM[arch]}
        if disambiguate_arm and ARCH_TO_GG_PLATFORM[arch] == "aarch64":
            platform["variant"] = arch
        elif arch == "x86_64_nvidia":
            platform["runtime"] = "nvidia"
        manifests.append({
            "Platform": platform,
            "Lifecycle": {
                # The Run script opens with the stale-workflow-registrations
                # (2.1) cleanup prefix: best-effort rm -rf of the workflow's
                # staging root (joined by ';' so failure never blocks
                # staging) before the mandatory mkdir && cp staging chain.
                # Deliberately NO Shutdown step: Greengrass runs Shutdown
                # ~10ms after a one-shot Run exits 0 (verified on device),
                # which deleted the freshly staged artifacts on every
                # deploy when the first fix tried a Shutdown cleanup.
                "Run": {
                    "Script": (
                        "rm -rf /aws_dda/workflows/{wf} 2>/dev/null; "
                        "mkdir -p {install} && cp -r "
                        "{{artifacts:decompressedPath}}/workflow-{arch}/. "
                        "{install}/".format(wf=workflow_id,
                                            install=install_dir, arch=arch)),
                    "Timeout": 300,
                    "requiresPrivilege": True,
                },
            },
            "Artifacts": [{
                "Uri": ("s3://{bucket}/workflows/components/{wf}/{wfv}/{cv}/"
                        "{arch}/workflow-{arch}.zip".format(
                            bucket=bucket, wf=workflow_id,
                            wfv=workflow_version, cv=component_version,
                            arch=arch)),
                "Unarchive": "ZIP",
                "Permission": {"Read": "ALL"},
            }],
        })

    return {
        "RecipeFormatVersion": "2020-01-25",
        "ComponentName": "dda.workflow.{}".format(workflow_id),
        "ComponentVersion": component_version,
        "ComponentType": "aws.greengrass.generic",
        "ComponentPublisher": "DDA Portal Workflow Manager",
        "ComponentConfiguration": {
            "DefaultConfiguration": {
                "WorkflowId": workflow_id,
                "WorkflowVersion": str(workflow_version),
            },
        },
        "Manifests": manifests,
        "Lifecycle": {},
    }


def recipe_modulo_dependencies(recipe):
    return {k: v for k, v in recipe.items() if k != "ComponentDependencies"}


def plugin_entries(recipe):
    """The recipe's ComponentDependencies restricted to dda.plugin.*."""
    deps = recipe.get("ComponentDependencies") or {}
    return {name: entry for name, entry in deps.items()
            if name.startswith(PLUGIN_PREFIX)}


# --------------------------------------------------------------------------
# Fixtures / harness
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging_env(aws_stack):
    """The training-jobs Model_Registry table (production GSI shape) plus a
    freshly imported workflow_packaging bound to it inside moto — the same
    setup as the task-1 exploration test, so the fixed resolution path has
    real published records to resolve against."""
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


def make_deployable_greengrass():
    gg = MagicMock(name="greengrassv2")
    gg.create_component_version.return_value = {
        "arn": ("arn:aws:greengrass:us-east-1:123456789012:"
                f"components:test:versions:{uuid.uuid4()}")
    }
    gg.describe_component.return_value = {
        "status": {"componentState": "DEPLOYABLE", "message": "simulated"}
    }
    return gg


def llm_workflow_definition():
    """folder_source -> model_inference -> llm_inference -> mqtt_publish:
    binds two model refs; compiles for arm64_jp6 with no plugin deps."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
             "parameters": {"location": "/data/images"}},
            {"id": "inf", "type": "model_inference", "position": {"x": 200, "y": 0},
             "parameters": {"modelName": VISION_MODEL_NAME}},
            {"id": "llm", "type": "llm_inference", "position": {"x": 400, "y": 0},
             "parameters": {"modelName": LLM_MODEL_NAME,
                            "prompt_template": "Summarize: {confidence}"}},
            {"id": "pub", "type": "mqtt_publish", "position": {"x": 600, "y": 0},
             "parameters": {"topic": "results", "broker_host": "localhost"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "src", "port": "out"},
             "to": {"node": "inf", "port": "in"}},
            {"id": "c2", "from": {"node": "inf", "port": "out"},
             "to": {"node": "llm", "port": "in"}},
            {"id": "c3", "from": {"node": "llm", "port": "out"},
             "to": {"node": "pub", "port": "in"}},
        ],
    }


def no_model_workflow_definition():
    """folder_source -> dewarp -> capture: no model_ref parameters; dewarp
    pulls the curated dda-dewarp plugin inline on every arch."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
             "parameters": {"location": "/data/images"}},
            {"id": "dw", "type": "dewarp", "position": {"x": 200, "y": 0},
             "parameters": {}},
            {"id": "cap", "type": "capture", "position": {"x": 400, "y": 0},
             "parameters": {"output_path": "/out"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "src", "port": "out"},
             "to": {"node": "dw", "port": "in"}},
            {"id": "c2", "from": {"node": "dw", "port": "out"},
             "to": {"node": "cap", "port": "in"}},
        ],
    }


class PreservationPackagingEnv:
    """Packaging harness (exploration-test pattern): a validated workflow
    version, a Use_Case with an S3 bucket, seeded model registry records,
    an optional curated plugin library, and patched Use_Case clients."""

    def __init__(self, env, packaging_env, monkeypatch, definition,
                 curated_archs=()):
        self.env = env
        self.packaging = packaging_env.packaging
        monkeypatch.setattr(self.packaging, "COMPONENT_STATUS_POLL_SECONDS", 0)

        # Per-test curated plugin library prefix (atomicity-test pattern).
        self.curated_prefix = f"workflow-plugins-{uuid.uuid4()}"
        monkeypatch.setattr(self.packaging,
                            "WORKFLOW_PLUGIN_LIBRARY_PREFIX",
                            self.curated_prefix)
        for arch in curated_archs:
            env.s3.put_object(
                Bucket=env.bucket,
                Key=f"{self.curated_prefix}/{arch}/dda-dewarp.so",
                Body=b"\x7fELF fake curated plugin " + arch.encode(),
            )

        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_bucket = f"usecase-bucket-{uuid.uuid4()}"
        env.s3.create_bucket(Bucket=self.usecase_bucket)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Edge Deploy Reliability Preservation",
            "account_id": "123456789012",
            "s3_bucket": self.usecase_bucket,
        })

        # Published Model_Registry records (greengrass_publish shape) so the
        # post-fix resolution path resolves instead of failing closed.
        packaging_env.training_table.put_item(Item={
            "training_id": f"tr-{uuid.uuid4()}",
            "usecase_id": self.usecase_id,
            "model_name": LLM_MODEL_NAME,
            "model_type": "vllm",
            "created_at": 1,
            "published_component": {
                "component_name": LLM_MODEL_COMPONENT,
                "component_version": "1.0.0",
                "runtime": "vllm",
                "supported_architectures": ["arm64_jp6"],
            },
        })
        packaging_env.training_table.put_item(Item={
            "training_id": f"tr-{uuid.uuid4()}",
            "usecase_id": self.usecase_id,
            "model_name": VISION_MODEL_NAME,
            "model_type": "anomaly_detection",
            "created_at": 1,
            "published_component": {
                "component_name": VISION_MODEL_COMPONENT,
                "component_version": "1.0.0",
            },
        })

        status, payload = env.invoke("POST", "/workflows", self.user, body={
            "usecase_id": self.usecase_id,
            "name": "preservation workflow",
            "definition": definition,
        })
        assert status == 201, payload
        self.workflow_id = payload["workflow"]["workflow_id"]

        env.stack.tables.versions.update_item(
            Key={"workflow_id": self.workflow_id, "version": 1},
            UpdateExpression="SET validation_status = :v",
            ExpressionAttributeValues={
                ":v": {"status": "passed", "validated_at": 1,
                       "findings_key": "findings/none.json"},
            },
        )

        self.greengrass = make_deployable_greengrass()

        def fake_get_usecase_client(service_name, usecase, session_name=None,
                                    region=None):
            if service_name == "s3":
                return env.s3
            if service_name == "greengrassv2":
                return self.greengrass
            raise AssertionError(f"unexpected usecase client: {service_name}")

        monkeypatch.setattr(self.packaging, "get_usecase_client",
                            fake_get_usecase_client)

    def package(self, architectures):
        event = self.env.event(
            "POST", "/workflows/{id}/package", self.user,
            workflow_id=self.workflow_id,
            body={"architectures": architectures},
        )
        response = self.packaging.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def registered_recipe(self):
        assert self.greengrass.create_component_version.called, (
            "no component version was registered")
        call = self.greengrass.create_component_version.call_args
        return json.loads(call.kwargs["inlineRecipe"])


def assert_stable_contract(recipe, workflow_id, usecase_bucket, archs,
                           workflow_version=1, component_version="1.0.0"):
    expected = expected_recipe_modulo_dependencies(
        workflow_id, workflow_version, component_version, usecase_bucket,
        archs)
    actual = recipe_modulo_dependencies(recipe)
    assert actual == expected, (
        "PRESERVATION REGRESSION (Property 7): the registered recipe "
        "changed outside ComponentDependencies for archs {}".format(archs))


# --------------------------------------------------------------------------
# End-to-end packaging scenarios (handler-level invariants)
# --------------------------------------------------------------------------

class TestRecipeEqualityModuloComponentDependencies:

    def test_model_ref_workflow_arm64_jp6_stable_contract(
            self, env, packaging_env, monkeypatch):
        """A workflow binding model refs, packaged for arm64_jp6: every
        recipe field except ComponentDependencies matches the recorded
        contract, and no dda.plugin.* entry appears (the workflow uses no
        Custom_Node_Type plugins) — pre-fix AND post-fix.

        Validates: Requirements 3.3, 3.8
        """
        penv = PreservationPackagingEnv(
            env, packaging_env, monkeypatch, llm_workflow_definition())
        status, payload = penv.package(["arm64_jp6"])
        assert status == 201, payload

        recipe = penv.registered_recipe()
        assert_stable_contract(recipe, penv.workflow_id,
                               penv.usecase_bucket, ["arm64_jp6"])
        assert plugin_entries(recipe) == {}, (
            "PRESERVATION REGRESSION (Property 7): dda.plugin.* entries "
            "appeared for a workflow using no Custom_Node_Type plugins")

    def test_no_model_workflow_amd64_flavors_stable_contract(
            self, env, packaging_env, monkeypatch):
        """A model-free workflow packaged for both amd64 flavors: the
        stable contract holds, including the x86_64_nvidia-before-x86_64
        manifest ordering and the 'runtime: nvidia' platform attribute.

        Validates: Requirements 3.3, 3.8
        """
        archs = ["x86_64_nvidia", "x86_64"]
        penv = PreservationPackagingEnv(
            env, packaging_env, monkeypatch, no_model_workflow_definition(),
            curated_archs=archs)
        status, payload = penv.package(archs)
        assert status == 201, payload

        recipe = penv.registered_recipe()
        assert_stable_contract(recipe, penv.workflow_id,
                               penv.usecase_bucket, archs)
        assert plugin_entries(recipe) == {}

    def test_no_model_workflow_multi_arm_variant_stable_contract(
            self, env, packaging_env, monkeypatch):
        """A model-free workflow packaged for two arm64 JetPack variants:
        the stable contract holds, including the per-manifest 'variant'
        platform attribute that disambiguates the aarch64 manifests.

        Validates: Requirements 3.3, 3.8
        """
        archs = ["arm64_jp5", "arm64_jp6"]
        penv = PreservationPackagingEnv(
            env, packaging_env, monkeypatch, no_model_workflow_definition(),
            curated_archs=archs)
        status, payload = penv.package(archs)
        assert status == 201, payload

        recipe = penv.registered_recipe()
        assert_stable_contract(recipe, penv.workflow_id,
                               penv.usecase_bucket, archs)
        assert plugin_entries(recipe) == {}

    def test_custom_plugin_entries_pass_through_byte_identical(
            self, env, packaging_env, monkeypatch):
        """A workflow using a Custom_Node_Type plugin: the dda.plugin.*
        restriction of the recipe's ComponentDependencies equals EXACTLY
        the pinned HARD entry plugin_component_dependencies emits today
        (name, pinned VersionRequirement, HARD type) — byte-identical
        passthrough pre-fix and post-fix — and every other recipe field
        matches the stable contract.

        Validates: Requirements 3.8
        """
        cenv = CustomPluginPackagingEnv(
            env, packaging_env.packaging, monkeypatch)
        record = cenv.seed_plugin_record()
        type_id, _ = cenv.register_node_type(record)
        cenv.create_workflow(type_id)
        gg = cenv.patch_usecase_clients(make_deployable_greengrass())

        status, payload = cenv.package(["x86_64"])
        assert status == 201, payload

        recipe = json.loads(
            gg.create_component_version.call_args.kwargs["inlineRecipe"])

        # Golden plugin entry recorded from the unfixed
        # plugin_component_dependencies output: pinned to the recorded
        # Plugin_Record version, HARD.
        expected_plugin_entries = {
            f"dda.plugin.{record['plugin_id']}": {
                "VersionRequirement": ">=1.0.0 <2.0.0",
                "DependencyType": "HARD",
            },
        }
        assert plugin_entries(recipe) == expected_plugin_entries, (
            "PRESERVATION REGRESSION (Property 7/3.8): the existing "
            "dda.plugin.* ComponentDependencies entries did not pass "
            "through byte-identical")
        assert_stable_contract(recipe, cenv.workflow_id,
                               cenv.usecase_bucket, ["x86_64"])


# --------------------------------------------------------------------------
# Pure build_recipe contract (Hypothesis)
# --------------------------------------------------------------------------

_ARCHS = sorted(ARCH_TO_GG_PLATFORM)

_workflow_ids = st.uuids().map(lambda u: f"wf-{u}")
_versions = st.integers(min_value=1, max_value=50)
_arch_sets = st.sets(st.sampled_from(_ARCHS), min_size=1)
_plugin_entry = st.integers(min_value=1, max_value=9).map(
    lambda major: {
        "VersionRequirement": f">={major}.0.0 <{major + 1}.0.0",
        "DependencyType": "HARD",
    })
_plugin_deps = st.dictionaries(
    keys=st.uuids().map(lambda u: f"{PLUGIN_PREFIX}plg-{u}"),
    values=_plugin_entry,
    max_size=4,
)


class TestBuildRecipePureContract:

    @settings(max_examples=25, deadline=None)
    @given(workflow_id=_workflow_ids, workflow_version=_versions,
           archs=_arch_sets, dependencies=_plugin_deps)
    def test_build_recipe_stable_fields_and_dependency_passthrough(
            self, packaging_env, workflow_id, workflow_version, archs,
            dependencies):
        """Property 7 (pure seam): for ANY workflow id, version, arch
        subset, and dda.plugin.* dependency dict, build_recipe emits the
        recorded stable contract in every non-ComponentDependencies field,
        and passes a non-empty dependency dict through byte-identical
        (omitting the key entirely when none are supplied) — exactly the
        unfixed behavior, which task 3.3 keeps (the fix merges new entries
        in the handler, not in build_recipe).

        Validates: Requirements 3.3, 3.8
        """
        component_version = f"{workflow_version}.0.0"
        bucket = "usecase-bucket-property"
        final_keys = {
            arch: ("workflows/components/{wf}/{wfv}/{cv}/{arch}/"
                   "workflow-{arch}.zip".format(
                       wf=workflow_id, wfv=workflow_version,
                       cv=component_version, arch=arch))
            for arch in archs
        }

        recipe = packaging_env.packaging.build_recipe(
            workflow_id, workflow_version, bucket, final_keys,
            component_dependencies=dependencies or None)

        assert recipe_modulo_dependencies(recipe) == \
            expected_recipe_modulo_dependencies(
                workflow_id, workflow_version, component_version, bucket,
                archs)

        if dependencies:
            assert recipe["ComponentDependencies"] == dependencies, (
                "PRESERVATION REGRESSION (Property 7): supplied "
                "ComponentDependencies did not pass through byte-identical")
            assert plugin_entries(recipe) == dependencies
        else:
            assert "ComponentDependencies" not in recipe
