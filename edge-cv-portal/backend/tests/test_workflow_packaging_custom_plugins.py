"""
Unit tests for Component_Packager custom-plugin integration
(functions/workflow_packaging.py, custom-node-designer task 10.1).

Covers packaging of workflows containing Custom_Node_Types against the
merged catalog resolving pinned Custom_Node_Type versions:

- custom: plugin dependencies are delivered by a Greengrass
  ComponentDependencies entry on dda.plugin.{pluginId} pinned to the
  recorded Plugin_Record version and are NEVER bundled inline, while
  curated plugins keep the inline plugins/{arch}/*.so bundling
  (Requirements 11.1, 16.4);
- per-plugin pluginChecksums / pluginComponents are written into each
  arch manifest.json (Requirements 10.4, 10.6);
- packaging gates: dev lifecycle state, missing per-arch Plugin_Artifact,
  and missing Plugin_Component version reject with the Custom_Node_Type
  and arch/state identified (Requirements 11.2, 11.3);
- artifact verification: streamed SHA-256 recompute + KMS signature
  verification failing via the existing PackagingError path — stage
  cleanup, no partial component (Requirement 10.4);
- x86_64_nvidia in ARCH_TO_GG_PLATFORM with the 'runtime: nvidia'
  platform attribute and the plain x86_64 manifest ordered after
  x86_64_nvidia (design: Target_Architecture x86_64_nvidia).

Runs against the moto-backed stack from conftest.py. Property-based
coverage of the same logic lands with tasks 10.2-10.4.
"""
import base64
import hashlib
import io
import json
import os
import sys
import uuid
import zipfile
from unittest.mock import MagicMock

import pytest

from conftest import TEST_ENV

STAGING_ROOT = "workflows/staging"
CURATED_PLUGIN_PREFIX = "workflow-plugins"


@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Import workflow_packaging inside the moto mock so its module-level
    boto3 clients (DynamoDB / S3 / KMS) are intercepted."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


def make_deployable_greengrass(component_state="DEPLOYABLE"):
    gg = MagicMock(name="greengrassv2")
    gg.create_component_version.return_value = {
        "arn": f"arn:aws:greengrass:us-east-1:123456789012:components:test:versions:{uuid.uuid4()}"
    }
    gg.describe_component.return_value = {
        "status": {"componentState": component_state, "message": "simulated"}
    }
    return gg


class CustomPluginPackagingEnv:
    """Packaging harness with a registered Custom_Node_Type backed by a
    Plugin_Record whose artifacts are genuinely KMS-signed in moto."""

    def __init__(self, env, packaging, monkeypatch):
        self.env = env
        self.packaging = packaging
        self.monkeypatch = monkeypatch
        self.s3 = env.s3
        self.kms = env.stack.kms
        self.bucket = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]
        self.signing_key = os.environ["PLUGIN_SIGNING_KEY_ARN"]

        monkeypatch.setattr(packaging, "COMPONENT_STATUS_POLL_SECONDS", 0)

        # Per-test curated plugin library prefix (as in the atomicity tests)
        self.curated_prefix = f"{CURATED_PLUGIN_PREFIX}-{uuid.uuid4()}"
        monkeypatch.setattr(
            packaging, "WORKFLOW_PLUGIN_LIBRARY_PREFIX", self.curated_prefix)

        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_bucket = f"usecase-bucket-{uuid.uuid4()}"
        self.s3.create_bucket(Bucket=self.usecase_bucket)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Custom Plugin Packaging Test",
            "account_id": "123456789012",
            "s3_bucket": self.usecase_bucket,
        })

    # ------------------------------------------------------------- setup
    def sign(self, data: bytes) -> str:
        response = self.kms.sign(
            KeyId=self.signing_key,
            Message=hashlib.sha256(data).digest(),
            MessageType="DIGEST",
            SigningAlgorithm="ECDSA_SHA_256",
        )
        return base64.b64encode(response["Signature"]).decode("ascii")

    def seed_plugin_record(self, archs=("x86_64",), lifecycle_state="test",
                           name="blur-regions", component_registered=True):
        """A Plugin_Record version with KMS-signed Plugin_Library artifacts
        and (optionally) a registered Plugin_Component pointer."""
        plugin_id = f"plg-{uuid.uuid4()}"
        artifacts = {}
        for arch in archs:
            data = f"\x7fELF {name} {arch}".encode()
            key = (f"workflow-plugins/custom/{self.usecase_id}/{arch}/"
                   f"{name}.so")
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)
            artifacts[arch] = {
                "buildStatus": "succeeded",
                "s3Key": key,
                "checksum": hashlib.sha256(data).hexdigest(),
                "signature": self.sign(data),
                "logTail": "",
            }
        component = {}
        if component_registered:
            component = {
                "name": f"dda.plugin.{plugin_id}",
                "version": "1.0.0",
                "arn": f"arn:aws:greengrass:us-east-1:123456789012:components:dda.plugin.{plugin_id}:versions:1.0.0",
                "architectures": sorted(archs),
                "status": "registered",
            }
        record = {
            "plugin_id": plugin_id,
            "version": 1,
            "usecase_id": self.usecase_id,
            "name": name,
            "lifecycle_state": lifecycle_state,
            "artifacts": artifacts,
            "component": component,
            "created_at": 1,
        }
        self.env.stack.tables.plugin_records.put_item(Item=record)
        return record

    def register_node_type(self, record, archs=("x86_64",)):
        """A CustomNodeTypes item whose declaration mappings carry the
        custom:{usecase}/{name} plugin dependency (as registration does)."""
        type_id = f"custom.{record['name']}-{uuid.uuid4().hex[:8]}"
        dependency = f"custom:{self.usecase_id}/{record['name']}"
        declaration = {
            "typeId": type_id,
            "category": "preprocessing",
            "displayName": "Blur Regions",
            "inputs": [{"name": "in", "portType": "VideoFrames"}],
            "outputs": [{"name": "out", "portType": "VideoFrames"}],
            "parameters": [{
                "name": "radius", "paramType": "int", "required": True,
                "default": 5, "constraints": {"min": 1, "max": 64},
                "description": "Blur kernel radius in pixels",
                "examples": [5, 9],
            }],
            "mappings": [{
                "arch": arch,
                "elementChain": [{"factory": "blurregions",
                                  "argsTemplate": {"radius": "{radius}"}}],
                "pluginDependencies": [dependency],
            } for arch in archs],
            "hardwareDependent": False,
        }
        self.env.stack.tables.custom_node_types.put_item(Item={
            "node_type_id": type_id,
            "version": 1,
            "usecase_id": self.usecase_id,
            "usecase_ids": [],
            "plugin_id": record["plugin_id"],
            "plugin_version": record["version"],
            "declaration": declaration,
            "deprecated": False,
            "created_at": 1,
        })
        return type_id, dependency

    def seed_curated_library(self, archs):
        for arch in archs:
            self.s3.put_object(
                Bucket=self.env.bucket,
                Key=f"{self.curated_prefix}/{arch}/dda-dewarp.so",
                Body=b"\x7fELF fake curated plugin " + arch.encode(),
            )

    def create_workflow(self, type_id, include_dewarp=False):
        nodes = [
            {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
             "parameters": {"location": "/data/images"}},
            {"id": "blur", "type": type_id, "position": {"x": 200, "y": 0},
             "parameters": {"radius": 5}},
        ]
        connections = [
            {"id": "c1", "from": {"node": "src", "port": "out"},
             "to": {"node": "blur", "port": "in"}},
        ]
        previous = "blur"
        if include_dewarp:
            nodes.append({"id": "dw", "type": "dewarp",
                          "position": {"x": 400, "y": 0}, "parameters": {}})
            connections.append({"id": "c2",
                                "from": {"node": "blur", "port": "out"},
                                "to": {"node": "dw", "port": "in"}})
            previous = "dw"
        nodes.append({"id": "cap", "type": "capture",
                      "position": {"x": 600, "y": 0},
                      "parameters": {"output_path": "/out"}})
        connections.append({"id": "c3",
                            "from": {"node": previous, "port": "out"},
                            "to": {"node": "cap", "port": "in"}})
        definition = {"schemaVersion": 1, "nodes": nodes,
                      "connections": connections}

        status, payload = self.env.invoke("POST", "/workflows", self.user, body={
            "usecase_id": self.usecase_id,
            "name": "custom plugin workflow",
            "definition": definition,
        })
        assert status == 201, payload
        self.workflow_id = payload["workflow"]["workflow_id"]

        # Record a passed Workflow_Validator run so packaging is allowed.
        self.env.stack.tables.versions.update_item(
            Key={"workflow_id": self.workflow_id, "version": 1},
            UpdateExpression="SET validation_status = :v",
            ExpressionAttributeValues={
                ":v": {"status": "passed", "validated_at": 1,
                       "findings_key": "findings/none.json"},
            },
        )
        return self.workflow_id

    def patch_usecase_clients(self, greengrass=None):
        greengrass = greengrass or make_deployable_greengrass()
        self.greengrass = greengrass

        def fake_get_usecase_client(service_name, usecase, session_name=None,
                                    region=None):
            if service_name == "s3":
                return self.s3
            if service_name == "greengrassv2":
                return greengrass
            raise AssertionError(f"unexpected usecase client: {service_name}")

        self.monkeypatch.setattr(
            self.packaging, "get_usecase_client", fake_get_usecase_client)
        return greengrass

    # ------------------------------------------------------------ invoke
    def package(self, architectures):
        event = self.env.event(
            "POST", "/workflows/{id}/package", self.user,
            workflow_id=self.workflow_id,
            body={"architectures": architectures},
        )
        response = self.packaging.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    # ----------------------------------------------------------- asserts
    def keys_under(self, prefix, bucket=None):
        listed = self.s3.list_objects_v2(
            Bucket=bucket or self.usecase_bucket, Prefix=prefix)
        return [o["Key"] for o in listed.get("Contents", [])]

    def packaged_zip(self, arch):
        # Artifacts are keyed by workflow version AND component version; the
        # first package of workflow v1 is component 1.0.0.
        key = (f"workflows/components/{self.workflow_id}/1/1.0.0/"
               f"{arch}/workflow-{arch}.zip")
        body = self.s3.get_object(Bucket=self.usecase_bucket,
                                  Key=key)["Body"].read()
        return zipfile.ZipFile(io.BytesIO(body))

    def assert_nothing_packaged(self):
        assert self.keys_under(f"{STAGING_ROOT}/{self.workflow_id}/") == []
        assert self.keys_under(
            f"workflows/components/{self.workflow_id}/1") == []
        self.greengrass.create_component_version.assert_not_called()


@pytest.fixture
def cenv(env, packaging, monkeypatch):
    return CustomPluginPackagingEnv(env, packaging, monkeypatch)


# --------------------------------------------------------- success (16.4)

class TestCustomPluginSuccess:
    def test_component_dependency_checksums_and_no_inline_bundling(self, cenv):
        """A verified custom plugin is declared as a pinned Plugin_Component
        dependency with its checksum in manifest.json, never bundled inline,
        while curated plugins keep the inline bundling (11.1, 10.4, 16.4)."""
        cenv.seed_curated_library(["x86_64"])
        record = cenv.seed_plugin_record()
        type_id, dependency = cenv.register_node_type(record)
        cenv.create_workflow(type_id, include_dewarp=True)
        gg = cenv.patch_usecase_clients()

        status, payload = cenv.package(["x86_64"])
        assert status == 201, payload

        component_name = f"dda.plugin.{record['plugin_id']}"
        with cenv.packaged_zip("x86_64") as zf:
            names = set(zf.namelist())
            # Curated plugin bundled inline, custom plugin absent (16.4)
            assert "plugins/x86_64/dda-dewarp.so" in names
            assert not any("blur-regions.so" in n for n in names)
            manifest = json.loads(zf.read("manifest.json"))

        assert manifest["pluginDependencies"] == ["dda-dewarp"]
        so_checksum = record["artifacts"]["x86_64"]["checksum"]
        assert manifest["pluginChecksums"] == {
            f"{component_name}/blur-regions.so": so_checksum}
        assert manifest["pluginComponents"] == {component_name: "1.0.0"}

        # The recipe declares the HARD dependency pinned to the recorded
        # Plugin_Record version (16.4).
        recipe = json.loads(
            gg.create_component_version.call_args.kwargs["inlineRecipe"])
        assert recipe["ComponentDependencies"] == {
            component_name: {
                "VersionRequirement": ">=1.0.0 <2.0.0",
                "DependencyType": "HARD",
            }
        }

    def test_workflow_without_custom_nodes_declares_no_dependencies(self, cenv):
        """Built-in-only workflows package exactly as before: no
        ComponentDependencies block, empty pluginChecksums."""
        cenv.seed_curated_library(["x86_64"])
        record = cenv.seed_plugin_record()
        type_id, _ = cenv.register_node_type(record)
        # Workflow uses only built-in nodes despite the registered type.
        status, payload = cenv.env.invoke("POST", "/workflows", cenv.user, body={
            "usecase_id": cenv.usecase_id,
            "name": "builtin workflow",
            "definition": {
                "schemaVersion": 1,
                "nodes": [
                    {"id": "src", "type": "folder_source",
                     "position": {"x": 0, "y": 0},
                     "parameters": {"location": "/data/images"}},
                    {"id": "dw", "type": "dewarp",
                     "position": {"x": 200, "y": 0}, "parameters": {}},
                    {"id": "cap", "type": "capture",
                     "position": {"x": 400, "y": 0},
                     "parameters": {"output_path": "/out"}},
                ],
                "connections": [
                    {"id": "c1", "from": {"node": "src", "port": "out"},
                     "to": {"node": "dw", "port": "in"}},
                    {"id": "c2", "from": {"node": "dw", "port": "out"},
                     "to": {"node": "cap", "port": "in"}},
                ],
            },
        })
        assert status == 201, payload
        cenv.workflow_id = payload["workflow"]["workflow_id"]
        cenv.env.stack.tables.versions.update_item(
            Key={"workflow_id": cenv.workflow_id, "version": 1},
            UpdateExpression="SET validation_status = :v",
            ExpressionAttributeValues={
                ":v": {"status": "passed", "validated_at": 1,
                       "findings_key": "findings/none.json"}},
        )
        gg = cenv.patch_usecase_clients()

        status, payload = cenv.package(["x86_64"])
        assert status == 201, payload
        recipe = json.loads(
            gg.create_component_version.call_args.kwargs["inlineRecipe"])
        assert "ComponentDependencies" not in recipe
        with cenv.packaged_zip("x86_64") as zf:
            manifest = json.loads(zf.read("manifest.json"))
        assert manifest["pluginChecksums"] == {}
        assert manifest["pluginComponents"] == {}


# ------------------------------------------------- gates (11.2, 11.3, 16.4)

class TestPackagingGates:
    def test_dev_lifecycle_state_rejected_identifying_type_and_state(self, cenv):
        """A dev-state backing Plugin_Record rejects packaging identifying
        the Custom_Node_Type and its Lifecycle_State (11.3)."""
        record = cenv.seed_plugin_record(lifecycle_state="dev")
        type_id, _ = cenv.register_node_type(record)
        cenv.create_workflow(type_id)
        cenv.patch_usecase_clients()

        status, payload = cenv.package(["x86_64"])

        assert status == 409
        assert payload["error"]["code"] == "PLUGIN_LIFECYCLE_VIOLATION"
        assert type_id in payload["error"]["message"]
        assert "'dev'" in payload["error"]["message"]
        finding = payload["error"]["details"]["findings"][0]
        assert finding["node_type_id"] == type_id
        assert finding["lifecycle_state"] == "dev"
        cenv.assert_nothing_packaged()

    def test_missing_arch_artifact_rejected_identifying_type_and_arch(self, cenv):
        """A selected Target_Architecture without a built Plugin_Artifact
        rejects packaging identifying the Custom_Node_Type and the missing
        architecture (11.2)."""
        record = cenv.seed_plugin_record(archs=("x86_64",))
        type_id, _ = cenv.register_node_type(
            record, archs=("x86_64", "arm64_jp5"))
        cenv.create_workflow(type_id)
        cenv.patch_usecase_clients()

        status, payload = cenv.package(["x86_64", "arm64_jp5"])

        assert status == 409
        assert payload["error"]["code"] == "PLUGIN_ARTIFACT_MISSING"
        assert type_id in payload["error"]["message"]
        assert "arm64_jp5" in payload["error"]["message"]
        finding = payload["error"]["details"]["findings"][0]
        assert finding["node_type_id"] == type_id
        assert finding["arch"] == "arm64_jp5"
        cenv.assert_nothing_packaged()

    def test_missing_plugin_component_rejected(self, cenv):
        """A backing Plugin_Record without a registered Plugin_Component
        version rejects packaging (16.4: the Workflow_Component recipe
        depends on it)."""
        record = cenv.seed_plugin_record(component_registered=False)
        type_id, _ = cenv.register_node_type(record)
        cenv.create_workflow(type_id)
        cenv.patch_usecase_clients()

        status, payload = cenv.package(["x86_64"])

        assert status == 409
        assert payload["error"]["code"] == "PLUGIN_COMPONENT_MISSING"
        assert type_id in payload["error"]["message"]
        cenv.assert_nothing_packaged()


# --------------------------------------------------- verification (10.4)

class TestArtifactVerification:
    def test_tampered_artifact_bytes_fail_packaging(self, cenv):
        """Tampered Plugin_Library bytes fail the checksum recompute via the
        PackagingError path: 502, stage cleaned, no component (10.4)."""
        record = cenv.seed_plugin_record()
        # Tamper with the stored .so after checksum/signature recording.
        cenv.s3.put_object(Bucket=cenv.bucket,
                           Key=record["artifacts"]["x86_64"]["s3Key"],
                           Body=b"\x7fELF tampered bytes")
        type_id, _ = cenv.register_node_type(record)
        cenv.create_workflow(type_id)
        cenv.patch_usecase_clients()

        status, payload = cenv.package(["x86_64"])

        assert status == 502
        assert payload["error"]["code"] == "PACKAGING_FAILED"
        assert (payload["error"]["details"]["failing_artifact"]
                == "custom-plugins/x86_64/blur-regions.so")
        assert "checksum" in payload["error"]["message"]
        cenv.assert_nothing_packaged()

    def test_wrong_signature_fails_packaging(self, cenv):
        """A signature that does not verify against the artifact bytes fails
        packaging via the PackagingError path (10.4)."""
        record = cenv.seed_plugin_record()
        # Replace the recorded signature with one over different bytes.
        record["artifacts"]["x86_64"]["signature"] = cenv.sign(b"other bytes")
        cenv.env.stack.tables.plugin_records.update_item(
            Key={"plugin_id": record["plugin_id"], "version": 1},
            UpdateExpression="SET artifacts = :a",
            ExpressionAttributeValues={":a": record["artifacts"]},
        )
        type_id, _ = cenv.register_node_type(record)
        cenv.create_workflow(type_id)
        cenv.patch_usecase_clients()

        status, payload = cenv.package(["x86_64"])

        assert status == 502
        assert payload["error"]["code"] == "PACKAGING_FAILED"
        assert (payload["error"]["details"]["failing_artifact"]
                == "custom-plugins/x86_64/blur-regions.so")
        assert "signature" in payload["error"]["message"]
        cenv.assert_nothing_packaged()


# --------------------------------------- x86_64_nvidia recipe (design)

class TestX8664NvidiaRecipe:
    def test_arch_map_gains_x86_64_nvidia(self, packaging):
        assert packaging.ARCH_TO_GG_PLATFORM["x86_64_nvidia"] == "amd64"

    def test_nvidia_manifest_attribute_and_ordering(self, packaging):
        """Both amd64 flavors packaged: the x86_64_nvidia manifest carries
        'runtime: nvidia' and precedes the plain x86_64 manifest so
        attribute-less amd64 devices match plain x86_64."""
        recipe = packaging.build_recipe("wf-1", 1, "bucket", {
            "x86_64": "workflows/components/wf-1/1/x86_64/workflow-x86_64.zip",
            "x86_64_nvidia": "workflows/components/wf-1/1/x86_64_nvidia/workflow-x86_64_nvidia.zip",
        })
        platforms = [m["Platform"] for m in recipe["Manifests"]]
        assert platforms == [
            {"os": "linux", "architecture": "amd64", "runtime": "nvidia"},
            {"os": "linux", "architecture": "amd64"},
        ]

    def test_plain_x86_64_only_manifest_carries_no_runtime(self, packaging):
        recipe = packaging.build_recipe("wf-1", 1, "bucket", {
            "x86_64": "workflows/components/wf-1/1/x86_64/workflow-x86_64.zip",
        })
        assert recipe["Manifests"][0]["Platform"] == {
            "os": "linux", "architecture": "amd64"}
